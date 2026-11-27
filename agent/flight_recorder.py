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

SCHEMA_VERSION = 2
DEFAULT_LOCAL_DIR = Path(os.environ.get("CAVERN_FLIGHT_RECORDER_DIR", "/tmp/cavern-flight-recorder"))
PHONE_AG_DIR = "/root/.gemini/antigravity"
PHONE_FR_DIR = f"{PHONE_AG_DIR}/ai_studio_bridge/flight_recorder"
PHONE_REPO_DIR = f"{PHONE_AG_DIR}/scratch/Cavern"
CLOUD_TASK_ID = "fr_latest"
CLOUD_ENVELOPE_CTX = "fr:latest"
CLOUD_SEMANTIC_TASK_ID = "fr_semantic"
CLOUD_SEMANTIC_CTX = "fr:semantic"
CLOUD_INLINE_BUNDLE_MAX_BYTES = 140 * 1024  # comfortably under Worker's 256 KB /v1/swarm/enqueue cap
TERMUX_INLINE_PAYLOAD_MAX_BYTES = 96 * 1024  # safe under shell ARG_MAX & Worker cmd body cap
MAX_LOCAL_HISTORY = 25
SEMANTIC_SCALAR_FIELDS = ("wave", "phase", "goal", "summary", "nextStep")
SEMANTIC_LIST_FIELDS = ("requests", "findings", "decisions", "completed", "pending")


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def is_auto_mirror_summary(text: Any) -> bool:
    s = str(text or "").strip()
    return not s or s.startswith("Auto live-mirror") or s in ("(live sync)", "Session initialized")


def extract_semantic_record(cp: dict[str, Any]) -> dict[str, Any]:
    """Extract lightweight semantic-only lane record (zero file blobs, <4 KB)."""
    return {
        "schema": SCHEMA_VERSION,
        "sessionId": str(cp.get("sessionId") or ""),
        "seq": int(cp.get("seq") or 0),
        "status": str(cp.get("status") or "active"),
        "source": str(cp.get("source") or "manual"),
        "timestampUnix": float(cp.get("timestampUnix") or 0.0),
        "updatedUtc": str(cp.get("updatedUtc") or ""),
        "wave": str(cp.get("wave") or ""),
        "phase": str(cp.get("phase") or ""),
        "goal": str(cp.get("goal") or ""),
        "summary": str(cp.get("summary") or ""),
        "nextStep": str(cp.get("nextStep") or ""),
        "commit": str(cp.get("commit") or ""),
        "requests": list(cp.get("requests") or []),
        "findings": list(cp.get("findings") or []),
        "decisions": list(cp.get("decisions") or []),
        "completed": list(cp.get("completed") or []),
        "pending": list(cp.get("pending") or []),
    }


def _has_semantic_content(rec: dict[str, Any] | None) -> bool:
    if not isinstance(rec, dict):
        return False
    if any(str(rec.get(k) or "").strip() for k in ("wave", "goal", "nextStep")):
        return True
    if not is_auto_mirror_summary(rec.get("summary")):
        return True
    return any(bool(rec.get(k)) for k in SEMANTIC_LIST_FIELDS)


def merge_semantic_into_checkpoint(
    cp: dict[str, Any],
    sem: dict[str, Any] | None,
) -> dict[str, Any]:
    """Merge durable semantic-lane fields into a checkpoint without letting auto_mirror clobber intent."""
    out = dict(cp)
    if not isinstance(sem, dict) or not _has_semantic_content(sem):
        return out

    cp_is_mirror = str(out.get("source") or "") == "auto_mirror" or is_auto_mirror_summary(out.get("summary"))
    cp_closed = str(out.get("status") or "") == "closed"
    sem_ts = float(sem.get("timestampUnix") or 0.0)
    cp_ts = float(out.get("timestampUnix") or 0.0)

    # Never reopen a cleanly closed session from an older semantic snapshot
    if cp_closed and sem_ts <= cp_ts:
        for k in ("wave", "goal", "nextStep"):
            if not str(out.get(k) or "").strip() and str(sem.get(k) or "").strip():
                out[k] = sem[k]
        return out

    for k in ("wave", "goal", "nextStep"):
        if (not str(out.get(k) or "").strip() or cp_is_mirror) and str(sem.get(k) or "").strip():
            out[k] = sem[k]

    if (not str(out.get("phase") or "").strip() or out.get("phase") == "live-auto-mirror") and str(sem.get("phase") or "").strip():
        out["phase"] = sem["phase"]

    if (is_auto_mirror_summary(out.get("summary")) or cp_is_mirror) and not is_auto_mirror_summary(sem.get("summary")):
        out["summary"] = sem["summary"]

    for k in SEMANTIC_LIST_FIELDS:
        if not out.get(k) and sem.get(k):
            out[k] = list(sem[k])
        elif cp_is_mirror and sem.get(k):
            out[k] = _dedupe_append(list(sem[k]), list(out.get(k) or []))

    return out


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


def write_local_semantic(local_dir: Path, sem: dict[str, Any]) -> None:
    _ensure_local_dir(local_dir)
    p = local_dir / "semantic.json"
    tmp = p.with_name(f"{p.name}.tmp.{os.getpid()}")
    tmp.write_text(json.dumps(sem, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, p)


def read_local_semantic(local_dir: Path) -> dict[str, Any] | None:
    p = local_dir / "semantic.json"
    if not p.is_file():
        return None
    return _safe_json_loads(p.read_text(encoding="utf-8"))


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
    task_id: str = CLOUD_TASK_ID,
    env_ctx: str = CLOUD_ENVELOPE_CTX,
    timeout: int = 12,
) -> dict[str, Any]:
    """Store E2E-encrypted checkpoint in Cloudflare Hub v6 SQLite DO via /v1/swarm/enqueue."""
    headers = proto.derive_agent_headers(pair_secret, channel="termux", admin_key=admin_key)
    payload: dict[str, Any] = {"checkpoint": cp}
    inline_included = False
    if bundle_gz_b64 and len(bundle_gz_b64) <= CLOUD_INLINE_BUNDLE_MAX_BYTES:
        payload["bundleGzB64"] = bundle_gz_b64
        inline_included = True

    env = proto.encrypt_envelope(pair_secret, env_ctx, payload)
    # Double-check body size stays comfortably below Worker's 256 KB cap
    body = {
        "taskId": task_id,
        "role": "flight_recorder",
        "title": f"FR:{cp.get('sessionId', 'session')}#{cp.get('seq', 0)}:{cp.get('wave', '')}"[:120],
        "createdBy": "aistudio-flight-recorder",
        "instructions": f"E2E-encrypted Flight Recorder checkpoint (decrypt with pairSecret ctx {env_ctx})",
        "envelope": env,
    }
    if len(json.dumps(body, separators=(",", ":"))) > 235 * 1024 and inline_included:
        payload.pop("bundleGzB64", None)
        inline_included = False
        body["envelope"] = proto.encrypt_envelope(pair_secret, env_ctx, payload)

    st, resp = proto.http_json("POST", f"{relay_url}/v1/swarm/enqueue", headers, body, timeout=timeout)
    ok = st < 400 and bool(resp.get("ok"))
    return {
        "ok": ok,
        "status": st,
        "taskId": task_id,
        "inlineBundle": inline_included,
        "error": "" if ok else str(resp.get("error") or resp),
    }


