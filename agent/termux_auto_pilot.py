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
import hashlib
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
DEFAULT_OMNIAGENT_REPO = Path("/root/.gemini/antigravity/scratch/OmniAgent")
DEFAULT_FRACTAL_REPO = Path("/root/.gemini/antigravity/scratch/dynamic-fractal-planner")
_PLUGIN_EXCLUDE_DIRS = frozenset({".git", "__pycache__", ".diagnostics", ".agents", "node_modules", ".last_good_backup"})
_PLUGIN_EXCLUDE_FILES = frozenset({".metrics.jsonl", ".model_modes.json", ".hook_state.json", ".workspaces.json", "package.json", "AGENTS.md"})

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


def _plugin_roots() -> tuple[Path, ...]:
    gemini_home = Path(os.environ.get("GEMINI_HOME") or (Path.home() / ".gemini"))
    candidates = (
        DEFAULT_FRACTAL_REPO,
        gemini_home / "config" / "plugins" / "dynamic-fractal-planner",
        DEFAULT_CAVERN_REPO / ".agents" / "plugins" / "dynamic-fractal-planner",
        DEFAULT_OMNIAGENT_REPO / ".agents" / "plugins" / "dynamic-fractal-planner",
    )
    return tuple(p for p in candidates if p.is_dir())


def _compute_plugin_fingerprint(roots: tuple[Path, ...]) -> str:
    h = hashlib.sha256()
    for root in roots:
        h.update(f"ROOT|{root}\n".encode("utf-8"))
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(d for d in dirnames if d not in _PLUGIN_EXCLUDE_DIRS)
            for fn in sorted(filenames):
                if fn in _PLUGIN_EXCLUDE_FILES or fn.endswith((".pyc", ".pyo")):
                    continue
                fp = Path(dirpath) / fn
                try:
                    st = fp.stat()
                except OSError:
                    continue
                rel = fp.relative_to(root).as_posix()
                h.update(f"{rel}|{st.st_size}|{st.st_mtime_ns}\n".encode("utf-8"))
    return h.hexdigest()


def _resolve_plan_tool() -> Path | None:
    gemini_home = Path(os.environ.get("GEMINI_HOME") or (Path.home() / ".gemini"))
    for p in (
        gemini_home / "config" / "plugins" / "dynamic-fractal-planner" / "scripts" / "plan_tool.py",
        DEFAULT_FRACTAL_REPO / "scripts" / "plan_tool.py",
        DEFAULT_CAVERN_REPO / ".agents" / "plugins" / "dynamic-fractal-planner" / "scripts" / "plan_tool.py",
    ):
        if p.is_file():
            return p
    return None


def sync_plugins_if_changed(state: dict[str, Any]) -> dict[str, Any]:
    roots = _plugin_roots()
    if not roots:
        return {"status": "no_plugin_roots"}
    fp = _compute_plugin_fingerprint(roots)
    if fp == state.get("lastPluginSyncFingerprint"):
        return {"status": "unchanged", "fingerprint": fp[:12]}

    plan_tool = _resolve_plan_tool()
    if plan_tool is None:
        return {"status": "no_plan_tool"}

    probe = subprocess.run(
        [sys.executable, "-B", str(plan_tool), "evolve", "sync-all", "--json"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if probe.returncode != 0:
        return {
            "status": "probe_failed",
            "exitCode": probe.returncode,
            "tail": (probe.stdout + "\n" + probe.stderr).strip()[-300:],
        }

    needs_sync = True
    try:
        pdata = json.loads(probe.stdout)
        if isinstance(pdata, dict):
            has_drift = bool(
                pdata.get("drift")
                or pdata.get("dirty")
                or pdata.get("needsSync")
                or pdata.get("needs_sync")
                or pdata.get("outOfSync")
                or pdata.get("out_of_sync")
                or pdata.get("changes")
                or pdata.get("pending")
                or pdata.get("actions")
                or pdata.get("dirtyRepos")
            )
            explicit_clean = (
                pdata.get("inSync") is True
                or pdata.get("in_sync") is True
                or pdata.get("status") in ("clean", "in_sync", "unchanged", "ok")
            )
            if explicit_clean and not has_drift:
                needs_sync = False
    except Exception:
        pass

    if not needs_sync:
        state["lastPluginSyncFingerprint"] = fp
        return {"status": "in_sync", "fingerprint": fp[:12]}

    applied = subprocess.run(
        [sys.executable, "-B", str(plan_tool), "evolve", "sync-all", "--apply", "--push"],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if applied.returncode != 0:
        tail = [ln.strip() for ln in (applied.stdout + "\n" + applied.stderr).splitlines() if ln.strip()][-8:]
        return {"status": "sync_failed", "exitCode": applied.returncode, "tail": tail}

    new_fp = _compute_plugin_fingerprint(_plugin_roots())
    state["lastPluginSyncFingerprint"] = new_fp
    state["lastPluginSyncUnix"] = int(time.time())
    return {"status": "synced", "fingerprint": new_fp[:12]}


def run_once(
    state_file: Path,
    verify_cache: Path,
    repo: Path,
    do_verify: bool,
    do_sync_plugins: bool = False,
) -> dict[str, Any]:
    state = _load_json(state_file)
    token = _gh_token()
    results = [check_and_download_release(item, state, token) for item in WATCHED_REPOS]
    verify_res = run_preverify_if_head_changed(repo, state, verify_cache) if do_verify else {"status": "skipped"}
    sync_res = sync_plugins_if_changed(state) if do_sync_plugins else None
    state["lastTickUnix"] = int(time.time())
    _save_json(state_file, state)
    out: dict[str, Any] = {"ok": True, "releases": results, "preVerify": verify_res}
    if sync_res is not None:
        out["pluginSync"] = sync_res
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Zero-Quota Termux Auto-Pilot (Auto-APK + Background Pre-Verify).")
    parser.add_argument("--daemon", action="store_true", help="Loop continuously in background")
    parser.add_argument("--interval", type=int, default=90, help="Poll interval seconds (default 90s; ETag 304 = 0 quota)")
    parser.add_argument("--with-preverify", action="store_true", help="Also pre-run check_android_cs.py on new git HEAD")
    parser.add_argument("--sync-plugins", action="store_true", help="Opt-in zero-quota plugin sync on drift/dirty changes")
    parser.add_argument("--state-file", default=str(DEFAULT_STATE_FILE))
    parser.add_argument("--verify-cache", default=str(DEFAULT_VERIFY_CACHE))
    parser.add_argument("--repo", default=str(DEFAULT_CAVERN_REPO))
    args = parser.parse_args(argv)

    state_file = Path(args.state_file)
    verify_cache = Path(args.verify_cache)
    repo = Path(args.repo)

    if not args.daemon:
        res = run_once(state_file, verify_cache, repo, bool(args.with_preverify), bool(args.sync_plugins))
        print(json.dumps(res, indent=2, ensure_ascii=False))
        return 0

    interval = max(30, int(args.interval))
    while True:
        try:
            run_once(state_file, verify_cache, repo, bool(args.with_preverify), bool(args.sync_plugins))
        except Exception as exc:
            sys.stderr.write(f"[auto_pilot] tick error: {exc}\n")
        time.sleep(interval)


if __name__ == "__main__":
    raise SystemExit(main())
