import json
import os
import subprocess
import threading
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from typing import Any, Dict, Optional, TypedDict
from urllib.parse import urlparse

from flask import Flask, jsonify, request, send_from_directory

app = Flask(__name__)

# Runtime and path settings
PROJECT_DIR = Path(__file__).resolve().parent
# if linux vs window
if os.name == "nt":
    VENV_PYTHON = PROJECT_DIR / "venv" / "Scripts" / "python.exe"
else:
    VENV_PYTHON = PROJECT_DIR / "venv" / "bin" / "python"
API_KEY = os.environ.get("API_KEY", "CHANGE_ME_TO_A_RANDOM_SECRET")
SCRAPE_TIMEOUT_SECONDS = 300

# Job status constants (keep values compatible with existing behavior)
STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_ERROR = "error"

# Event constants
EVENT_JOB_STARTED = "job_started"
EVENT_ALREADY_DOWNLOADED = "already_downloaded"

# Error code constants
ERR_UNAUTHORIZED = "UNAUTHORIZED"
ERR_INVALID_JSON = "INVALID_JSON"
ERR_INVALID_URL = "INVALID_URL"
ERR_JOB_NOT_FOUND = "JOB_NOT_FOUND"
ERR_SCRAPE_TIMEOUT = "SCRAPE_TIMEOUT"
ERR_SCRAPE_FAILED = "SCRAPE_FAILED"
ERR_INTERNAL = "INTERNAL_SERVER_ERROR"


class ApiErrorShape(TypedDict):
    code: str
    message: str


class ApiMetaShape(TypedDict):
    timestamp: str


class JobShape(TypedDict, total=False):
    job_id: str
    status: str
    output: str
    url: str
    skip_login: bool
    created_at: str
    started_at: Optional[str]
    finished_at: Optional[str]
    return_code: Optional[int]
    error_code: Optional[str]
    error_message: Optional[str]


# Shared in-memory job store
jobs: Dict[str, JobShape] = {}
job_lock = threading.Lock()
job_counter = 0


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# Single source of truth for successful API responses
def api_ok(data: Any, status_code: int = 200):
    return jsonify(
        {
            "ok": True,
            "data": data,
            "error": None,
            "meta": ApiMetaShape(timestamp=now_iso()),
        }
    ), status_code


# Single source of truth for error API responses
def api_error(code: str, message: str, status_code: int):
    return jsonify(
        {
            "ok": False,
            "data": None,
            "error": ApiErrorShape(code=code, message=message),
            "meta": ApiMetaShape(timestamp=now_iso()),
        }
    ), status_code


# Auth
def require_api_key(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if request.headers.get("X-API-Key", "") != API_KEY:
            return api_error(ERR_UNAUTHORIZED, "Invalid or missing API key", 401)
        return f(*args, **kwargs)

    return decorated


# URL helpers
def normalize_post_url(post_url: str) -> str:
    parsed = urlparse(post_url.strip())
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}".rstrip("/")


def is_valid_substack_post_url(post_url: str) -> bool:
    if not post_url:
        return False
    parsed = urlparse(post_url.strip())
    return bool(parsed.scheme and parsed.netloc and "/p/" in parsed.path)


def writer_handle_from_url(post_url: str) -> str:
    parsed = urlparse(post_url)
    return parsed.netloc.split(".")[0]


