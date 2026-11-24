#!/usr/bin/env python3
"""OmniAgent v4 — Termux / Debian PRoot Full-Agent Daemon ("Antigravity 2 Engine").

Zero external pip dependencies (Python 3 stdlib only).
Runs inside Termux OR Debian PRoot on Android (or any Linux host) and connects to the user's
free Cloudflare Unified Hub v4 over E2E-encrypted channel `termux` (or custom `--channel`).

Capabilities:
  - True 2-way Git & Workspace Sync (`git_status`, `git_pull`, `git_diff`, `git_log`,
    `git_manifest`, `git_apply_bundle`, `git_commit_push`, `file_export_bundle`)
  - Termux + Debian PRoot shell & parallel worker pool (`shell_exec`, `parallel_exec`)
  - Wireless ADB / Shizuku APK installer & live logcat (`adb_exec`, `install_apk`, `logcat_tail`)
  - Native Antigravity Debian integration (`ag_discover`, `ag_inspect`, `ag_sync_session`)
    with strict token-diet compact artifacts (never corrupts binary .pb files)
  - Optional official local CLI subagent (`subagent_run` via `gemini` CLI / `ollama` if installed)
  - Smart Security Modes (`smart` | `biometric` | `trusted`) w/ optional `termux-fingerprint`
    only when explicitly enabled so the user is never nagged unnecessarily.
"""

from __future__ import annotations

import argparse
import base64
import concurrent.futures
import errno
import fcntl
import hashlib
import hmac
import http.server
import json
import os
import platform
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import omni_protocol as proto

