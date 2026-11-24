#!/usr/bin/env python3
"""OmniConnect v5 — Zero-Paste Credential Resolver, Local Gitignored Vault & Smart Knock Client.

Eliminates the need for the user to ever copy/paste Relay URLs or tokens into chat:
  1. Explicit CLI flags (--url, --pair-secret, --code, --admin-key)
  2. Environment variables / AI Studio Secrets (CAVERN_RELAY_URL, CAVERN_BRIDGE_PAIR_SECRET,
     CAVERN_BRIDGE_SESSION_TOKEN, CAVERN_ADMIN_KEY)
  3. Local gitignored workspace vault (`.agents/.bridge_vault.json`, chmod 0600, excluded from
     git and `push-workspace` delta bundles)
  4. Public non-secret repo profile (`.agents/bridge_profile.json` — Relay URL + double-hashed
     public Knock ID only, NEVER secrets)
  5. Zero-Secret Ephemeral NIST P-256 ECDH Smart Knock (`/v1/knock/*`) when no secret is present yet:
     rings the user's phone with a 6-char Visual Verification Code (SAS) and BiometricPrompt,
     then receives the approved session/workspace grant E2E-encrypted with Forward Secrecy.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import sys
import time
import urllib.parse
from pathlib import Path
from typing import Any

AGENT_DIR = Path(__file__).resolve().parent
REPO_ROOT = AGENT_DIR.parents[1] if AGENT_DIR.parent.name == ".agents" else AGENT_DIR.parent
if str(AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_DIR))

import local_capabilities as lcap
import omni_protocol as proto

PUBLIC_PROFILE_PATH = REPO_ROOT / ".agents" / "bridge_profile.json"
LEGACY_WORKSPACE_VAULT_PATH = REPO_ROOT / ".agents" / ".bridge_vault.json"
LOCAL_VAULT_PATH = Path("/tmp/.cavern_bridge_vault.json")
HEX64_RE = re.compile(r"^[0-9a-f]{64}$")


def _read_json_safe(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def load_public_profile(profile_path: Path = PUBLIC_PROFILE_PATH) -> dict[str, Any]:
    raw = _read_json_safe(profile_path)
    relay_url = str(raw.get("relayUrl") or "").strip()
    if relay_url:
        try:
            relay_url = proto.validate_https_url(relay_url)
        except Exception:
            relay_url = ""
    knock_id = str(raw.get("knockId") or "").strip().lower()
    if not HEX64_RE.match(knock_id):
        knock_id = ""
    return {
        "schema": int(raw.get("schema") or 5),
        "relayUrl": relay_url,
        "knockId": knock_id,
        "defaultMode": str(raw.get("defaultMode") or "auto"),
        "preferredChannels": raw.get("preferredChannels") or ["android", "termux", "cavernize"],
    }


def save_public_profile(
    *,
    relay_url: str | None = None,
    knock_id: str | None = None,
    profile_path: Path = PUBLIC_PROFILE_PATH,
) -> dict[str, Any]:
    cur = load_public_profile(profile_path)
    if relay_url is not None and relay_url.strip():
        cur["relayUrl"] = proto.validate_https_url(relay_url)
    if knock_id is not None:
        clean_kid = knock_id.strip().lower()
        if clean_kid and not HEX64_RE.match(clean_kid):
            raise ValueError("knockId must be 64 lowercase hex chars (double-hashed public mailbox ID).")
        cur["knockId"] = clean_kid
    cur["schema"] = 5
    cur["note"] = (
        "PUBLIC non-secret connection profile only. NEVER store pairSecret, session tokens, or API keys here. "
        "knockId is a double-hashed one-way mailbox ID (sha256('knock-channel-v5:' + sha256('wake-channel-v2:' + pairSecret))) "
        "that only lets an unpaired AI ring the phone's biometric approval bell."
    )
    profile_path.parent.mkdir(parents=True, exist_ok=True)
    profile_path.write_text(json.dumps(cur, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return cur


def load_vault(vault_path: Path = LOCAL_VAULT_PATH) -> dict[str, Any]:
    data = _read_json_safe(vault_path)
    if not data and Path(vault_path).resolve() == LOCAL_VAULT_PATH.resolve():
        data = _read_json_safe(LEGACY_WORKSPACE_VAULT_PATH)
    if not data:
        return {}
    now = time.time()
    # Automatically prune expired ephemeral Live Bridge session tokens
    exp = float(data.get("sessionExpiresUnix") or 0)
    if data.get("sessionToken") and exp > 0 and now >= exp:
        data.pop("sessionToken", None)
        data.pop("sessionExpiresUnix", None)
        data.pop("sessionTier", None)
    return data


def save_vault(
    *,
    relay_url: str | None = None,
    pair_secret: str | None = None,
    session_token: str | None = None,
    session_ttl_seconds: int | None = None,
    session_tier: int | None = None,
    admin_key: str | None = None,
    knock_id: str | None = None,
    device: str | None = None,
    vault_path: Path = LOCAL_VAULT_PATH,
) -> dict[str, Any]:
    """Persist credentials strictly inside gitignored local workspace vault (chmod 0600)."""
    data = load_vault(vault_path)
    if relay_url:
        data["relayUrl"] = proto.validate_https_url(relay_url)
    if pair_secret:
        norm = proto.normalize_pair_secret_hex(pair_secret)
        data["pairSecret"] = norm
        data["knockId"] = proto.compute_knock_channel_id(norm)
    if knock_id and HEX64_RE.match(knock_id.strip().lower()):
        data["knockId"] = knock_id.strip().lower()
    if session_token:
        data["sessionToken"] = session_token.strip()
        ttl = max(60, min(3600, int(session_ttl_seconds or 1800)))
        data["sessionExpiresUnix"] = int(time.time()) + ttl
        if session_tier is not None:
            data["sessionTier"] = int(session_tier)
    if admin_key:
        data["adminKey"] = admin_key.strip()
    if device:
        data["device"] = device.strip()
    data["updatedAtUtc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    vault_path.parent.mkdir(parents=True, exist_ok=True)
    vault_path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    try:
        os.chmod(vault_path, 0o600)
    except OSError:
        pass
    # Clean legacy workspace vault once migrated to /tmp so Git/AI Studio stays 100% clean
    if Path(vault_path).resolve() == LOCAL_VAULT_PATH.resolve() and LEGACY_WORKSPACE_VAULT_PATH.is_file():
        try:
            LEGACY_WORKSPACE_VAULT_PATH.unlink()
        except OSError:
            pass
    return data


def clear_vault(vault_path: Path = LOCAL_VAULT_PATH) -> bool:
    removed = False
    if vault_path.is_file():
        vault_path.unlink()
        removed = True
    if Path(vault_path).resolve() == LOCAL_VAULT_PATH.resolve() and LEGACY_WORKSPACE_VAULT_PATH.is_file():
        try:
            LEGACY_WORKSPACE_VAULT_PATH.unlink()
            removed = True
        except OSError:
            pass
    return removed


def resolve_connection(
    *,
    cli_url: str = "",
    cli_pair_secret: str = "",
    cli_code: str = "",
    cli_admin_key: str = "",
    cli_knock_id: str = "",
    profile_path: Path = PUBLIC_PROFILE_PATH,
    vault_path: Path = LOCAL_VAULT_PATH,
) -> dict[str, Any]:
    """Resolve effective connection parameters across CLI > Env > Local Vault > Public Profile."""
    profile = load_public_profile(profile_path)
    vault = load_vault(vault_path)

    use_runtime_bridge = Path(vault_path).resolve() == LOCAL_VAULT_PATH.resolve()
    rt_path = lcap.RUNTIME_ENV_FILE if use_runtime_bridge else Path("/nonexistent_test_runtime_env.json")

    env_relay, env_relay_src = lcap.resolve_secret_with_source(
        "CAVERN_RELAY_URL",
        runtime_file=rt_path,
        vault_path=vault_path,
        allow_proc_scan=use_runtime_bridge,
    )
    relay_url = ""
    relay_source = "none"
    relay_candidates = [
        ("cli", cli_url),
        (
            env_relay_src if (env_relay and not env_relay_src.startswith(("vault:", "omni_local_config:"))) else "env",
            env_relay if (env_relay and not env_relay_src.startswith(("vault:", "omni_local_config:"))) else "",
        ),
        ("vault", vault.get("relayUrl", "")),
        ("omni_local_config", env_relay if env_relay_src.startswith("omni_local_config:") else ""),
        ("profile", profile.get("relayUrl", "")),
    ]
    for src_name, val in relay_candidates:
        clean = str(val or "").strip()
        if clean:
            relay_url = proto.validate_https_url(clean)
            relay_source = src_name
            break

    env_pair, env_pair_src = lcap.resolve_secret_with_source(
        "CAVERN_BRIDGE_PAIR_SECRET",
        runtime_file=rt_path,
        vault_path=vault_path,
        allow_proc_scan=use_runtime_bridge,
    )
    pair_secret = ""
    pair_source = "none"
    pair_candidates = [
        ("cli", cli_pair_secret),
        (
            env_pair_src if (env_pair and not env_pair_src.startswith(("vault:", "omni_local_config:"))) else "env",
            env_pair if (env_pair and not env_pair_src.startswith(("vault:", "omni_local_config:"))) else "",
        ),
        ("vault", vault.get("pairSecret", "")),
        ("omni_local_config", env_pair if env_pair_src.startswith("omni_local_config:") else ""),
    ]
    for src_name, val in pair_candidates:
        clean = str(val or "").strip()
        if clean:
            pair_secret = proto.normalize_pair_secret_hex(clean)
            pair_source = src_name
            break

    session_token = ""
    session_source = "none"
    for src_name, val in (
        ("cli", cli_code),
        ("env", os.environ.get("CAVERN_BRIDGE_SESSION_TOKEN", "")),
        ("vault", vault.get("sessionToken", "")),
    ):
        clean = str(val or "").strip()
        if clean:
            session_token = clean
            session_source = src_name
            break

    env_admin = lcap.get_secret("CAVERN_ADMIN_KEY", runtime_file=rt_path, vault_path=vault_path, allow_proc_scan=use_runtime_bridge)
    admin_key = ""
    for val in (cli_admin_key, env_admin, vault.get("adminKey", "")):
        clean = str(val or "").strip()
        if clean:
            admin_key = clean
            break

    env_knock = lcap.get_secret("CAVERN_KNOCK_ID", runtime_file=rt_path, vault_path=vault_path, allow_proc_scan=use_runtime_bridge)
    knock_id = ""
    for val in (
        cli_knock_id,
        env_knock,
        proto.compute_knock_channel_id(pair_secret) if pair_secret else "",
        vault.get("knockId", ""),
        profile.get("knockId", ""),
    ):
        clean = str(val or "").strip().lower()
        if HEX64_RE.match(clean):
            knock_id = clean
            break

    session_rem = 0
    if session_token and session_source == "vault":
        exp = float(vault.get("sessionExpiresUnix") or 0)
        if exp > 0:
            session_rem = max(0, int(exp - time.time()))

    return {
        "relayUrl": relay_url,
        "relaySource": relay_source,
        "pairSecret": pair_secret,
        "pairSource": pair_source,
        "sessionToken": session_token,
        "sessionSource": session_source,
        "sessionRemainingSec": session_rem,
        "adminKey": admin_key,
        "knockId": knock_id,
        "device": str(vault.get("device") or ""),
    }


def request_smart_knock(
    relay_url: str,
    knock_id: str,
    *,
    requested_scope: str = "session",
    tier: int = 2,
    duration_seconds: int = 3600,
    reason: str = "Zero-paste AI Studio connection",
    requester: str = "AI Studio",
    timeout_sec: int = 120,
    admin_key: str = "",
    persist_vault: bool = True,
    vault_path: Path = LOCAL_VAULT_PATH,
) -> dict[str, Any]:
    """Perform Zero-Secret Ephemeral P-256 ECDH Smart Knock against the user's phone."""
    base = proto.validate_https_url(relay_url)
    clean_kid = (knock_id or "").strip().lower()
    if not HEX64_RE.match(clean_kid):
        raise ValueError("Valid 64-hex public knockId is required for Smart Knock (configure in .agents/bridge_profile.json).")

    priv_int, pub_bytes = proto.p256_generate_keypair()
    client_pub_b64 = proto.p256_encode_public_key(pub_bytes)
    knock_req_id = f"k_{int(time.time())}_{secrets.token_hex(6)}"
    sas_code = proto.compute_knock_sas(clean_kid, knock_req_id, client_pub_b64)

    headers = {
        "User-Agent": proto.USER_AGENT,
        "Accept": "application/json",
        "X-Knock-Id": clean_kid,
    }
    if admin_key:
        headers["X-Admin-Key"] = admin_key.strip()

    st, resp = proto.http_json(
        "POST",
        f"{base}/v1/knock/request",
        headers,
        {
            "knockReqId": knock_req_id,
            "clientPubKey": client_pub_b64,
            "requestedScope": "workspace" if requested_scope == "workspace" else "session",
            "requestedTier": max(0, min(2, int(tier))),
            "durationSeconds": max(300, min(3600, int(duration_seconds))),
            "requester": (requester or "AI Studio")[:64],
            "reason": (reason or "Zero-paste AI Studio connection")[:160],
        },
        timeout=25,
    )
    if st >= 400 or not resp.get("ok"):
        raise RuntimeError(f"Smart Knock request rejected (HTTP {st}): {resp.get('error', resp)}")

    print(
        f"\n[SMART_KNOCK_SENT|id={knock_req_id}|SAS={sas_code}]\n"
        f"  >>> Verification Code on Phone: {sas_code} <<<\n"
        f"  Tap Approve (Fingerprint) on your phone within {timeout_sec}s...\n",
        file=sys.stderr,
        flush=True,
    )

    deadline = time.time() + max(15, timeout_sec)
    time.sleep(2.5)
    while time.time() < deadline:
        st_s, data = proto.http_json(
            "GET",
            f"{base}/v1/knock/status?knockReqId={urllib.parse.quote(knock_req_id)}",
            headers,
            timeout=20,
        )
        if st_s == 404:
            raise TimeoutError("Smart Knock expired on Cloudflare relay before approval.")
        if st_s == 200 and data.get("ok"):
            status = str(data.get("status") or "pending").lower()
            if status == "denied":
                raise PermissionError(f"Device user denied Smart Knock: {data.get('reason', 'denied')}")
            if status == "approved":
                server_pub = str(data.get("serverPubKey") or "")
                enc_grant = data.get("encryptedGrant")
                grant = proto.decrypt_knock_grant(priv_int, server_pub, knock_req_id, enc_grant)
                if persist_vault and isinstance(grant, dict):
                    save_vault(
                        relay_url=str(grant.get("relayUrl") or base),
                        pair_secret=str(grant.get("pairSecret") or "") or None,
                        session_token=str(grant.get("sessionCode") or "") or None,
                        session_ttl_seconds=int(grant.get("ttlSeconds") or duration_seconds),
                        session_tier=int(grant.get("tier") if grant.get("tier") is not None else tier),
                        knock_id=clean_kid,
                        device=str(grant.get("device") or ""),
                        vault_path=vault_path,
                    )
                return {
                    "ok": True,
                    "sasCode": sas_code,
                    "scope": str(data.get("scope") or grant.get("mode") or "session"),
                    "hasPairSecret": bool(grant.get("pairSecret")),
                    "hasSessionCode": bool(grant.get("sessionCode")),
                    "device": grant.get("device", ""),
                    "tier": grant.get("tier", tier),
                    "ttlSeconds": grant.get("ttlSeconds", duration_seconds),
                    "grant": grant,
                }
        time.sleep(2.5)

    raise TimeoutError(f"Timed out after {timeout_sec}s waiting for biometric approval on phone (SAS={sas_code}).")
