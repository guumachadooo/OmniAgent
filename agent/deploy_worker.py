#!/usr/bin/env python3
"""Zero-dependency 1-second Cloudflare Worker v6 Deployer (preserves RELAY_KV & enables SQLite Durable Object HUB_DO).

Eliminates manual Cloudflare Dashboard Quick Edit uploads.
Uploads `.agents/docs/cloudflare_live_relay_worker.js` directly via Cloudflare Workers REST API,
preserving existing `RELAY_KV` & secrets via `keep_bindings` while optionally binding the
Free-Tier SQLite Durable Object `HUB_DO` (`HubCoordinator`, `new_sqlite_classes: ["HubCoordinator"]`).

Usage:
  python3 -B .agents/agent/deploy_worker.py
  # or non-interactive:
  CLOUDFLARE_API_TOKEN="..." python3 -B .agents/agent/deploy_worker.py
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import secrets
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

AGENT_DIR = Path(__file__).resolve().parent
REPO_ROOT = AGENT_DIR.parents[1] if AGENT_DIR.parent.name == ".agents" else AGENT_DIR.parent
if str(AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_DIR))

import local_capabilities as lcap

DEFAULT_WORKER_FILE = (
    REPO_ROOT / "worker" / "cloudflare_live_relay_worker.js"
    if (REPO_ROOT / "worker" / "cloudflare_live_relay_worker.js").is_file()
    else REPO_ROOT / ".agents" / "docs" / "cloudflare_live_relay_worker.js"
)
BRIDGE_PROFILE_PATH = REPO_ROOT / ".agents" / "bridge_profile.json"
PRESERVED_BINDING_TYPES = ["kv_namespace", "secret_text", "plain_text", "r2_bucket", "d1"]
CF_API_BASE = "https://api.cloudflare.com/client/v4"
DO_BINDING_NAME = "HUB_DO"
DO_CLASS_NAME = "HubCoordinator"
DO_MIGRATION_TAG = "v6-hub-sqlite"


def infer_worker_name_from_url(relay_url: str) -> str:
    raw = (relay_url or "").strip()
    if not raw:
        return ""
    try:
        host = (urllib.parse.urlparse(raw).hostname or "").strip().lower()
        if host.endswith(".workers.dev"):
            sub = host.split(".")[0]
            if sub:
                return sub
    except Exception:
        pass
    return ""


def load_profile_relay_url() -> str:
    if BRIDGE_PROFILE_PATH.is_file():
        try:
            data = json.loads(BRIDGE_PROFILE_PATH.read_text(encoding="utf-8"))
            return str(data.get("relayUrl") or "").strip()
        except Exception:
            pass
    return ""


def build_worker_multipart(
    script_source: bytes,
    main_module: str = "worker.js",
    keep_bindings: list[str] | None = None,
    boundary: str | None = None,
    enable_durable_object: bool = True,
    include_sqlite_migration: bool = True,
    compatibility_date: str = "2026-04-01",
) -> tuple[bytes, str]:
    """Build RFC 7578 multipart/form-data payload for Cloudflare Module Worker upload."""
    bnd = boundary or f"----OmniAgentCF{secrets.token_hex(12)}"
    metadata: dict[str, Any] = {
        "main_module": main_module,
        "compatibility_date": compatibility_date,
        "keep_bindings": list(PRESERVED_BINDING_TYPES if keep_bindings is None else keep_bindings),
    }
    if enable_durable_object:
        metadata["bindings"] = [
            {
                "name": DO_BINDING_NAME,
                "type": "durable_object_namespace",
                "class_name": DO_CLASS_NAME,
            }
        ]
        if include_sqlite_migration:
            metadata["migrations"] = {
                "new_tag": DO_MIGRATION_TAG,
                "new_sqlite_classes": [DO_CLASS_NAME],
            }
    meta_bytes = json.dumps(metadata, separators=(",", ":")).encode("utf-8")

    parts = [
        (
            f"--{bnd}\r\n"
            f'Content-Disposition: form-data; name="metadata"\r\n'
            f"Content-Type: application/json\r\n\r\n"
        ).encode("utf-8")
        + meta_bytes
        + b"\r\n",
        (
            f"--{bnd}\r\n"
            f'Content-Disposition: form-data; name="{main_module}"; filename="{main_module}"\r\n'
            f"Content-Type: application/javascript+module\r\n\r\n"
        ).encode("utf-8")
        + script_source
        + b"\r\n",
        f"--{bnd}--\r\n".encode("utf-8"),
    ]
    return b"".join(parts), f"multipart/form-data; boundary={bnd}"


def _cf_json_request(method: str, url: str, token: str, body: bytes | None = None, content_type: str | None = None) -> dict[str, Any]:
    headers = {"Authorization": f"Bearer {token}"}
    if content_type:
        headers["Content-Type"] = content_type
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            err_json = json.loads(raw)
        except Exception:
            err_json = {"success": False, "errors": [{"code": exc.code, "message": raw[:400]}]}
        err_json["_httpStatus"] = exc.code
        return err_json


def auto_detect_account_id(token: str, non_interactive: bool = False) -> str:
    res = _cf_json_request("GET", f"{CF_API_BASE}/accounts?per_page=10", token)
    accounts = res.get("result") if isinstance(res, dict) else None
    if not res.get("success") or not isinstance(accounts, list) or not accounts:
        raise RuntimeError(f"Failed to list Cloudflare accounts: {res.get('errors')}")
    if len(accounts) == 1 or non_interactive:
        return str(accounts[0]["id"])
    print("Multiple Cloudflare accounts found:")
    for idx, acc in enumerate(accounts):
        print(f"  [{idx}] {acc.get('name')} ({acc.get('id')})")
    choice = input("Select account index [0]: ").strip() or "0"
    return str(accounts[int(choice)]["id"])


def _err_text(res: dict[str, Any]) -> str:
    return json.dumps(res.get("errors") or res, ensure_ascii=False).lower()


def deploy_worker(
    token: str,
    account_id: str,
    script_name: str,
    worker_file: Path = DEFAULT_WORKER_FILE,
    main_module: str = "worker.js",
    enable_durable_object: bool = True,
) -> dict[str, Any]:
    """Deploy Worker script, auto-handling first-time SQLite DO migration vs already-migrated updates."""
    source = worker_file.read_bytes()
    url = f"{CF_API_BASE}/accounts/{account_id}/workers/scripts/{script_name}"

    if enable_durable_object:
        # Attempt 1: include new_sqlite_classes migration (succeeds on first v6 upgrade)
        body, content_type = build_worker_multipart(
            source,
            main_module=main_module,
            enable_durable_object=True,
            include_sqlite_migration=True,
        )
        res = _cf_json_request("PUT", url, token, body=body, content_type=content_type)
        if res.get("success"):
            res["_deployMode"] = "durable_object_sqlite_migrated"
            return res

        err = _err_text(res)
        # Attempt 2: class/tag already migrated -> re-upload keeping DO binding without re-running migration
        if any(k in err for k in ("already", "migration", "tag", "exists", "10074", "10075")):
            body2, ct2 = build_worker_multipart(
                source,
                main_module=main_module,
                enable_durable_object=True,
                include_sqlite_migration=False,
            )
            res2 = _cf_json_request("PUT", url, token, body=body2, content_type=ct2)
            if res2.get("success"):
                res2["_deployMode"] = "durable_object_sqlite_updated"
                return res2
            res = res2

        # Attempt 3: graceful KV-only fallback if account cannot attach DO yet
        body3, ct3 = build_worker_multipart(
            source,
            main_module=main_module,
            enable_durable_object=False,
            include_sqlite_migration=False,
        )
        res3 = _cf_json_request("PUT", url, token, body=body3, content_type=ct3)
        if res3.get("success"):
            res3["_deployMode"] = "kv_fallback"
            res3["_doNotice"] = res.get("errors")
            return res3
        return res

    body, content_type = build_worker_multipart(
        source,
        main_module=main_module,
        enable_durable_object=False,
        include_sqlite_migration=False,
    )
    res = _cf_json_request("PUT", url, token, body=body, content_type=content_type)
    if res.get("success"):
        res["_deployMode"] = "kv_only"
    return res


def verify_worker_health(worker_url: str) -> dict[str, Any]:
    url = worker_url.strip().rstrip("/") + "/health"
    req = urllib.request.Request(url, headers={"User-Agent": "OmniAgentDeploy/6.0"}, method="GET")
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def main(argv: list[str] | None = None) -> int:
    default_relay = lcap.get_secret("CAVERN_RELAY_URL") or load_profile_relay_url()
    default_worker = lcap.get_secret("CLOUDFLARE_WORKER_NAME") or infer_worker_name_from_url(default_relay)

    parser = argparse.ArgumentParser(description="Upload Cavernize/OmniAgent Worker v6 via Cloudflare API (SQLite DO + RELAY_KV preserved).")
    parser.add_argument("--file", type=Path, default=DEFAULT_WORKER_FILE, help="Path to worker JS file")
    parser.add_argument("--account-id", default=lcap.get_secret("CLOUDFLARE_ACCOUNT_ID"), help="Cloudflare Account ID (auto-detected if omitted)")
    parser.add_argument("--worker-name", default=default_worker, help="Existing Worker name (auto-detected from relay URL)")
    parser.add_argument("--token", default=lcap.get_secret("CLOUDFLARE_API_TOKEN"), help="Cloudflare API Token (Edit Cloudflare Workers)")
    parser.add_argument("--main-module", default="worker.js", help="Entry module filename inside Cloudflare Worker (default: worker.js)")
    parser.add_argument("--no-do", action="store_true", help="Deploy without attaching HubCoordinator Durable Object")
    parser.add_argument("--verify-url", default=default_relay, help="Optional Worker URL to check /health after deploy")
    args = parser.parse_args(argv)

    if not args.file.is_file():
        print(f"ERROR: Worker file not found: {args.file}", file=sys.stderr)
        return 2

    token = args.token.strip() or getpass.getpass("Cloudflare API Token (Edit Cloudflare Workers): ").strip()
    if not token:
        print("ERROR: Missing Cloudflare API Token.", file=sys.stderr)
        return 2

    account_id = args.account_id.strip() or auto_detect_account_id(token, non_interactive=not sys.stdin.isatty())
    worker_name = args.worker_name.strip() or (
        "cavernize-diag-relay" if not sys.stdin.isatty() else (input("Worker name [cavernize-diag-relay]: ").strip() or "cavernize-diag-relay")
    )

    print(f"[Deploy] Uploading {args.file.name} ({args.file.stat().st_size} bytes) -> {worker_name} (DO={'off' if args.no_do else 'HubCoordinator SQLite'}, keep_bindings={PRESERVED_BINDING_TYPES})...")
    res = deploy_worker(
        token,
        account_id,
        worker_name,
        args.file,
        main_module=args.main_module,
        enable_durable_object=not args.no_do,
    )
    if not res.get("success"):
        print(f"ERROR: Cloudflare API deploy failed: {json.dumps(res, indent=2)}", file=sys.stderr)
        return 1

    print(f"[Deploy] SUCCESS! Worker '{worker_name}' updated on Cloudflare (mode={res.get('_deployMode')}, RELAY_KV & secrets preserved).")
    if args.verify_url.strip():
        try:
            health = verify_worker_health(args.verify_url)
            print(f"[Health] {json.dumps(health)}")
        except Exception as exc:
            print(f"[Health] Warning: could not verify {args.verify_url}/health: {exc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
