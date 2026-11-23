#!/usr/bin/env python3
"""Anti-Cancellation / Anti-Rollback 3-Tier Session Flight Recorder ("Caixa-Preta da Sessao").

Protects long AI Studio sessions against mid-turn cancellation, context loss, and automatic
workspace rollback by continuously persisting structured session state + verified delta file
snapshots across 3 independent storage tiers OUTSIDE the AI Studio workspace rollback boundary:

  Tier 1 — Local container cache (`/tmp/cavern-flight-recorder/`):
           <5 ms, survives same-container workspace rollback even offline.
  Tier 2 — Phone Termux permanent storage (`/root/.gemini/antigravity/ai_studio_bridge/flight_recorder/`):
           survives container resets, new AI Studio chats, >24h gaps, and phone reboots;
           lives strictly outside the git repo so `git status` stays 100% clean.
  Tier 3 — Cloudflare Unified Hub v6 SQLite Durable Object (`/v1/swarm/enqueue`, `taskId="fr_latest"`):
           E2E-encrypted (`omni_protocol.encrypt_envelope`), zero Worker redeploy required,
           zero KV writes, works across brand-new containers even when Termux is momentarily offline.

Pure Python 3 stdlib (zero pip dependencies). Never stores secrets or `.bridge_vault.json`.
"""

from __future__ import annotations

import argparse
import base64
import concurrent.futures
import gzip
import hashlib
import json
import os
import secrets
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path
from typing import Any

AGENT_DIR = Path(__file__).resolve().parent
REPO_ROOT = AGENT_DIR.parent.parent
if str(AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_DIR))

import bridge_auth as bauth
import omni_protocol as proto

SCHEMA_VERSION = 1
DEFAULT_LOCAL_DIR = Path(os.environ.get("CAVERN_FLIGHT_RECORDER_DIR", "/tmp/cavern-flight-recorder"))
PHONE_AG_DIR = "/root/.gemini/antigravity"
PHONE_FR_DIR = f"{PHONE_AG_DIR}/ai_studio_bridge/flight_recorder"
PHONE_REPO_DIR = f"{PHONE_AG_DIR}/scratch/Cavern"
CLOUD_TASK_ID = "fr_latest"
CLOUD_ENVELOPE_CTX = "fr:latest"
CLOUD_INLINE_BUNDLE_MAX_BYTES = 140 * 1024  # comfortably under Worker's 256 KB /v1/swarm/enqueue cap
TERMUX_INLINE_PAYLOAD_MAX_BYTES = 96 * 1024  # safe under shell ARG_MAX & Worker cmd body cap
MAX_LOCAL_HISTORY = 25


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _safe_json_loads(raw: str) -> dict[str, Any] | None:
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _dedupe_append(existing: list[str], additions: list[str], limit: int = 80) -> list[str]:
    out = [str(x).strip() for x in existing if str(x).strip()]
    seen = set(out)
    for item in additions:
        s = str(item).strip()
        if s and s not in seen:
            out.append(s)
            seen.add(s)
    return out[-limit:]


def compute_manifest_diff(
    baseline: dict[str, dict[str, Any]],
    current: dict[str, dict[str, Any]],
) -> tuple[list[str], list[str]]:
    """Return (changed_or_added_paths, deleted_paths) between baseline and current manifests."""
    changed: list[str] = []
    deleted: list[str] = []
    for rel, meta in current.items():
        base_meta = baseline.get(rel)
        if base_meta is None or base_meta.get("sha256") != meta.get("sha256"):
            changed.append(rel)
    for rel in baseline:
        if rel not in current:
            deleted.append(rel)
    return sorted(changed), sorted(deleted)


def detect_workspace_rollback(
    workspace: Path,
    file_hashes: dict[str, str],
    deleted_paths: list[str] | None = None,
) -> dict[str, Any]:
    """Compare checkpoint's expected file SHA-256 hashes against current workspace disk."""
    root = workspace.resolve()
    reverted: list[dict[str, str]] = []
    missing: list[str] = []
    intact: list[str] = []
    reappeared: list[str] = []

    for rel, expected_sha in sorted(file_hashes.items()):
        norm = rel.replace("\\", "/").lstrip("/")
        if not norm or ".." in Path(norm).parts:
            continue
        full = (root / norm).resolve()
        try:
            full.relative_to(root)
        except ValueError:
            continue
        if not full.is_file():
            missing.append(norm)
            continue
        actual_sha = hashlib.sha256(full.read_bytes()).hexdigest()
        if actual_sha != expected_sha:
            reverted.append({"path": norm, "expected": expected_sha[:12], "actual": actual_sha[:12]})
        else:
            intact.append(norm)

    for rel in sorted(deleted_paths or []):
        norm = rel.replace("\\", "/").lstrip("/")
        if not norm or ".." in Path(norm).parts:
            continue
        full = (root / norm).resolve()
        try:
            full.relative_to(root)
        except ValueError:
            continue
        if full.is_file():
            reappeared.append(norm)

    rollback_detected = bool(reverted or missing or reappeared)
    return {
        "rollbackDetected": rollback_detected,
        "reverted": reverted,
        "missing": missing,
        "reappeared": reappeared,
        "intactCount": len(intact),
        "trackedCount": len(file_hashes),
    }


def render_diary_markdown(cp: dict[str, Any]) -> str:
    """Render a compact token-diet Markdown diary for instant session recovery."""
    lines = [
        f"# Flight Recorder — Session `{cp.get('sessionId', '?')}` (seq #{cp.get('seq', 0)})",
        f"- **Updated:** `{cp.get('updatedUtc', '')}` | **Status:** `{cp.get('status', 'active')}` | **Wave:** `{cp.get('wave', '')}` | **Phase:** `{cp.get('phase', '')}`",
    ]
    if cp.get("goal"):
        lines.append(f"- **Goal:** {cp['goal']}")
    if cp.get("summary"):
        lines.append(f"- **Summary:** {cp['summary']}")
    if cp.get("nextStep"):
        lines.append(f"- **Exact Next Step:** {cp['nextStep']}")
    if cp.get("commit"):
        lines.append(f"- **Commit:** `{cp['commit']}`")

    def _add_section(title: str, items: list[str]) -> None:
        if items:
            lines.append(f"\n## {title}")
            for it in items:
                lines.append(f"- {it}")

    _add_section("User Requests", cp.get("requests") or [])
    _add_section("Verified Findings / Root Causes", cp.get("findings") or [])
    _add_section("Architectural Decisions", cp.get("decisions") or [])
    _add_section("Completed", cp.get("completed") or [])
    _add_section("Pending", cp.get("pending") or [])

    file_hashes = cp.get("fileHashes") or {}
    deleted = cp.get("deletedFiles") or []
    if file_hashes or deleted:
        lines.append(f"\n## Tracked Workspace Snapshot ({len(file_hashes)} modified/added, {len(deleted)} deleted)")
        for rel in sorted(file_hashes)[:40]:
            lines.append(f"- `{rel}` (`{file_hashes[rel][:12]}`)")
        if len(file_hashes) > 40:
            lines.append(f"- ... (+{len(file_hashes) - 40} more)")
        for rel in sorted(deleted)[:20]:
            lines.append(f"- [DELETED] `{rel}`")
    return "\n".join(lines).strip() + "\n"


