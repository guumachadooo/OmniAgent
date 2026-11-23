#!/usr/bin/env python3
"""Zero-Quota Termux Auto-Pilot Daemon (0 LLM Tokens, 0 GitHub Rate-Limit Waste).

Runs quietly on the user's phone (Termux / Debian PRoot) to offload routine tasks
away from AI Studio sessions:
  1. Auto-APK Watcher: polls GitHub Releases (`Cavern` + `OmniAgent`) using ETag
     conditional HTTP (`304 Not Modified` = 0 rate-limit cost). Whenever GitHub
     Actions publishes a new `Cavernize-Android.apk` or `OmniAgent-Android.apk`,
     automatically downloads & verifies it into `/sdcard/Download/` AND the repo
     folder, firing an optional `termux-notification`.
  2. Background Pre-Verify Cache: when Cavern git HEAD changes on the phone,
     runs `check_android_cs.py --ci-parity --strict-warnings` in background and
     writes a tiny 1-line JSON digest to `/root/.omni_agent/last_verify.json`
     so the next AI turn reads the result in <100 ms instead of spending a turn.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

DEFAULT_STATE_FILE = Path("/root/.omni_agent/auto_pilot_state.json")
DEFAULT_VERIFY_CACHE = Path("/root/.omni_agent/last_verify.json")
DEFAULT_DOWNLOAD_DIR = Path("/sdcard/Download")
DEFAULT_CAVERN_REPO = Path("/root/.gemini/antigravity/scratch/Cavern")

WATCHED_REPOS = (
    {
        "slug": "guumachadooo/Cavern",
        "asset": "Cavernize-Android.apk",
        "mirrors": [
            "/sdcard/Download/Cavernize-Android.apk",
            "/root/.gemini/antigravity/scratch/Cavern/Cavernize-Android.apk",
        ],
    },
    {
        "slug": "guumachadooo/OmniAgent",
        "asset": "OmniAgent-Android.apk",
        "mirrors": [
            "/sdcard/Download/OmniAgent-Android.apk",
        ],
    },
)


def _load_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def _gh_token() -> str:
    for k in ("GITHUB_TOKEN", "GH_TOKEN"):
        v = os.environ.get(k, "").strip()
        if v:
            return v
    cfg = _load_json(Path("/root/.omni_agent/config.json"))
    return str(cfg.get("githubToken") or "").strip()


def _notify(title: str, body: str) -> None:
    if shutil.which("termux-notification"):
        try:
            subprocess.run(
                ["termux-notification", "--id", "omni_autopilot", "--title", title, "--content", body],
                timeout=5,
                check=False,
                capture_output=True,
            )
        except Exception:
            pass


def check_and_download_release(item: dict[str, Any], state: dict[str, Any], token: str) -> dict[str, Any]:
    slug = item["slug"]
    asset_name = item["asset"]
    url = f"https://api.github.com/repos/{slug}/releases/latest"
    repo_state = state.setdefault("releases", {}).setdefault(slug, {})

    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "OmniAgent-AutoPilot/1.0",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if repo_state.get("etag"):
        headers["If-None-Match"] = str(repo_state["etag"])

    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            etag = resp.headers.get("ETag", "")
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as err:
        if err.code == 304:
            return {"slug": slug, "status": "unchanged_304", "tag": repo_state.get("tag", "")}
        return {"slug": slug, "status": f"http_{err.code}", "error": str(err)}
    except Exception as exc:
        return {"slug": slug, "status": "error", "error": str(exc)}

    if etag:
        repo_state["etag"] = etag

    tag = str(data.get("tag_name") or "")
    if not tag or tag == repo_state.get("tag"):
        return {"slug": slug, "status": "up_to_date", "tag": tag}

    dl_url = ""
    expected_size = 0
    for a in data.get("assets") or []:
        if str(a.get("name") or "").lower() == asset_name.lower():
            dl_url = str(a.get("browser_download_url") or "")
            expected_size = int(a.get("size") or 0)
            break
    if not dl_url:
        return {"slug": slug, "status": "no_asset", "tag": tag}

    tmp_apk = Path("/tmp") / f".autopilot_{asset_name}.part"
    dl_headers = {"User-Agent": "OmniAgent-AutoPilot/1.0"}
    if token:
        dl_headers["Authorization"] = f"Bearer {token}"
    try:
        dl_req = urllib.request.Request(dl_url, headers=dl_headers)
        with urllib.request.urlopen(dl_req, timeout=180) as r, open(tmp_apk, "wb") as out:
            shutil.copyfileobj(r, out, length=131072)
    except Exception as exc:
        tmp_apk.unlink(missing_ok=True)
        return {"slug": slug, "status": "download_failed", "tag": tag, "error": str(exc)}

    got_size = tmp_apk.stat().st_size
    if expected_size > 0 and got_size != expected_size:
        tmp_apk.unlink(missing_ok=True)
        return {"slug": slug, "status": "size_mismatch", "tag": tag, "expected": expected_size, "got": got_size}

    delivered: list[str] = []
    for dest_str in item["mirrors"]:
        dest = Path(dest_str)
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(tmp_apk, dest)
            delivered.append(dest_str)
        except OSError:
            pass
    tmp_apk.unlink(missing_ok=True)

    repo_state["tag"] = tag
    repo_state["sizeBytes"] = got_size
    repo_state["updatedUnix"] = int(time.time())
    repo_state["delivered"] = delivered
    _notify(f"{asset_name} {tag} pronto", f"Baixado automaticamente ({got_size / 1048576:.1f} MB) -> Downloads")
    return {"slug": slug, "status": "downloaded", "tag": tag, "sizeBytes": got_size, "delivered": delivered}


def run_preverify_if_head_changed(repo: Path, state: dict[str, Any], verify_cache: Path) -> dict[str, Any]:
    if not (repo / ".git").exists():
        return {"status": "no_git_repo"}
    head = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, check=False)
    sha = head.stdout.strip()
    if not sha or sha == state.get("lastVerifiedSha"):
        return {"status": "cached", "sha": sha[:8]}

    check_script = repo / ".agents" / "scripts" / "check_android_cs.py"
    if not check_script.is_file():
        return {"status": "no_checker"}

    t0 = time.time()
    proc = subprocess.run(
        [sys.executable, "-B", str(check_script), "--ci-parity", "--strict-warnings"],
        cwd=str(repo),
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    elapsed = round(time.time() - t0, 2)
    tail = [ln.strip() for ln in (proc.stdout + "\n" + proc.stderr).splitlines() if ln.strip()][-12:]
    digest = {
        "ok": proc.returncode == 0,
        "exitCode": proc.returncode,
        "sha": sha[:8],
        "shaFull": sha,
        "elapsedSec": elapsed,
        "timestampUnix": int(time.time()),
        "tail": tail,
    }
    _save_json(verify_cache, digest)
    state["lastVerifiedSha"] = sha
    return digest


def run_once(state_file: Path, verify_cache: Path, repo: Path, do_verify: bool) -> dict[str, Any]:
    state = _load_json(state_file)
    token = _gh_token()
    results = [check_and_download_release(item, state, token) for item in WATCHED_REPOS]
    verify_res = run_preverify_if_head_changed(repo, state, verify_cache) if do_verify else {"status": "skipped"}
    state["lastTickUnix"] = int(time.time())
    _save_json(state_file, state)
    return {"ok": True, "releases": results, "preVerify": verify_res}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Zero-Quota Termux Auto-Pilot (Auto-APK + Background Pre-Verify).")
    parser.add_argument("--daemon", action="store_true", help="Loop continuously in background")
    parser.add_argument("--interval", type=int, default=90, help="Poll interval seconds (default 90s; ETag 304 = 0 quota)")
    parser.add_argument("--with-preverify", action="store_true", help="Also pre-run check_android_cs.py on new git HEAD")
    parser.add_argument("--state-file", default=str(DEFAULT_STATE_FILE))
    parser.add_argument("--verify-cache", default=str(DEFAULT_VERIFY_CACHE))
    parser.add_argument("--repo", default=str(DEFAULT_CAVERN_REPO))
    args = parser.parse_args(argv)

    state_file = Path(args.state_file)
    verify_cache = Path(args.verify_cache)
    repo = Path(args.repo)

    if not args.daemon:
        res = run_once(state_file, verify_cache, repo, bool(args.with_preverify))
        print(json.dumps(res, indent=2, ensure_ascii=False))
        return 0

    interval = max(30, int(args.interval))
    while True:
        try:
            run_once(state_file, verify_cache, repo, bool(args.with_preverify))
        except Exception as exc:
            sys.stderr.write(f"[auto_pilot] tick error: {exc}\n")
        time.sleep(interval)


if __name__ == "__main__":
    raise SystemExit(main())