def read_cloud_tier(
    relay_url: str,
    pair_secret: str,
    *,
    admin_key: str = "",
    task_id: str = CLOUD_TASK_ID,
    env_ctx: str = CLOUD_ENVELOPE_CTX,
    timeout: int = 6,
) -> tuple[dict[str, Any] | None, str | None, str]:
    """Return (checkpoint_dict, inline_bundle_gz_b64, error_string)."""
    headers = proto.derive_agent_headers(pair_secret, channel="termux", admin_key=admin_key)
    st, resp = proto.http_json(
        "GET",
        f"{relay_url}/v1/swarm/task?taskId={urllib.parse.quote(task_id)}",
        headers,
        timeout=timeout,
    )
    if st == 404 or not resp.get("ok"):
        return None, None, "" if st == 404 else str(resp.get("error") or f"HTTP {st}")
    task = resp.get("task") or {}
    env = task.get("envelope")
    if not isinstance(env, dict):
        return None, None, "Cloud task has no E2E envelope"
    try:
        dec = proto.decrypt_envelope(pair_secret, env_ctx, env)
    except Exception as exc:
        return None, None, f"Decrypt failed: {exc}"
    cp = dec.get("checkpoint") if isinstance(dec, dict) else None
    b64 = dec.get("bundleGzB64") if isinstance(dec, dict) else None
    return (cp if isinstance(cp, dict) else None), (b64 if isinstance(b64, str) else None), ""


# ==================== TIER 2: PHONE TERMUX PERMANENT STORAGE ====================


def _check_termux_online_fast(
    relay_url: str,
    pair_secret: str,
    *,
    admin_key: str = "",
    timeout: int = 4,
) -> bool:
    """Fast ~180ms Cloudflare DO check so read-only probes never stall waiting on a sleeping Termux daemon."""
    try:
        headers = proto.derive_agent_headers(pair_secret, channel="termux", admin_key=admin_key)
        st, resp = proto.http_json("GET", f"{relay_url}/v1/agent/status?all=1", headers, timeout=timeout)
        if st == 200 and resp.get("ok"):
            chans = resp.get("channels") or {}
            return bool((chans.get("termux") or {}).get("online"))
    except Exception:
        pass
    return False