# Metadata helpers
def load_json_array(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        if isinstance(payload, list):
            return payload
        return []
    except (OSError, json.JSONDecodeError):
        return []


def find_existing_post(post_url: str) -> Optional[dict[str, Any]]:
    normalized_url = normalize_post_url(post_url)
    writer_handle = writer_handle_from_url(normalized_url)
    json_path = PROJECT_DIR / "data" / f"{writer_handle}.json"

    posts = load_json_array(json_path)
    for post in posts:
        saved_url = str(post.get("url", "")).strip()
        if saved_url and normalize_post_url(saved_url) == normalized_url:
            return post

    return None


# Job lifecycle helpers
def next_job_id() -> str:
    global job_counter
    with job_lock:
        job_counter += 1
        return str(job_counter)


def create_job(post_url: str, skip_login: bool) -> str:
    job_id = next_job_id()
    record: JobShape = {
        "job_id": job_id,
        "status": STATUS_RUNNING,
        "output": "",
        "url": post_url,
        "skip_login": skip_login,
        "created_at": now_iso(),
        "started_at": now_iso(),
        "finished_at": None,
        "return_code": None,
        "error_code": None,
        "error_message": None,
    }
    with job_lock:
        jobs[job_id] = record
    return job_id


def get_job_snapshot(job_id: str) -> Optional[JobShape]:
    with job_lock:
        job = jobs.get(job_id)
        if job is None:
            return None
        return dict(job)


def update_job(job_id: str, **changes: Any) -> None:
    with job_lock:
        current = jobs.get(job_id)
        if current is None:
            return
        current.update(changes)


def mark_running(job_id: str) -> None:
    update_job(job_id, status=STATUS_RUNNING, started_at=now_iso())


def mark_succeeded(job_id: str, output: str, return_code: int) -> None:
    update_job(
        job_id,
        status=STATUS_DONE,
        output=output,
        return_code=return_code,
        finished_at=now_iso(),
        error_code=None,
        error_message=None,
    )


def mark_failed(
    job_id: str,
    output: str,
    return_code: Optional[int],
    error_code: str,
    error_message: str,
) -> None:
    update_job(
        job_id,
        status=STATUS_ERROR,
        output=output,
        return_code=return_code,
        finished_at=now_iso(),
        error_code=error_code,
        error_message=error_message,
    )


def mark_timed_out(job_id: str, output: str) -> None:
    mark_failed(
        job_id,
        output=output,
        return_code=None,
        error_code=ERR_SCRAPE_TIMEOUT,
        error_message=f"Scrape timed out after {SCRAPE_TIMEOUT_SECONDS} seconds",
    )


# Scraper command helper
def build_scrape_cmd(post_url: str, skip_login: bool) -> list[str]:
    cmd = [
        str(VENV_PYTHON),
        "substack_scraper.py",
        "--premium",
        "--headless",
        "--persistent-profile",
        "--images",
    ]
    if skip_login:
        cmd.append("--skip-login")
    cmd += ["--post-url", post_url]
    return cmd


# Background worker
def run_scrape(job_id: str, post_url: str, skip_login: bool) -> None:
    mark_running(job_id)
    cmd = build_scrape_cmd(post_url, skip_login)

    try:
        result = subprocess.run(
            cmd,
            cwd=str(PROJECT_DIR),
            capture_output=True,
            text=True,
            timeout=SCRAPE_TIMEOUT_SECONDS,
        )
        output = f"{result.stdout}\n{result.stderr}".strip()

        if result.returncode == 0:
            mark_succeeded(job_id, output, result.returncode)
        else:
            mark_failed(
                job_id,
                output=output,
                return_code=result.returncode,
                error_code=ERR_SCRAPE_FAILED,
                error_message=f"Scraper exited with code {result.returncode}",
            )

    except subprocess.TimeoutExpired as exc:
        timeout_output = ""
        if exc.stdout:
            timeout_output += str(exc.stdout)
        if exc.stderr:
            if timeout_output:
                timeout_output += "\n"
            timeout_output += str(exc.stderr)
        mark_timed_out(job_id, timeout_output)

    except Exception as exc:
        mark_failed(
            job_id,
            output="",
            return_code=None,
            error_code=ERR_INTERNAL,
            error_message=str(exc),
        )


# Endpoint payload helpers
def parse_scrape_request() -> tuple[Optional[str], Optional[bool], Optional[Any]]:
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return None, None, api_error(ERR_INVALID_JSON, "Request body must be valid JSON", 400)

    post_url = str(payload.get("url", "")).strip()
    mode = str(payload.get("mode", "2")).strip()
    skip_login = mode == "2"

    if not is_valid_substack_post_url(post_url):
        return None, None, api_error(ERR_INVALID_URL, "Invalid Substack post URL", 400)

    return post_url, skip_login, None


def build_post_row(author: str, slug: str, metadata: list[dict[str, Any]]) -> dict[str, Any]:
    md_file = PROJECT_DIR / "substack_md_files" / author / f"{slug}.md"
    post_meta = next(
        (item for item in metadata if str(item.get("file_link", "")).endswith(f"{slug}.md")),
        {},
    )
    return {
        "title": post_meta.get("title", slug),
        "author": author,
        "slug": slug,
        "date": post_meta.get("date", ""),
        "likes": post_meta.get("like_count", "0"),
        "subtitle": post_meta.get("subtitle", ""),
        "html_url": f"/substack_html_pages/{author}/{slug}.html",
        "md_url": f"/substack_md_files/{author}/{slug}.md" if md_file.exists() else None,
    }


# API endpoints
@app.route("/api/scrape", methods=["POST"])
@require_api_key
def scrape():
    post_url, skip_login, error_response = parse_scrape_request()
    if error_response is not None:
        return error_response

    existing_post = find_existing_post(post_url)
    if existing_post:
        return api_ok(
            {
                "event": EVENT_ALREADY_DOWNLOADED,
                "post": existing_post,
            },
            status_code=200,
        )

    job_id = create_job(post_url, skip_login)
    thread = threading.Thread(
        target=run_scrape,
        args=(job_id, post_url, skip_login),
        daemon=True,
    )
    thread.start()

    return api_ok(
        {
            "event": EVENT_JOB_STARTED,
            "job": get_job_snapshot(job_id),
        },
        status_code=202,
    )


@app.route("/api/status/<job_id>", methods=["GET"])
@require_api_key
def status(job_id: str):
    job = get_job_snapshot(job_id)
    if job is None:
        return api_error(ERR_JOB_NOT_FOUND, "Job not found", 404)
    return api_ok({"job": job})


@app.route("/api/posts", methods=["GET"])
@require_api_key
def list_posts():
    posts: list[dict[str, Any]] = []
    html_dir = PROJECT_DIR / "substack_html_pages"
    data_dir = PROJECT_DIR / "data"

    if not html_dir.exists():
        return api_ok({"posts": []})

    for author_dir in html_dir.iterdir():
        if not author_dir.is_dir():
            continue

        author = author_dir.name
        metadata = load_json_array(data_dir / f"{author}.json")

        for html_file in author_dir.glob("*.html"):
            posts.append(build_post_row(author, html_file.stem, metadata))

    posts.sort(key=lambda item: str(item.get("title", "")).lower())
    return api_ok({"posts": posts})


# Static file routes
@app.route("/substack_html_pages/<path:filepath>")
def serve_html(filepath: str):
    return send_from_directory(str(PROJECT_DIR / "substack_html_pages"), filepath)


@app.route("/substack_images/<path:filepath>")
def serve_images(filepath: str):
    return send_from_directory(str(PROJECT_DIR / "substack_images"), filepath)


@app.route("/substack_md_files/<path:filepath>")
def serve_md(filepath: str):
    return send_from_directory(str(PROJECT_DIR / "substack_md_files"), filepath)


@app.route("/assets/<path:filepath>")
def serve_assets(filepath: str):
    return send_from_directory(str(PROJECT_DIR / "assets"), filepath)


if __name__ == "__main__":
    # print first the API_KEY for checking only in local
    print(f"API_KEY: {os.getenv('API_KEY')}")
    print(os.name)
    app.run(host="0.0.0.0", port=8000, debug=True)