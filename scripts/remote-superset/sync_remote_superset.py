#!/usr/bin/env python3
"""Make remote Substack2Markdown a path-superset of local (merge, no delete)."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path


def die(msg: str, code: int = 1) -> None:
    print(msg, file=sys.stderr)
    raise SystemExit(code)


def run(cmd: list[str], check: bool = True, **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, check=check, text=True, **kwargs)


def ssh_opts() -> list[str]:
    return ["-o", "BatchMode=yes", "-o", "ConnectTimeout=20", "-o", "ForwardX11=no"]


def ssh_run(host: str, remote_cmd: str) -> str:
    cp = run(["ssh", *ssh_opts(), host, remote_cmd], capture_output=True)
    if cp.returncode != 0:
        die(f"ssh failed: {cp.stderr or cp.stdout}")
    return cp.stdout


def load_config(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def list_local(root: Path, dirs: list[str], excludes: set[str]) -> list[str]:
    rels: list[str] = []
    for d in dirs:
        base = root / d
        if not base.is_dir():
            continue
        for p in base.rglob("*"):
            if not p.is_file():
                continue
            if p.name in excludes:
                continue
            rels.append(p.relative_to(root).as_posix())
    return sorted(set(rels))


def list_remote(host: str, remote_root: str, dirs: list[str], excludes: set[str]) -> list[str]:
    excl = " ".join(f"! -name {json.dumps(x)}" for x in sorted(excludes))
    dir_args = " ".join(dirs)
    out = ssh_run(host, f"cd {json.dumps(remote_root)} && find {dir_args} -type f {excl} 2>/dev/null")
    rels = [ln.strip().replace("\\", "/") for ln in out.splitlines() if ln.strip()]
    return sorted(set(rels))


def show_counts(label: str, rels: list[str]) -> None:
    print(f"\n{label} ({len(rels)} files)")
    counts: dict[str, int] = {}
    for r in rels:
        top = r.split("/", 1)[0]
        counts[top] = counts.get(top, 0) + 1
    for k in sorted(counts):
        print(f"  {k:<22} {counts[k]:5d}")


def remote_free_mb(host: str) -> int:
    line = ssh_run(host, "df -PB1 / | tail -1").strip().split()
    # filesystem size used avail ...
    return int(int(line[3]) / (1024 * 1024))


def stash_remote_only(
    host: str,
    remote_root: str,
    stash_dir: Path,
    only_remote: list[str],
    dry_run: bool,
) -> None:
    if not only_remote:
        print("No remote-only files to stash.")
        return
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dest = stash_dir / stamp
    print(f"Remote-only files: {len(only_remote)}")
    print(f"Stash -> {dest}")
    if dry_run:
        for p in only_remote[:15]:
            print(f"  would stash: {p}")
        if len(only_remote) > 15:
            print(f"  ... +{len(only_remote) - 15} more")
        return

    dest.mkdir(parents=True, exist_ok=True)
    (dest / "content").mkdir(exist_ok=True)
    list_file = dest / "remote-only.txt"
    list_file.write_text("\n".join(only_remote) + "\n", encoding="utf-8", newline="\n")

    rlist = f"/tmp/ss2md-remote-only-{stamp}.txt"
    rtar = f"/tmp/ss2md-remote-only-{stamp}.tar.gz"
    run(["scp", *ssh_opts(), str(list_file), f"{host}:{rlist}"])
    ssh_run(host, f"cd {json.dumps(remote_root)} && tar czf {json.dumps(rtar)} -T {json.dumps(rlist)}")
    local_tar = dest / "remote-only.tar.gz"
    run(["scp", *ssh_opts(), f"{host}:{rtar}", str(local_tar)])
    run(["tar", "-xzf", str(local_tar), "-C", str(dest / "content")])
    ssh_run(host, f"rm -f {json.dumps(rlist)} {json.dumps(rtar)}")
    print(f"Stashed and extracted under {dest / 'content'}")


def push_merge(local_root: Path, host: str, remote_root: str, dirs: list[str], dry_run: bool) -> None:
    existing = [d for d in dirs if (local_root / d).is_dir()]
    missing = [d for d in dirs if d not in existing]
    for d in missing:
        print(f"WARN: missing local dir {d}", file=sys.stderr)
    if not existing:
        die("No local content dirs to push")
    print(f"Push merge (no delete): {', '.join(existing)}")
    if dry_run:
        print(f"DryRun: would tar|ssh extract into {remote_root}")
        return

    # tar on Windows (bsdtar) -> remote GNU tar
    tar_cmd = ["tar", "-czf", "-", "--format", "ustar", *existing]
    ssh_cmd = ["ssh", *ssh_opts(), host, f"cd {json.dumps(remote_root)} && tar xzf -"]
    print(f"Streaming tar to {host}:{remote_root} ...")
    p1 = subprocess.Popen(tar_cmd, cwd=str(local_root), stdout=subprocess.PIPE)
    p2 = subprocess.Popen(ssh_cmd, stdin=p1.stdout)
    assert p1.stdout is not None
    p1.stdout.close()
    rc2 = p2.wait()
    rc1 = p1.wait()
    if rc1 != 0 or rc2 != 0:
        die(f"tar|ssh push failed (tar={rc1}, ssh={rc2})")
    print("Push complete.")


def main() -> None:
    cfg_path = Path(os.environ.get("SS2MD_CONFIG", Path(__file__).with_name("config.json")))
    dry_run = os.environ.get("SS2MD_DRY_RUN", "0") == "1"
    skip_stash = os.environ.get("SS2MD_SKIP_STASH", "0") == "1"
    skip_push = os.environ.get("SS2MD_SKIP_PUSH", "0") == "1"

    cfg = load_config(cfg_path)
    tool_dir = Path(__file__).resolve().parent
    # localRoot / stashDir may be relative to this chunk folder (repo is ../.. when localRoot is "../..")
    local_root = Path(cfg["localRoot"])
    if not local_root.is_absolute():
        local_root = (tool_dir / local_root).resolve()
    stash_dir = Path(cfg["stashDir"])
    if not stash_dir.is_absolute():
        stash_dir = (tool_dir / stash_dir).resolve()
    host = cfg["sshHost"]
    remote_root = cfg["remoteRoot"]
    min_free = int(cfg["minRemoteFreeMb"])
    dirs = list(cfg["contentDirs"])
    excludes = set(cfg.get("excludeNames") or ["README.md"])

    print("=== Substack sync: make remote the superset ===")
    print(f"Local : {local_root}")
    print(f"Remote: {host}:{remote_root}")
    if dry_run:
        print("MODE  : DryRun (no changes)")

    if not local_root.is_dir():
        die(f"Local root missing: {local_root}")

    print("\nChecking remote free space...")
    free_mb = remote_free_mb(host)
    print(f"Remote free: {free_mb} MB (want >= {min_free} MB)")
    if not dry_run and not skip_push and free_mb < min_free:
        die(
            f"Remote disk too full ({free_mb} MB free). Free space on the droplet first, then re-run.\n"
            "Suggestions: old ~/.vscode-server builds, /var/log journals, ~/.substack_scraper caches."
        )

    print("\nIndexing paths...")
    local_rels = list_local(local_root, dirs, excludes)
    remote_rels = list_remote(host, remote_root, dirs, excludes)
    local_set = set(local_rels)
    remote_set = set(remote_rels)
    only_remote = sorted(remote_set - local_set)
    only_local = sorted(local_set - remote_set)
    both = sorted(local_set & remote_set)

    show_counts("LOCAL", local_rels)
    show_counts("REMOTE (before)", remote_rels)
    print(f"\nOnly remote : {len(only_remote)}")
    print(f"Only local  : {len(only_local)}")
    print(f"Same path   : {len(both)}")

    if not skip_stash:
        print("\n--- Phase 1: stash remote-only locally ---")
        stash_remote_only(host, remote_root, stash_dir, only_remote, dry_run)
    else:
        print("\nSkipping stash (--skip-stash)")

    if not skip_push:
        print("\n--- Phase 2: merge local -> remote (no delete) ---")
        push_merge(local_root, host, remote_root, dirs, dry_run)
    else:
        print("\nSkipping push (--skip-push)")

    if not dry_run and not skip_push:
        print("\n--- Phase 3: verify ---")
        after = list_remote(host, remote_root, dirs, excludes)
        after_set = set(after)
        show_counts("REMOTE (after)", after)
        missing_local = sorted(local_set - after_set)
        missing_ro = sorted(set(only_remote) - after_set)
        print(f"Local paths missing on remote now : {len(missing_local)}")
        print(
            f"Prior remote-only still present   : {len(only_remote) - len(missing_ro)} / {len(only_remote)}"
        )
        if not missing_local and not missing_ro:
            print("\nOK: remote is a path-superset of local, and prior remote-only kept.")
        else:
            if missing_local:
                print("Sample missing local:")
                for p in missing_local[:10]:
                    print(f"  {p}")
            if missing_ro:
                print("Sample lost remote-only:")
                for p in missing_ro[:10]:
                    print(f"  {p}")

    print("\nDone.")
    if dry_run:
        print("Re-run without --dry-run after freeing remote disk.")


if __name__ == "__main__":
    main()