def _send_termux_cmd(
    relay_url: str,
    pair_secret: str,
    cmd: str,
    args: dict[str, Any],
    *,
    admin_key: str = "",
    timeout_sec: int = 25,
    auto_wake: bool = True,
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
        auto_wake=auto_wake,
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
    sem_path = fr_dir / "semantic.json"
    is_auto = str(cp.get("source") or "") == "auto_mirror" or str(cp.get("summary") or "").startswith("Auto live-mirror")

    if isinstance(payload.get("semanticCheckpoint"), dict):
        atomic_write_text(sem_path, json.dumps(payload["semanticCheckpoint"], indent=2, ensure_ascii=False) + "\n")

    # Preserve durable semantic lane when an auto_mirror tick writes latest.json / LATEST_HANDOFF.md
    effective_cp = dict(cp)
    if is_auto and sem_path.is_file():
        try:
            sem = json.loads(sem_path.read_text(encoding="utf-8"))
            if isinstance(sem, dict):
                sem_closed = str(sem.get("status") or "") == "closed"
                if not sem_closed:
                    for k in ("wave", "goal", "nextStep"):
                        if str(sem.get(k) or "").strip() and not str(effective_cp.get(k) or "").strip():
                            effective_cp[k] = sem[k]
                    if str(sem.get("phase") or "").strip() and effective_cp.get("phase") in ("", "live-auto-mirror"):
                        effective_cp["phase"] = sem["phase"]
                    s_sum = str(sem.get("summary") or "").strip()
                    if s_sum and not s_sum.startswith("Auto live-mirror") and (
                        not str(effective_cp.get("summary") or "").strip()
                        or str(effective_cp.get("summary") or "").startswith("Auto live-mirror")
                    ):
                        effective_cp["summary"] = s_sum
                    for lk in ("requests", "findings", "decisions", "completed", "pending"):
                        if sem.get(lk) and not effective_cp.get(lk):
                            effective_cp[lk] = list(sem[lk])
        except Exception:
            pass

    cp_text = json.dumps(effective_cp, indent=2, ensure_ascii=False) + "\n"
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
        sid_raw = str(effective_cp.get("sessionId") or "aistudio_live")
        safe_id = "".join(c for c in sid_raw if c.isalnum() or c in ("_", "-"))[:64] or "session"
        title = f"{effective_cp.get('wave') or 'AI Studio'} — {effective_cp.get('phase') or effective_cp.get('status') or 'live'}"
        files_list = sorted((effective_cp.get("fileHashes") or {}).keys())[:64]
        lines = [
            f"# {title}",
            f"- **UTC**: `{effective_cp.get('updatedUtc', '')}` | **Session**: `{safe_id}` (seq `#{seq}`) | **Status**: `{effective_cp.get('status', 'active')}` | **Commit**: `{effective_cp.get('commit') or 'live-mirror'}`",
            f"- **Repo**: `{repo_dir}`",
            "",
            "## Compact Summary (Token-Diet)",
            str(effective_cp.get("summary") or effective_cp.get("goal") or "(live sync)"),
            "",
        ]
        if effective_cp.get("goal") and effective_cp.get("goal") != effective_cp.get("summary"):
            lines.append(f"- **Goal**: {effective_cp['goal']}")
        if effective_cp.get("pending"):
            lines.append("## Pending")
            lines.extend(f"- {p}" for p in list(effective_cp["pending"])[:12])
            lines.append("")
        if files_list:
            lines.append("## Live Mirrored / Tracked Files")
            lines.extend(f"- `{f}`" for f in files_list)
            lines.append("")
        if effective_cp.get("nextStep"):
            lines.append("## Next")
            lines.append(str(effective_cp["nextStep"]))
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
            "utc": effective_cp.get("updatedUtc", ""),
            "repo": str(repo_dir),
            "title": title[:160],
            "commit": str(effective_cp.get("commit") or "")[:64],
            "files": files_list,
            "summary": str(effective_cp.get("summary") or effective_cp.get("goal") or "")[:4000],
            "next": str(effective_cp.get("nextStep") or "")[:1000],
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


def build_termux_reconcile_pull_script(
    remote: str = "origin",
    branch: str = "master",
    state_path: str = "",
) -> str:
    """Return a single-RPC content-addressed reconcile + FF-only pull script.

    1. Fetches `<remote> <branch>` FIRST.
    2. Builds `<remote>/<branch>` git blob SHA-1 map (`git ls-tree -r`).
    3. Reconciles tracked/untracked dirty files that match `live_mirror_state.json`
       OR are byte-for-byte identical to `<remote>/<branch>` (already pushed!).
    4. Preserves any genuine manual local phone edits via `git stash push -u` + `git stash pop`.
    5. Pulls via `git pull --ff-only` (falling back to `--rebase` ONLY if local commits exist ahead).
    """
    safe_remote = json.dumps(remote or "origin")
    safe_branch = json.dumps(branch or "master")
    safe_state = json.dumps(state_path or f"{PHONE_FR_DIR}/live_mirror_state.json")
    return f"""
import hashlib, json, os, subprocess
from pathlib import Path

repo = Path.cwd().resolve()
remote = {safe_remote}
branch = {safe_branch}
state_path = Path({safe_state}).expanduser()
reconciled = []
preserved_manual = []

def git_blob_sha1(raw: bytes) -> str:
    return hashlib.sha1(f"blob {{len(raw)}}\\x00".encode("ascii") + raw).hexdigest()

r_fetch = subprocess.run(["git", "fetch", remote, branch], cwd=str(repo), capture_output=True, text=True, timeout=60, check=False)

remote_ref = f"{{remote}}/{{branch}}"
remote_blobs = {{}}
r_tree = subprocess.run(["git", "ls-tree", "-r", "-z", remote_ref], cwd=str(repo), capture_output=True, timeout=30, check=False)
if r_tree.returncode == 0:
    for entry in r_tree.stdout.split(b"\\x00"):
        if not entry or b"\\t" not in entry:
            continue
        meta_b, path_b = entry.split(b"\\t", 1)
        parts = meta_b.decode("utf-8", errors="ignore").split()
        if len(parts) >= 3 and parts[1] == "blob":
            remote_blobs[path_b.decode("utf-8", errors="replace").replace("\\\\", "/")] = parts[2]

mirrored = {{}}
if state_path.is_file():
    try:
        loaded = json.loads(state_path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict) and isinstance(loaded.get("mirrored"), dict):
            mirrored = loaded["mirrored"]
    except Exception:
        mirrored = {{}}

remaining_mirrored = dict(mirrored)
to_checkout = []
stash_targets = []
ignore_prefixes = (".tmp_", "__pycache__/", ".agents/.bridge_vault.json", ".agents/.metrics.jsonl", ".agents/.model_modes.json", "public/assets/aistudio/")

r_st = subprocess.run(["git", "status", "--porcelain", "-z", "-uall"], cwd=str(repo), capture_output=True, timeout=20, check=False)
entries = []
if r_st.returncode == 0:
    parts = r_st.stdout.split(b"\\x00")
    i = 0
    while i < len(parts):
        ent = parts[i]
        if len(ent) >= 4:
            code = ent[:2].decode("utf-8", errors="ignore")
            rel = ent[3:].decode("utf-8", errors="replace").replace("\\\\", "/").lstrip("/")
            if rel and ".." not in Path(rel).parts:
                entries.append((code, rel))
            if "R" in code or "C" in code:
                i += 1
        i += 1

for code, norm in entries:
    if any(norm.startswith(pfx) or f"/{{pfx}}" in norm for pfx in ignore_prefixes):
        continue
    full = (repo / norm).resolve()
    try:
        full.relative_to(repo)
    except ValueError:
        continue
    rec = mirrored.get(norm)
    expected_sha256 = str((rec or {{}}).get("sha256") or "")
    is_untracked = code == "??"

    if not full.is_file():
        # Deleted locally: reconcile if also absent on remote or tracked as mirrored
        if (norm not in remote_blobs) or (rec is not None):
            if not is_untracked:
                to_checkout.append(norm)
            reconciled.append(norm)
            remaining_mirrored.pop(norm, None)
        else:
            preserved_manual.append(norm)
            stash_targets.append(norm)
        continue

    raw = full.read_bytes()
    actual_sha256 = hashlib.sha256(raw).hexdigest()
    actual_blob = git_blob_sha1(raw)
    matches_mirror = bool(expected_sha256 and actual_sha256 == expected_sha256)
    matches_remote = bool(norm in remote_blobs and remote_blobs[norm] == actual_blob)

    if is_untracked:
        if matches_mirror or matches_remote:
            try:
                full.unlink()
            except OSError:
                pass
            reconciled.append(norm)
            remaining_mirrored.pop(norm, None)
        elif norm in remote_blobs:
            # Untracked local file colliding with a new remote file -> preserve via stash
            preserved_manual.append(norm)
            stash_targets.append(norm)
        # Non-colliding untracked scratch files do not block git pull --ff-only
    else:
        if matches_mirror or matches_remote:
            to_checkout.append(norm)
            reconciled.append(norm)
            remaining_mirrored.pop(norm, None)
        else:
            preserved_manual.append(norm)
            stash_targets.append(norm)

if to_checkout:
    subprocess.run(["git", "checkout", "--", *to_checkout], cwd=str(repo), capture_output=True, timeout=30, check=False)

if state_path.is_file() or remaining_mirrored != mirrored:
    try:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = state_path.with_name(state_path.name + f".tmp.{{os.getpid()}}")
        tmp.write_text(json.dumps({{"mirrored": remaining_mirrored}}, indent=2) + "\\n", encoding="utf-8")
        os.replace(tmp, state_path)
    except OSError:
        pass

stashed = False
stash_err = ""
if stash_targets:
    r_stash = subprocess.run(
        ["git", "stash", "push", "-u", "-m", "omni-auto-preserve", "--", *stash_targets],
        cwd=str(repo),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if r_stash.returncode == 0 and "No local changes" not in (r_stash.stdout or ""):
        stashed = True
    elif r_stash.returncode != 0:
        stash_err = (r_stash.stderr or r_stash.stdout or "").strip()[:300]

ahead = 0
r_cnt = subprocess.run(["git", "rev-list", "--count", f"{{remote_ref}}..HEAD"], cwd=str(repo), capture_output=True, text=True, timeout=15, check=False)
if r_cnt.returncode == 0:
    try:
        ahead = int((r_cnt.stdout or "0").strip())
    except ValueError:
        ahead = 0

pull_cmd = ["git", "pull", "--rebase", remote, branch] if ahead > 0 else ["git", "pull", "--ff-only", remote, branch]
r_pull = subprocess.run(pull_cmd, cwd=str(repo), capture_output=True, text=True, timeout=90, check=False)

stash_pop_err = ""
if stashed:
    r_pop = subprocess.run(["git", "stash", "pop"], cwd=str(repo), capture_output=True, text=True, timeout=30, check=False)
    if r_pop.returncode != 0:
        stash_pop_err = (r_pop.stderr or r_pop.stdout or "").strip()[:300]

r_head = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=str(repo), capture_output=True, text=True, timeout=15, check=False)
print(json.dumps({{
    "ok": r_pull.returncode == 0,
    "head": r_head.stdout.strip(),
    "mode": "rebase" if ahead > 0 else "ff-only",
    "reconciled": reconciled,
    "preservedManual": preserved_manual,
    "stashed": stashed,
    "stashPopError": stash_pop_err or stash_err,
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
    semantic_cp: dict[str, Any] | None = None,
    apply_live_mirror: bool = False,
    clear_mirror_state: bool = False,
    sync_antigravity_handoff: bool = True,
    admin_key: str = "",
) -> dict[str, Any]:
    """Persist checkpoint + diary + bundle + Antigravity handoff (+ optional live repo mirror) in ONE atomic Termux RPC."""
    try:
        if not _check_termux_online_fast(relay_url, pair_secret, admin_key=admin_key):
            return {"ok": False, "skippedOffline": True, "error": "Termux channel is offline (fast check)"}
        inline_payload: dict[str, Any] = {
            "frDir": PHONE_FR_DIR,
            "agDir": PHONE_AG_DIR,
            "repoDir": PHONE_REPO_DIR,
            "checkpoint": cp,
            "semanticCheckpoint": semantic_cp,
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
            "semanticCheckpoint": semantic_cp,
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
    timeout_sec: int = 6,
    auto_wake: bool = False,
) -> tuple[dict[str, Any] | None, str]:
    try:
        res = _send_termux_cmd(
            relay_url,
            pair_secret,
            "file_read",
            {"path": f"{PHONE_FR_DIR}/latest.json", "maxBytes": 256 * 1024},
            admin_key=admin_key,
            timeout_sec=timeout_sec,
            auto_wake=auto_wake,
        )
        if not res.get("ok"):
            return None, str(res.get("error") or "not found")
        cp = _safe_json_loads(str(res.get("content") or ""))
        return cp, "" if cp else "invalid json"
    except Exception as exc:
        return None, str(exc)


def _build_termux_probe_script(include_dirty: bool = True, branch: str = "master") -> str:
    safe_fr = json.dumps(PHONE_FR_DIR)
    safe_repo = json.dumps(PHONE_REPO_DIR)
    safe_branch = json.dumps(branch or "master")
    return f"""
import hashlib, json, subprocess
from pathlib import Path

fr_dir = Path({safe_fr}).expanduser()
repo = Path({safe_repo}).expanduser()
out = {{"latest": None, "semantic": None, "dirty": None}}

for key, fname in (("latest", "latest.json"), ("semantic", "semantic.json")):
    p = fr_dir / fname
    if p.is_file():
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(d, dict):
                out[key] = d
        except Exception:
            pass

if {bool(include_dirty)!r} and (repo / ".git").exists():
    def git_blob_sha1(raw: bytes) -> str:
        return hashlib.sha1(f"blob {{len(raw)}}\\x00".encode("ascii") + raw).hexdigest()

    remote_ref = f"origin/{{{safe_branch}}}"
    r_head = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=str(repo), capture_output=True, text=True, timeout=5, check=False)
    r_beh = subprocess.run(["git", "rev-list", "--count", f"HEAD..{{remote_ref}}"], cwd=str(repo), capture_output=True, text=True, timeout=5, check=False)
    remote_blobs = {{}}
    r_tree = subprocess.run(["git", "ls-tree", "-r", "-z", remote_ref], cwd=str(repo), capture_output=True, timeout=8, check=False)
    if r_tree.returncode == 0:
        for ent in r_tree.stdout.split(b"\\x00"):
            if not ent or b"\\t" not in ent:
                continue
            m, pb = ent.split(b"\\t", 1)
            ps = m.decode("utf-8", errors="ignore").split()
            if len(ps) >= 3 and ps[1] == "blob":
                remote_blobs[pb.decode("utf-8", errors="replace").replace("\\\\", "/")] = ps[2]
    uncommitted = []
    ignore_pfx = (".tmp_", "__pycache__/", ".agents/.bridge_vault.json", ".agents/.metrics.jsonl", ".agents/.model_modes.json", "public/assets/aistudio/")
    r_st = subprocess.run(["git", "status", "--porcelain", "-z", "-uall"], cwd=str(repo), capture_output=True, timeout=8, check=False)
    if r_st.returncode == 0:
        parts = r_st.stdout.split(b"\\x00")
        i = 0
        while i < len(parts):
            ent = parts[i]
            if len(ent) >= 4:
                code = ent[:2].decode("utf-8", errors="ignore")
                rel = ent[3:].decode("utf-8", errors="replace").replace("\\\\", "/").lstrip("/")
                if "R" in code or "C" in code:
                    i += 1
                if rel and ".." not in Path(rel).parts and not any(rel.startswith(p) or f"/{{p}}" in rel for p in ignore_pfx):
                    full = (repo / rel).resolve()
                    if full.is_file():
                        try:
                            st = full.stat()
                            if st.st_size <= 2 * 1024 * 1024:
                                raw = full.read_bytes()
                                if remote_blobs.get(rel) != git_blob_sha1(raw):
                                    uncommitted.append({{
                                        "path": rel,
                                        "sha256": hashlib.sha256(raw).hexdigest(),
                                        "size": st.st_size,
                                        "mtimeUnix": round(st.st_mtime, 1),
                                    }})
                        except OSError:
                            pass
            i += 1
    out["dirty"] = {{
        "head": (r_head.stdout or "").strip(),
        "behindOrigin": int((r_beh.stdout or "0").strip() or 0) if (r_beh.stdout or "").strip().isdigit() else 0,
        "uncommittedVsOrigin": uncommitted[:40],
        "uncommittedCount": len(uncommitted),
    }}