def encode_bundle_gz_b64(bundle: dict[str, Any]) -> tuple[bytes, str, str]:
    """Compress omni-delta-v1 bundle to gzip bytes + base64 + sha256."""
    raw = json.dumps(bundle, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    gz = gzip.compress(raw, compresslevel=6)
    return gz, base64.b64encode(gz).decode("ascii"), hashlib.sha256(gz).hexdigest()


def decode_bundle_gz_b64(b64_text: str, expected_sha256: str = "") -> dict[str, Any]:
    gz = base64.b64decode(b64_text.encode("ascii"))
    if expected_sha256:
        actual = hashlib.sha256(gz).hexdigest()
        if actual != expected_sha256:
            raise ValueError(f"Bundle gzip SHA-256 mismatch: {actual} != {expected_sha256}")
    bundle = json.loads(gzip.decompress(gz).decode("utf-8"))
    if not isinstance(bundle, dict) or bundle.get("format") != "omni-delta-v1":
        raise ValueError("Decoded payload is not a valid omni-delta-v1 bundle.")
    return bundle


def pick_freshest_checkpoint(candidates: dict[str, dict[str, Any]]) -> tuple[str, dict[str, Any]] | tuple[None, None]:
    """Pick freshest checkpoint across tiers by (timestampUnix, seq, tier_priority)."""
    prio = {"local": 3, "termux": 2, "cloud": 1}
    best_tier: str | None = None
    best_cp: dict[str, Any] | None = None
    best_key = (-1.0, -1, -1)
    for tier, cp in candidates.items():
        if not isinstance(cp, dict) or not cp.get("sessionId"):
            continue
        key = (float(cp.get("timestampUnix") or 0), int(cp.get("seq") or 0), prio.get(tier, 0))
        if key > best_key:
            best_key = key
            best_tier = tier
            best_cp = cp
    return best_tier, best_cp


# ==================== TIER 1: LOCAL CONTAINER CACHE ====================


def _ensure_local_dir(local_dir: Path) -> None:
    local_dir.mkdir(parents=True, exist_ok=True)
    (local_dir / "history").mkdir(parents=True, exist_ok=True)


def save_local_baseline(local_dir: Path, workspace: Path) -> dict[str, Any]:
    _ensure_local_dir(local_dir)
    manifest = proto.build_workspace_manifest(workspace)
    payload = {
        "schema": SCHEMA_VERSION,
        "workspace": str(workspace.resolve()),
        "capturedUtc": _utc_now(),
        "fileCount": len(manifest),
        "manifest": manifest,
    }
    (local_dir / "baseline_manifest.json").write_text(
        json.dumps(payload, separators=(",", ":")),
        encoding="utf-8",
    )
    return payload


def load_local_baseline(local_dir: Path) -> dict[str, dict[str, Any]]:
    p = local_dir / "baseline_manifest.json"
    if not p.is_file():
        return {}
    data = _safe_json_loads(p.read_text(encoding="utf-8"))
    if not data or not isinstance(data.get("manifest"), dict):
        return {}
    return data["manifest"]


def write_local_tier(
    local_dir: Path,
    cp: dict[str, Any],
    diary_md: str,
    bundle_gz: bytes | None,
) -> dict[str, Any]:
    _ensure_local_dir(local_dir)
    latest_path = local_dir / "latest.json"
    diary_path = local_dir / "LATEST_DIARY.md"
    latest_path.write_text(json.dumps(cp, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    diary_path.write_text(diary_md, encoding="utf-8")

    seq = int(cp.get("seq") or 0)
    ts = int(cp.get("timestampUnix") or int(time.time()))
    hist_path = local_dir / "history" / f"cp_{seq:04d}_{ts}.json"
    hist_path.write_text(json.dumps(cp, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    # Trim old history entries
    hist_files = sorted((local_dir / "history").glob("cp_*.json"))
    for stale in hist_files[:-MAX_LOCAL_HISTORY]:
        try:
            stale.unlink()
        except OSError:
            pass

    bundle_path = local_dir / "latest_bundle.json.gz"
    if bundle_gz is not None:
        bundle_path.write_bytes(bundle_gz)
    elif bundle_path.is_file() and not (cp.get("fileHashes") or cp.get("deletedFiles")):
        try:
            bundle_path.unlink()
        except OSError:
            pass

    return {
        "ok": True,
        "path": str(latest_path),
        "bundleBytes": len(bundle_gz) if bundle_gz is not None else 0,
    }


def read_local_tier(local_dir: Path) -> dict[str, Any] | None:
    p = local_dir / "latest.json"
    if not p.is_file():
        return None
    return _safe_json_loads(p.read_text(encoding="utf-8"))


def read_local_bundle(local_dir: Path) -> dict[str, Any] | None:
    p = local_dir / "latest_bundle.json.gz"
    if not p.is_file():
        return None
    try:
        bundle = json.loads(gzip.decompress(p.read_bytes()).decode("utf-8"))
        return bundle if isinstance(bundle, dict) and bundle.get("format") == "omni-delta-v1" else None
    except Exception:
        return None


# ==================== CREDENTIAL RESOLUTION (ZERO-PASTE, NON-INTERACTIVE) ====================


def _try_resolve_remote_auth(
    url_arg: str = "",
    pair_secret_arg: str = "",
    admin_key_arg: str = "",
) -> tuple[str, str, str] | None:
    resolved = bauth.resolve_connection(
        cli_url=url_arg,
        cli_pair_secret=pair_secret_arg,
        cli_admin_key=admin_key_arg,
    )
    relay = resolved.get("relayUrl") or ""
    secret = resolved.get("pairSecret") or ""
    admin = resolved.get("adminKey") or ""
    if not relay or not secret:
        return None
    return relay, secret, admin


# ==================== TIER 3: CLOUDFLARE HUB v6 SQLITE DO ====================


def write_cloud_tier(
    relay_url: str,
    pair_secret: str,
    cp: dict[str, Any],
    bundle_gz_b64: str | None,
    *,
    admin_key: str = "",
) -> dict[str, Any]:
    """Store E2E-encrypted checkpoint in Cloudflare Hub v6 SQLite DO via /v1/swarm/enqueue."""
    headers = proto.derive_agent_headers(pair_secret, channel="termux", admin_key=admin_key)
    payload: dict[str, Any] = {"checkpoint": cp}
    inline_included = False
    if bundle_gz_b64 and len(bundle_gz_b64) <= CLOUD_INLINE_BUNDLE_MAX_BYTES:
        payload["bundleGzB64"] = bundle_gz_b64
        inline_included = True

    env = proto.encrypt_envelope(pair_secret, CLOUD_ENVELOPE_CTX, payload)
    # Double-check body size stays comfortably below Worker's 256 KB cap
    body = {
        "taskId": CLOUD_TASK_ID,
        "role": "flight_recorder",
        "title": f"FR:{cp.get('sessionId', 'session')}#{cp.get('seq', 0)}:{cp.get('wave', '')}"[:120],
        "createdBy": "aistudio-flight-recorder",
        "instructions": "E2E-encrypted Flight Recorder checkpoint (decrypt with pairSecret ctx fr:latest)",
        "envelope": env,
    }
    if len(json.dumps(body, separators=(",", ":"))) > 235 * 1024 and inline_included:
        payload.pop("bundleGzB64", None)
        inline_included = False
        body["envelope"] = proto.encrypt_envelope(pair_secret, CLOUD_ENVELOPE_CTX, payload)

    st, resp = proto.http_json("POST", f"{relay_url}/v1/swarm/enqueue", headers, body, timeout=20)
    ok = st < 400 and bool(resp.get("ok"))
    return {
        "ok": ok,
        "status": st,
        "taskId": CLOUD_TASK_ID,
        "inlineBundle": inline_included,
        "error": "" if ok else str(resp.get("error") or resp),
    }


def read_cloud_tier(
    relay_url: str,
    pair_secret: str,
    *,
    admin_key: str = "",
) -> tuple[dict[str, Any] | None, str | None, str]:
    """Return (checkpoint_dict, inline_bundle_gz_b64, error_string)."""
    headers = proto.derive_agent_headers(pair_secret, channel="termux", admin_key=admin_key)
    st, resp = proto.http_json(
        "GET",
        f"{relay_url}/v1/swarm/task?taskId={urllib.parse.quote(CLOUD_TASK_ID)}",
        headers,
        timeout=15,
    )
    if st == 404 or not resp.get("ok"):
        return None, None, "" if st == 404 else str(resp.get("error") or f"HTTP {st}")
    task = resp.get("task") or {}
    env = task.get("envelope")
    if not isinstance(env, dict):
        return None, None, "Cloud task has no E2E envelope"
    try:
        dec = proto.decrypt_envelope(pair_secret, CLOUD_ENVELOPE_CTX, env)
    except Exception as exc:
        return None, None, f"Decrypt failed: {exc}"
    cp = dec.get("checkpoint") if isinstance(dec, dict) else None
    b64 = dec.get("bundleGzB64") if isinstance(dec, dict) else None
    return (cp if isinstance(cp, dict) else None), (b64 if isinstance(b64, str) else None), ""


# ==================== TIER 2: PHONE TERMUX PERMANENT STORAGE ====================


def _send_termux_cmd(
    relay_url: str,
    pair_secret: str,
    cmd: str,
    args: dict[str, Any],
    *,
    admin_key: str = "",
    timeout_sec: int = 25,
) -> dict[str, Any]:
    import omni_agent_client as oac

    return oac.send_agent_command(
        relay_url,
        pair_secret,
        cmd,
        args,
        channel="termux",
        admin_key=admin_key,
        timeout_sec=timeout_sec,
    )


# Self-contained pure-stdlib Python runner executed in ONE Termux shell_exec RPC.
# Writes all Flight Recorder files + Antigravity LATEST_HANDOFF.md + knowledge mirror
# AND optionally applies a conflict-safe live mirror directly into the local Antigravity repo.
_TERMUX_ATOMIC_SYNC_RUNNER = r"""
import base64, gzip, hashlib, json, os, subprocess, sys
from pathlib import Path

def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp.{os.getpid()}")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)

def atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp.{os.getpid()}")
    tmp.write_bytes(data)
    os.replace(tmp, path)

def safe_rel(root: Path, rel: str):
    norm = str(rel or "").replace("\\", "/").lstrip("/")
    if not norm or ".." in Path(norm).parts:
        return None, None
    full = (root / norm).resolve()
    try:
        full.relative_to(root)
    except ValueError:
        return None, None
    return norm, full

def git_dirty_paths(repo: Path) -> set:
    dirty = set()
    if not (repo / ".git").exists():
        return dirty
    try:
        r = subprocess.run(
            ["git", "status", "--porcelain", "-z", "-uall"],
            cwd=str(repo),
            capture_output=True,
            timeout=15,
            check=False,
        )
        if r.returncode != 0:
            return dirty
        parts = r.stdout.split(b"\x00")
        i = 0
        while i < len(parts):
            entry = parts[i]
            if len(entry) >= 4:
                code = entry[:2].decode("utf-8", errors="ignore")
                p = entry[3:].decode("utf-8", errors="replace").replace("\\", "/")
                if p:
                    dirty.add(p)
                if "R" in code or "C" in code:
                    i += 1
            i += 1
    except Exception:
        pass
    return dirty

def run_payload(payload: dict) -> dict:
    fr_dir = Path(payload["frDir"]).expanduser()
    ag_dir = Path(payload["agDir"]).expanduser()
    repo_dir = Path(payload["repoDir"]).expanduser().resolve()
    cp = payload["checkpoint"]
    diary_md = str(payload.get("diaryMd") or "")
    seq = int(cp.get("seq") or 0)
    ts = int(cp.get("timestampUnix") or 0)

    (fr_dir / "history").mkdir(parents=True, exist_ok=True)
    cp_text = json.dumps(cp, indent=2, ensure_ascii=False) + "\n"
    atomic_write_text(fr_dir / "latest.json", cp_text)
    atomic_write_text(fr_dir / "LATEST_DIARY.md", diary_md)
    atomic_write_text(fr_dir / "history" / f"cp_{seq:04d}_{ts}.json", cp_text)

    # Trim old history entries (keep newest 25)
    hist_files = sorted((fr_dir / "history").glob("cp_*.json"))
    for stale in hist_files[:-25]:
        try:
            stale.unlink()
        except OSError:
            pass

    bundle_saved = False
    bundle_mode = "none"
    if payload.get("bundleGzB64"):
        atomic_write_text(fr_dir / "latest_bundle.json.gz.b64", str(payload["bundleGzB64"]))
        bundle_saved = True
        bundle_mode = "direct_b64"
    elif payload.get("bundleBlob"):
        atomic_write_text(
            fr_dir / "latest_bundle_blob.json",
            json.dumps(payload["bundleBlob"], separators=(",", ":")),
        )
        bundle_saved = True
        bundle_mode = f"blob:{payload['bundleBlob'].get('blobId', '')}"

    ag_handoff_written = False
    if payload.get("syncAntigravityHandoff", True):
        bridge_dir = ag_dir / "ai_studio_bridge"
        sessions_dir = bridge_dir / "sessions"
        sessions_dir.mkdir(parents=True, exist_ok=True)
        sid_raw = str(cp.get("sessionId") or "aistudio_live")
        safe_id = "".join(c for c in sid_raw if c.isalnum() or c in ("_", "-"))[:64] or "session"
        title = f"{cp.get('wave') or 'AI Studio'} — {cp.get('phase') or cp.get('status') or 'live'}"
        files_list = sorted((cp.get("fileHashes") or {}).keys())[:64]
        lines = [
            f"# {title}",
            f"- **UTC**: `{cp.get('updatedUtc', '')}` | **Session**: `{safe_id}` (seq `#{seq}`) | **Status**: `{cp.get('status', 'active')}` | **Commit**: `{cp.get('commit') or 'live-mirror'}`",
            f"- **Repo**: `{repo_dir}`",
            "",
            "## Compact Summary (Token-Diet)",
            str(cp.get("summary") or cp.get("goal") or "(live sync)"),
            "",
        ]
        if files_list:
            lines.append("## Live Mirrored / Tracked Files")
            lines.extend(f"- `{f}`" for f in files_list)
            lines.append("")
        if cp.get("nextStep"):
            lines.append("## Next")
            lines.append(str(cp["nextStep"]))
            lines.append("")
        handoff_text = "\n".join(lines)
        atomic_write_text(sessions_dir / f"{safe_id}.md", handoff_text)
        atomic_write_text(bridge_dir / "LATEST_HANDOFF.md", handoff_text)

        index_path = bridge_dir / "index.json"
        index = []
        if index_path.is_file():
            try:
                parsed = json.loads(index_path.read_text(encoding="utf-8"))
                if isinstance(parsed, list):
                    index = parsed
            except Exception:
                index = []
        entry = {
            "sessionId": safe_id,
            "utc": cp.get("updatedUtc", ""),
            "repo": str(repo_dir),
            "title": title[:160],
            "commit": str(cp.get("commit") or "")[:64],
            "files": files_list,
            "summary": str(cp.get("summary") or cp.get("goal") or "")[:4000],
            "next": str(cp.get("nextStep") or "")[:1000],
        }
        index = [x for x in index if x.get("sessionId") != safe_id]
        index.insert(0, entry)
        atomic_write_text(index_path, json.dumps(index[:40], indent=2, ensure_ascii=False))

        knowledge_dir = ag_dir / "knowledge"
        if knowledge_dir.is_dir():
            atomic_write_text(knowledge_dir / "ai_studio_latest_sync.md", handoff_text)
        ag_handoff_written = True

    mirror_state_path = fr_dir / "live_mirror_state.json"
    mirror_res = {"applied": False, "written": [], "deleted": [], "alreadySynced": 0, "skippedConflicts": []}
    if payload.get("clearMirrorState"):
        atomic_write_text(mirror_state_path, json.dumps({"mirrored": {}, "updatedUtc": cp.get("updatedUtc", "")}, indent=2) + "\n")
    elif payload.get("applyLiveMirror") and isinstance(payload.get("bundle"), dict) and repo_dir.is_dir():
        bundle = payload["bundle"]
        state = {"mirrored": {}}
        if mirror_state_path.is_file():
            try:
                loaded = json.loads(mirror_state_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict) and isinstance(loaded.get("mirrored"), dict):
                    state = loaded
            except Exception:
                pass
        mirrored = state["mirrored"]
        dirty = git_dirty_paths(repo_dir)

        for fentry in bundle.get("files") or []:
            norm, full = safe_rel(repo_dir, fentry.get("path", ""))
            if not norm or full is None:
                continue
            target_sha = str(fentry.get("sha256") or "")
            prev_rec = mirrored.get(norm)
            exists_now = full.is_file()
            cur_sha = hashlib.sha256(full.read_bytes()).hexdigest() if exists_now else ""
            if exists_now and cur_sha == target_sha:
                mirror_res["alreadySynced"] += 1
                if norm not in mirrored:
                    mirrored[norm] = {"sha256": target_sha, "existedBefore": True}
                else:
                    mirrored[norm]["sha256"] = target_sha
                continue
            # Conflict guard: never clobber a file manually modified by the user on the phone!
            if exists_now and norm in dirty:
                if not prev_rec or cur_sha != prev_rec.get("sha256"):
                    mirror_res["skippedConflicts"].append(norm)
                    continue
            raw = gzip.decompress(base64.b64decode(fentry["gz_b64"].encode("ascii")))
            if target_sha and hashlib.sha256(raw).hexdigest() != target_sha:
                raise ValueError(f"SHA-256 mismatch for mirrored file {norm}")
            existed_before = prev_rec["existedBefore"] if prev_rec else exists_now
            atomic_write_bytes(full, raw)
            mirrored[norm] = {"sha256": target_sha or hashlib.sha256(raw).hexdigest(), "existedBefore": existed_before}
            mirror_res["written"].append(norm)

        for drel in bundle.get("deleted") or []:
            norm, full = safe_rel(repo_dir, drel)
            if not norm or full is None or not full.is_file():
                mirrored.pop(norm or "", None)
                continue
            prev_rec = mirrored.get(norm)
            cur_sha = hashlib.sha256(full.read_bytes()).hexdigest()
            if norm in dirty and (not prev_rec or cur_sha != prev_rec.get("sha256")):
                mirror_res["skippedConflicts"].append(norm)
                continue
            full.unlink()
            mirrored.pop(norm, None)
            mirror_res["deleted"].append(norm)

        state["updatedUtc"] = cp.get("updatedUtc", "")
        atomic_write_text(mirror_state_path, json.dumps(state, indent=2, ensure_ascii=False) + "\n")
        mirror_res["applied"] = True
    elif payload.get("recordMirroredHashes") and isinstance(payload.get("recordMirroredHashes"), dict):
        state = {"mirrored": {}}
        if mirror_state_path.is_file():
            try:
                loaded = json.loads(mirror_state_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict) and isinstance(loaded.get("mirrored"), dict):
                    state = loaded
            except Exception:
                pass
        for rel, sha in payload["recordMirroredHashes"].items():
            prev_rec = state["mirrored"].get(rel)
            state["mirrored"][rel] = {
                "sha256": sha,
                "existedBefore": prev_rec["existedBefore"] if prev_rec else True,
            }
        state["updatedUtc"] = cp.get("updatedUtc", "")
        atomic_write_text(mirror_state_path, json.dumps(state, indent=2, ensure_ascii=False) + "\n")

    return {
        "ok": True,
        "path": str(fr_dir / "latest.json"),
        "bundleSaved": bundle_saved,
        "bundleMode": bundle_mode,
        "agHandoff": ag_handoff_written,
        "liveMirror": mirror_res,
    }

if __name__ == "__main__":
    raw_b64 = os.environ.get("FR_PAYLOAD_B64") or (sys.argv[1] if len(sys.argv) > 1 else "")
    data = json.loads(gzip.decompress(base64.b64decode(raw_b64.encode("ascii"))).decode("utf-8"))
    print(json.dumps(run_payload(data), separators=(",", ":")))
"""


def build_termux_atomic_sync_script() -> str:
    """Return the self-contained pure-stdlib Python atomic sync script (testable locally)."""
    return _TERMUX_ATOMIC_SYNC_RUNNER.strip()


def build_termux_reconcile_pull_script(remote: str = "origin", branch: str = "master") -> str:
    """Return a single-RPC script that safely reverts ONLY unmodified live-mirrored files before git pull --rebase."""
    safe_remote = json.dumps(remote or "origin")
    safe_branch = json.dumps(branch or "master")
    safe_state = json.dumps(f"{PHONE_FR_DIR}/live_mirror_state.json")
    return f"""
import hashlib, json, os, subprocess
from pathlib import Path

repo = Path.cwd().resolve()
state_path = Path({safe_state}).expanduser()
reconciled = []
preserved_manual = []

if state_path.is_file():
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except Exception:
        state = {{}}
    mirrored = state.get("mirrored") if isinstance(state, dict) else {{}}
    if isinstance(mirrored, dict) and mirrored:
        remaining = {{}}
        to_checkout = []
        for rel, rec in mirrored.items():
            norm = str(rel).replace("\\\\", "/").lstrip("/")
            if not norm or ".." in Path(norm).parts:
                continue
            full = (repo / norm).resolve()
            try:
                full.relative_to(repo)
            except ValueError:
                continue
            expected_sha = str((rec or {{}}).get("sha256") or "")
            existed_before = bool((rec or {{}}).get("existedBefore", True))
            if not full.is_file():
                reconciled.append(norm)
                continue
            actual_sha = hashlib.sha256(full.read_bytes()).hexdigest()
            if expected_sha and actual_sha == expected_sha:
                if existed_before:
                    to_checkout.append(norm)
                else:
                    try:
                        full.unlink()
                    except OSError:
                        pass
                reconciled.append(norm)
            else:
                preserved_manual.append(norm)
                remaining[norm] = rec
        if to_checkout:
            subprocess.run(["git", "checkout", "--", *to_checkout], cwd=str(repo), capture_output=True, timeout=30, check=False)
        tmp = state_path.with_name(state_path.name + f".tmp.{{os.getpid()}}")
        tmp.write_text(json.dumps({{"mirrored": remaining}}, indent=2) + "\\n", encoding="utf-8")
        os.replace(tmp, state_path)

r_fetch = subprocess.run(["git", "fetch", {safe_remote}], cwd=str(repo), capture_output=True, text=True, timeout=60, check=False)
r_pull = subprocess.run(["git", "pull", "--rebase", {safe_remote}, {safe_branch}], cwd=str(repo), capture_output=True, text=True, timeout=90, check=False)
r_head = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=str(repo), capture_output=True, text=True, timeout=15, check=False)
print(json.dumps({{
    "ok": r_pull.returncode == 0,
    "head": r_head.stdout.strip(),
    "reconciled": reconciled,
    "preservedManual": preserved_manual,
    "stderr": (r_pull.stderr or r_fetch.stderr or "").strip()[:1000],
}}, separators=(",", ":")))
""".strip()


def _run_termux_atomic_script(
    relay_url: str,
    pair_secret: str,
    payload_obj: dict[str, Any],
    *,
    admin_key: str = "",
    timeout_sec: int = 35,
) -> dict[str, Any]:
    raw_json = json.dumps(payload_obj, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    payload_b64 = base64.b64encode(gzip.compress(raw_json, compresslevel=6)).decode("ascii")
    script_b64 = base64.b64encode(build_termux_atomic_sync_script().encode("utf-8")).decode("ascii")
    # Both script_b64 and payload_b64 contain strictly [A-Za-z0-9+/=] — zero shell injection/quoting hazards.
    cmd = (
        f"FR_PAYLOAD_B64={payload_b64} "
        f"python3 -B -c \"import base64; exec(base64.b64decode('{script_b64}').decode('utf-8'))\""
    )
    res = _send_termux_cmd(
        relay_url,
        pair_secret,
        "shell_exec",
        {"command": cmd, "timeoutSec": min(60, timeout_sec)},
        admin_key=admin_key,
        timeout_sec=timeout_sec,
    )
    if not res.get("ok"):
        return {"ok": False, "error": str(res.get("stderr") or res.get("error") or res)}
    out_lines = [ln.strip() for ln in str(res.get("stdout") or "").splitlines() if ln.strip()]
    parsed = _safe_json_loads(out_lines[-1]) if out_lines else None
    if not parsed:
        return {"ok": False, "error": f"Unexpected atomic sync output: {str(res.get('stdout') or '')[:240]}"}
    return parsed


def write_termux_tier(
    relay_url: str,
    pair_secret: str,
    cp: dict[str, Any],
    diary_md: str,
    bundle_gz_b64: str | None,
    *,
    bundle: dict[str, Any] | None = None,
    apply_live_mirror: bool = False,
    clear_mirror_state: bool = False,
    sync_antigravity_handoff: bool = True,
    admin_key: str = "",
) -> dict[str, Any]:
    """Persist checkpoint + diary + bundle + Antigravity handoff (+ optional live repo mirror) in ONE atomic Termux RPC."""
    try:
        inline_payload: dict[str, Any] = {
            "frDir": PHONE_FR_DIR,
            "agDir": PHONE_AG_DIR,
            "repoDir": PHONE_REPO_DIR,
            "checkpoint": cp,
            "diaryMd": diary_md,
            "bundleGzB64": bundle_gz_b64 if (bundle_gz_b64 and len(bundle_gz_b64) <= TERMUX_INLINE_PAYLOAD_MAX_BYTES) else None,
            "bundle": bundle if apply_live_mirror else None,
            "applyLiveMirror": bool(apply_live_mirror and bundle),
            "clearMirrorState": bool(clear_mirror_state),
            "syncAntigravityHandoff": bool(sync_antigravity_handoff),
        }
        est_size = len(gzip.compress(json.dumps(inline_payload, separators=(",", ":")).encode("utf-8"), compresslevel=6))

        if est_size <= TERMUX_INLINE_PAYLOAD_MAX_BYTES:
            return _run_termux_atomic_script(
                relay_url,
                pair_secret,
                inline_payload,
                admin_key=admin_key,
                timeout_sec=35,
            )

        # Large bundle (>96 KB compressed): stream via encrypted chunked blob + apply via git_apply_bundle if mirroring,
        # then write compact metadata + Antigravity handoff in ONE tiny atomic RPC!
        blob = proto.upload_encrypted_blob(
            relay_url,
            pair_secret,
            bundle if (apply_live_mirror and bundle) else {"bundleGzB64": bundle_gz_b64 or "", "sha256": cp.get("bundleSha256", "")},
            admin_key=admin_key,
            meta={"kind": "flight_recorder_bundle", "sessionId": cp.get("sessionId", "")},
        )
        mirror_applied_via_blob = None
        if apply_live_mirror and bundle:
            mirror_applied_via_blob = _send_termux_cmd(
                relay_url,
                pair_secret,
                "git_apply_bundle",
                {"blobId": blob["blobId"], "allowDelete": bool(bundle.get("deleted"))},
                admin_key=admin_key,
                timeout_sec=60,
            )

        meta_payload = {
            "frDir": PHONE_FR_DIR,
            "agDir": PHONE_AG_DIR,
            "repoDir": PHONE_REPO_DIR,
            "checkpoint": cp,
            "diaryMd": diary_md,
            "bundleGzB64": None,
            "bundleBlob": {"blobId": blob["blobId"], "sha256": cp.get("bundleSha256", "")},
            "applyLiveMirror": False,
            "recordMirroredHashes": (cp.get("fileHashes") or {}) if (apply_live_mirror and mirror_applied_via_blob and mirror_applied_via_blob.get("ok")) else None,
            "clearMirrorState": bool(clear_mirror_state),
            "syncAntigravityHandoff": bool(sync_antigravity_handoff),
        }
        res = _run_termux_atomic_script(
            relay_url,
            pair_secret,
            meta_payload,
            admin_key=admin_key,
            timeout_sec=35,
        )
        if mirror_applied_via_blob is not None:
            res["blobMirror"] = {
                "ok": bool(mirror_applied_via_blob.get("ok")),
                "applied": mirror_applied_via_blob.get("applied"),
            }
        return res
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def read_termux_tier(
    relay_url: str,
    pair_secret: str,
    *,
    admin_key: str = "",
) -> tuple[dict[str, Any] | None, str]:
    try:
        res = _send_termux_cmd(
            relay_url,
            pair_secret,
            "file_read",
            {"path": f"{PHONE_FR_DIR}/latest.json", "maxBytes": 256 * 1024},
            admin_key=admin_key,
            timeout_sec=20,
        )
        if not res.get("ok"):
            return None, str(res.get("error") or "not found")
        cp = _safe_json_loads(str(res.get("content") or ""))
        return cp, "" if cp else "invalid json"
    except Exception as exc:
        return None, str(exc)


def read_termux_bundle(
    relay_url: str,
    pair_secret: str,
    expected_sha256: str = "",
    *,
    admin_key: str = "",
) -> dict[str, Any] | None:
    try:
        res = _send_termux_cmd(
            relay_url,
            pair_secret,
            "file_read",
            {"path": f"{PHONE_FR_DIR}/latest_bundle.json.gz.b64", "maxBytes": 300 * 1024},
            admin_key=admin_key,
            timeout_sec=25,
        )
        if res.get("ok") and res.get("content"):
            return decode_bundle_gz_b64(str(res["content"]).strip(), expected_sha256=expected_sha256)
        ptr = _send_termux_cmd(
            relay_url,
            pair_secret,
            "file_read",
            {"path": f"{PHONE_FR_DIR}/latest_bundle_blob.json", "maxBytes": 16 * 1024},
            admin_key=admin_key,
            timeout_sec=15,
        )
        if ptr.get("ok") and ptr.get("content"):
            meta = _safe_json_loads(str(ptr["content"]))
            if meta and meta.get("blobId"):
                data = proto.download_encrypted_blob(
                    relay_url,
                    pair_secret,
                    str(meta["blobId"]),
                    admin_key=admin_key,
                    delete_after=False,
                )
                return decode_bundle_gz_b64(str(data["bundleGzB64"]), expected_sha256=expected_sha256)
    except Exception:
        return None
    return None


# ==================== HIGH-LEVEL OPERATIONS ====================


def _collect_tier_checkpoints(
    local_dir: Path,
    *,
    use_remote: bool,
    url_arg: str = "",
    pair_secret_arg: str = "",
    admin_key_arg: str = "",
) -> dict[str, Any]:
    candidates: dict[str, dict[str, Any]] = {}
    tier_errors: dict[str, str] = {}
    cloud_inline_b64: str | None = None

    local_cp = read_local_tier(local_dir)
    if local_cp:
        candidates["local"] = local_cp

    auth = _try_resolve_remote_auth(url_arg, pair_secret_arg, admin_key_arg) if use_remote else None
    if use_remote and auth:
        relay, secret, admin = auth
        try:
            c_cp, c_b64, c_err = read_cloud_tier(relay, secret, admin_key=admin)
            if c_cp:
                candidates["cloud"] = c_cp
                cloud_inline_b64 = c_b64
            elif c_err:
                tier_errors["cloud"] = c_err
        except Exception as exc:
            tier_errors["cloud"] = str(exc)

        try:
            t_cp, t_err = read_termux_tier(relay, secret, admin_key=admin)
            if t_cp:
                candidates["termux"] = t_cp
            elif t_err:
                tier_errors["termux"] = t_err
        except Exception as exc:
            tier_errors["termux"] = str(exc)
    elif use_remote:
        tier_errors["remote"] = "No pairSecret cached yet (local tier only)"

    return {
        "candidates": candidates,
        "errors": tier_errors,
        "cloudInlineB64": cloud_inline_b64,
        "auth": auth,
    }


def _spawn_background_remote_flush(
    local_dir: Path,
    *,
    apply_live_mirror: bool = False,
    clear_mirror_state: bool = False,
    url_arg: str = "",
    pair_secret_arg: str = "",
    admin_key_arg: str = "",
) -> dict[str, Any]:
    """Flush Tier 2 (Termux+Antigravity) & Tier 3 (Cloudflare Hub v6) in a detached background process (<40ms CLI return)."""
    cmd = [
        sys.executable,
        "-B",
        str(Path(__file__).resolve()),
        "--local-dir",
        str(local_dir),
    ]
    if url_arg:
        cmd += ["--url", url_arg]
    if pair_secret_arg:
        cmd += ["--pair-secret", pair_secret_arg]
    if admin_key_arg:
        cmd += ["--admin-key", admin_key_arg]
    cmd.append("flush-remote")
    if apply_live_mirror:
        cmd.append("--mirror")
    if clear_mirror_state:
        cmd.append("--clear-mirror-state")
    try:
        log_path = local_dir / "bg_flush.log"
        log_f = open(log_path, "ab")
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=log_f,
            stderr=log_f,
            start_new_session=True,
            close_fds=True,
        )
        log_f.close()
        return {"ok": True, "mode": "background", "pid": proc.pid}
    except Exception as exc:
        return {"ok": False, "mode": "background", "error": str(exc)}


def _persist_remote_tiers(
    cp: dict[str, Any],
    diary_md: str,
    bundle: dict[str, Any] | None,
    bundle_gz_b64: str | None,
    *,
    apply_live_mirror: bool = False,
    clear_mirror_state: bool = False,
    url_arg: str = "",
    pair_secret_arg: str = "",
    admin_key_arg: str = "",
) -> dict[str, Any]:
    tiers: dict[str, Any] = {}
    auth = _try_resolve_remote_auth(url_arg, pair_secret_arg, admin_key_arg)
    if not auth:
        tiers["cloud"] = {"ok": False, "skipped": True, "error": "No pairSecret configured"}
        tiers["termux"] = {"ok": False, "skipped": True, "error": "No pairSecret configured"}
        return tiers

    relay, secret, admin = auth

    def _do_cloud() -> dict[str, Any]:
        try:
            return write_cloud_tier(relay, secret, cp, bundle_gz_b64, admin_key=admin)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def _do_termux() -> dict[str, Any]:
        try:
            return write_termux_tier(
                relay,
                secret,
                cp,
                diary_md,
                bundle_gz_b64,
                bundle=bundle,
                apply_live_mirror=apply_live_mirror,
                clear_mirror_state=clear_mirror_state,
                sync_antigravity_handoff=True,
                admin_key=admin,
            )
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        f_cloud = pool.submit(_do_cloud)
        f_termux = pool.submit(_do_termux)
        tiers["cloud"] = f_cloud.result()
        tiers["termux"] = f_termux.result()
    return tiers


def _persist_to_all_tiers(
    local_dir: Path,
    cp: dict[str, Any],
    bundle: dict[str, Any] | None,
    *,
    use_remote: bool,
    background: bool = False,
    apply_live_mirror: bool = False,
    clear_mirror_state: bool = False,
    url_arg: str = "",
    pair_secret_arg: str = "",
    admin_key_arg: str = "",
) -> dict[str, Any]:
    bundle_gz: bytes | None = None
    bundle_gz_b64: str | None = None
    if bundle and (bundle.get("files") or bundle.get("deleted")):
        bundle_gz, bundle_gz_b64, bundle_sha = encode_bundle_gz_b64(bundle)
        cp["bundleSha256"] = bundle_sha
        cp["bundleGzBytes"] = len(bundle_gz)
        cp["hasBundle"] = True
    else:
        cp["bundleSha256"] = ""
        cp["bundleGzBytes"] = 0
        cp["hasBundle"] = False

    diary_md = render_diary_markdown(cp)
    tiers: dict[str, Any] = {}
    tiers["local"] = write_local_tier(local_dir, cp, diary_md, bundle_gz)

    if use_remote:
        if background:
            tiers["remote"] = _spawn_background_remote_flush(
                local_dir,
                apply_live_mirror=apply_live_mirror,
                clear_mirror_state=clear_mirror_state,
                url_arg=url_arg,
                pair_secret_arg=pair_secret_arg,
                admin_key_arg=admin_key_arg,
            )
        else:
            tiers.update(
                _persist_remote_tiers(
                    cp,
                    diary_md,
                    bundle,
                    bundle_gz_b64,
                    apply_live_mirror=apply_live_mirror,
                    clear_mirror_state=clear_mirror_state,
                    url_arg=url_arg,
                    pair_secret_arg=pair_secret_arg,
                    admin_key_arg=admin_key_arg,
                )
            )
    return tiers


def flush_local_to_remote(
    local_dir: Path,
    *,
    apply_live_mirror: bool = False,
    clear_mirror_state: bool = False,
    url_arg: str = "",
    pair_secret_arg: str = "",
    admin_key_arg: str = "",
) -> dict[str, Any]:
    """Read freshest Tier 1 local checkpoint + bundle and push concurrently to Tier 2 (Termux) & Tier 3 (Cloudflare)."""
    cp = read_local_tier(local_dir)
    if not cp:
        return {"ok": False, "error": "No local checkpoint in Tier 1 cache to flush."}
    bundle = read_local_bundle(local_dir) if cp.get("hasBundle") else None
    bundle_gz_b64: str | None = None
    if bundle and (bundle.get("files") or bundle.get("deleted")):
        _, bundle_gz_b64, _ = encode_bundle_gz_b64(bundle)
    diary_md = render_diary_markdown(cp)
    tiers = _persist_remote_tiers(
        cp,
        diary_md,
        bundle,
        bundle_gz_b64,
        apply_live_mirror=apply_live_mirror,
        clear_mirror_state=clear_mirror_state,
        url_arg=url_arg,
        pair_secret_arg=pair_secret_arg,
        admin_key_arg=admin_key_arg,
    )
    return {"ok": bool(tiers.get("cloud", {}).get("ok") or tiers.get("termux", {}).get("ok")), "tiers": tiers}


def init_session(
    *,
    workspace: Path,
    local_dir: Path,
    session_id: str,
    wave: str = "",
    goal: str = "",
    requests: list[str] | None = None,
    use_remote: bool = True,
    background: bool = False,
    url_arg: str = "",
    pair_secret_arg: str = "",
    admin_key_arg: str = "",
) -> dict[str, Any]:
    base = save_local_baseline(local_dir, workspace)
    now = time.time()
    cp: dict[str, Any] = {
        "schema": SCHEMA_VERSION,
        "sessionId": session_id or f"sess_{int(now)}_{secrets.token_hex(3)}",
        "seq": 1,
        "status": "active",
        "timestampUnix": round(now, 3),
        "updatedUtc": _utc_now(),
        "wave": wave,
        "phase": "init",
        "goal": goal,
        "summary": goal or "Session initialized",
        "nextStep": "",
        "commit": "",
        "requests": _dedupe_append([], requests or []),
        "findings": [],
        "decisions": [],
        "completed": [],
        "pending": [],
        "fileHashes": {},
        "deletedFiles": [],
        "baselineFileCount": base["fileCount"],
    }
    tiers = _persist_to_all_tiers(
        local_dir,
        cp,
        None,
        use_remote=use_remote,
        background=background,
        url_arg=url_arg,
        pair_secret_arg=pair_secret_arg,
        admin_key_arg=admin_key_arg,
    )
    return {"ok": True, "checkpoint": cp, "tiers": tiers}


def save_checkpoint(
    *,
    workspace: Path,
    local_dir: Path,
    session_id: str = "",
    wave: str = "",
    phase: str = "",
    goal: str = "",
    summary: str = "",
    next_step: str = "",
    requests: list[str] | None = None,
    findings: list[str] | None = None,
    decisions: list[str] | None = None,
    completed: list[str] | None = None,
    pending: list[str] | None = None,
    replace_pending: bool = False,
    files: list[str] | None = None,
    deleted: list[str] | None = None,
    auto_diff: bool = False,
    apply_live_mirror: bool = True,
    use_remote: bool = True,
    background: bool = False,
    url_arg: str = "",
    pair_secret_arg: str = "",
    admin_key_arg: str = "",
) -> dict[str, Any]:
    prev = read_local_tier(local_dir)
    now = time.time()
    if prev and prev.get("status") == "active" and (not session_id or prev.get("sessionId") == session_id):
        cp = dict(prev)
        cp["seq"] = int(prev.get("seq") or 0) + 1
    else:
        if not (local_dir / "baseline_manifest.json").is_file():
            save_local_baseline(local_dir, workspace)
        cp = {
            "schema": SCHEMA_VERSION,
            "sessionId": session_id or f"sess_{int(now)}_{secrets.token_hex(3)}",
            "seq": 1,
            "status": "active",
            "wave": "",
            "phase": "",
            "goal": "",
            "summary": "",
            "nextStep": "",
            "commit": "",
            "requests": [],
            "findings": [],
            "decisions": [],
            "completed": [],
            "pending": [],
            "fileHashes": {},
            "deletedFiles": [],
        }

    cp["status"] = "active"
    cp["timestampUnix"] = round(now, 3)
    cp["updatedUtc"] = _utc_now()
    if wave:
        cp["wave"] = wave
    if phase:
        cp["phase"] = phase
    if goal:
        cp["goal"] = goal
    if summary:
        cp["summary"] = summary
    if next_step:
        cp["nextStep"] = next_step

    cp["requests"] = _dedupe_append(cp.get("requests") or [], requests or [])
    cp["findings"] = _dedupe_append(cp.get("findings") or [], findings or [])
    cp["decisions"] = _dedupe_append(cp.get("decisions") or [], decisions or [])
    cp["completed"] = _dedupe_append(cp.get("completed") or [], completed or [])
    if replace_pending:
        cp["pending"] = _dedupe_append([], pending or [])
    else:
        cp["pending"] = _dedupe_append(cp.get("pending") or [], pending or [])

    changed_paths: list[str] = list(files or [])
    deleted_paths: list[str] = list(deleted or [])

    if auto_diff:
        baseline = load_local_baseline(local_dir)
        if not baseline:
            save_local_baseline(local_dir, workspace)
            baseline = load_local_baseline(local_dir)
        current = proto.build_workspace_manifest(workspace)
        diff_changed, diff_deleted = compute_manifest_diff(baseline, current)
        changed_paths = sorted(set(changed_paths + diff_changed))
        deleted_paths = sorted(set(deleted_paths + diff_deleted))

    bundle: dict[str, Any] | None = None
    if changed_paths or deleted_paths:
        bundle = proto.pack_delta_bundle(workspace, changed_paths, deleted_paths=deleted_paths)
        cp["fileHashes"] = {entry["path"]: entry["sha256"] for entry in bundle.get("files", [])}
        cp["deletedFiles"] = list(bundle.get("deleted", []))
    else:
        # Preserve previous bundle/hashes when saving a diary-only milestone
        prev_bundle = read_local_bundle(local_dir)
        if prev_bundle and cp.get("fileHashes"):
            bundle = prev_bundle

    tiers = _persist_to_all_tiers(
        local_dir,
        cp,
        bundle,
        use_remote=use_remote,
        background=background,
        apply_live_mirror=bool(apply_live_mirror and bundle),
        url_arg=url_arg,
        pair_secret_arg=pair_secret_arg,
        admin_key_arg=admin_key_arg,
    )
    return {"ok": True, "checkpoint": cp, "tiers": tiers}


def recover_session(
    *,
    workspace: Path,
    local_dir: Path,
    use_remote: bool = True,
    url_arg: str = "",
    pair_secret_arg: str = "",
    admin_key_arg: str = "",
) -> dict[str, Any]:
    probe = _collect_tier_checkpoints(
        local_dir,
        use_remote=use_remote,
        url_arg=url_arg,
        pair_secret_arg=pair_secret_arg,
        admin_key_arg=admin_key_arg,
    )
    candidates = probe["candidates"]
    tier, cp = pick_freshest_checkpoint(candidates)
    tier_summary = {
        t: {
            "sessionId": c.get("sessionId"),
            "seq": c.get("seq"),
            "status": c.get("status"),
            "updatedUtc": c.get("updatedUtc"),
            "hasBundle": bool(c.get("hasBundle")),
        }
        for t, c in candidates.items()
    }
    if not cp or not tier:
        return {
            "ok": True,
            "found": False,
            "tiers": tier_summary,
            "errors": probe["errors"],
        }

    rollback = (
        detect_workspace_rollback(workspace, cp.get("fileHashes") or {}, cp.get("deletedFiles") or [])
        if cp.get("status") == "active"
        else {"rollbackDetected": False, "reverted": [], "missing": [], "reappeared": [], "intactCount": 0, "trackedCount": 0}
    )
    return {
        "ok": True,
        "found": True,
        "freshestTier": tier,
        "tiers": tier_summary,
        "errors": probe["errors"],
        "checkpoint": cp,
        "rollback": rollback,
        "diaryMarkdown": render_diary_markdown(cp),
        "restoreCommand": (
            f"python3 -B .agents/scripts/flight_recorder.py restore --source {tier}"
            if rollback.get("rollbackDetected") and cp.get("hasBundle")
            else ""
        ),
    }


def restore_session(
    *,
    workspace: Path,
    local_dir: Path,
    source: str = "auto",
    dry_run: bool = False,
    allow_delete: bool = False,
    use_remote: bool = True,
    url_arg: str = "",
    pair_secret_arg: str = "",
    admin_key_arg: str = "",
) -> dict[str, Any]:
    probe = _collect_tier_checkpoints(
        local_dir,
        use_remote=use_remote,
        url_arg=url_arg,
        pair_secret_arg=pair_secret_arg,
        admin_key_arg=admin_key_arg,
    )
    candidates = probe["candidates"]
    if source == "auto":
        tier, cp = pick_freshest_checkpoint(candidates)
    else:
        tier = source if source in candidates else None
        cp = candidates.get(source)
    if not cp or not tier:
        return {"ok": False, "error": f"No checkpoint found for source='{source}'.", "errors": probe["errors"]}

    expected_sha = str(cp.get("bundleSha256") or "")
    bundle: dict[str, Any] | None = None
    bundle_source = ""
    order = [tier] + [t for t in ("local", "cloud", "termux") if t != tier]
    auth = probe["auth"]

    for cand_tier in order:
        if cand_tier == "local":
            b = read_local_bundle(local_dir)
            if b:
                bundle = b
                bundle_source = "local"
                break
        elif cand_tier == "cloud" and probe.get("cloudInlineB64"):
            try:
                bundle = decode_bundle_gz_b64(probe["cloudInlineB64"], expected_sha256=expected_sha)
                bundle_source = "cloud"
                break
            except Exception:
                pass
        elif cand_tier == "termux" and auth:
            relay, secret, admin = auth
            b = read_termux_bundle(relay, secret, expected_sha256=expected_sha, admin_key=admin)
            if b:
                bundle = b
                bundle_source = "termux"
                break

    if not bundle:
        return {
            "ok": False,
            "error": "Checkpoint found, but no file snapshot bundle is attached or reachable.",
            "checkpoint": cp,
        }

    file_list = [f["path"] for f in bundle.get("files", [])]
    del_list = list(bundle.get("deleted", []))
    if dry_run:
        return {
            "ok": True,
            "dryRun": True,
            "checkpointTier": tier,
            "bundleSource": bundle_source,
            "wouldWrite": file_list,
            "wouldDelete": del_list if allow_delete else [],
        }

    applied = proto.apply_delta_bundle(workspace, bundle, allow_delete=allow_delete)
    # Refresh local copy too so subsequent local commands see it immediately
    write_local_tier(local_dir, cp, render_diary_markdown(cp), encode_bundle_gz_b64(bundle)[0])
    return {
        "ok": True,
        "dryRun": False,
        "checkpointTier": tier,
        "bundleSource": bundle_source,
        "applied": applied,
    }


def close_session(
    *,
    workspace: Path | None = None,
    local_dir: Path,
    commit: str = "",
    note: str = "",
    next_step: str = "",
    clear_pending: bool = True,
    use_remote: bool = True,
    background: bool = False,
    url_arg: str = "",
    pair_secret_arg: str = "",
    admin_key_arg: str = "",
) -> dict[str, Any]:
    prev = read_local_tier(local_dir)
    now = time.time()
    cp = dict(prev) if prev else {
        "schema": SCHEMA_VERSION,
        "sessionId": f"sess_{int(now)}",
        "seq": 0,
        "requests": [],
        "findings": [],
        "decisions": [],
        "completed": [],
        "pending": [],
    }
    cp["seq"] = int(cp.get("seq") or 0) + 1
    cp["status"] = "closed"
    cp["timestampUnix"] = round(now, 3)
    cp["updatedUtc"] = _utc_now()
    if commit:
        cp["commit"] = commit
    if note:
        cp["summary"] = note
    if next_step:
        cp["nextStep"] = next_step
    if clear_pending:
        cp["pending"] = []
    cp["fileHashes"] = {}
    cp["deletedFiles"] = []

    if workspace is not None and workspace.is_dir():
        save_local_baseline(local_dir, workspace)

    tiers = _persist_to_all_tiers(
        local_dir,
        cp,
        None,
        use_remote=use_remote,
        background=background,
        clear_mirror_state=True,
        url_arg=url_arg,
        pair_secret_arg=pair_secret_arg,
        admin_key_arg=admin_key_arg,
    )
    return {"ok": True, "checkpoint": cp, "tiers": tiers}


# ==================== CLI ====================


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Cavern Anti-Cancellation 3-Tier Flight Recorder (/tmp + Phone Termux + Cloudflare Hub v6)."
    )
    p.add_argument("--workspace", default=str(REPO_ROOT))
    p.add_argument("--local-dir", default=str(DEFAULT_LOCAL_DIR))
    p.add_argument("--url", default="")
    p.add_argument("--pair-secret", default="")
    p.add_argument("--admin-key", default="")
    p.add_argument("--no-remote", action="store_true", help="Operate on Tier 1 local cache only (offline/tests)")

    sub = p.add_subparsers(dest="action", required=True)

    p_init = sub.add_parser("init", help="Capture baseline manifest and open a new Flight Recorder session")
    p_init.add_argument("--session", default="", help="Session identifier (auto-generated if omitted)")
    p_init.add_argument("--wave", default="")
    p_init.add_argument("--goal", default="")
    p_init.add_argument("--request", action="append", default=[])
    p_init.add_argument("-b", "--background", action="store_true", help="Write Tier 1 local (<10ms) and flush Termux+Cloud in background")

    p_save = sub.add_parser("save", help="Save incremental diary checkpoint + optional workspace snapshot to all 3 tiers")
    p_save.add_argument("--session", default="")
    p_save.add_argument("--wave", default="")
    p_save.add_argument("--phase", default="")
    p_save.add_argument("--goal", default="")
    p_save.add_argument("--summary", default="")
    p_save.add_argument("--next", default="")
    p_save.add_argument("--request", action="append", default=[])
    p_save.add_argument("--finding", action="append", default=[])
    p_save.add_argument("--decision", action="append", default=[])
    p_save.add_argument("--done", action="append", default=[])
    p_save.add_argument("--pending", action="append", default=[])
    p_save.add_argument("--replace-pending", action="store_true")
    p_save.add_argument("--files", nargs="*", default=[])
    p_save.add_argument("--delete", nargs="*", default=[])
    p_save.add_argument("--auto-diff", action="store_true", help="Auto-detect all modified/added/deleted workspace files vs baseline")
    p_save.add_argument("--no-mirror", action="store_true", help="Do not live-mirror changed files into local Antigravity repo")
    p_save.add_argument("-b", "--background", action="store_true", help="Write Tier 1 local (<10ms) and flush Termux+Cloud in background")

    p_rec = sub.add_parser("recover", help="Probe all 3 tiers, detect workspace rollback, and print recovery brief")
    p_rec.add_argument("--json", action="store_true", help="Output machine-readable JSON instead of compact brief")

    p_res = sub.add_parser("restore", help="Restore rolled-back workspace edits from verified omni-delta-v1 snapshot")
    p_res.add_argument("--source", choices=("auto", "local", "cloud", "termux"), default="auto")
    p_res.add_argument("--dry-run", action="store_true")
    p_res.add_argument("--allow-delete", action="store_true")

    sub.add_parser("status", help="Show checkpoint sync status across Local (/tmp), Phone Termux, and Cloudflare Hub v6")

    p_cls = sub.add_parser("close", help="Mark active session cleanly closed after commit/push")
    p_cls.add_argument("--commit", default="")
    p_cls.add_argument("--note", default="")
    p_cls.add_argument("--next", default="")
    p_cls.add_argument("--keep-pending", action="store_true")
    p_cls.add_argument("-b", "--background", action="store_true", help="Write Tier 1 local (<10ms) and flush Termux+Cloud in background")

    p_fl = sub.add_parser("flush-remote", help="Internal/manual command: flush Tier 1 checkpoint to Termux + Cloudflare")
    p_fl.add_argument("--mirror", action="store_true")
    p_fl.add_argument("--clear-mirror-state", action="store_true")

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    workspace = Path(args.workspace).expanduser().resolve()
    local_dir = Path(args.local_dir).expanduser().resolve()
    use_remote = not args.no_remote

    if args.action == "init":
        res = init_session(
            workspace=workspace,
            local_dir=local_dir,
            session_id=args.session,
            wave=args.wave,
            goal=args.goal,
            requests=args.request,
            use_remote=use_remote,
            background=bool(args.background),
            url_arg=args.url,
            pair_secret_arg=args.pair_secret,
            admin_key_arg=args.admin_key,
        )
        print(json.dumps(res, indent=2, ensure_ascii=False))
        return 0 if res.get("ok") else 1

    if args.action == "save":
        res = save_checkpoint(
            workspace=workspace,
            local_dir=local_dir,
            session_id=args.session,
            wave=args.wave,
            phase=args.phase,
            goal=args.goal,
            summary=args.summary,
            next_step=args.next,
            requests=args.request,
            findings=args.finding,
            decisions=args.decision,
            completed=args.done,
            pending=args.pending,
            replace_pending=args.replace_pending,
            files=args.files,
            deleted=args.delete,
            auto_diff=args.auto_diff,
            apply_live_mirror=not bool(args.no_mirror),
            use_remote=use_remote,
            background=bool(args.background),
            url_arg=args.url,
            pair_secret_arg=args.pair_secret,
            admin_key_arg=args.admin_key,
        )
        print(json.dumps(res, indent=2, ensure_ascii=False))
        return 0 if res.get("ok") else 1

    if args.action == "recover":
        res = recover_session(
            workspace=workspace,
            local_dir=local_dir,
            use_remote=use_remote,
            url_arg=args.url,
            pair_secret_arg=args.pair_secret,
            admin_key_arg=args.admin_key,
        )
        if args.json:
            print(json.dumps(res, indent=2, ensure_ascii=False))
        else:
            if not res.get("found"):
                print("[FLIGHT_RECORDER] No active or prior checkpoint found across reachable tiers.")
                if res.get("errors"):
                    print(f"[FLIGHT_RECORDER] Tier notes: {json.dumps(res['errors'])}")
            else:
                cp = res["checkpoint"]
                rb = res["rollback"]
                print(f"[FLIGHT_RECORDER] Freshest tier={res['freshestTier']} | session={cp.get('sessionId')} | seq=#{cp.get('seq')} | status={cp.get('status')} | tiers={list(res['tiers'].keys())}")
                if rb.get("rollbackDetected"):
                    print(
                        f"[ROLLBACK_DETECTED] reverted={len(rb['reverted'])} missing={len(rb['missing'])} reappeared={len(rb['reappeared'])}"
                    )
                    for r in rb["reverted"][:10]:
                        print(f"  - REVERTED: {r['path']} (expected {r['expected']}, disk {r['actual']})")
                    for m in rb["missing"][:10]:
                        print(f"  - MISSING:  {m}")
                    if res.get("restoreCommand"):
                        print(f"[RESTORE_CMD] {res['restoreCommand']}")
                else:
                    print(f"[WORKSPACE_SYNC] OK (intact={rb.get('intactCount', 0)}/{rb.get('trackedCount', 0)})")
                print("")
                print(res["diaryMarkdown"].rstrip())
        return 0

    if args.action == "restore":
        res = restore_session(
            workspace=workspace,
            local_dir=local_dir,
            source=args.source,
            dry_run=args.dry_run,
            allow_delete=args.allow_delete,
            use_remote=use_remote,
            url_arg=args.url,
            pair_secret_arg=args.pair_secret,
            admin_key_arg=args.admin_key,
        )
        print(json.dumps(res, indent=2, ensure_ascii=False))
        return 0 if res.get("ok") else 1

    if args.action == "status":
        res = recover_session(
            workspace=workspace,
            local_dir=local_dir,
            use_remote=use_remote,
            url_arg=args.url,
            pair_secret_arg=args.pair_secret,
            admin_key_arg=args.admin_key,
        )
        compact = {
            "ok": res.get("ok"),
            "found": res.get("found"),
            "freshestTier": res.get("freshestTier"),
            "tiers": res.get("tiers"),
            "rollback": res.get("rollback"),
            "errors": res.get("errors"),
        }
        print(json.dumps(compact, indent=2, ensure_ascii=False))
        return 0

    if args.action == "close":
        res = close_session(
            workspace=workspace,
            local_dir=local_dir,
            commit=args.commit,
            note=args.note,
            next_step=args.next,
            clear_pending=not bool(args.keep_pending),
            use_remote=use_remote,
            background=bool(args.background),
            url_arg=args.url,
            pair_secret_arg=args.pair_secret,
            admin_key_arg=args.admin_key,
        )
        print(json.dumps(res, indent=2, ensure_ascii=False))
        return 0 if res.get("ok") else 1

    if args.action == "flush-remote":
        res = flush_local_to_remote(
            local_dir=local_dir,
            apply_live_mirror=bool(args.mirror),
            clear_mirror_state=bool(args.clear_mirror_state),
            url_arg=args.url,
            pair_secret_arg=args.pair_secret,
            admin_key_arg=args.admin_key,
        )
        print(json.dumps(res, indent=2, ensure_ascii=False))
        return 0 if res.get("ok") else 1

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