AGENT_VERSION = "4.1.1"
DEFAULT_LOCAL_FS_PORT = 47921
CRITICAL_COMMANDS = {"git_commit_push", "shell_exec", "adb_exec", "install_apk", "file_write"}
DEFAULT_ANTIGRAVITY_CANDIDATES = (
    "/root/.gemini/antigravity",
    str(Path.home() / ".gemini" / "antigravity"),
    "/data/data/com.termux/files/usr/var/lib/proot-distro/installed-rootfs/debian/root/.gemini/antigravity",
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _truncate(text: str, max_chars: int = 24000, keep_tail: bool = False) -> str:
    if not text or len(text) <= max_chars:
        return text or ""
    if keep_tail:
        return f"... [truncated {len(text) - max_chars} leading chars]\n" + text[-max_chars:]
    return text[:max_chars] + f"\n... [truncated {len(text) - max_chars} trailing chars]"


def detect_environment(repo_path: Path, antigravity_dir: Path | None = None) -> dict[str, Any]:
    in_termux = bool(os.environ.get("TERMUX_VERSION")) or Path("/data/data/com.termux").is_dir()
    in_proot = Path("/etc/debian_version").is_file() and (
        os.environ.get("PROOT_TMP_DIR") or Path("/root/.gemini").is_dir()
    )
    ag_path = antigravity_dir
    if ag_path is None:
        for cand in DEFAULT_ANTIGRAVITY_CANDIDATES:
            p = Path(cand)
            if p.is_dir():
                ag_path = p
                break
    tools = {
        "git": bool(shutil.which("git")),
        "adb": bool(shutil.which("adb")),
        "rish_shizuku": bool(shutil.which("rish")),
        "termux_fingerprint": bool(shutil.which("termux-fingerprint")),
        "termux_notification": bool(shutil.which("termux-notification")),
        "termux_open": bool(shutil.which("termux-open")),
        "proot_distro": bool(shutil.which("proot-distro")),
        "dotnet": bool(shutil.which("dotnet") or Path("/root/.dotnet/dotnet").is_file()),
        "gemini_cli": bool(shutil.which("gemini")),
        "ollama": bool(shutil.which("ollama")),
    }
    return {
        "hostEnv": "debian-proot" if in_proot else ("termux" if in_termux else platform.system().lower()),
        "machine": platform.machine(),
        "python": platform.python_version(),
        "repoPath": str(repo_path),
        "repoHasGit": (repo_path / ".git").exists(),
        "antigravityDir": str(ag_path) if ag_path else "",
        "tools": tools,
    }


def verify_biometric_if_needed(security_mode: str, cmd: str, reason: str = "") -> tuple[bool, str]:
    mode = (security_mode or "smart").strip().lower()
    if mode == "trusted":
        return True, "trusted"
    if mode == "smart":
        # Smart mode: zero unnecessary fingerprint prompts on paired E2E sessions!
        # Only prompt if user explicitly switches to `biometric` mode.
        return True, "smart-hmac"
    if mode == "biometric" and cmd in CRITICAL_COMMANDS:
        if not shutil.which("termux-fingerprint"):
            return True, "termux-fingerprint-unavailable-hmac-ok"
        try:
            res = subprocess.run(
                ["termux-fingerprint"],
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
            data = json.loads(res.stdout or "{}")
            if str(data.get("auth_result", "")).upper() == "AUTH_RESULT_SUCCESS":
                return True, "biometric-approved"
            return False, f"Biometric denied: {data.get('auth_result') or res.stderr.strip()}"
        except Exception as exc:
            return False, f"Biometric check failed: {exc}"
    return True, "ok"


def notify_termux(title: str, content: str) -> None:
    if not shutil.which("termux-notification"):
        return
    try:
        subprocess.run(
            [
                "termux-notification",
                "--id",
                "omniagent-activity",
                "--title",
                title[:60],
                "--content",
                content[:180],
                "--priority",
                "low",
            ],
            capture_output=True,
            timeout=5,
            check=False,
        )
    except Exception:
        pass


def run_subprocess(
    argv: list[str],
    cwd: Path | None = None,
    timeout: int = 120,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    t0 = time.time()
    try:
        res = subprocess.run(
            argv,
            cwd=str(cwd) if cwd else None,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        return {
            "ok": res.returncode == 0,
            "exitCode": res.returncode,
            "elapsedMs": int((time.time() - t0) * 1000),
            "stdout": _truncate(res.stdout, 20000, keep_tail=True),
            "stderr": _truncate(res.stderr, 8000, keep_tail=True),
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "ok": False,
            "exitCode": -1,
            "elapsedMs": int((time.time() - t0) * 1000),
            "stdout": _truncate((exc.stdout or "") if isinstance(exc.stdout, str) else "", 4000, keep_tail=True),
            "stderr": f"Command timed out after {timeout}s",
        }
    except Exception as exc:
        return {
            "ok": False,
            "exitCode": -1,
            "elapsedMs": int((time.time() - t0) * 1000),
            "stdout": "",
            "stderr": str(exc),
        }


def resolve_repo(default_repo: Path, args: dict[str, Any]) -> Path:
    raw = str(args.get("repo") or "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    return default_repo.resolve()


def discover_antigravity(ag_root: Path | None) -> dict[str, Any]:
    root = ag_root
    if root is None or not root.is_dir():
        for cand in DEFAULT_ANTIGRAVITY_CANDIDATES:
            p = Path(cand)
            if p.is_dir():
                root = p
                break
    if root is None or not root.is_dir():
        return {"found": False, "searched": list(DEFAULT_ANTIGRAVITY_CANDIDATES)}

    subdirs: dict[str, Any] = {}
    for child in sorted(root.iterdir()):
        name = child.name
        if child.is_dir():
            items = []
            try:
                entries = sorted(child.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)
                for e in entries[:15]:
                    st = e.stat()
                    items.append({
                        "name": e.name,
                        "type": "dir" if e.is_dir() else "file",
                        "size": st.st_size,
                        "mtime": int(st.st_mtime),
                    })
            except OSError:
                pass
            subdirs[name] = {"count": len(list(child.iterdir())) if child.is_dir() else 0, "recent": items}
        else:
            try:
                st = child.stat()
                subdirs[name] = {"type": "file", "size": st.st_size, "mtime": int(st.st_mtime)}
            except OSError:
                pass
    return {"found": True, "root": str(root), "entries": subdirs}


def sync_antigravity_session(
    ag_root: Path | None,
    repo: Path,
    args: dict[str, Any],
) -> dict[str, Any]:
    """Write ultra-compact token-diet session mirror into Antigravity local dir (never touches binary .pb files)."""
    root = ag_root
    if root is None or not root.is_dir():
        for cand in DEFAULT_ANTIGRAVITY_CANDIDATES:
            p = Path(cand)
            if p.is_dir():
                root = p
                break
    if root is None:
        root = Path.home() / ".gemini" / "antigravity"

    bridge_dir = root / "ai_studio_bridge"
    sessions_dir = bridge_dir / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)

    session_id = str(args.get("sessionId") or datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")).strip()
    safe_id = "".join(ch for ch in session_id if ch.isalnum() or ch in ("_", "-"))[:64] or "session"
    title = str(args.get("title") or "AI Studio Session").strip()[:160]
    summary = str(args.get("summary") or "").strip()[:4000]
    commit_sha = str(args.get("commit") or "").strip()[:64]
    files = [str(f)[:200] for f in (args.get("files") or [])[:64]]
    next_steps = str(args.get("next") or "").strip()[:1000]
    now = _utc_now()

    entry = {
        "sessionId": safe_id,
        "utc": now,
        "repo": str(repo),
        "title": title,
        "commit": commit_sha,
        "files": files,
        "summary": summary,
        "next": next_steps,
    }
    session_md = sessions_dir / f"{safe_id}.md"
    lines = [
        f"# {title}",
        f"- **UTC**: `{now}` | **Session**: `{safe_id}` | **Commit**: `{commit_sha or 'n/a'}`",
        f"- **Repo**: `{repo}`",
        "",
        "## Compact Summary (Token-Diet)",
        summary or "(none)",
        "",
    ]
    if files:
        lines.append("## Files")
        lines.extend(f"- `{f}`" for f in files)
        lines.append("")
    if next_steps:
        lines.append("## Next")
        lines.append(next_steps)
        lines.append("")
    session_md.write_text("\n".join(lines), encoding="utf-8")

    # Update rolling compact JSON index + LATEST_HANDOFF.md for instant Antigravity pickup
    index_path = bridge_dir / "index.json"
    index: list[dict[str, Any]] = []
    if index_path.is_file():
        try:
            parsed = json.loads(index_path.read_text(encoding="utf-8"))
            if isinstance(parsed, list):
                index = parsed
        except Exception:
            index = []
    index = [x for x in index if x.get("sessionId") != safe_id]
    index.insert(0, entry)
    index = index[:40]
    index_path.write_text(json.dumps(index, indent=2, ensure_ascii=False), encoding="utf-8")

    latest_md = bridge_dir / "LATEST_HANDOFF.md"
    latest_md.write_text("\n".join(lines), encoding="utf-8")

    # Also mirror a compact line into Antigravity knowledge dir if present
    knowledge_mirror = ""
    knowledge_dir = root / "knowledge"
    if knowledge_dir.is_dir():
        km = knowledge_dir / "ai_studio_latest_sync.md"
        km.write_text("\n".join(lines), encoding="utf-8")
        knowledge_mirror = str(km)

    return {
        "ok": True,
        "sessionFile": str(session_md),
        "indexFile": str(index_path),
        "latestFile": str(latest_md),
        "knowledgeMirror": knowledge_mirror,
        "totalIndexedSessions": len(index),
    }


def install_apk_from_url(url: str, dest_path: Path, method: str = "auto") -> dict[str, Any]:
    parsed = urllib.parse.urlparse((url or "").strip())
    if parsed.scheme != "https":
        return {"ok": False, "error": "APK download URL must use https://."}
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": proto.USER_AGENT})
    with urllib.request.urlopen(req, timeout=180) as resp:
        apk_bytes = resp.read()
    if len(apk_bytes) < 1024 or apk_bytes[:4] != b"PK\x03\x04":
        return {"ok": False, "error": "Downloaded file is not a valid APK/ZIP archive."}
    dest_path.write_bytes(apk_bytes)
    sha = hashlib.sha256(apk_bytes).hexdigest()

    chosen = (method or "auto").lower()
    install_res: dict[str, Any] = {"method": "download_only", "ok": True}
    if chosen in ("auto", "adb") and shutil.which("adb"):
        r = run_subprocess(["adb", "install", "-r", "-d", str(dest_path)], timeout=120)
        if r["ok"] or chosen == "adb":
            install_res = {"method": "adb", **r}
            return {"ok": r["ok"], "path": str(dest_path), "bytes": len(apk_bytes), "sha256": sha, "install": install_res}
    if chosen in ("auto", "shizuku", "rish") and shutil.which("rish"):
        r = run_subprocess(["rish", "-c", f'pm install -r -d "{dest_path}"'], timeout=120)
        if r["ok"] or chosen in ("shizuku", "rish"):
            install_res = {"method": "shizuku", **r}
            return {"ok": r["ok"], "path": str(dest_path), "bytes": len(apk_bytes), "sha256": sha, "install": install_res}
    if chosen in ("auto", "open") and shutil.which("termux-open"):
        r = run_subprocess(["termux-open", "--content-type", "application/vnd.android.package-archive", str(dest_path)], timeout=20)
        install_res = {"method": "termux-open", **r}
    return {
        "ok": bool(install_res.get("ok", True)),
        "path": str(dest_path),
        "bytes": len(apk_bytes),
        "sha256": sha,
        "install": install_res,
    }


class OmniAgentDaemon:
    CAPABILITIES = [
        "ping",
        "capabilities",
        "status",
        "git_status",
        "git_pull",
        "git_diff",
        "git_log",
        "git_manifest",
        "git_apply_bundle",
        "git_commit_push",
        "file_list",
        "file_read",
        "file_write",
        "file_export_bundle",
        "shell_exec",
        "parallel_exec",
        "adb_exec",
        "install_apk",
        "logcat_tail",
        "ag_discover",
        "ag_inspect",
        "ag_sync_session",
        "subagent_run",
    ]

    def __init__(
        self,
        relay_url: str,
        pair_secret_hex: str,
        repo_path: Path,
        channel: str = "termux",
        security_mode: str = "smart",
        antigravity_dir: Path | None = None,
        admin_key: str = "",
        debian_distro: str = "debian",
    ) -> None:
        self.relay_url = proto.validate_https_url(relay_url)
        self.pair_secret_hex = proto.normalize_pair_secret_hex(pair_secret_hex)
        self.repo_path = repo_path.expanduser().resolve()
        self.channel = (channel or "termux").strip().lower()
        self.security_mode = (security_mode or "smart").strip().lower()
        self.antigravity_dir = antigravity_dir.expanduser().resolve() if antigravity_dir else None
        self.admin_key = admin_key
        self.debian_distro = debian_distro
        self.running = True
        self.headers = proto.derive_agent_headers(self.pair_secret_hex, self.channel, self.admin_key)
        self.device_hash = self.headers["X-Device-Hash"]

    def send_heartbeat(self) -> None:
        env_info = detect_environment(self.repo_path, self.antigravity_dir)
        payload = {
            "agentVersion": AGENT_VERSION,
            "hostEnv": env_info["hostEnv"],
            "device": f"{platform.node()} ({env_info['machine']})",
            "securityMode": self.security_mode,
            "repoPath": str(self.repo_path),
            "tools": env_info["tools"],
            "capabilities": self.CAPABILITIES,
        }
        proto.http_json("POST", f"{self.relay_url}/v1/agent/heartbeat", self.headers, payload, timeout=15)

    def handle_command(self, cmd_payload: dict[str, Any]) -> dict[str, Any]:
        cmd = str(cmd_payload.get("cmd") or "").strip().lower()
        args = cmd_payload.get("args") if isinstance(cmd_payload.get("args"), dict) else {}

        allowed, auth_note = verify_biometric_if_needed(self.security_mode, cmd, str(args.get("reason") or ""))
        if not allowed:
            return {"ok": False, "cmd": cmd, "error": auth_note}

        repo = resolve_repo(self.repo_path, args)

        try:
            if cmd in ("ping", "status"):
                return {
                    "ok": True,
                    "cmd": cmd,
                    "agentVersion": AGENT_VERSION,
                    "channel": self.channel,
                    "securityMode": self.security_mode,
                    "auth": auth_note,
                    "utc": _utc_now(),
                    "env": detect_environment(repo, self.antigravity_dir),
                }

            if cmd == "capabilities":
                return {"ok": True, "cmd": cmd, "capabilities": self.CAPABILITIES, "securityMode": self.security_mode}

            if cmd == "git_status":
                r1 = run_subprocess(["git", "status", "-sb"], cwd=repo, timeout=30)
                r2 = run_subprocess(["git", "rev-parse", "--short", "HEAD"], cwd=repo, timeout=15)
                r3 = run_subprocess(["git", "log", "-n", "5", "--oneline"], cwd=repo, timeout=15)
                return {
                    "ok": r1["ok"],
                    "cmd": cmd,
                    "repo": str(repo),
                    "head": r2["stdout"].strip(),
                    "status": r1["stdout"],
                    "recentCommits": r3["stdout"],
                    "stderr": r1["stderr"],
                }

            if cmd == "git_pull":
                remote = str(args.get("remote") or "origin")
                branch = str(args.get("branch") or "master")
                pull_res = _safe_ff_only_pull(repo, remote=remote, branch=branch)
                head_r = run_subprocess(["git", "rev-parse", "--short", "HEAD"], cwd=repo, timeout=15)
                return {
                    "ok": bool(pull_res["ok"]),
                    "cmd": cmd,
                    "status": pull_res.get("status", ""),
                    "head": head_r["stdout"].strip(),
                    "fetch": pull_res.get("fetch"),
                    "pull": pull_res.get("pull"),
                    "output": pull_res.get("output", ""),
                }

            if cmd == "git_diff":
                stat_only = bool(args.get("stat", True))
                ref = str(args.get("ref") or "").strip()
                argv = ["git", "diff", "--stat"] if stat_only else ["git", "diff"]
                if ref:
                    argv.append(ref)
                r = run_subprocess(argv, cwd=repo, timeout=45)
                return {"ok": r["ok"], "cmd": cmd, **r}

            if cmd == "git_log":
                count = max(1, min(50, int(args.get("n") or 10)))
                r = run_subprocess(["git", "log", f"-n{count}", "--oneline"], cwd=repo, timeout=20)
                return {"ok": r["ok"], "cmd": cmd, **r}

            if cmd == "git_manifest":
                manifest = proto.build_workspace_manifest(repo)
                head_r = run_subprocess(["git", "rev-parse", "HEAD"], cwd=repo, timeout=15)
                branch_r = run_subprocess(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo, timeout=15)
                if len(json.dumps(manifest)) > 350_000:
                    blob = proto.upload_encrypted_blob(
                        self.relay_url, self.pair_secret_hex, {"manifest": manifest}, admin_key=self.admin_key
                    )
                    return {
                        "ok": True,
                        "cmd": cmd,
                        "repo": str(repo),
                        "head": head_r["stdout"].strip(),
                        "branch": branch_r["stdout"].strip(),
                        "fileCount": len(manifest),
                        "manifestBlobId": blob["blobId"],
                    }
                return {
                    "ok": True,
                    "cmd": cmd,
                    "repo": str(repo),
                    "head": head_r["stdout"].strip(),
                    "branch": branch_r["stdout"].strip(),
                    "fileCount": len(manifest),
                    "manifest": manifest,
                }

            if cmd in ("git_apply_bundle", "git_commit_push"):
                blob_id = str(args.get("blobId") or "").strip()
                bundle = (
                    proto.download_encrypted_blob(self.relay_url, self.pair_secret_hex, blob_id, admin_key=self.admin_key)
                    if blob_id
                    else args.get("bundle")
                )
                if not isinstance(bundle, dict):
                    return {"ok": False, "cmd": cmd, "error": "Missing delta bundle or blobId."}
                pull_first = bool(args.get("pullFirst", False))
                pull_info = None
                if pull_first:
                    pull_info = _safe_ff_only_pull(repo, remote="origin", branch=str(args.get("branch") or "master"))
                applied = proto.apply_delta_bundle(repo, bundle, allow_delete=bool(args.get("allowDelete", True)))
                if cmd == "git_apply_bundle":
                    st_r = run_subprocess(["git", "status", "-sb"], cwd=repo, timeout=20)
                    return {"ok": True, "cmd": cmd, "applied": applied, "status": st_r["stdout"], "pull": pull_info}

                # git_commit_push
                message = str(args.get("message") or "").strip()
                if not message:
                    return {"ok": False, "cmd": cmd, "error": "Commit message is required for git_commit_push."}
                remote = str(args.get("remote") or "origin")
                branch = str(args.get("branch") or "master")
                paths_to_add = applied["written"] + applied["deleted"]
                if paths_to_add:
                    add_r = run_subprocess(["git", "add", "-A", "--", *paths_to_add], cwd=repo, timeout=30)
                else:
                    add_r = run_subprocess(["git", "add", "-A"], cwd=repo, timeout=30)
                if not add_r["ok"]:
                    return {"ok": False, "cmd": cmd, "stage": "git_add", "applied": applied, **add_r}
                staged_r = run_subprocess(["git", "diff", "--cached", "--stat"], cwd=repo, timeout=20)
                if not staged_r["stdout"].strip():
                    head_r = run_subprocess(["git", "rev-parse", "--short", "HEAD"], cwd=repo, timeout=15)
                    return {
                        "ok": True,
                        "cmd": cmd,
                        "nothingToCommit": True,
                        "head": head_r["stdout"].strip(),
                        "applied": applied,
                    }
                commit_r = run_subprocess(["git", "commit", "-m", message], cwd=repo, timeout=45)
                if not commit_r["ok"]:
                    return {"ok": False, "cmd": cmd, "stage": "git_commit", "applied": applied, **commit_r}
                do_push = bool(args.get("push", True))
                push_r = run_subprocess(["git", "push", remote, branch], cwd=repo, timeout=120) if do_push else {"ok": True, "skipped": True}
                head_r = run_subprocess(["git", "rev-parse", "--short", "HEAD"], cwd=repo, timeout=15)
                notify_termux("OmniAgent Git Push", f"{head_r['stdout'].strip()} -> {remote}/{branch}: {message[:80]}")
                return {
                    "ok": bool(push_r.get("ok")),
                    "cmd": cmd,
                    "head": head_r["stdout"].strip(),
                    "applied": applied,
                    "stagedStat": staged_r["stdout"],
                    "commit": commit_r,
                    "push": push_r,
                }

            if cmd == "file_list":
                target = Path(str(args.get("path") or repo)).expanduser().resolve()
                limit = max(1, min(300, int(args.get("limit") or 100)))
                if not target.is_dir():
                    return {"ok": False, "cmd": cmd, "error": f"Directory not found: {target}"}
                items = []
                for p in sorted(target.iterdir())[:limit]:
                    try:
                        st = p.stat()
                        items.append({"name": p.name, "dir": p.is_dir(), "size": st.st_size, "mtime": int(st.st_mtime)})
                    except OSError:
                        continue
                return {"ok": True, "cmd": cmd, "path": str(target), "items": items}

            if cmd == "file_read":
                target = Path(str(args.get("path") or "")).expanduser().resolve()
                if not target.is_file():
                    return {"ok": False, "cmd": cmd, "error": f"File not found: {target}"}
                max_chars = max(100, min(60000, int(args.get("maxChars") or 16000)))
                tail = bool(args.get("tail", False))
                txt = target.read_text(encoding="utf-8", errors="replace")
                return {"ok": True, "cmd": cmd, "path": str(target), "size": len(txt), "content": _truncate(txt, max_chars, keep_tail=tail)}

            if cmd == "file_write":
                target = Path(str(args.get("path") or "")).expanduser().resolve()
                content = str(args.get("content") or "")
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")
                return {"ok": True, "cmd": cmd, "path": str(target), "bytes": len(content.encode("utf-8"))}

            if cmd == "file_export_bundle":
                paths = [str(p) for p in (args.get("paths") or [])]
                if not paths:
                    return {"ok": False, "cmd": cmd, "error": "paths[] list required."}
                bundle = proto.pack_delta_bundle(repo, paths)
                blob = proto.upload_encrypted_blob(self.relay_url, self.pair_secret_hex, bundle, admin_key=self.admin_key)
                return {"ok": True, "cmd": cmd, "fileCount": len(bundle["files"]), "blobId": blob["blobId"], "bytes": blob["bytes"]}

            if cmd == "shell_exec":
                command = str(args.get("command") or "").strip()
                if not command:
                    return {"ok": False, "cmd": cmd, "error": "Missing command."}
                timeout = max(5, min(300, int(args.get("timeoutSec") or 90)))
                use_debian = bool(args.get("debian", False))
                cwd_str = str(args.get("cwd") or repo).strip()
                cwd = Path(cwd_str).expanduser().resolve() if cwd_str else repo
                in_debian = Path("/etc/debian_version").is_file()
                if use_debian and not in_debian and shutil.which("proot-distro"):
                    wrapped = f"cd {shlex_quote(str(cwd))} 2>/dev/null || true; {command}"
                    r = run_subprocess(["proot-distro", "login", self.debian_distro, "--", "bash", "-lc", wrapped], timeout=timeout)
                else:
                    r = run_subprocess(["bash", "-lc", command], cwd=cwd if cwd.is_dir() else None, timeout=timeout)
                return {"ok": r["ok"], "cmd": cmd, **r}

            if cmd == "parallel_exec":
                tasks = args.get("tasks")
                if not isinstance(tasks, list) or not tasks:
                    return {"ok": False, "cmd": cmd, "error": "tasks[] array required."}
                tasks = tasks[:6]
                max_workers = min(4, len(tasks))

                def _run_one(item: dict[str, Any]) -> dict[str, Any]:
                    tid = str(item.get("id") or "task")
                    c = str(item.get("command") or "").strip()
                    t_cwd = Path(str(item.get("cwd") or repo)).expanduser().resolve()
                    t_sec = max(5, min(180, int(item.get("timeoutSec") or 60)))
                    r = run_subprocess(["bash", "-lc", c], cwd=t_cwd if t_cwd.is_dir() else None, timeout=t_sec)
                    return {"id": tid, **r}

                results = []
                with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
                    futs = [pool.submit(_run_one, t) for t in tasks if isinstance(t, dict)]
                    for f in concurrent.futures.as_completed(futs):
                        results.append(f.result())
                return {"ok": all(r.get("ok") for r in results), "cmd": cmd, "results": results}

            if cmd == "adb_exec":
                adb_args = [str(a) for a in (args.get("args") or [])]
                if not adb_args:
                    return {"ok": False, "cmd": cmd, "error": "args[] required for adb_exec."}
                if shutil.which("adb"):
                    r = run_subprocess(["adb", *adb_args], timeout=60)
                    return {"ok": r["ok"], "cmd": cmd, "via": "adb", **r}
                if shutil.which("rish") and adb_args[0] == "shell":
                    r = run_subprocess(["rish", "-c", " ".join(adb_args[1:])], timeout=60)
                    return {"ok": r["ok"], "cmd": cmd, "via": "shizuku", **r}
                return {"ok": False, "cmd": cmd, "error": "Neither adb nor rish (Shizuku) found in PATH."}

            if cmd == "install_apk":
                url = str(args.get("url") or "").strip()
                dest = Path(str(args.get("dest") or "/sdcard/Download/Cavernize-Android.apk")).expanduser()
                method = str(args.get("method") or "auto")
                res = install_apk_from_url(url, dest, method=method)
                if res.get("ok"):
                    notify_termux("OmniAgent APK Ready", f"Downloaded & triggered install ({res.get('install', {}).get('method')})")
                return {"cmd": cmd, **res}

            if cmd == "logcat_tail":
                lines = max(20, min(500, int(args.get("lines") or 120)))
                filt = str(args.get("filter") or "Cavernize").strip()
                if shutil.which("adb"):
                    r = run_subprocess(["adb", "logcat", "-d", "-t", str(lines)], timeout=25)
                elif shutil.which("rish"):
                    r = run_subprocess(["rish", "-c", f"logcat -d -t {lines}"], timeout=25)
                else:
                    return {"ok": False, "cmd": cmd, "error": "logcat_tail requires Wireless ADB or Shizuku (rish)."}
                out = r["stdout"]
                if filt:
                    out = "\n".join(ln for ln in out.splitlines() if filt.lower() in ln.lower())
                return {"ok": r["ok"], "cmd": cmd, "lines": len(out.splitlines()), "output": _truncate(out, 22000, keep_tail=True)}

            if cmd == "ag_discover":
                return {"ok": True, "cmd": cmd, **discover_antigravity(self.antigravity_dir)}

            if cmd == "ag_inspect":
                ag_info = discover_antigravity(self.antigravity_dir)
                if not ag_info.get("found"):
                    return {"ok": False, "cmd": cmd, "error": "Antigravity directory not found."}
                root = Path(ag_info["root"]).resolve()
                sub = str(args.get("subpath") or "").lstrip("/")
                target = (root / sub).resolve()
                target.relative_to(root)
                if target.is_dir():
                    items = [{"name": p.name, "dir": p.is_dir(), "size": p.stat().st_size} for p in sorted(target.iterdir())[:80]]
                    return {"ok": True, "cmd": cmd, "path": str(target), "items": items}
                if target.is_file():
                    if target.suffix.lower() in (".pb", ".db", ".sqlite", ".bin"):
                        return {"ok": True, "cmd": cmd, "path": str(target), "binary": True, "size": target.stat().st_size}
                    txt = target.read_text(encoding="utf-8", errors="replace")
                    return {"ok": True, "cmd": cmd, "path": str(target), "content": _truncate(txt, 16000)}
                return {"ok": False, "cmd": cmd, "error": f"Not found: {target}"}

            if cmd == "ag_sync_session":
                return {"cmd": cmd, **sync_antigravity_session(self.antigravity_dir, repo, args)}

            if cmd == "subagent_run":
                prompt = str(args.get("prompt") or "").strip()
                backend = str(args.get("backend") or "auto").lower()
                if not prompt:
                    return {"ok": False, "cmd": cmd, "error": "prompt is required."}
                if backend in ("auto", "gemini") and shutil.which("gemini"):
                    r = run_subprocess(["gemini", "-p", prompt], cwd=repo, timeout=180)
                    return {"ok": r["ok"], "cmd": cmd, "backend": "gemini-cli", **r}
                if backend in ("auto", "ollama") and shutil.which("ollama"):
                    model = str(args.get("model") or "qwen2.5-coder:7b")
                    r = run_subprocess(["ollama", "run", model, prompt], cwd=repo, timeout=180)
                    return {"ok": r["ok"], "cmd": cmd, "backend": f"ollama:{model}", **r}
                return {
                    "ok": False,
                    "cmd": cmd,
                    "error": "No local CLI subagent installed (install official `gemini` CLI or `ollama`, or use Cloudflare `/v1/swarm/*` with Antigravity/secondary chat).",
                }

            return {"ok": False, "cmd": cmd, "error": f"Unknown command '{cmd}'."}
        except Exception as exc:
            return {"ok": False, "cmd": cmd, "error": f"{type(exc).__name__}: {exc}"}

    def probe_heartbeat_interval(self) -> float:
        try:
            st, data = proto.http_json("GET", f"{self.relay_url}/health", {}, timeout=10)
            if st == 200 and isinstance(data, dict):
                rec = data.get("recommendedHeartbeatSec")
                if isinstance(rec, (int, float)) and 30 <= rec <= 600:
                    return float(rec)
                if data.get("supportsDurableObjects"):
                    return 60.0
        except Exception:
            pass
        return 180.0

    def _try_wake_android_for_knock(self) -> bool:
        """Best-effort wake of OmniAgent Android APK when a Smart Knock is queued and app was closed."""
        am_bin = shutil.which("am") or ("/system/bin/am" if os.path.exists("/system/bin/am") else "")
        if not am_bin:
            return False
        woke = False
        for argv in (
            [am_bin, "start-foreground-service", "-n", "hu.sbence.cavern.omniagent/.Services.OmniAgentForegroundService"],
            [am_bin, "broadcast", "-a", "hu.sbence.cavern.ipc.OMNI_WAKE_KNOCK", "-p", "hu.sbence.cavern.omniagent", "--include-stopped-packages"],
        ):
            try:
                res = subprocess.run(argv, capture_output=True, text=True, timeout=6, check=False)
                if res.returncode == 0:
                    woke = True
            except Exception:
                pass
        return woke

    def run_forever(self, poll_fast_sec: float = 2.0, poll_idle_sec: float = 6.0, local_fs_port: int = DEFAULT_LOCAL_FS_PORT) -> None:
        hb_interval = self.probe_heartbeat_interval()
        last_probe = time.time()
        local_server = None
        if local_fs_port and local_fs_port > 0:
            local_server = start_local_fs_server(
                self.pair_secret_hex,
                self.repo_path,
                port=local_fs_port,
            )
        print(
            f"[OmniAgent v{AGENT_VERSION}] Starting daemon | channel={self.channel} | security={self.security_mode} | hb={int(hb_interval)}s | localFs=127.0.0.1:{local_fs_port if local_server else 0} | repo={self.repo_path}",
            flush=True,
        )
        last_hb = 0.0
        last_knock_wake = 0.0
        idle_ticks = 0
        reset_streak = 0
        try:
            while self.running:
                now = time.time()
                if now - last_probe >= 1800.0:
                    hb_interval = self.probe_heartbeat_interval()
                    last_probe = now
                if now - last_hb >= hb_interval:
                    try:
                        self.send_heartbeat()
                        last_hb = now
                        reset_streak = 0
                    except Exception as exc:
                        if _is_connection_reset_error(exc):
                            reset_streak = min(reset_streak + 1, 6)
                            backoff = min(60.0, 2.0 ** reset_streak)
                            print(f"[OmniAgent] Heartbeat connection reset (Errno 104); backoff {backoff:.1f}s (streak={reset_streak})", file=sys.stderr, flush=True)
                            time.sleep(backoff)
                            continue
                        print(f"[OmniAgent] Heartbeat warning: {exc}", file=sys.stderr, flush=True)

                try:
                    st, poll = proto.http_json("GET", f"{self.relay_url}/v1/agent/poll", self.headers, timeout=20)
                    reset_streak = 0
                    if st == 200 and poll.get("ok") and poll.get("hasCommand"):
                        idle_ticks = 0
                        cmd_wrap = poll["command"]
                        req_id = str(cmd_wrap.get("requestId") or "")
                        env = cmd_wrap.get("envelope")
                        try:
                            cmd_obj = proto.decrypt_envelope(self.pair_secret_hex, f"cmd:{req_id}", env)
                            print(f"[OmniAgent] Exec #{req_id[:8]} -> {cmd_obj.get('cmd')}", flush=True)
                            res_obj = self.handle_command(cmd_obj)
                        except Exception as exc:
                            res_obj = {"ok": False, "error": f"Command decrypt/exec error: {exc}"}

                        res_env = proto.encrypt_envelope(self.pair_secret_hex, f"res:{req_id}", res_obj)
                        proto.http_json(
                            "POST",
                            f"{self.relay_url}/v1/agent/result",
                            self.headers,
                            {"requestId": req_id, "envelope": res_env},
                            timeout=30,
                        )
                        continue
                    # Wake OmniAgent Android automatically if a Smart Knock is waiting while the Android app was closed
                    if now - last_knock_wake >= 15.0:
                        k_st, k_data = proto.http_json(
                            "GET",
                            f"{self.relay_url}/v1/knock/poll",
                            {"X-Device-Hash": self.device_hash},
                            timeout=8,
                        )
                        if k_st == 200 and isinstance(k_data, dict) and k_data.get("hasKnock"):
                            last_knock_wake = now
                            if self._try_wake_android_for_knock():
                                print("[OmniAgent] Woke OmniAgent Android for pending Smart Knock.", flush=True)
                    idle_ticks = min(idle_ticks + 1, 20)
                except KeyboardInterrupt:
                    break
                except Exception as exc:
                    idle_ticks = min(idle_ticks + 1, 20)
                    if _is_connection_reset_error(exc):
                        reset_streak = min(reset_streak + 1, 6)
                        backoff = min(60.0, 2.0 ** reset_streak)
                        print(f"[OmniAgent] Poll connection reset (Errno 104); exponential backoff {backoff:.1f}s (streak={reset_streak})", file=sys.stderr, flush=True)
                        time.sleep(backoff)
                        continue
                    print(f"[OmniAgent] Poll error: {exc}", file=sys.stderr, flush=True)

                time.sleep(poll_idle_sec if idle_ticks >= 5 else poll_fast_sec)
        finally:
            if local_server is not None:
                try:
                    local_server.shutdown()
                    local_server.server_close()
                except Exception:
                    pass


def derive_local_fs_key(pair_secret_hex: str) -> str:
    """Derives the deterministic 127.0.0.1 loopback HMAC key shared by OmniAgentAndroid & Debian daemon."""
    norm = proto.normalize_pair_secret_hex(pair_secret_hex)
    return hmac.new(bytes.fromhex(norm), b"omni-local-fs-v1", hashlib.sha256).hexdigest()


def _allowed_local_roots(repo_path: Path) -> list[str]:
    candidates = [
        "/root",
        str(Path.home()),
        "/data/data/com.termux/files/home",
        "/sdcard",
        "/storage/emulated/0",
        "/tmp/cavern-federation",
        str(repo_path),
    ]
    roots: list[str] = []
    for c in candidates:
        if not c:
            continue
        rp = os.path.realpath(c)
        if rp and rp not in roots:
            roots.append(rp)
    return roots


def _resolve_confined_path(raw_path: str, allowed_roots: list[str]) -> tuple[str | None, str]:
    clean = (raw_path or "").strip()
    if not clean:
        return None, "Path is required."
    expanded = os.path.expanduser(clean)
    resolved = os.path.realpath(expanded)
    for root in allowed_roots:
        if resolved == root or resolved.startswith(root.rstrip("/") + os.sep):
            return resolved, ""
    return None, f"Path outside allowed roots: {clean}"


def _is_connection_reset_error(exc: BaseException) -> bool:
    if isinstance(exc, ConnectionResetError):
        return True
    if getattr(exc, "errno", None) == errno.ECONNRESET:
        return True
    reason = getattr(exc, "reason", None)
    if isinstance(reason, BaseException):
        if isinstance(reason, ConnectionResetError) or getattr(reason, "errno", None) == errno.ECONNRESET:
            return True
    msg = str(exc).lower()
    return ("104" in msg and "reset" in msg) or "connection reset by peer" in msg


def _safe_ff_only_pull(repo_path: Path, remote: str = "origin", branch: str = "") -> dict[str, Any]:
    if not (repo_path.is_dir() and (repo_path / ".git").exists()):
        return {"ok": False, "status": "not_cloned", "dirtyCount": 0, "output": "not cloned"}

    st = run_subprocess(["git", "status", "--porcelain"], cwd=repo_path, timeout=15)
    if not st["ok"]:
        return {
            "ok": False,
            "status": "status_error",
            "dirtyCount": 0,
            "output": (st["stderr"] or st["stdout"] or "git status failed").strip()[-240:],
        }
    dirty_lines = [ln for ln in st["stdout"].splitlines() if ln.strip()]
    if dirty_lines:
        return {
            "ok": False,
            "status": "quarantined_dirty",
            "dirtyCount": len(dirty_lines),
            "output": f"quarantined_dirty ({len(dirty_lines)} dirty path(s)); skipping pull",
        }

    lock_path = repo_path / ".git" / "omni_pull.lock"
    with open(lock_path, "a+", encoding="utf-8") as lock_fh:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
        try:
            st2 = run_subprocess(["git", "status", "--porcelain"], cwd=repo_path, timeout=15)
            dirty2 = [ln for ln in (st2["stdout"] if st2["ok"] else "").splitlines() if ln.strip()]
            if dirty2:
                return {
                    "ok": False,
                    "status": "quarantined_dirty",
                    "dirtyCount": len(dirty2),
                    "output": f"quarantined_dirty ({len(dirty2)} dirty path(s)); skipping pull",
                }
            fetch_r = run_subprocess(["git", "fetch", remote], cwd=repo_path, timeout=60)
            if not fetch_r["ok"]:
                return {
                    "ok": False,
                    "status": "fetch_failed",
                    "dirtyCount": 0,
                    "fetch": fetch_r,
                    "output": (fetch_r["stderr"] or fetch_r["stdout"] or "git fetch failed").strip()[-240:],
                }
            pull_cmd = ["git", "pull", "--ff-only", remote, branch] if branch else ["git", "pull", "--ff-only"]
            pull_r = run_subprocess(pull_cmd, cwd=repo_path, timeout=90)
            return {
                "ok": bool(pull_r["ok"]),
                "status": "pulled" if pull_r["ok"] else "pull_failed",
                "dirtyCount": 0,
                "fetch": fetch_r,
                "pull": pull_r,
                "output": (pull_r["stdout"] or pull_r["stderr"] or "").strip()[-240:],
            }
        finally:
            try:
                fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass


def _inspect_local_triad(default_repo: Path) -> dict[str, Any]:
    scratch_base = default_repo.parent if default_repo.parent.is_dir() else Path("/root/.gemini/antigravity/scratch")
    specs = (
        ("cavern", "guumachadooo/Cavern", "master", scratch_base / "Cavern"),
        ("omniagent", "guumachadooo/OmniAgent", "main", scratch_base / "OmniAgent"),
        ("fractal", "guumachadooo/dynamic-fractal-planner", "main", scratch_base / "dynamic-fractal-planner"),
    )
    repos_out: list[dict[str, Any]] = []
    handoff_info: dict[str, str] = {"wave": "", "summary": "", "next": ""}
    extra_pending = 0
    lessons_count = 0

    for alias, owner_repo, default_branch, rpath in specs:
        exists = rpath.is_dir()
        entry: dict[str, Any] = {
            "alias": alias,
            "ownerRepo": owner_repo,
            "path": str(rpath),
            "exists": exists,
            "branch": default_branch,
            "head": "",
            "lastSubject": "",
            "dirtyCount": 0,
            "quarantinedDirty": False,
            "status": "not_cloned",
        }
        if exists and (rpath / ".git").exists():
            br = run_subprocess(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=rpath, timeout=8)
            if br["ok"] and br["stdout"].strip():
                entry["branch"] = br["stdout"].strip()
            hd = run_subprocess(["git", "log", "-1", "--format=%h|%s"], cwd=rpath, timeout=8)
            if hd["ok"] and "|" in hd["stdout"]:
                parts = hd["stdout"].strip().split("|", 1)
                entry["head"] = parts[0]
                entry["lastSubject"] = parts[1][:120]
            st = run_subprocess(["git", "status", "--porcelain"], cwd=rpath, timeout=8)
            if st["ok"]:
                lines = [ln for ln in st["stdout"].splitlines() if ln.strip()]
                entry["dirtyCount"] = len(lines)
                entry["quarantinedDirty"] = len(lines) > 0
                entry["status"] = "quarantined_dirty" if len(lines) > 0 else "clean"
            else:
                entry["status"] = "status_error"
        if alias == "cavern" and exists:
            hf = rpath / ".agents" / "docs" / "HANDOFF.md"
            if hf.is_file():
                for ln in hf.read_text(encoding="utf-8", errors="replace").splitlines():
                    if ln.startswith("WAVE|"):
                        handoff_info["wave"] = ln[5:].strip()
                    elif ln.startswith("SUMMARY|"):
                        handoff_info["summary"] = ln[8:].strip()[:320]
                    elif ln.startswith("NEXT|"):
                        handoff_info["next"] = ln[5:].strip()[:240]
            ef = rpath / ".agents" / "docs" / "EXTRA_REQUESTS.md"
            if ef.is_file():
                txt = ef.read_text(encoding="utf-8", errors="replace")
                pending_blk = txt.split("## Completed Requests")[0]
                extra_pending = sum(1 for ln in pending_blk.splitlines() if ln.strip().startswith("- REQ|"))
            bf = rpath / ".agents" / "docs" / "BUILD_LESSONS.md"
            if bf.is_file():
                lessons_count = len(re.findall(r"^###\s+`?BL-\d+`?", bf.read_text(encoding="utf-8", errors="replace"), re.M))
        repos_out.append(entry)

    return {
        "ok": True,
        "utc": _utc_now(),
        "hostname": platform.node(),
        "repos": repos_out,
        "handoff": handoff_info,
        "pendingExtraRequests": extra_pending,
        "buildLessonsCount": lessons_count,
    }


def start_local_fs_server(
    pair_secret_hex: str,
    repo_path: Path,
    *,
    host: str = "127.0.0.1",
    port: int = DEFAULT_LOCAL_FS_PORT,
) -> http.server.ThreadingHTTPServer | None:
    """Starts the authenticated 127.0.0.1 loopback server for OmniAgent's Debian File Explorer & Triad Hub."""
    expected_key = derive_local_fs_key(pair_secret_hex)
    allowed_roots = _allowed_local_roots(repo_path)
    started_ts = time.time()

    class LocalFsHandler(http.server.BaseHTTPRequestHandler):
        def setup(self) -> None:
            super().setup()
            try:
                self.connection.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            except OSError:
                pass

        def log_message(self, format: str, *args: Any) -> None:
            return  # Keep daemon stdout clean

        def _send_json(self, code: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            try:
                self.send_response(code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def _check_auth(self) -> bool:
            got = (self.headers.get("X-Omni-Local-Key") or "").strip().lower()
            if not got or not hmac.compare_digest(got, expected_key):
                self._send_json(401, {"ok": False, "error": "Unauthorized local request"})
                return False
            return True

        def _read_body_json(self) -> dict[str, Any]:
            try:
                length = min(int(self.headers.get("Content-Length") or "0"), 1_048_576)
                raw = self.rfile.read(length) if length > 0 else b"{}"
                parsed = json.loads(raw.decode("utf-8", errors="replace"))
                return parsed if isinstance(parsed, dict) else {}
            except Exception:
                return {}

        def do_GET(self) -> None:
            try:
                self._handle_get()
            except Exception as exc:
                try:
                    self._send_json(500, {"ok": False, "error": f"{type(exc).__name__}: {exc}"})
                except Exception:
                    pass

        def _handle_get(self) -> None:
            if not self._check_auth():
                return
            parsed = urllib.parse.urlparse(self.path)
            route = parsed.path
            qs = urllib.parse.parse_qs(parsed.query)

            if route == "/local/health":
                self._send_json(
                    200,
                    {
                        "ok": True,
                        "service": "omniagent-local-fs",
                        "version": AGENT_VERSION,
                        "hostname": platform.node(),
                        "uptimeSec": int(time.time() - started_ts),
                        "roots": [
                            {"label": "Cavern", "path": "/root/.gemini/antigravity/scratch/Cavern"},
                            {"label": "OmniAgent", "path": "/root/.gemini/antigravity/scratch/OmniAgent"},
                            {"label": "Fractal", "path": "/root/.gemini/antigravity/scratch/dynamic-fractal-planner"},
                            {"label": "Antigravity", "path": "/root/.gemini/antigravity"},
                            {"label": "Debian /root", "path": "/root"},
                            {"label": "Termux ~", "path": "/data/data/com.termux/files/home"},
                            {"label": "Android /sdcard", "path": "/sdcard"},
                        ],
                    },
                )
                return

            if route == "/local/triad":
                self._send_json(200, _inspect_local_triad(repo_path))
                return

            if route == "/local/fs/list":
                req_path = (qs.get("path") or ["/root"])[0]
                show_hidden = (qs.get("hidden") or ["1"])[0] in ("1", "true", "yes")
                resolved, err = _resolve_confined_path(req_path, allowed_roots)
                if not resolved:
                    self._send_json(403, {"ok": False, "error": err})
                    return
                if not os.path.isdir(resolved):
                    self._send_json(404, {"ok": False, "error": f"Directory not found: {resolved}"})
                    return
                entries: list[dict[str, Any]] = []
                try:
                    with os.scandir(resolved) as it:
                        for entry in it:
                            name = entry.name
                            if not show_hidden and name.startswith("."):
                                continue
                            try:
                                st = entry.stat(follow_symlinks=False)
                                is_dir = entry.is_dir(follow_symlinks=True)
                                is_link = entry.is_symlink()
                                entries.append(
                                    {
                                        "name": name,
                                        "path": os.path.join(resolved, name),
                                        "isDir": is_dir,
                                        "isSymlink": is_link,
                                        "sizeBytes": 0 if is_dir else int(st.st_size),
                                        "mtimeUnix": int(st.st_mtime),
                                        "ext": "" if is_dir else Path(name).suffix.lower(),
                                    }
                                )
                            except OSError:
                                continue
                except OSError as exc:
                    self._send_json(500, {"ok": False, "error": str(exc)})
                    return
                entries.sort(key=lambda e: (not e["isDir"], e["name"].lower()))
                parent = os.path.dirname(resolved.rstrip("/")) or "/"
                parent_allowed, _ = _resolve_confined_path(parent, allowed_roots)
                self._send_json(
                    200,
                    {
                        "ok": True,
                        "path": resolved,
                        "parent": parent_allowed or "",
                        "count": len(entries),
                        "entries": entries[:500],
                    },
                )
                return

            if route == "/local/fs/read":
                req_path = (qs.get("path") or [""])[0]
                max_bytes = min(max(int((qs.get("maxBytes") or ["262144"])[0]), 1024), 524288)
                resolved, err = _resolve_confined_path(req_path, allowed_roots)
                if not resolved:
                    self._send_json(403, {"ok": False, "error": err})
                    return
                if not os.path.isfile(resolved):
                    self._send_json(404, {"ok": False, "error": f"File not found: {resolved}"})
                    return
                try:
                    size = os.path.getsize(resolved)
                    with open(resolved, "rb") as f:
                        chunk = f.read(max_bytes)
                    is_binary = b"\x00" in chunk[:4096]
                    text = "" if is_binary else chunk.decode("utf-8", errors="replace")
                    self._send_json(
                        200,
                        {
                            "ok": True,
                            "path": resolved,
                            "name": os.path.basename(resolved),
                            "sizeBytes": size,
                            "truncated": size > len(chunk),
                            "isBinary": is_binary,
                            "lineCount": 0 if is_binary else len(text.splitlines()),
                            "content": text,
                        },
                    )
                except OSError as exc:
                    self._send_json(500, {"ok": False, "error": str(exc)})
                return

            self._send_json(404, {"ok": False, "error": f"Unknown route: {route}"})

        def do_POST(self) -> None:
            try:
                self._handle_post()
            except Exception as exc:
                try:
                    self._send_json(500, {"ok": False, "error": f"{type(exc).__name__}: {exc}"})
                except Exception:
                    pass

        def _handle_post(self) -> None:
            if not self._check_auth():
                return
            route = urllib.parse.urlparse(self.path).path
            body = self._read_body_json()

            if route == "/local/fs/export":
                req_path = str(body.get("path") or "")
                resolved, err = _resolve_confined_path(req_path, allowed_roots)
                if not resolved:
                    self._send_json(403, {"ok": False, "error": err})
                    return
                if not os.path.isfile(resolved):
                    self._send_json(404, {"ok": False, "error": "Source file not found"})
                    return
                export_dir = Path("/sdcard/Download/OmniExport")
                if not export_dir.parent.exists():
                    export_dir = Path("/storage/emulated/0/Download/OmniExport")
                try:
                    export_dir.mkdir(parents=True, exist_ok=True)
                    safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", os.path.basename(resolved)).strip("._") or "exported_file"
                    dest = export_dir / safe_name
                    shutil.copy2(resolved, dest)
                    self._send_json(200, {"ok": True, "exportedPath": str(dest), "sizeBytes": dest.stat().st_size})
                except OSError as exc:
                    self._send_json(500, {"ok": False, "error": str(exc)})
                return

            if route == "/local/fs/mkdir":
                req_path = str(body.get("path") or "")
                resolved, err = _resolve_confined_path(req_path, allowed_roots)
                if not resolved:
                    self._send_json(403, {"ok": False, "error": err})
                    return
                try:
                    os.makedirs(resolved, exist_ok=True)
                    self._send_json(200, {"ok": True, "path": resolved})
                except OSError as exc:
                    self._send_json(500, {"ok": False, "error": str(exc)})
                return

            if route == "/local/fs/delete":
                req_path = str(body.get("path") or "")
                resolved, err = _resolve_confined_path(req_path, allowed_roots)
                if not resolved:
                    self._send_json(403, {"ok": False, "error": err})
                    return
                if resolved in allowed_roots or resolved in ("/", "/root", "/sdcard", "/storage/emulated/0"):
                    self._send_json(403, {"ok": False, "error": "Refusing to delete protected root directory"})
                    return
                try:
                    if os.path.isdir(resolved) and not os.path.islink(resolved):
                        shutil.rmtree(resolved)
                    elif os.path.exists(resolved) or os.path.islink(resolved):
                        os.unlink(resolved)
                    self._send_json(200, {"ok": True, "deleted": resolved})
                except OSError as exc:
                    self._send_json(500, {"ok": False, "error": str(exc)})
                return

            if route == "/local/git/pull_all":
                triad = _inspect_local_triad(repo_path)
                results = []
                for repo in triad.get("repos", []):
                    rp = Path(repo["path"])
                    pull_res = _safe_ff_only_pull(rp)
                    results.append(
                        {
                            "alias": repo["alias"],
                            "ok": bool(pull_res["ok"]),
                            "status": pull_res.get("status", ""),
                            "dirtyCount": int(pull_res.get("dirtyCount", 0)),
                            "output": pull_res.get("output", ""),
                        }
                    )
                self._send_json(200, {"ok": all(x["ok"] for x in results), "results": results})
                return

            if route == "/local/logs/save":
                content = str(body.get("content") or "")
                raw_name = str(body.get("fileName") or "omniagent_log.txt")
                safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", os.path.basename(raw_name)).strip("._") or "omniagent_log.txt"
                if len(content.encode("utf-8", errors="replace")) > 2 * 1024 * 1024:
                    content = content[-1_500_000:]
                log_dir = Path("/sdcard/Download/OmniAgent/Logs")
                if not log_dir.parent.parent.exists():
                    log_dir = Path("/storage/emulated/0/Download/OmniAgent/Logs")
                if not log_dir.parent.parent.exists():
                    log_dir = Path.home() / ".omni_agent" / "logs"
                try:
                    log_dir.mkdir(parents=True, exist_ok=True)
                    dest = log_dir / safe_name
                    dest.write_text(content, encoding="utf-8")
                    self._send_json(200, {"ok": True, "savedPath": str(dest), "bytes": dest.stat().st_size})
                except OSError as exc:
                    self._send_json(500, {"ok": False, "error": str(exc)})
                return

            self._send_json(404, {"ok": False, "error": f"Unknown route: {route}"})

    class _ReusableLocalFsServer(http.server.ThreadingHTTPServer):
        allow_reuse_address = True
        daemon_threads = True

        def server_bind(self) -> None:
            super().server_bind()
            try:
                self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            except OSError:
                pass

    try:
        server = _ReusableLocalFsServer((host, port), LocalFsHandler)
        t = threading.Thread(target=server.serve_forever, name="omni-local-fs", daemon=True)
        t.start()
        return server
    except OSError as exc:
        print(f"[OmniAgent] Local FS loopback warning ({host}:{port}): {exc}", file=sys.stderr, flush=True)
        return None


def shlex_quote(s: str) -> str:
    return "'" + s.replace("'", "'\"'\"'") + "'"


def load_config_file(cfg_path: Path) -> dict[str, Any]:
    if cfg_path.is_file():
        try:
            return json.loads(cfg_path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def main() -> int:
    parser = argparse.ArgumentParser(description="OmniAgent v4 Termux/Debian Full-Agent Daemon.")
    parser.add_argument("--config", default=str(Path.home() / ".omni_agent" / "config.json"))
    parser.add_argument("--url", default="")
    parser.add_argument("--pair-secret", default="")
    parser.add_argument("--repo", default="")
    parser.add_argument("--channel", default="")
    parser.add_argument("--security-mode", choices=("smart", "biometric", "trusted"), default="")
    parser.add_argument("--antigravity-dir", default="")
    parser.add_argument("--admin-key", default="")
    parser.add_argument("--once-self-test", action="store_true", help="Print detected environment & exit.")
    args = parser.parse_args()

    cfg = load_config_file(Path(args.config).expanduser())
    repo_str = args.repo or os.environ.get("OMNI_REPO") or cfg.get("repo") or "/root/.gemini/antigravity/scratch/Cavern"
    repo_path = Path(repo_str).expanduser()
    if not repo_path.is_dir():
        repo_path = Path.cwd()
    ag_dir_str = args.antigravity_dir or os.environ.get("OMNI_ANTIGRAVITY_DIR") or cfg.get("antigravity_dir") or ""
    ag_dir = Path(ag_dir_str).expanduser() if ag_dir_str else None

    if args.once_self_test:
        print(json.dumps(detect_environment(repo_path, ag_dir), indent=2))
        return 0

    relay_url = args.url or os.environ.get("CAVERN_RELAY_URL") or cfg.get("relay_url") or ""
    pair_secret = args.pair_secret or os.environ.get("CAVERN_BRIDGE_PAIR_SECRET") or cfg.get("pair_secret") or ""
    channel = args.channel or os.environ.get("OMNI_CHANNEL") or cfg.get("channel") or "termux"
    sec_mode = args.security_mode or os.environ.get("OMNI_SECURITY_MODE") or cfg.get("security_mode") or "smart"
    admin_key = args.admin_key or os.environ.get("CAVERN_ADMIN_KEY") or cfg.get("admin_key") or ""

    if not relay_url or not pair_secret:
        print("ERROR: Provide --url and --pair-secret (or configure ~/.omni_agent/config.json via install_termux_agent.sh).", file=sys.stderr)
        return 2

    lock_fd = None
    try:
        import fcntl  # Unix only (Termux/Debian/Linux)

        lock_dir = Path(args.config).expanduser().parent
        lock_dir.mkdir(parents=True, exist_ok=True)
        lock_path = lock_dir / f"daemon_{channel}.lock"
        lock_fd = open(lock_path, "a+", encoding="utf-8")
        try:
            fcntl.flock(lock_fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            lock_fd.seek(0)
            lock_fd.truncate()
            lock_fd.write(f"{os.getpid()}\n")
            lock_fd.flush()
        except BlockingIOError:
            lock_fd.seek(0)
            existing_pid = lock_fd.read().strip()
            print(f"[OmniAgent] Daemon already running (channel={channel}, pid={existing_pid or '?'}). Exiting duplicate.", flush=True)
            lock_fd.close()
            return 0
    except Exception:
        lock_fd = None

    daemon = OmniAgentDaemon(
        relay_url=relay_url,
        pair_secret_hex=pair_secret,
        repo_path=repo_path,
        channel=channel,
        security_mode=sec_mode,
        antigravity_dir=ag_dir,
        admin_key=admin_key,
    )

    def _stop(_sig: int, _frame: Any) -> None:
        daemon.running = False

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)
    try:
        daemon.run_forever()
    finally:
        if lock_fd is not None:
            try:
                lock_fd.close()
            except Exception:
                pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
