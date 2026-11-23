#!/usr/bin/env python3
"""Zero-Quota Background Live Antigravity Mirror & Flight Recorder Watcher.

Runs silently alongside `.agents/preview/server.mjs` inside the AI Studio container
with ZERO extra LLM tool calls and ZERO extra quota consumption.

How it works:
  1. Lightweight stat-only workspace scan every ~1.5s (<8 ms across ~940 files using
     an in-memory `(mtime_ns, size) -> sha256` cache).
  2. Debounces bursts of parallel AI file edits (1.5s settle window) into ONE transfer.
  3. On ANY detected file edit/creation/deletion vs baseline:
       - Tier 1 Local (`/tmp/cavern-flight-recorder/`): <10 ms snapshot + diary update.
       - Tier 3 Cloudflare Hub v6 SQLite DO (`fr_latest`): E2E-encrypted delta bundle
         (so switching to a brand-new AI Studio account recovers all edits in 1 command).
       - Tier 2 Phone Antigravity LIVE Mirror: in ONE atomic Termux RPC, writes edited
         files directly into `/root/.gemini/antigravity/scratch/Cavern` (with SHA-256
         manual-edit conflict protection!) AND updates `flight_recorder/` +
         `ai_studio_bridge/LATEST_HANDOFF.md` + `knowledge/ai_studio_latest_sync.md`.
  4. Fresh-account safety invariant: NEVER pushes an empty diff on startup, so booting
     a fresh container with older GitHub files can NEVER clobber a prior account's newer
     remote checkpoint or live-mirrored phone edits.

Pure Python 3 stdlib (zero pip dependencies). Never logs or writes secrets.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import sys
import time
from pathlib import Path
from typing import Any

AGENT_DIR = Path(__file__).resolve().parent
REPO_ROOT = AGENT_DIR.parent.parent
if str(AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_DIR))

import flight_recorder as fr
import omni_protocol as proto

DEFAULT_POLL_SEC = 1.5
DEFAULT_DEBOUNCE_SEC = 1.5
MAX_FILE_BYTES = 2 * 1024 * 1024


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _atomic_write_json(path: Path, obj: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    tmp.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _is_pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def scan_workspace_fast(
    workspace: Path,
    stat_cache: dict[str, tuple[int, int, str]],
) -> dict[str, dict[str, Any]]:
    """Fast stat-cached workspace manifest (only hashes files whose (mtime_ns, size) changed)."""
    root = workspace.resolve()
    manifest: dict[str, dict[str, Any]] = {}
    seen: set[str] = set()

    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in proto.SKIP_SYNC_DIRS)
        dpath = Path(dirpath)
        for fname in sorted(filenames):
            if fname in proto.SKIP_SYNC_FILES:
                continue
            full = dpath / fname
            try:
                rel = full.relative_to(root).as_posix()
            except ValueError:
                continue
            if not proto.should_include_workspace_path(rel):
                continue
            try:
                st = full.stat()
            except OSError:
                continue
            if not os.path.isfile(full) or st.st_size > MAX_FILE_BYTES:
                continue
            seen.add(rel)
            cached = stat_cache.get(rel)
            if cached is not None and cached[0] == st.st_mtime_ns and cached[1] == st.st_size:
                sha = cached[2]
            else:
                try:
                    sha = hashlib.sha256(full.read_bytes()).hexdigest()
                except OSError:
                    continue
                stat_cache[rel] = (st.st_mtime_ns, st.st_size, sha)
            manifest[rel] = {"sha256": sha, "size": st.st_size}

    for stale in list(stat_cache.keys()):
        if stale not in seen:
            stat_cache.pop(stale, None)
    return manifest


def compute_diff_fingerprint(
    changed: list[str],
    deleted: list[str],
    current_manifest: dict[str, dict[str, Any]],
) -> str:
    if not changed and not deleted:
        return ""
    h = hashlib.sha256()
    for rel in sorted(changed):
        sha = str((current_manifest.get(rel) or {}).get("sha256") or "")
        h.update(f"M:{rel}:{sha}\n".encode("utf-8"))
    for rel in sorted(deleted):
        h.update(f"D:{rel}\n".encode("utf-8"))
    return h.hexdigest()


class LiveSyncWatcher:
    def __init__(
        self,
        workspace: Path,
        local_dir: Path,
        *,
        poll_sec: float = DEFAULT_POLL_SEC,
        debounce_sec: float = DEFAULT_DEBOUNCE_SEC,
        use_remote: bool = True,
        apply_live_mirror: bool = True,
    ) -> None:
        self.workspace = workspace.resolve()
        self.local_dir = local_dir.resolve()
        self.poll_sec = max(0.3, float(poll_sec))
        self.debounce_sec = max(0.0, float(debounce_sec))
        self.use_remote = use_remote
        self.apply_live_mirror = apply_live_mirror

        self.running = True
        self.stat_cache: dict[str, tuple[int, int, str]] = {}
        self.baseline: dict[str, dict[str, Any]] = {}
        self.baseline_mtime_ns: int = 0
        self.last_synced_fingerprint: str = ""
        self.pending_fingerprint: str = ""
        self.pending_since: float = 0.0
        self.started_utc = _utc_now()
        self.sync_count = 0
        self.last_sync_utc = ""
        self.last_result: dict[str, Any] = {}
        self.mirrored_files: list[str] = []
        self.skipped_conflicts: list[str] = []

        self.pid_path = self.local_dir / "watcher.pid"
        self.status_path = self.local_dir / "watcher_status.json"

    def _baseline_path(self) -> Path:
        return self.local_dir / "baseline_manifest.json"

    def ensure_baseline(self) -> None:
        bp = self._baseline_path()
        if bp.is_file():
            try:
                st = bp.stat()
                if st.st_mtime_ns != self.baseline_mtime_ns:
                    loaded = fr.load_local_baseline(self.local_dir)
                    if loaded:
                        self.baseline = loaded
                        self.baseline_mtime_ns = st.st_mtime_ns
                        # Baseline refreshed (e.g. after push/init/close) -> reset dedup state
                        self.last_synced_fingerprint = ""
                        self.pending_fingerprint = ""
                        return
            except OSError:
                pass
        if not self.baseline:
            fr.save_local_baseline(self.local_dir, self.workspace)
            self.baseline = fr.load_local_baseline(self.local_dir)
            try:
                self.baseline_mtime_ns = bp.stat().st_mtime_ns
            except OSError:
                self.baseline_mtime_ns = 0

    def write_status(self, *, running: bool = True, note: str = "") -> None:
        payload = {
            "ok": True,
            "pid": os.getpid(),
            "running": running,
            "workspace": str(self.workspace),
            "startedUtc": self.started_utc,
            "lastScanUtc": _utc_now(),
            "lastSyncUtc": self.last_sync_utc,
            "syncCount": self.sync_count,
            "mirroredCount": len(self.mirrored_files),
            "mirroredFiles": self.mirrored_files[:40],
            "skippedConflicts": self.skipped_conflicts[:20],
            "lastResult": self.last_result,
            "note": note,
        }
        try:
            _atomic_write_json(self.status_path, payload)
        except OSError:
            pass

    def sync_once(self, *, force_immediate: bool = False) -> dict[str, Any]:
        """Run one stat scan and sync if changed files settled past debounce window."""
        self.ensure_baseline()
        current = scan_workspace_fast(self.workspace, self.stat_cache)
        changed, deleted = fr.compute_manifest_diff(self.baseline, current)
        fp = compute_diff_fingerprint(changed, deleted, current)

        # Fresh-account safety invariant: NEVER push an empty diff over remote state!
        if not fp:
            self.pending_fingerprint = ""
            self.pending_since = 0.0
            self.mirrored_files = []
            self.write_status(running=self.running, note="clean")
            return {"ok": True, "synced": False, "reason": "clean", "changed": [], "deleted": []}

        if fp == self.last_synced_fingerprint:
            self.write_status(running=self.running, note="up_to_date")
            return {"ok": True, "synced": False, "reason": "already_synced", "changed": changed, "deleted": deleted}

        now = time.time()
        if not force_immediate and self.debounce_sec > 0.0:
            if fp != self.pending_fingerprint:
                self.pending_fingerprint = fp
                self.pending_since = now
                self.write_status(running=self.running, note=f"debouncing:{len(changed)}m/{len(deleted)}d")
                return {"ok": True, "synced": False, "reason": "debouncing", "changed": changed, "deleted": deleted}
            if now - self.pending_since < self.debounce_sec:
                return {"ok": True, "synced": False, "reason": "debouncing", "changed": changed, "deleted": deleted}

        # Settled! Pack and push to Tier 1 + Tier 3 (Cloudflare DO) + Tier 2 (Phone Antigravity live mirror)
        prev = fr.read_local_tier(self.local_dir)
        active_session = prev.get("sessionId") if (prev and prev.get("status") == "active") else ""
        summary_note = (
            prev.get("summary")
            if (prev and prev.get("status") == "active" and prev.get("summary"))
            else f"Auto live-mirror ({len(changed)} modified/added, {len(deleted)} deleted)"
        )
        res = fr.save_checkpoint(
            workspace=self.workspace,
            local_dir=self.local_dir,
            session_id=active_session or f"live_{time.strftime('%Y%m%d', time.gmtime())}",
            phase=(prev.get("phase") if (prev and prev.get("status") == "active" and prev.get("phase")) else "live-auto-mirror"),
            summary=summary_note,
            files=changed,
            deleted=deleted,
            auto_diff=False,
            apply_live_mirror=self.apply_live_mirror,
            use_remote=self.use_remote,
            background=False,
        )

        self.last_synced_fingerprint = fp
        self.pending_fingerprint = ""
        self.pending_since = 0.0
        self.sync_count += 1
        self.last_sync_utc = _utc_now()
        self.mirrored_files = list(changed)

        tiers = res.get("tiers") or {}
        t2 = tiers.get("termux") or {}
        lm = t2.get("liveMirror") or {}
        self.skipped_conflicts = list(lm.get("skippedConflicts") or [])
        self.last_result = {
            "localOk": bool((tiers.get("local") or {}).get("ok")),
            "cloudOk": bool((tiers.get("cloud") or {}).get("ok")),
            "termuxOk": bool(t2.get("ok")),
            "agHandoff": bool(t2.get("agHandoff")),
            "writtenOnPhone": len(lm.get("written") or []),
            "alreadySyncedOnPhone": int(lm.get("alreadySynced") or 0),
            "skippedConflicts": self.skipped_conflicts,
            "termuxError": str(t2.get("error") or "")[:200],
            "cloudError": str((tiers.get("cloud") or {}).get("error") or "")[:200],
        }
        self.write_status(running=self.running, note="synced")
        return {
            "ok": True,
            "synced": True,
            "changed": changed,
            "deleted": deleted,
            "result": self.last_result,
        }

    def acquire_pid_lock(self) -> bool:
        self.local_dir.mkdir(parents=True, exist_ok=True)
        if self.pid_path.is_file():
            try:
                old_pid = int(self.pid_path.read_text(encoding="utf-8").strip())
                if old_pid != os.getpid() and _is_pid_alive(old_pid):
                    return False
            except Exception:
                pass
        self.pid_path.write_text(f"{os.getpid()}\n", encoding="utf-8")
        return True

    def release_pid_lock(self) -> None:
        try:
            if self.pid_path.is_file():
                cur = int(self.pid_path.read_text(encoding="utf-8").strip())
                if cur == os.getpid():
                    self.pid_path.unlink()
        except Exception:
            pass
        self.write_status(running=False, note="stopped")

    def run_forever(self) -> int:
        if not self.acquire_pid_lock():
            return 0
        self.ensure_baseline()
        # Prime stat cache once without triggering remote push on clean startup
        scan_workspace_fast(self.workspace, self.stat_cache)
        self.write_status(running=True, note="watching")
        try:
            while self.running:
                try:
                    self.sync_once(force_immediate=False)
                except Exception as exc:
                    self.last_result = {"error": str(exc)[:300]}
                    self.write_status(running=True, note=f"error:{type(exc).__name__}")
                # Sleep in short slices so SIGTERM exits cleanly <200ms
                slept = 0.0
                while self.running and slept < self.poll_sec:
                    step = min(0.25, self.poll_sec - slept)
                    time.sleep(step)
                    slept += step
        finally:
            self.release_pid_lock()
        return 0


def read_watcher_status(local_dir: Path) -> dict[str, Any]:
    status_path = local_dir / "watcher_status.json"
    pid_path = local_dir / "watcher.pid"
    pid = 0
    if pid_path.is_file():
        try:
            pid = int(pid_path.read_text(encoding="utf-8").strip())
        except Exception:
            pid = 0
    alive = _is_pid_alive(pid)
    data: dict[str, Any] = {}
    if status_path.is_file():
        try:
            parsed = json.loads(status_path.read_text(encoding="utf-8"))
            if isinstance(parsed, dict):
                data = parsed
        except Exception:
            data = {}
    data["pid"] = pid
    data["running"] = bool(alive)
    return data


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Zero-Quota Live Antigravity Mirror & Flight Recorder Watcher.")
    parser.add_argument("--workspace", default=str(REPO_ROOT))
    parser.add_argument("--local-dir", default=str(fr.DEFAULT_LOCAL_DIR))
    parser.add_argument("--poll", type=float, default=DEFAULT_POLL_SEC)
    parser.add_argument("--debounce", type=float, default=DEFAULT_DEBOUNCE_SEC)
    parser.add_argument("--no-remote", action="store_true")
    parser.add_argument("--no-mirror", action="store_true")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--daemon", action="store_true", help="Run continuously watching workspace edits")
    mode.add_argument("--once", action="store_true", help="Run one immediate scan+sync pass and exit")
    mode.add_argument("--status", action="store_true", help="Print live watcher status JSON")
    mode.add_argument("--reset-baseline", action="store_true", help="Snapshot current workspace as clean baseline")
    args = parser.parse_args(argv)

    workspace = Path(args.workspace).expanduser().resolve()
    local_dir = Path(args.local_dir).expanduser().resolve()

    if args.status:
        print(json.dumps(read_watcher_status(local_dir), indent=2, ensure_ascii=False))
        return 0

    if args.reset_baseline:
        base = fr.save_local_baseline(local_dir, workspace)
        print(json.dumps({"ok": True, "fileCount": base["fileCount"], "capturedUtc": base["capturedUtc"]}, indent=2))
        return 0

    watcher = LiveSyncWatcher(
        workspace=workspace,
        local_dir=local_dir,
        poll_sec=args.poll,
        debounce_sec=args.debounce,
        use_remote=not args.no_remote,
        apply_live_mirror=not args.no_mirror,
    )

    if args.once:
        res = watcher.sync_once(force_immediate=True)
        print(json.dumps(res, indent=2, ensure_ascii=False))
        return 0 if res.get("ok") else 1

    def _stop(_sig: int, _frame: Any) -> None:
        watcher.running = False

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)
    return watcher.run_forever()


if __name__ == "__main__":
    raise SystemExit(main())
