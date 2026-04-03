import os
import json
import subprocess
import threading
from pathlib import Path
from functools import wraps
from urllib.parse import urlparse
from flask import Flask, request, jsonify, send_from_directory, abort

app = Flask(__name__)

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
VENV_PYTHON = os.path.join(PROJECT_DIR, "venv", "bin", "python")
API_KEY = os.environ.get("API_KEY", "CHANGE_ME_TO_A_RANDOM_SECRET")

jobs = {}
job_lock = threading.Lock()
job_counter = 0


# ─── Auth ────────────────────────────────────────────────────────────────────

def require_api_key(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if request.headers.get("X-API-Key", "") != API_KEY:
            abort(401, description="Invalid or missing API key")
        return f(*args, **kwargs)
    return decorated


def normalize_post_url(post_url):
    parsed = urlparse(post_url.strip())
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}".rstrip("/")


def find_existing_post(post_url):
    normalized_url = normalize_post_url(post_url)
    parsed = urlparse(normalized_url)
    writer_handle = parsed.netloc.split(".")[0]
    json_path = Path(PROJECT_DIR) / "data" / f"{writer_handle}.json"

    if not json_path.exists():
        return None

    try:
        with open(json_path, "r", encoding="utf-8") as f:
            posts = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None

    for post in posts:
        saved_url = post.get("url", "").strip()
        if saved_url and normalize_post_url(saved_url) == normalized_url:
            return post

    return None


# ─── Scrape worker ───────────────────────────────────────────────────────────

def run_scrape(job_id, post_url, skip_login):
    cmd = [
        VENV_PYTHON, "substack_scraper.py",
        "--premium", "--headless", "--persistent-profile", "--images",
    ]
    if skip_login:
        cmd.append("--skip-login")
    cmd += ["--post-url", post_url]

    try:
        result = subprocess.run(
            cmd, cwd=PROJECT_DIR,
            capture_output=True, text=True, timeout=300
        )
        with job_lock:
            jobs[job_id]["status"] = "done" if result.returncode == 0 else "error"
            jobs[job_id]["output"] = result.stdout + "\n" + result.stderr
    except subprocess.TimeoutExpired:
        with job_lock:
            jobs[job_id]["status"] = "error"
            jobs[job_id]["output"] = "Scrape timed out after 5 minutes"
    except Exception as e:
        with job_lock:
            jobs[job_id]["status"] = "error"
            jobs[job_id]["output"] = str(e)


# ─── API endpoints ───────────────────────────────────────────────────────────

@app.route("/api/scrape", methods=["POST"])
@require_api_key
def scrape():
    """
    POST JSON body:
      {"url": "https://x.substack.com/p/slug", "mode": "1" or "2"}
      mode "1" = first-time login, "2" = skip login (subsequent)
    """
    data = request.get_json(force=True)
    post_url = data.get("url", "").strip()
    mode = data.get("mode", "2").strip()

    if not post_url or "/p/" not in post_url:
        return jsonify({"error": "Invalid Substack post URL"}), 400

    existing_post = find_existing_post(post_url)
    if existing_post:
        return jsonify({
            "status": "already_downloaded",
            "post": existing_post,
        }), 200

    global job_counter
    with job_lock:
        job_counter += 1
        job_id = str(job_counter)
        jobs[job_id] = {"status": "running", "output": ""}

    thread = threading.Thread(
        target=run_scrape,
        args=(job_id, post_url, mode == "2")
    )
    thread.start()

    return jsonify({"job_id": job_id, "status": "running"}), 202


@app.route("/api/status/<job_id>", methods=["GET"])
@require_api_key
def status(job_id):
    with job_lock:
        job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    return jsonify(job)


@app.route("/api/posts", methods=["GET"])
@require_api_key
def list_posts():
    """List all scraped posts with metadata."""
    posts = []
    html_dir = Path(PROJECT_DIR) / "substack_html_pages"
    data_dir = Path(PROJECT_DIR) / "data"

    if not html_dir.exists():
        return jsonify({"posts": []})

    for author_dir in html_dir.iterdir():
        if not author_dir.is_dir():
            continue
        author = author_dir.name
        metadata = []
        json_path = data_dir / f"{author}.json"
        if json_path.exists():
            try:
                with open(json_path, "r", encoding="utf-8") as f:
                    metadata = json.load(f)
            except Exception:
                pass

        for html_file in author_dir.glob("*.html"):
            slug = html_file.stem
            md_file = Path(PROJECT_DIR) / "substack_md_files" / author / f"{slug}.md"
            post_meta = next(
                (p for p in metadata if p.get("file_link", "").endswith(slug + ".md")),
                {},
            )
            posts.append({
                "title": post_meta.get("title", slug),
                "author": author,
                "slug": slug,
                "date": post_meta.get("date", ""),
                "likes": post_meta.get("like_count", "0"),
                "subtitle": post_meta.get("subtitle", ""),
                "html_url": f"/substack_html_pages/{author}/{slug}.html",
                "md_url": f"/substack_md_files/{author}/{slug}.md" if md_file.exists() else None,
            })

    return jsonify({"posts": sorted(posts, key=lambda x: x["title"])})


# ─── Static file routes ─────────────────────────────────────────────────────
# These paths match the relative references inside generated HTML files:
#   ../../substack_images/...  →  /substack_images/...
#   ../../assets/css/...       →  /assets/css/...

@app.route("/substack_html_pages/<path:filepath>")
def serve_html(filepath):
    return send_from_directory(
        os.path.join(PROJECT_DIR, "substack_html_pages"), filepath
    )

@app.route("/substack_images/<path:filepath>")
def serve_images(filepath):
    return send_from_directory(
        os.path.join(PROJECT_DIR, "substack_images"), filepath
    )

@app.route("/substack_md_files/<path:filepath>")
def serve_md(filepath):
    return send_from_directory(
        os.path.join(PROJECT_DIR, "substack_md_files"), filepath
    )

@app.route("/assets/<path:filepath>")
def serve_assets(filepath):
    return send_from_directory(
        os.path.join(PROJECT_DIR, "assets"), filepath
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=True)