print(json.dumps(out, separators=(",", ":")))
""".strip()


def probe_termux_snapshot_and_dirty(
    relay_url: str,
    pair_secret: str,
    *,
    admin_key: str = "",
    timeout_sec: int = 6,
    include_dirty: bool = True,
    branch: str = "master",
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, dict[str, Any] | None, str]:
    """Single fast Termux RPC returning (latest_cp, semantic_cp, dirty_info, err)."""
    try:
        script_b64 = base64.b64encode(_build_termux_probe_script(include_dirty=include_dirty, branch=branch).encode("utf-8")).decode("ascii")
        cmd = f"python3 -B -c \"import base64; exec(base64.b64decode('{script_b64}').decode('utf-8'))\""
        res = _send_termux_cmd(
            relay_url,
            pair_secret,
            "shell_exec",
            {"command": cmd, "timeoutSec": min(15, max(5, timeout_sec))},
            admin_key=admin_key,
            timeout_sec=timeout_sec,
            auto_wake=False,
        )
        if not res.get("ok"):
            return None, None, None, str(res.get("error") or res.get("stderr") or "termux probe failed")
        lines = [ln.strip() for ln in str(res.get("stdout") or "").splitlines() if ln.strip()]
        parsed = _safe_json_loads(lines[-1]) if lines else None
        if not parsed:
            return None, None, None, "invalid termux probe output"
        latest = parsed.get("latest") if isinstance(parsed.get("latest"), dict) else None
        sem = parsed.get("semantic") if isinstance(parsed.get("semantic"), dict) else None
        dirty = parsed.get("dirty") if isinstance(parsed.get("dirty"), dict) else None
        return latest, sem, dirty, ""
    except Exception as exc:
        return None, None, None, str(exc)


def read_termux_bundle(
    relay_url: str,
    pair_secret: str,
    expected_sha256: str = "",
    *,
    admin_key: str = "",
    timeout_sec: int = 15,
) -> dict[str, Any] | None:
    try:
        res = _send_termux_cmd(
            relay_url,
            pair_secret,
            "file_read",
            {"path": f"{PHONE_FR_DIR}/latest_bundle.json.gz.b64", "maxBytes": 300 * 1024},
            admin_key=admin_key,
            timeout_sec=timeout_sec,
            auto_wake=False,
        )
        if res.get("ok") and res.get("content"):
            return decode_bundle_gz_b64(str(res["content"]).strip(), expected_sha256=expected_sha256)
        ptr = _send_termux_cmd(
            relay_url,
            pair_secret,
            "file_read",
            {"path": f"{PHONE_FR_DIR}/latest_bundle_blob.json", "maxBytes": 16 * 1024},
            admin_key=admin_key,
            timeout_sec=min(12, timeout_sec),
            auto_wake=False,
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


def restore_from_termux_repo(
    workspace: Path,
    local_dir: Path,
    auth: tuple[str, str, str],
    *,
    paths: list[str] | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Forensic fallback restore: export uncommitted files differing from origin/master directly from Termux repo."""
    relay, secret, admin = auth
    target_paths = list(paths or [])
    if not target_paths:
        _, _, dirty, err = probe_termux_snapshot_and_dirty(relay, secret, admin_key=admin, timeout_sec=8, include_dirty=True)
        if err and not dirty:
            return {"ok": False, "error": f"Termux repo probe failed: {err}"}
        uncommitted = (dirty or {}).get("uncommittedVsOrigin") or []
        # Filter to paths that actually differ from current local workspace disk
        root = workspace.resolve()
        for item in uncommitted:
            rel = str(item.get("path") or "")
            t_sha = str(item.get("sha256") or "")
            if not rel:
                continue
            full = (root / rel).resolve()
            if not full.is_file() or hashlib.sha256(full.read_bytes()).hexdigest() != t_sha:
                target_paths.append(rel)

    if not target_paths:
        return {"ok": True, "dryRun": dry_run, "bundleSource": "termux-repo", "applied": {"written": [], "deleted": []}, "message": "Workspace already matches Termux uncommitted files."}

    if dry_run:
        return {
            "ok": True,
            "dryRun": True,
            "checkpointTier": "termux-repo",
            "bundleSource": "termux-repo",
            "wouldWrite": target_paths,
            "wouldDelete": [],
        }

    exp_res = _send_termux_cmd(
        relay,
        secret,
        "file_export_bundle",
        {"repo": PHONE_REPO_DIR, "paths": target_paths},
        admin_key=admin,
        timeout_sec=35,
        auto_wake=False,
    )
    if not exp_res.get("ok") or not exp_res.get("blobId"):
        return {"ok": False, "error": f"Termux file_export_bundle failed: {exp_res.get('error') or exp_res}"}
    bundle = proto.download_encrypted_blob(relay, secret, str(exp_res["blobId"]), admin_key=admin)
    applied = proto.apply_delta_bundle(workspace, bundle, allow_delete=False)
    return {
        "ok": True,
        "dryRun": False,
        "checkpointTier": "termux-repo",
        "bundleSource": "termux-repo",
        "applied": applied,
    }


# ==================== HIGH-LEVEL OPERATIONS ====================


def _collect_tier_checkpoints(
    local_dir: Path,
    *,
    use_remote: bool,
    url_arg: str = "",
    pair_secret_arg: str = "",
    admin_key_arg: str = "",
    cloud_timeout: int = 6,
    termux_timeout: int = 6,
    include_termux_dirty: bool = False,
) -> dict[str, Any]:
    candidates: dict[str, dict[str, Any]] = {}
    semantic_candidates: dict[str, dict[str, Any]] = {}
    tier_errors: dict[str, str] = {}
    cloud_inline_b64: str | None = None
    termux_dirty: dict[str, Any] | None = None
    termux_online: bool | None = None

    local_cp = read_local_tier(local_dir)
    if local_cp:
        candidates["local"] = local_cp
    local_sem = read_local_semantic(local_dir)
    if local_sem:
        semantic_candidates["local"] = local_sem

    auth = _try_resolve_remote_auth(url_arg, pair_secret_arg, admin_key_arg) if use_remote else None
    if use_remote and auth:
        relay, secret, admin = auth

        def _probe_cloud_latest() -> tuple[dict[str, Any] | None, str | None, str]:
            try:
                return read_cloud_tier(relay, secret, admin_key=admin, timeout=cloud_timeout)
            except Exception as exc:
                return None, None, str(exc)

        def _probe_cloud_semantic() -> tuple[dict[str, Any] | None, str]:
            try:
                scp, _, serr = read_cloud_tier(
                    relay,
                    secret,
                    admin_key=admin,
                    task_id=CLOUD_SEMANTIC_TASK_ID,
                    env_ctx=CLOUD_SEMANTIC_CTX,
                    timeout=cloud_timeout,
                )
                return scp, serr
            except Exception as exc:
                return None, str(exc)

        def _probe_termux() -> tuple[bool, dict[str, Any] | None, dict[str, Any] | None, dict[str, Any] | None, str]:
            online = _check_termux_online_fast(relay, secret, admin_key=admin, timeout=min(4, termux_timeout))
            if not online:
                return False, None, None, None, "termux offline (skipped fast)"
            t_cp, t_sem, t_dirty, t_err = probe_termux_snapshot_and_dirty(
                relay,
                secret,
                admin_key=admin,
                timeout_sec=termux_timeout,
                include_dirty=include_termux_dirty,
            )
            return True, t_cp, t_sem, t_dirty, t_err

        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            f_c = pool.submit(_probe_cloud_latest)
            f_s = pool.submit(_probe_cloud_semantic)
            f_t = pool.submit(_probe_termux)

            c_cp, c_b64, c_err = f_c.result()
            if c_cp:
                candidates["cloud"] = c_cp
                cloud_inline_b64 = c_b64
            elif c_err:
                tier_errors["cloud"] = c_err

            s_cp, _ = f_s.result()
            if s_cp:
                semantic_candidates["cloud"] = s_cp

            termux_online, t_cp, t_sem, termux_dirty, t_err = f_t.result()
            if t_cp:
                candidates["termux"] = t_cp
            elif t_err:
                tier_errors["termux"] = t_err
            if t_sem:
                semantic_candidates["termux"] = t_sem
    elif use_remote:
        tier_errors["remote"] = "No pairSecret cached yet (local tier only)"

    _, freshest_semantic = pick_freshest_checkpoint(semantic_candidates)
    return {
        "candidates": candidates,
        "semanticCandidates": semantic_candidates,
        "freshestSemantic": freshest_semantic,
        "termuxDirty": termux_dirty,
        "termuxOnline": termux_online,
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
    semantic_cp: dict[str, Any] | None = None,
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
    write_semantic = semantic_cp is not None and str(cp.get("source") or "manual") != "auto_mirror"

    def _do_cloud() -> dict[str, Any]:
        try:
            return write_cloud_tier(relay, secret, cp, bundle_gz_b64, admin_key=admin)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def _do_cloud_semantic() -> dict[str, Any]:
        if not write_semantic or semantic_cp is None:
            return {"ok": True, "skipped": True}
        try:
            return write_cloud_tier(
                relay,
                secret,
                semantic_cp,
                None,
                admin_key=admin,
                task_id=CLOUD_SEMANTIC_TASK_ID,
                env_ctx=CLOUD_SEMANTIC_CTX,
            )
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
                semantic_cp=semantic_cp if write_semantic else None,
                apply_live_mirror=apply_live_mirror,
                clear_mirror_state=clear_mirror_state,
                sync_antigravity_handoff=True,
                admin_key=admin,
            )
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        f_cloud = pool.submit(_do_cloud)
        f_sem = pool.submit(_do_cloud_semantic)
        f_termux = pool.submit(_do_termux)
        tiers["cloud"] = f_cloud.result()
        if write_semantic:
            tiers["cloudSemantic"] = f_sem.result()
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

    is_manual = str(cp.get("source") or "manual") != "auto_mirror"
    semantic_cp = extract_semantic_record(cp) if is_manual else None

    diary_md = render_diary_markdown(cp)
    tiers: dict[str, Any] = {}
    tiers["local"] = write_local_tier(local_dir, cp, diary_md, bundle_gz)
    if semantic_cp is not None:
        write_local_semantic(local_dir, semantic_cp)

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
                    semantic_cp=semantic_cp,
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
    semantic_cp = read_local_semantic(local_dir) if str(cp.get("source") or "manual") != "auto_mirror" else None
    diary_md = render_diary_markdown(cp)
    tiers = _persist_remote_tiers(
        cp,
        diary_md,
        bundle,
        bundle_gz_b64,
        semantic_cp=semantic_cp,
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
        "source": "manual",
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
    source: str = "manual",
    use_remote: bool = True,
    background: bool = False,
    url_arg: str = "",
    pair_secret_arg: str = "",
    admin_key_arg: str = "",
) -> dict[str, Any]:
    prev = read_local_tier(local_dir)
    prev_sem = read_local_semantic(local_dir)
    now = time.time()
    is_auto = source == "auto_mirror"

    if prev and prev.get("status") == "active" and (not session_id or prev.get("sessionId") == session_id):
        cp = dict(prev)
        cp["seq"] = int(prev.get("seq") or 0) + 1
    else:
        if not (local_dir / "baseline_manifest.json").is_file():
            save_local_baseline(local_dir, workspace)
        cp = {
            "schema": SCHEMA_VERSION,
            "sessionId": session_id or (prev_sem.get("sessionId") if (is_auto and prev_sem and prev_sem.get("status") == "active") else "") or f"sess_{int(now)}_{secrets.token_hex(3)}",
            "seq": (int(prev.get("seq") or 0) + 1) if prev else 1,
            "status": "active",
            "source": source,
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

    # Dual-lane monotonic preservation: auto_mirror NEVER clobbers semantic intent!
    if is_auto and prev_sem and prev_sem.get("status") == "active":
        cp = merge_semantic_into_checkpoint(cp, prev_sem)

    cp["status"] = "active"
    cp["source"] = source
    cp["timestampUnix"] = round(now, 3)
    cp["updatedUtc"] = _utc_now()

    if is_auto:
        if wave and not cp.get("wave"):
            cp["wave"] = wave
        if phase and not cp.get("phase"):
            cp["phase"] = phase
        if goal and not cp.get("goal"):
            cp["goal"] = goal
        if summary and is_auto_mirror_summary(cp.get("summary")):
            cp["summary"] = summary
        if next_step and not cp.get("nextStep"):
            cp["nextStep"] = next_step
    else:
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


def pin_session(
    *,
    workspace: Path,
    local_dir: Path,
    goal: str = "",
    wave: str = "",
    phase: str = "",
    summary: str = "",
    next_step: str = "",
    findings: list[str] | None = None,
    decisions: list[str] | None = None,
    completed: list[str] | None = None,
    pending: list[str] | None = None,
    replace_pending: bool = False,
    use_remote: bool = True,
    background: bool = True,
    url_arg: str = "",
    pair_secret_arg: str = "",
    admin_key_arg: str = "",
) -> dict[str, Any]:
    """Zero-friction semantic breadcrumb write (<15ms in -b mode; never triggers heavy file mirroring)."""
    return save_checkpoint(
        workspace=workspace,
        local_dir=local_dir,
        wave=wave,
        phase=phase,
        goal=goal,
        summary=summary or goal,
        next_step=next_step,
        findings=findings,
        decisions=decisions,
        completed=completed,
        pending=pending,
        replace_pending=replace_pending,
        auto_diff=False,
        apply_live_mirror=False,
        source="manual",
        use_remote=use_remote,
        background=background,
        url_arg=url_arg,
        pair_secret_arg=pair_secret_arg,
        admin_key_arg=admin_key_arg,
    )


def recover_session(
    *,
    workspace: Path,
    local_dir: Path,
    use_remote: bool = True,
    url_arg: str = "",
    pair_secret_arg: str = "",
    admin_key_arg: str = "",
    include_termux_dirty: bool = False,
) -> dict[str, Any]:
    probe = _collect_tier_checkpoints(
        local_dir,
        use_remote=use_remote,
        url_arg=url_arg,
        pair_secret_arg=pair_secret_arg,
        admin_key_arg=admin_key_arg,
        include_termux_dirty=include_termux_dirty,
    )
    candidates = probe["candidates"]
    tier, cp = pick_freshest_checkpoint(candidates)
    freshest_sem = probe.get("freshestSemantic")
    if cp and freshest_sem:
        cp = merge_semantic_into_checkpoint(cp, freshest_sem)

    tier_summary = {
        t: {
            "sessionId": c.get("sessionId"),
            "seq": c.get("seq"),
            "status": c.get("status"),
            "source": c.get("source", "manual"),
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
            "termuxDirty": probe.get("termuxDirty"),
            "termuxOnline": probe.get("termuxOnline"),
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
        "freshestSemantic": freshest_sem,
        "termuxDirty": probe.get("termuxDirty"),
        "termuxOnline": probe.get("termuxOnline"),
        "rollback": rollback,
        "diaryMarkdown": render_diary_markdown(cp),
        "restoreCommand": (
            f"python3 -B .agents/scripts/flight_recorder.py restore --source {tier}"
            if rollback.get("rollbackDetected") and cp.get("hasBundle")
            else ""
        ),
    }


def _read_compact_handoff(workspace: Path) -> dict[str, str]:
    p = workspace / ".agents" / "docs" / "HANDOFF.md"
    if not p.is_file():
        return {}
    out: dict[str, str] = {}
    try:
        for raw in p.read_text(encoding="utf-8", errors="replace").splitlines()[:40]:
            line = raw.strip()
            if line.startswith("HANDOFF|"):
                out["header"] = line
            elif line.startswith("DONE|"):
                out["done"] = line[5:].strip()
            elif line.startswith("NEXT|"):
                out["next"] = line[5:].strip()
            elif line.startswith("WAVE|"):
                out["wave"] = line[5:].strip()
    except OSError:
        pass
    return out


def _read_pending_extra_requests(workspace: Path) -> list[str]:
    p = workspace / ".agents" / "docs" / "EXTRA_REQUESTS.md"
    if not p.is_file():
        return []
    pending: list[str] = []
    in_pending = False
    try:
        for raw in p.read_text(encoding="utf-8", errors="replace").splitlines():
            s = raw.strip()
            if s.startswith("## "):
                low = s.lower()
                in_pending = "pending" in low or "active" in low or "open" in low
                continue
            if in_pending and s.startswith(("- ", "* ")):
                body = s[2:].strip()
                if body and "(none" not in body.lower() and not body.startswith("[x]"):
                    pending.append(body[:160])
    except OSError:
        pass
    return pending[:8]


def _clip_line(text: Any, limit: int = 180) -> str:
    s = " ".join(str(text or "").split())
    return s if len(s) <= limit else s[: max(1, limit - 3)] + "..."


def render_resume_packet(data: dict[str, Any], *, compact: bool = False) -> str:
    """Render ultra-compact (<35L, <450 tokens) telegraphic Resume Packet."""
    verdict = str(data.get("verdict") or "CLEAN")
    cp = data.get("checkpoint") or {}
    rb = data.get("rollback") or {}
    elapsed = data.get("elapsedSec", 0.0)
    tiers_ok = ",".join(sorted((data.get("tiers") or {}).keys())) or "none"

    if compact:
        sess = f"{cp.get('sessionId', '-')}#{cp.get('seq', 0)}:{cp.get('status', '-')}" if cp else "none"
        nxt = _clip_line(cp.get("nextStep") or (data.get("handoff") or {}).get("next") or "", 90)
        act = _clip_line(data.get("action") or "READY_TO_RESUME", 90)
        return f"RESUME|{verdict}|sess={sess}|tiers={tiers_ok}|t={elapsed:.2f}s|next={nxt or '-'}|act={act}"

    lines = [
        f"RESUME_VERDICT|{verdict}|tiers={tiers_ok}|termux_online={int(bool(data.get('termuxOnline')))}|elapsed={elapsed:.2f}s",
    ]
    if cp:
        lines.append(
            f"SESSION|id={cp.get('sessionId', '?')}|seq=#{cp.get('seq', 0)}|status={cp.get('status', '?')}|tier={data.get('freshestTier', '?')}|src={cp.get('source', 'manual')}|utc={cp.get('updatedUtc', '')}"
        )
        if cp.get("wave") or cp.get("phase"):
            lines.append(f"SCOPE|wave={cp.get('wave') or '-'}|phase={cp.get('phase') or '-'}")
        if cp.get("goal"):
            lines.append(f"GOAL|{_clip_line(cp['goal'], 180)}")
        if cp.get("summary") and cp.get("summary") != cp.get("goal") and not is_auto_mirror_summary(cp.get("summary")):
            lines.append(f"SUMMARY|{_clip_line(cp['summary'], 180)}")
        if cp.get("completed"):
            lines.append(f"DONE|{' ; '.join(_clip_line(x, 70) for x in list(cp['completed'])[-5:])}")
        if cp.get("pending"):
            lines.append(f"PENDING|{' ; '.join(_clip_line(x, 75) for x in list(cp['pending'])[:6])}")
        if cp.get("nextStep"):
            lines.append(f"NEXT|{_clip_line(cp['nextStep'], 180)}")

    if rb.get("rollbackDetected"):
        rev_paths = [r["path"] for r in rb.get("reverted") or []]
        mis_paths = list(rb.get("missing") or [])
        reapp = list(rb.get("reappeared") or [])
        lines.append(
            f"ROLLBACK|reverted={len(rev_paths)}|missing={len(mis_paths)}|reappeared={len(reapp)}|files={','.join((rev_paths + mis_paths)[:10])}"
        )

    if data.get("autoRestored"):
        ar = data["autoRestored"]
        lines.append(
            f"AUTO_RESTORE|ok={bool(ar.get('ok'))}|src={ar.get('bundleSource', '?')}|written={len(((ar.get('applied') or {}).get('written')) or [])}"
        )

    td_paths = data.get("termuxUncommittedDiffs") or []
    if td_paths:
        lines.append(f"TERMUX_UNCOMMITTED|count={len(td_paths)}|files={','.join(td_paths[:10])}")

    gh_dirty = data.get("localDirtyVsGithub") or {}
    if gh_dirty.get("changed") or gh_dirty.get("deleted"):
        ch = list(gh_dirty.get("changed") or [])
        dl = list(gh_dirty.get("deleted") or [])
        lines.append(f"LOCAL_DIRTY|changed={len(ch)}|deleted={len(dl)}|files={','.join((ch + dl)[:10])}")

    ho = data.get("handoff") or {}
    if ho.get("next") and ho.get("next") != cp.get("nextStep"):
        lines.append(f"HANDOFF_NEXT|{_clip_line(ho['next'], 160)}")
    if ho.get("done") and not cp.get("completed"):
        lines.append(f"HANDOFF_DONE|{_clip_line(ho['done'], 160)}")

    ex = data.get("pendingExtraRequests") or []
    if ex:
        lines.append(f"EXTRA_REQ|{' ; '.join(_clip_line(x, 80) for x in ex[:4])}")

    if data.get("envAlert"):
        lines.append(f"ENV_ALERT|{_clip_line(data['envAlert'], 160)}")

    lines.append(f"ACTION|{data.get('action') or 'READY_TO_RESUME'}")
    return "\n".join(lines[:35])


def resume_session(
    *,
    workspace: Path,
    local_dir: Path,
    use_remote: bool = True,
    auto_restore: bool = False,
    skip_github: bool = False,
    skip_termux_diff: bool = False,
    url_arg: str = "",
    pair_secret_arg: str = "",
    admin_key_arg: str = "",
) -> dict[str, Any]:
    """1-shot zero-waste parallel session recovery packet (<=2.5s, <=35 lines)."""
    t0 = time.monotonic()

    def _probe_github_dirty() -> dict[str, Any]:
        if skip_github or not use_remote:
            return {}
        try:
            import github_direct as ghdirect
            import local_capabilities as lcap

            tok = lcap.get_secret("GITHUB_TOKEN", aliases=("GH_TOKEN",))
            if not tok:
                return {}
            diff = ghdirect.diff_workspace_against_github(workspace, token=tok, sync_deletes=False)
            return {"changed": diff.get("changed") or [], "deleted": diff.get("deleted") or [], "headSha": diff.get("headSha", "")}
        except Exception as exc:
            return {"error": str(exc)[:120]}

    def _probe_env_alert() -> str:
        try:
            import local_capabilities as lcap

            caps = lcap.inspect_capabilities()
            return str(caps.get("alert") or "")
        except Exception:
            return ""

    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        f_rec = pool.submit(
            recover_session,
            workspace=workspace,
            local_dir=local_dir,
            use_remote=use_remote,
            url_arg=url_arg,
            pair_secret_arg=pair_secret_arg,
            admin_key_arg=admin_key_arg,
            include_termux_dirty=not skip_termux_diff,
        )
        f_gh = pool.submit(_probe_github_dirty)
        f_env = pool.submit(_probe_env_alert)

        rec = f_rec.result()
        gh_dirty = f_gh.result()
        env_alert = f_env.result()

    handoff = _read_compact_handoff(workspace)
    extra_req = _read_pending_extra_requests(workspace)

    cp = rec.get("checkpoint") or {}
    rb = rec.get("rollback") or {}
    tier = rec.get("freshestTier") or ""

    # Identify Termux uncommitted files that differ from current workspace disk
    termux_uncommitted_diffs: list[str] = []
    td = rec.get("termuxDirty") or {}
    root = workspace.resolve()
    for item in td.get("uncommittedVsOrigin") or []:
        rel = str(item.get("path") or "")
        t_sha = str(item.get("sha256") or "")
        if not rel:
            continue
        full = (root / rel).resolve()
        try:
            full.relative_to(root)
        except ValueError:
            continue
        if not full.is_file() or hashlib.sha256(full.read_bytes()).hexdigest() != t_sha:
            termux_uncommitted_diffs.append(rel)

    verdict = "CLEAN"
    action = "READY_TO_RESUME"
    auto_restored = None

    if rb.get("rollbackDetected") and cp.get("hasBundle"):
        verdict = f"ROLLBACK_{tier.upper() or 'REMOTE'}"
        action = f"python3 -B .agents/scripts/flight_recorder.py restore --source {tier or 'auto'}"
        if auto_restore:
            auto_restored = restore_session(
                workspace=workspace,
                local_dir=local_dir,
                source=tier or "auto",
                use_remote=use_remote,
                url_arg=url_arg,
                pair_secret_arg=pair_secret_arg,
                admin_key_arg=admin_key_arg,
            )
            if auto_restored.get("ok"):
                verdict = "RESTORED_AND_READY"
                action = "READY_TO_RESUME"
    elif termux_uncommitted_diffs and (not cp or cp.get("status") != "closed"):
        verdict = "ROLLBACK_TERMUX"
        action = "python3 -B .agents/scripts/flight_recorder.py restore --source termux-repo"
        if auto_restore:
            auto_restored = restore_session(
                workspace=workspace,
                local_dir=local_dir,
                source="termux-repo",
                use_remote=use_remote,
                url_arg=url_arg,
                pair_secret_arg=pair_secret_arg,
                admin_key_arg=admin_key_arg,
            )
            if auto_restored.get("ok"):
                verdict = "RESTORED_AND_READY"
                action = "READY_TO_RESUME"
    elif gh_dirty.get("changed") or gh_dirty.get("deleted"):
        verdict = "DIRTY_LOCAL"
        action = "READY_TO_RESUME"

    return {
        "ok": True,
        "verdict": verdict,
        "found": bool(rec.get("found")),
        "freshestTier": tier,
        "tiers": rec.get("tiers") or {},
        "termuxOnline": rec.get("termuxOnline"),
        "checkpoint": cp,
        "rollback": rb,
        "autoRestored": auto_restored,
        "termuxUncommittedDiffs": termux_uncommitted_diffs,
        "localDirtyVsGithub": gh_dirty,
        "handoff": handoff,
        "pendingExtraRequests": extra_req,
        "envAlert": env_alert,
        "errors": rec.get("errors") or {},
        "action": action,
        "elapsedSec": round(time.monotonic() - t0, 3),
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
    if source == "termux-repo":
        auth = _try_resolve_remote_auth(url_arg, pair_secret_arg, admin_key_arg) if use_remote else None
        if not auth:
            return {"ok": False, "error": "No pairSecret configured for source='termux-repo'."}
        return restore_from_termux_repo(workspace, local_dir, auth, dry_run=dry_run)

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
        if source == "auto" and probe.get("auth") and probe.get("termuxOnline"):
            return restore_from_termux_repo(workspace, local_dir, probe["auth"], dry_run=dry_run)
        return {"ok": False, "error": f"No checkpoint found for source='{source}'.", "errors": probe["errors"]}

    if probe.get("freshestSemantic"):
        cp = merge_semantic_into_checkpoint(cp, probe["freshestSemantic"])

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
        elif cand_tier == "termux" and auth and probe.get("termuxOnline", True):
            relay, secret, admin = auth
            b = read_termux_bundle(relay, secret, expected_sha256=expected_sha, admin_key=admin)
            if b:
                bundle = b
                bundle_source = "termux"
                break

    if not bundle:
        # Forensic fallback: if bundle missing/clobbered in auto mode, check Termux repo uncommitted edits!
        if source == "auto" and auth and probe.get("termuxOnline"):
            return restore_from_termux_repo(workspace, local_dir, auth, dry_run=dry_run)
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
    if _has_semantic_content(cp):
        write_local_semantic(local_dir, extract_semantic_record(cp))
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
    cp["source"] = "manual"
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


def _format_compact_write_line(verb: str, res: dict[str, Any]) -> str:
    cp = res.get("checkpoint") or {}
    tiers = res.get("tiers") or {}
    t1 = "ok" if (tiers.get("local") or {}).get("ok") else "err"
    rem = tiers.get("remote")
    if isinstance(rem, dict) and rem.get("mode") == "background":
        rem_s = f"bg={rem.get('pid', '?')}"
    else:
        c_ok = int(bool((tiers.get("cloud") or {}).get("ok")))
        t_ok = int(bool((tiers.get("termux") or {}).get("ok")))
        rem_s = f"cloud={c_ok},termux={t_ok}"
    return f"FR_{verb.upper()}|sess={cp.get('sessionId', '?')}|seq=#{cp.get('seq', 0)}|status={cp.get('status', '?')}|t1={t1}|{rem_s}"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Cavern Anti-Cancellation Dual-Lane 3-Tier Flight Recorder (/tmp + Phone Termux + Cloudflare Hub v6)."
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
    p_init.add_argument("--compact", action="store_true", help="Emit 1-line telegraphic summary instead of full JSON")

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
    p_save.add_argument("--compact", action="store_true", help="Emit 1-line telegraphic summary instead of full JSON")

    p_pin = sub.add_parser("pin", help="Zero-friction semantic breadcrumb (<15ms in -b mode; writes durable semantic lane)")
    p_pin.add_argument("--wave", default="")
    p_pin.add_argument("--phase", default="")
    p_pin.add_argument("--goal", default="")
    p_pin.add_argument("--summary", default="")
    p_pin.add_argument("--next", default="")
    p_pin.add_argument("--finding", action="append", default=[])
    p_pin.add_argument("--decision", action="append", default=[])
    p_pin.add_argument("--done", action="append", default=[])
    p_pin.add_argument("--pending", action="append", default=[])
    p_pin.add_argument("--replace-pending", action="store_true")
    p_pin.add_argument("-b", "--background", action="store_true", help="Write local (<10ms) and flush remote in background")
    p_pin.add_argument("--json", action="store_true", help="Output full JSON instead of 1-line compact summary")

    p_rsm = sub.add_parser("resume", help="1-shot parallel instant resume packet (<=2.5s, <=35 lines, optional --auto-restore)")
    p_rsm.add_argument("--auto-restore", action="store_true", help="Immediately restore rolled-back files if rollback is detected")
    p_rsm.add_argument("--compact", action="store_true", help="Emit single-line RESUME| summary (for host-boot)")
    p_rsm.add_argument("--json", action="store_true", help="Output machine-readable JSON")
    p_rsm.add_argument("--skip-github", action="store_true", help="Skip GitHub HEAD diff probe")
    p_rsm.add_argument("--skip-termux-diff", action="store_true", help="Skip Termux uncommitted-vs-origin diff probe")

    p_rec = sub.add_parser("recover", help="Probe all 3 tiers, detect workspace rollback, and print recovery brief")
    p_rec.add_argument("--json", action="store_true", help="Output machine-readable JSON instead of compact brief")

    p_res = sub.add_parser("restore", help="Restore rolled-back workspace edits from verified omni-delta-v1 snapshot or Termux repo")
    p_res.add_argument("--source", choices=("auto", "local", "cloud", "termux", "termux-repo"), default="auto")
    p_res.add_argument("--dry-run", action="store_true")
    p_res.add_argument("--allow-delete", action="store_true")

    sub.add_parser("status", help="Show checkpoint sync status across Local (/tmp), Phone Termux, and Cloudflare Hub v6")

    p_cls = sub.add_parser("close", help="Mark active session cleanly closed after commit/push")
    p_cls.add_argument("--commit", default="")
    p_cls.add_argument("--note", default="")
    p_cls.add_argument("--next", default="")
    p_cls.add_argument("--keep-pending", action="store_true")
    p_cls.add_argument("-b", "--background", action="store_true", help="Write Tier 1 local (<10ms) and flush Termux+Cloud in background")
    p_cls.add_argument("--compact", action="store_true", help="Emit 1-line telegraphic summary instead of full JSON")

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
        print(_format_compact_write_line("init", res) if args.compact else json.dumps(res, indent=2, ensure_ascii=False))
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
        print(_format_compact_write_line("save", res) if args.compact else json.dumps(res, indent=2, ensure_ascii=False))
        return 0 if res.get("ok") else 1

    if args.action == "pin":
        res = pin_session(
            workspace=workspace,
            local_dir=local_dir,
            wave=args.wave,
            phase=args.phase,
            goal=args.goal,
            summary=args.summary,
            next_step=args.next,
            findings=args.finding,
            decisions=args.decision,
            completed=args.done,
            pending=args.pending,
            replace_pending=args.replace_pending,
            use_remote=use_remote,
            background=bool(args.background),
            url_arg=args.url,
            pair_secret_arg=args.pair_secret,
            admin_key_arg=args.admin_key,
        )
        print(json.dumps(res, indent=2, ensure_ascii=False) if args.json else _format_compact_write_line("pin", res))
        return 0 if res.get("ok") else 1

    if args.action == "resume":
        res = resume_session(
            workspace=workspace,
            local_dir=local_dir,
            use_remote=use_remote,
            auto_restore=bool(args.auto_restore),
            skip_github=bool(args.skip_github),
            skip_termux_diff=bool(args.skip_termux_diff),
            url_arg=args.url,
            pair_secret_arg=args.pair_secret,
            admin_key_arg=args.admin_key,
        )
        if args.json:
            print(json.dumps(res, indent=2, ensure_ascii=False))
        else:
            print(render_resume_packet(res, compact=bool(args.compact)))
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
        print(_format_compact_write_line("close", res) if args.compact else json.dumps(res, indent=2, ensure_ascii=False))
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
