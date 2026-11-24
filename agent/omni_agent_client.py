#!/usr/bin/env python3
"""OmniAgent v4 — AI Studio Full-Agent Controller CLI.

Connects AI Studio to:
  1. Termux / Debian PRoot Full-Agent Daemon (`--channel termux`)
  2. OmniAgent Android Companion App (`--channel android`)
  3. Cavernize Android Live Bridge v3 + Live C# Hot-Execution (`hot-exec`)
  4. Cloudflare Unified Hub v4 Multi-Agent Swarm Bus (`swarm-*`)
  5. Direct GitHub 2-Way Pull Sync (`pull-github`) even when phone daemon is offline!

Zero external pip dependencies (Python 3 stdlib only). Never writes secrets to disk.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import secrets
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

AGENT_DIR = Path(__file__).resolve().parent
REPO_ROOT = AGENT_DIR.parents[1] if AGENT_DIR.parent.name == ".agents" else AGENT_DIR.parent
_EMBEDDED_PLUGIN_SCRIPTS = REPO_ROOT / ".agents" / "plugins" / "dynamic-fractal-planner" / "scripts"
_FED_PLUGIN_SCRIPTS = Path(os.environ.get("CAVERN_FEDERATION_ROOT", "/tmp/cavern-federation")) / "fractal" / "scripts"
PLUGIN_SCRIPTS = _EMBEDDED_PLUGIN_SCRIPTS if _EMBEDDED_PLUGIN_SCRIPTS.is_dir() else _FED_PLUGIN_SCRIPTS
for p in (str(AGENT_DIR), str(PLUGIN_SCRIPTS)):
    if p not in sys.path:
        sys.path.insert(0, p)

import bridge_auth as bauth
import github_direct as ghdirect
import local_capabilities as lcap
import omni_protocol as proto


def _resolve_auth(args: argparse.Namespace, *, allow_knock: bool = False) -> tuple[str, str, str]:
    conn = bauth.resolve_connection(
        cli_url=getattr(args, "url", "") or "",
        cli_pair_secret=getattr(args, "pair_secret", "") or "",
        cli_admin_key=getattr(args, "admin_key", "") or "",
    )
    relay = conn["relayUrl"]
    secret = conn["pairSecret"]
    admin = conn["adminKey"]
    if relay and not secret and allow_knock and conn.get("knockId"):
        kr = bauth.request_smart_knock(
            relay,
            conn["knockId"],
            requested_scope="workspace",
            tier=2,
            duration_seconds=3600,
            reason="OmniAgent workspace auto-connect",
            admin_key=admin,
            persist_vault=True,
        )
        secret = str((kr.get("grant") or {}).get("pairSecret") or "").strip()
        if secret:
            secret = proto.normalize_pair_secret_hex(secret)
    if not relay or not secret:
        hint = (
            "Run `python3 -B .agents/scripts/omni_agent.py connect` (Smart Knock) "
            "or set CAVERN_BRIDGE_PAIR_SECRET in AI Studio Secrets."
        )
        raise SystemExit(f"ERROR: Missing relay URL or pairing secret. {hint}")
    return relay, secret, admin


def cmd_connect(args: argparse.Namespace) -> int:
    if args.clear_vault:
        removed = bauth.clear_vault()
        print(json.dumps({"ok": True, "vaultCleared": removed}, indent=2))
        return 0

    if args.set_profile:
        prof = bauth.save_public_profile(
            relay_url=args.url or None,
            knock_id=args.knock_id or None,
        )
        print(json.dumps({"ok": True, "profile": prof}, indent=2, ensure_ascii=False))
        return 0

    if args.save_session or args.save_pair:
        saved = bauth.save_vault(
            relay_url=args.url or None,
            pair_secret=args.save_pair or None,
            session_token=args.save_session or None,
            session_ttl_seconds=args.duration,
            session_tier=args.tier,
            admin_key=args.admin_key or None,
        )
        if args.save_pair and saved.get("knockId"):
            # Sync ONLY the non-secret double-hashed knockId to the public profile if requested
            if args.sync_public_profile:
                bauth.save_public_profile(relay_url=saved.get("relayUrl"), knock_id=saved["knockId"])
        print(
            json.dumps(
                {
                    "ok": True,
                    "vaultPath": str(bauth.LOCAL_VAULT_PATH.relative_to(REPO_ROOT)),
                    "relayUrl": saved.get("relayUrl", ""),
                    "hasPairSecret": bool(saved.get("pairSecret")),
                    "hasSessionToken": bool(saved.get("sessionToken")),
                    "publicKnockId": saved.get("knockId", ""),
                },
                indent=2,
                ensure_ascii=False,
            )
        )
        return 0

    conn = bauth.resolve_connection(
        cli_url=args.url or "",
        cli_pair_secret=args.pair_secret or "",
        cli_code=args.code or "",
        cli_admin_key=args.admin_key or "",
        cli_knock_id=args.knock_id or "",
    )

    if args.status_only:
        summary = {
            "ok": bool(conn["relayUrl"]),
            "relayUrl": conn["relayUrl"],
            "relaySource": conn["relaySource"],
            "hasPairSecret": bool(conn["pairSecret"]),
            "pairSource": conn["pairSource"],
            "hasActiveSessionToken": bool(conn["sessionToken"]),
            "sessionSource": conn["sessionSource"],
            "sessionRemainingSec": conn["sessionRemainingSec"],
            "hasPublicKnockId": bool(conn["knockId"]),
            "knockIdPreview": (conn["knockId"][:12] + "...") if conn["knockId"] else "",
            "device": conn["device"],
        }
        # Probe relay health + agent channels if paired
        if conn["relayUrl"]:
            st_h, h_resp = proto.http_json("GET", f"{conn['relayUrl']}/health", {"User-Agent": proto.USER_AGENT}, timeout=10)
            summary["relayHealth"] = h_resp if st_h == 200 else {"ok": False, "http": st_h}
        if conn["relayUrl"] and conn["pairSecret"]:
            headers = proto.derive_agent_headers(conn["pairSecret"], channel="termux", admin_key=conn["adminKey"])
            st_a, a_resp = proto.http_json("GET", f"{conn['relayUrl']}/v1/agent/status?all=1", headers, timeout=12)
            if st_a == 200 and a_resp.get("ok"):
                summary["channels"] = {
                    k: {"online": bool(v.get("online")), "device": v.get("device", ""), "mode": v.get("securityMode", "")}
                    for k, v in (a_resp.get("channels") or {}).items()
                }
        caps = lcap.inspect_capabilities()
        summary["localCapabilities"] = {
            "presentVars": [v["name"] for v in caps["presentVars"]],
            "missingExpectedVars": [m["name"] for m in caps["missingExpectedVars"]],
            "alert": caps["alert"],
            "routing": {
                k: v for k, v in caps["routing"].items() if k in ("githubPush", "githubCiInspect", "cloudflareWorkerDeploy", "omniAgentAuth")
            },
        }
        print(json.dumps(summary, indent=2, ensure_ascii=False))
        return 0

    if not conn["relayUrl"]:
        raise SystemExit("ERROR: No relayUrl found in CLI, Env, Vault, or .agents/bridge_profile.json.")

    if not args.force_knock and (conn["pairSecret"] or conn["sessionToken"]):
        print(
            json.dumps(
                {
                    "ok": True,
                    "mode": "pair_auto" if conn["pairSecret"] else "session_vault",
                    "relayUrl": conn["relayUrl"],
                    "source": conn["pairSource"] if conn["pairSecret"] else conn["sessionSource"],
                    "sessionRemainingSec": conn["sessionRemainingSec"],
                    "message": "Already connected via zero-paste credentials (no knock needed).",
                },
                indent=2,
                ensure_ascii=False,
            )
        )
        return 0

    if not conn["knockId"]:
        raise SystemExit(
            "ERROR: No pairSecret/sessionToken in vault and no public knockId in .agents/bridge_profile.json yet.\n"
            "Fix once: tap 'Copy Public Knock ID' in Cavernize/OmniAgent -> run `omni_agent.py connect --set-profile --knock-id <id>`, "
            "OR set CAVERN_BRIDGE_PAIR_SECRET once in AI Studio Secrets."
        )

    res = bauth.request_smart_knock(
        conn["relayUrl"],
        conn["knockId"],
        requested_scope=args.scope,
        tier=args.tier,
        duration_seconds=args.duration,
        reason=args.reason,
        requester="AI Studio",
        timeout_sec=args.timeout,
        admin_key=conn["adminKey"],
        persist_vault=True,
    )
    safe_out = {k: v for k, v in res.items() if k != "grant"}
    print(json.dumps(safe_out, indent=2, ensure_ascii=False))
    return 0


def send_agent_command(
    relay_url: str,
    pair_secret_hex: str,
    cmd: str,
    args_obj: dict[str, Any] | None = None,
    channel: str = "termux",
    admin_key: str = "",
    timeout_sec: int = 120,
) -> dict[str, Any]:
    headers = proto.derive_agent_headers(pair_secret_hex, channel=channel, admin_key=admin_key)
    req_id = f"r_{int(time.time())}_{secrets.token_hex(6)}"
    payload = {"cmd": cmd, "args": args_obj or {}, "utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    envelope = proto.encrypt_envelope(pair_secret_hex, f"cmd:{req_id}", payload)
    st, resp = proto.http_json(
        "POST",
        f"{relay_url}/v1/agent/cmd",
        headers,
        {"requestId": req_id, "envelope": envelope},
        timeout=30,
    )
    if st >= 400 or not resp.get("ok"):
        raise RuntimeError(f"Failed queueing agent command '{cmd}' on channel '{channel}' (HTTP {st}): {resp}")

    deadline = time.time() + max(5, timeout_sec)
    # Cloudflare Hub v6 uses SQLite Durable Objects (<50ms strong consistency, zero KV negative cache);
    # use fast adaptive polling backoff so Termux RPCs complete in ~0.6-1.5s instead of 3-5s+.
    poll_delays = (0.35, 0.45, 0.65, 0.9, 1.25, 1.6)
    attempt = 0
    while time.time() < deadline:
        delay = poll_delays[min(attempt, len(poll_delays) - 1)]
        time.sleep(min(delay, max(0.05, deadline - time.time())))
        attempt += 1
        try:
            st_r, res_r = proto.http_json(
                "GET",
                f"{relay_url}/v1/agent/result?requestId={urllib.parse.quote(req_id)}",
                headers,
                timeout=20,
            )
        except (TimeoutError, OSError):
            continue
        if st_r == 200 and res_r.get("ok") and res_r.get("ready"):
            raw_res = res_r["result"]
            return proto.decrypt_envelope(pair_secret_hex, f"res:{req_id}", raw_res["envelope"])
    raise TimeoutError(f"Timed out waiting {timeout_sec}s for channel '{channel}' to answer '{cmd}'.")


def pick_channel(relay_url: str, pair_secret_hex: str, preferred: str, admin_key: str = "") -> str:
    if preferred and preferred != "auto":
        return preferred
    headers = proto.derive_agent_headers(pair_secret_hex, channel="termux", admin_key=admin_key)
    st, resp = proto.http_json("GET", f"{relay_url}/v1/agent/status?all=1", headers, timeout=15)
    if st == 200 and resp.get("ok"):
        chans = resp.get("channels") or {}
        for c in ("termux", "debian", "android"):
            if (chans.get(c) or {}).get("online"):
                return c
    return "termux"


def cmd_push_workspace(args: argparse.Namespace) -> int:
    repo_root = Path(args.workspace or REPO_ROOT).resolve()
    explicit_files: list[str] = list(args.files or [])
    deleted_files: list[str] = list(args.delete or [])

    via = getattr(args, "via", "auto") or "auto"
    local_gh_token = lcap.get_secret("GITHUB_TOKEN", aliases=("GH_TOKEN",))
    use_direct_github = (
        via == "github"
        or (via == "auto" and bool(local_gh_token) and bool(args.commit) and not args.no_push)
    )
    if use_direct_github:
        owner_repo = (getattr(args, "owner_repo", "") or "guumachadooo/Cavern").strip()
        print(f"Pushing directly to GitHub ({owner_repo}@{args.branch}) via local GITHUB_TOKEN...", file=sys.stderr)
        try:
            res = ghdirect.push_workspace_direct(
                repo_root,
                args.commit,
                owner_repo=owner_repo,
                branch=args.branch,
                token=local_gh_token,
                explicit_files=explicit_files or None,
                explicit_deletes=deleted_files or None,
                sync_deletes=bool(args.sync_deletes),
            )
            # Refresh local Flight Recorder / live-sync watcher baseline immediately so pushed files aren't re-mirrored.
            try:
                import flight_recorder as fr
                fr.save_local_baseline(fr.DEFAULT_LOCAL_DIR, repo_root)
            except Exception:
                pass
            # Keep Antigravity / Termux repo on phone 100% synced whenever online!
            # Safely reconciles any unmodified live-mirrored files before git pull --rebase (never clobbers manual phone edits).
            if not getattr(args, "no_sync_termux", False):
                termux_sync: dict[str, Any] = {"attempted": False}
                try:
                    conn = bauth.resolve_connection(
                        cli_url=getattr(args, "url", "") or "",
                        cli_pair_secret=getattr(args, "pair_secret", "") or "",
                        cli_admin_key=getattr(args, "admin_key", "") or "",
                    )
                    if conn["relayUrl"] and conn["pairSecret"] and _is_channel_online(conn["relayUrl"], conn["pairSecret"], "termux", conn["adminKey"]):
                        termux_sync["attempted"] = True
                        import flight_recorder as fr
                        script_b64 = base64.b64encode(
                            fr.build_termux_reconcile_pull_script(args.remote, args.branch).encode("utf-8")
                        ).decode("ascii")
                        reconcile_cmd = f"python3 -B -c \"import base64; exec(base64.b64decode('{script_b64}').decode('utf-8'))\""
                        pull_res = send_agent_command(
                            conn["relayUrl"],
                            conn["pairSecret"],
                            "shell_exec",
                            {"command": reconcile_cmd, "cwd": args.repo or "", "timeoutSec": 90},
                            channel="termux",
                            admin_key=conn["adminKey"],
                            timeout_sec=100,
                        )
                        parsed_sync = None
                        if pull_res.get("ok"):
                            lines = [ln.strip() for ln in str(pull_res.get("stdout") or "").splitlines() if ln.strip()]
                            if lines:
                                try:
                                    parsed_sync = json.loads(lines[-1])
                                except Exception:
                                    parsed_sync = None
                        if isinstance(parsed_sync, dict):
                            termux_sync["ok"] = bool(parsed_sync.get("ok"))
                            termux_sync["head"] = parsed_sync.get("head", "")
                            if parsed_sync.get("reconciled"):
                                termux_sync["reconciledMirrored"] = parsed_sync["reconciled"]
                            if parsed_sync.get("preservedManual"):
                                termux_sync["preservedManual"] = parsed_sync["preservedManual"]
                            if not parsed_sync.get("ok"):
                                termux_sync["error"] = parsed_sync.get("stderr") or "git_pull failed"
                        else:
                            # Fallback to legacy git_pull command
                            legacy = send_agent_command(
                                conn["relayUrl"],
                                conn["pairSecret"],
                                "git_pull",
                                {"repo": args.repo or "", "remote": args.remote, "branch": args.branch},
                                channel="termux",
                                admin_key=conn["adminKey"],
                                timeout_sec=60,
                            )
                            termux_sync["ok"] = bool(legacy.get("ok"))
                            termux_sync["head"] = legacy.get("head", "")
                            if not legacy.get("ok"):
                                termux_sync["error"] = legacy.get("error") or legacy.get("stderr") or "git_pull failed"
                except Exception as exc:
                    termux_sync["ok"] = False
                    termux_sync["error"] = str(exc)
                res["termuxSync"] = termux_sync
            print(json.dumps(res, indent=2, ensure_ascii=False))
            return 0 if res.get("ok") else 1
        except RuntimeError as exc:
            if via == "github" or "HTTP 403" not in str(exc):
                raise
            print(
                f"WARN: Local GITHUB_TOKEN lacks repository Contents:Write permission ({exc}). "
                "Falling back automatically to OmniAgent phone git push...",
                file=sys.stderr,
            )

    if (
        via == "auto"
        and (repo_root / ".git").exists()
        and not local_gh_token
        and any(os.environ.get(k, "").strip() for k in ("ANTIGRAVITY_AGENT", "ANTIGRAVITY_CONVERSATION_ID"))
    ):
        msg = "Local Antigravity host has native .git repo; run `git commit && git push` directly (or pass `--via phone` to force relay push)."
        print(f"INFO: {msg}", file=sys.stderr)
        print(json.dumps({"ok": False, "host": "antigravity", "routing": "native_git", "message": msg}, indent=2))
        return 1

    relay, secret, admin = _resolve_auth(args, allow_knock=True)
    channel = pick_channel(relay, secret, args.channel, admin)

    if not explicit_files:
        # Ask phone repo for its exact manifest so we only transmit changed/added/deleted files!
        print(f"Fetching remote manifest from channel '{channel}'...", file=sys.stderr)
        m_res = send_agent_command(relay, secret, "git_manifest", {"repo": args.repo or ""}, channel=channel, admin_key=admin, timeout_sec=90)
        if not m_res.get("ok"):
            print(json.dumps(m_res, indent=2))
            return 1
        if m_res.get("manifestBlobId"):
            remote_manifest = proto.download_encrypted_blob(relay, secret, m_res["manifestBlobId"], admin_key=admin).get("manifest", {})
        else:
            remote_manifest = m_res.get("manifest") or {}
        local_manifest = proto.build_workspace_manifest(repo_root)
        for rel, meta in local_manifest.items():
            rem = remote_manifest.get(rel)
            if not rem or rem.get("sha256") != meta["sha256"]:
                explicit_files.append(rel)
        # If files were ALREADY live-mirrored to the phone repo by live_sync_watcher.py,
        # their working-tree SHA-256 matches local_manifest, so also include tracked live-mirrored
        # files when committing so git_commit_push stages & commits them cleanly!
        if args.commit:
            try:
                import flight_recorder as fr
                cp = fr.read_local_tier(fr.DEFAULT_LOCAL_DIR) or {}
                for rel in (cp.get("fileHashes") or {}).keys():
                    if rel in local_manifest and rel not in explicit_files:
                        explicit_files.append(rel)
                if args.sync_deletes:
                    for rel in (cp.get("deletedFiles") or []):
                        if rel not in local_manifest and rel not in deleted_files:
                            deleted_files.append(rel)
            except Exception:
                pass
        if args.sync_deletes:
            for rel in remote_manifest:
                if rel not in local_manifest and rel not in deleted_files:
                    deleted_files.append(rel)

    if not explicit_files and not deleted_files:
        print(json.dumps({"ok": True, "message": "Workspace already 100% identical to remote repo (0 changed files)."}, indent=2))
        return 0

    bundle = proto.pack_delta_bundle(repo_root, explicit_files, deleted_files)
    print(f"Packing delta bundle: {len(bundle['files'])} modified/added, {len(bundle['deleted'])} deleted...", file=sys.stderr)
    blob = proto.upload_encrypted_blob(relay, secret, bundle, admin_key=admin)
    print(f"Uploaded encrypted blob {blob['blobId']} ({blob['bytes']} bytes, {blob['totalChunks']} chunk(s)).", file=sys.stderr)

    target_cmd = "github_push_bundle" if channel == "android" else ("git_commit_push" if args.commit else "git_apply_bundle")
    res = send_agent_command(
        relay,
        secret,
        target_cmd,
        {
            "repo": args.repo or "",
            "blobId": blob["blobId"],
            "message": args.commit or "",
            "branch": args.branch,
            "remote": args.remote,
            "push": not args.no_push,
            "pullFirst": bool(args.pull_first),
            "allowDelete": bool(deleted_files),
        },
        channel=channel,
        admin_key=admin,
        timeout_sec=180,
    )
    if res.get("ok") and args.commit:
        try:
            import flight_recorder as fr
            fr.save_local_baseline(fr.DEFAULT_LOCAL_DIR, repo_root)
        except Exception:
            pass
    print(json.dumps(res, indent=2, ensure_ascii=False))
    return 0 if res.get("ok") else 1


def cmd_pull_workspace(args: argparse.Namespace) -> int:
    repo_root = Path(args.workspace or REPO_ROOT).resolve()
    if args.from_github:
        # Direct GitHub API pull (works even if phone is offline!)
        owner_repo = args.from_github.strip()
        branch = args.branch
        headers = {"User-Agent": proto.USER_AGENT, "Accept": "application/vnd.github+json"}
        st, tree_resp = proto.http_json(
            "GET",
            f"https://api.github.com/repos/{owner_repo}/git/trees/{urllib.parse.quote(branch)}?recursive=1",
            headers,
            timeout=45,
        )
        if st != 200 or "tree" not in tree_resp:
            print(json.dumps({"ok": False, "http": st, "error": tree_resp}, indent=2))
            return 1
        local_manifest = proto.build_workspace_manifest(repo_root)
        updated: list[str] = []
        target_paths = set(args.files or [])
        for node in tree_resp["tree"]:
            if node.get("type") != "blob":
                continue
            rel = str(node.get("path") or "")
            if target_paths and rel not in target_paths:
                continue
            if not proto.should_include_workspace_path(rel):
                continue
            if int(node.get("size") or 0) > 2 * 1024 * 1024:
                continue
            # Compare git blob sha1 against local file quickly before downloading
            full = repo_root / rel
            if full.is_file():
                raw_local = full.read_bytes()
                git_sha1 = hashlib.sha1(f"blob {len(raw_local)}\0".encode("ascii") + raw_local).hexdigest()
                if git_sha1 == node.get("sha"):
                    continue
            raw_url = f"https://raw.githubusercontent.com/{owner_repo}/{urllib.parse.quote(branch)}/{urllib.parse.quote(rel)}"
            req = urllib.request.Request(raw_url, headers={"User-Agent": proto.USER_AGENT})
            with urllib.request.urlopen(req, timeout=45) as resp:
                data = resp.read()
            full.parent.mkdir(parents=True, exist_ok=True)
            full.write_bytes(data)
            updated.append(rel)
        print(json.dumps({"ok": True, "source": f"github:{owner_repo}@{branch}", "updatedCount": len(updated), "updated": updated}, indent=2))
        return 0

    relay, secret, admin = _resolve_auth(args, allow_knock=True)
    channel = pick_channel(relay, secret, args.channel, admin)
    m_res = send_agent_command(relay, secret, "git_manifest", {"repo": args.repo or ""}, channel=channel, admin_key=admin, timeout_sec=90)
    if not m_res.get("ok"):
        print(json.dumps(m_res, indent=2))
        return 1
    remote_manifest = (
        proto.download_encrypted_blob(relay, secret, m_res["manifestBlobId"], admin_key=admin).get("manifest", {})
        if m_res.get("manifestBlobId")
        else (m_res.get("manifest") or {})
    )
    local_manifest = proto.build_workspace_manifest(repo_root)
    to_fetch: list[str] = list(args.files or [])
    if not to_fetch:
        for rel, rmeta in remote_manifest.items():
            lmeta = local_manifest.get(rel)
            if not lmeta or lmeta.get("sha256") != rmeta.get("sha256"):
                to_fetch.append(rel)
    if not to_fetch:
        print(json.dumps({"ok": True, "message": "AI Studio workspace already in sync with phone repo (0 files)."}, indent=2))
        return 0
    exp_res = send_agent_command(relay, secret, "file_export_bundle", {"repo": args.repo or "", "paths": to_fetch}, channel=channel, admin_key=admin, timeout_sec=120)
    if not exp_res.get("ok"):
        print(json.dumps(exp_res, indent=2))
        return 1
    bundle = proto.download_encrypted_blob(relay, secret, exp_res["blobId"], admin_key=admin)
    applied = proto.apply_delta_bundle(repo_root, bundle, allow_delete=False)
    print(json.dumps({"ok": True, "source": f"channel:{channel}", "head": m_res.get("head"), "applied": applied}, indent=2))
    return 0


def build_cavernize_cmd_payload(
    name: str,
    args_raw: str | dict[str, Any] = "{}",
    tier: int = 2,
    timeout_sec: int = 120,
) -> dict[str, Any]:
    """Build payload matching OmniAgentForegroundService.cs (`cmd`, JSON object `args`, int `tier`)."""
    if isinstance(args_raw, dict):
        parsed_args = args_raw
    else:
        raw_text = (args_raw or "{}").strip() or "{}"
        parsed = json.loads(raw_text)
        if not isinstance(parsed, dict):
            raise ValueError("cavernize-cmd --args must be a JSON object (e.g. '{\"command\":\"...\"}').")
        parsed_args = parsed
    return {
        "cmd": name,
        "args": parsed_args,
        "tier": int(tier),
        "timeoutSec": min(150, max(15, int(timeout_sec) - 10)),
    }


def _is_channel_online(relay: str, secret: str, channel: str, admin: str = "") -> bool:
    try:
        headers = proto.derive_agent_headers(secret, channel="termux", admin_key=admin)
        st, resp = proto.http_json("GET", f"{relay}/v1/agent/status?all=1", headers, timeout=10)
        if st == 200 and resp.get("ok"):
            ch = (resp.get("channels") or {}).get(channel) or {}
            return bool(ch.get("online"))
    except Exception:
        pass
    return False


def cmd_hot_exec(args: argparse.Namespace) -> int:
    import hot_probe_compiler as hpc

    relay, secret, admin = _resolve_auth(args, allow_knock=True)
    if args.file:
        src = Path(args.file).read_text(encoding="utf-8")
    elif args.code:
        src = args.code
    else:
        raise SystemExit("ERROR: Provide --file or --code for hot-exec.")

    # Default include_android=False to avoid CavernizeAndroid.TypeCheck assembly mismatch at runtime;
    # hot probes inspect CavernizeAndroid/Mono.Android via reflection unless --with-android-refs is explicit.
    include_android = bool(getattr(args, "with_android_refs", False)) and not bool(getattr(args, "no_android_refs", False))
    print(f"Compiling C# hot-probe with local .NET Roslyn (include_android={include_android})...", file=sys.stderr)
    comp = hpc.compile_hot_probe(src, probe_name=args.name, include_android=include_android)
    if not comp.get("ok"):
        print(json.dumps(comp, indent=2))
        return 1
    print(f"Compiled {comp['assemblyName']} ({comp['sizeBytes']} bytes) in {comp['elapsedMs']}ms.", file=sys.stderr)

    req_id = f"hot_{int(time.time())}_{secrets.token_hex(5)}"
    hot_args = hpc.build_signed_hot_exec_args(
        comp["dllBytes"],
        secret,
        req_id,
        entry_type=args.entry_type or "",
        args_json=args.args_json or "{}",
        timeout_sec=args.timeout,
    )

    if args.compile_only:
        out = {k: v for k, v in hot_args.items() if k != "dllBase64"}
        out["sizeBytes"] = comp["sizeBytes"]
        out["compileElapsedMs"] = comp["elapsedMs"]
        out["includeAndroid"] = include_android
        print(json.dumps(out, indent=2))
        return 0

    via = getattr(args, "via", "auto")
    code = (args.code_session or os.environ.get("CAVERN_SESSION_CODE") or "").strip()
    use_android_ipc = via == "android" or (via == "auto" and not code and _is_channel_online(relay, secret, "android", admin))
    if use_android_ipc:
        res = send_agent_command(
            relay,
            secret,
            "cavernize_cmd",
            build_cavernize_cmd_payload("hot_exec", hot_args, tier=2, timeout_sec=max(90, args.timeout + 30)),
            channel="android",
            admin_key=admin,
            timeout_sec=max(90, args.timeout + 30),
        )
        print(json.dumps(res, indent=2, ensure_ascii=False))
        return 0 if res.get("ok") else 1

    import live_bridge_client as lbc

    if not code:
        # Wake Cavernize Live Bridge v3 automatically if no active session code passed
        wake_res = lbc.wake_and_get_session(
            relay,
            secret,
            tier=2,
            duration_seconds=1800,
            reason=f"Live C# hot_exec ({args.name})",
            admin_key=admin,
            wait_seconds=120,
        )
        code = wake_res["sessionCode"]

    res = lbc.send_command_and_wait(
        relay,
        code,
        "hot_exec",
        args_obj=hot_args,
        admin_key=admin,
        timeout_seconds=args.timeout + 30,
    )
    print(json.dumps(res, indent=2, ensure_ascii=False))
    return 0 if res.get("ok") else 1


def cmd_swarm(args: argparse.Namespace) -> int:
    relay, secret, admin = _resolve_auth(args, allow_knock=True)
    headers = proto.derive_agent_headers(secret, channel="termux", admin_key=admin)
    act = args.swarm_action
    if act == "enqueue":
        st, resp = proto.http_json(
            "POST",
            f"{relay}/v1/swarm/enqueue",
            headers,
            {
                "role": args.role,
                "title": args.title,
                "createdBy": args.worker_id,
                "instructions": args.instructions,
            },
        )
    elif act == "list":
        q = f"?status={urllib.parse.quote(args.status)}" if args.status else ""
        st, resp = proto.http_json("GET", f"{relay}/v1/swarm/list{q}", headers)
    elif act == "claim":
        st, resp = proto.http_json(
            "POST",
            f"{relay}/v1/swarm/claim",
            headers,
            {"workerId": args.worker_id, "role": args.role, "taskId": args.task_id},
        )
    elif act == "complete":
        st, resp = proto.http_json(
            "POST",
            f"{relay}/v1/swarm/complete",
            headers,
            {"taskId": args.task_id, "workerId": args.worker_id, "ok": not args.failed, "summary": args.summary},
        )
    elif act == "task":
        st, resp = proto.http_json("GET", f"{relay}/v1/swarm/task?taskId={urllib.parse.quote(args.task_id)}", headers)
    elif act == "clear":
        q = "?doneOnly=1" if args.done_only else ""
        st, resp = proto.http_json("DELETE", f"{relay}/v1/swarm/clear{q}", headers)
    else:
        raise SystemExit(f"Unknown swarm action: {act}")
    print(json.dumps(resp, indent=2, ensure_ascii=False))
    return 0 if st < 400 and resp.get("ok") else 1


def _needs_apk_ci(paths: list[str]) -> bool:
    code_prefixes = ("Cavern/", "Cavern.Format/", "CavernSamples/", ".github/workflows/BuildAndroid.yml")
    code_exts = (".cs", ".csproj", ".c", ".cpp", ".h", ".xml", ".axml", ".ttf", ".so")
    for p in paths:
        norm = p.replace("\\", "/")
        if norm == ".github/workflows/BuildAndroid.yml":
            return True
        if norm.startswith(code_prefixes) and norm.lower().endswith(code_exts):
            return True
    return False


def cmd_ship(args: argparse.Namespace) -> int:
    """One-shot quota-saving ship pipeline: verify + file-budget + push + Termux sync + Flight Recorder close."""
    ws = Path(args.workspace or REPO_ROOT).resolve()
    import subprocess as sp

    if not args.no_verify:
        checks = [
            ("file_budget", [sys.executable, "-B", str(ws / ".agents/scripts/file_budget_guard.py"), "--check", "--warn", "850", "--fail", "920"]),
            ("verify_universal", [sys.executable, "-B", str(ws / ".agents/plugins/dynamic-fractal-planner/scripts/verify_universal.py"), "--all"]),
        ]
        for label, cmd in checks:
            r = sp.run(cmd, cwd=str(ws), capture_output=True, text=True, timeout=120, check=False)
            if r.returncode != 0:
                print(json.dumps({"ok": False, "stage": label, "stdout": r.stdout[-1500:], "stderr": r.stderr[-1500:]}, indent=2))
                return r.returncode

    owner_repo = args.owner_repo or "guumachadooo/Cavern"
    branch = args.branch or "master"
    diff = ghdirect.diff_workspace_against_github(ws, owner_repo=owner_repo, branch=branch, sync_deletes=bool(args.sync_deletes))
    touched = list(diff["changed"]) + list(diff["deleted"])
    commit_msg = args.commit.strip()
    if args.auto_skip_ci and "[skip ci]" not in commit_msg.lower() and not _needs_apk_ci(touched):
        commit_msg = f"{commit_msg} [skip ci]"

    push_ns = argparse.Namespace(
        workspace=str(ws),
        commit=commit_msg,
        via=args.via,
        owner_repo=owner_repo,
        branch=branch,
        remote="origin",
        no_push=False,
        no_sync_termux=bool(args.no_sync_termux),
        pull_first=False,
        sync_deletes=bool(args.sync_deletes),
        files=None,
        delete=None,
        url=args.url,
        pair_secret=args.pair_secret,
        admin_key=args.admin_key,
        channel=args.channel,
        repo=args.repo,
        timeout=args.timeout,
    )
    rc = cmd_push_workspace(push_ns)
    if rc != 0:
        return rc

    if not args.no_close_fr:
        fr_script = ws / ".agents/scripts/flight_recorder.py"
        if fr_script.is_file():
            fr_cmd = [sys.executable, "-B", str(fr_script), "close", "--note", commit_msg]
            if args.next_step:
                fr_cmd += ["--next", args.next_step]
            sp.run(fr_cmd, cwd=str(ws), check=False)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="OmniAgent v4 Full-Agent CLI (Antigravity 2 Controller).")
    parser.add_argument("--url", default="", help="Cloudflare Unified Hub v4 URL (or env CAVERN_RELAY_URL)")
    parser.add_argument("--pair-secret", default="", help="64-hex pairing key (or env CAVERN_BRIDGE_PAIR_SECRET)")
    parser.add_argument("--admin-key", default="", help="Optional Worker X-Admin-Key")
    parser.add_argument("--channel", default="auto", help="Target channel: auto|termux|android|debian")
    parser.add_argument("--repo", default="", help="Override remote git repo path")
    parser.add_argument("--timeout", type=int, default=120)

    sub = parser.add_subparsers(dest="action", required=True)

    sub.add_parser("status", help="Check online status of all agent channels (termux, android, debian)")
    sub.add_parser("ping", help="Ping target agent channel and print detected tools/environment")

    p_cmd = sub.add_parser("cmd", help="Send any arbitrary OmniAgent command + JSON args")
    p_cmd.add_argument("name")
    p_cmd.add_argument("--args", default="{}")

    p_sh = sub.add_parser("shell", help="Run shell command on phone Termux or Debian PRoot")
    p_sh.add_argument("command")
    p_sh.add_argument("--debian", action="store_true", help="Execute inside Debian PRoot")
    p_sh.add_argument("--cwd", default="")

    sub.add_parser("capabilities", help="Inspect local AI Studio capabilities, secret presence & missing expected env alerts")

    p_ci = sub.add_parser("ci-status", help="Inspect latest GitHub Actions CI runs directly via local GITHUB_TOKEN")
    p_ci.add_argument("--owner-repo", default="guumachadooo/Cavern")
    p_ci.add_argument("--workflow", default="BuildAndroid.yml")
    p_ci.add_argument("--limit", type=int, default=5)

    p_sp = sub.add_parser("sync-parity", help="Verify exact 1:1 AI Studio <-> GitHub workspace parity (and optionally clean build/cache artifacts)")
    p_sp.add_argument("--workspace", default=str(REPO_ROOT))
    p_sp.add_argument("--owner-repo", default="guumachadooo/Cavern")
    p_sp.add_argument("--branch", default="master")
    p_sp.add_argument("--clean", action="store_true", help="Remove ephemeral caches (__pycache__, bin/, obj/, .metrics.jsonl, .model_modes.json, legacy vault) before checking parity")

    p_push = sub.add_parser("push-workspace", help="Diff AI Studio workspace -> direct GitHub API push (local-first) or phone git commit & push")
    p_push.add_argument("--workspace", default=str(REPO_ROOT))
    p_push.add_argument("--commit", default="", help="Commit message (if omitted, only applies files on phone without commit/push)")
    p_push.add_argument("--via", choices=("auto", "github", "phone"), default="auto", help="auto prefers local GITHUB_TOKEN direct push + Termux git_pull sync")
    p_push.add_argument("--owner-repo", default="guumachadooo/Cavern")
    p_push.add_argument("--branch", default="master")
    p_push.add_argument("--remote", default="origin")
    p_push.add_argument("--no-push", action="store_true")
    p_push.add_argument("--no-sync-termux", action="store_true", help="Skip automatic git_pull on phone Termux after direct GitHub push")
    p_push.add_argument("--pull-first", action="store_true")
    p_push.add_argument("--sync-deletes", action="store_true")
    p_push.add_argument("--files", nargs="*")
    p_push.add_argument("--delete", nargs="*")

    p_pull = sub.add_parser("pull-workspace", help="2-way pull from GitHub or phone repo into AI Studio workspace")
    p_pull.add_argument("--workspace", default=str(REPO_ROOT))
    p_pull.add_argument("--from-github", default="", help="Pull directly from GitHub owner/repo (e.g. guumachadooo/Cavern)")
    p_pull.add_argument("--branch", default="master")
    p_pull.add_argument("--files", nargs="*")

    p_apk = sub.add_parser("install-apk", help="Download & install APK on phone via ADB/Shizuku/Android installer")
    p_apk.add_argument("--apk-url", required=True)
    p_apk.add_argument("--dest", default="/sdcard/Download/Cavernize-Android.apk")
    p_apk.add_argument("--method", default="auto", choices=("auto", "adb", "shizuku", "open"))

    p_lc = sub.add_parser("logcat", help="Tail live Android logcat via ADB/Shizuku")
    p_lc.add_argument("--lines", type=int, default=120)
    p_lc.add_argument("--filter", default="Cavernize")

    sub.add_parser("ag-discover", help="Discover local Antigravity Debian brain/conversations/scratch structure")

    p_ags = sub.add_parser("ag-sync", help="Write compact token-diet session mirror into Antigravity Debian")
    p_ags.add_argument("--title", required=True)
    p_ags.add_argument("--summary", required=True)
    p_ags.add_argument("--commit", default="")
    p_ags.add_argument("--next", default="")
    p_ags.add_argument("--session-id", default="")
    p_ags.add_argument("--files", nargs="*", default=[])

    p_hot = sub.add_parser("hot-exec", help="Compile C# probe locally & run LIVE in Cavernize RAM (zero APK rebuild!)")
    p_hot.add_argument("--file", default="")
    p_hot.add_argument("--code", default="")
    p_hot.add_argument("--name", default="HotProbe")
    p_hot.add_argument("--entry-type", default="")
    p_hot.add_argument("--args-json", default="{}")
    p_hot.add_argument("--code-session", default="", help="Optional existing Live Bridge CAV-... code")
    p_hot.add_argument("--via", choices=("auto", "android", "bridge"), default="auto", help="Transport: auto prefers online android IPC channel")
    p_hot.add_argument("--with-android-refs", action="store_true", help="Opt-in Android TypeCheck refs (default False; use reflection on device)")
    p_hot.add_argument("--no-android-refs", action="store_true", help="Compat flag (already default)")
    p_hot.add_argument("--compile-only", action="store_true")

    p_sw = sub.add_parser("swarm", help="Zero-API-cost Multi-Agent Swarm Task Bus (/v1/swarm/*)")
    p_sw.add_argument("swarm_action", choices=("enqueue", "list", "claim", "complete", "task", "clear"))
    p_sw.add_argument("--role", default="worker")
    p_sw.add_argument("--title", default="Task")
    p_sw.add_argument("--instructions", default="")
    p_sw.add_argument("--worker-id", default="aistudio-architect")
    p_sw.add_argument("--task-id", default="")
    p_sw.add_argument("--summary", default="")
    p_sw.add_argument("--status", default="")
    p_sw.add_argument("--failed", action="store_true")
    p_sw.add_argument("--done-only", action="store_true")

    p_conn = sub.add_parser("connect", help="OmniConnect v5 zero-paste auto-connect, vault & Smart Knock manager")
    p_conn.add_argument("--status-only", action="store_true", help="Inspect active auto-connect sources & relay health without knocking")
    p_conn.add_argument("--force-knock", action="store_true", help="Force a fresh P-256 ECDH Smart Knock even if credentials exist")
    p_conn.add_argument("--scope", default="workspace", choices=("session", "workspace"), help="Requested Smart Knock scope")
    p_conn.add_argument("--tier", type=int, default=2, choices=(0, 1, 2))
    p_conn.add_argument("--duration", type=int, default=3600)
    p_conn.add_argument("--reason", default="AI Studio auto-connect")
    p_conn.add_argument("--code", default="", help="Optional active Live Bridge session token")
    p_conn.add_argument("--knock-id", default="", help="Public 64-hex double-hashed Knock ID")
    p_conn.add_argument("--set-profile", action="store_true", help="Update public non-secret .agents/bridge_profile.json")
    p_conn.add_argument("--save-session", default="", help="Cache an active ephemeral CAV-... session token in local gitignored vault")
    p_conn.add_argument("--save-pair", default="", help="Cache pair secret in local gitignored vault (0600, never synced to Git)")
    p_conn.add_argument("--sync-public-profile", action="store_true", help="Also write derived public knockId (non-secret) into .agents/bridge_profile.json")
    p_conn.add_argument("--clear-vault", action="store_true", help="Wipe local gitignored .agents/.bridge_vault.json")

    p_cf = sub.add_parser("deploy-worker", help="Deploy Cloudflare Unified Hub Worker v6 (via local CF API token OR E2E via OmniAgent phone)")
    p_cf.add_argument("--file", default=str(REPO_ROOT / ".agents" / "docs" / "cloudflare_live_relay_worker.js"))
    p_cf.add_argument("--worker-name", default="")
    p_cf.add_argument("--no-do", action="store_true", help="Deploy without attaching SQLite Durable Object HUB_DO")
    p_cf.add_argument("--via", choices=("auto", "direct", "phone"), default="auto")

    p_cav = sub.add_parser("cavernize-cmd", help="Forward any Cavernize Live Bridge command via OmniAgent local signature-verified IPC")
    p_cav.add_argument("--name", required=True, help="Cavernize Live Bridge command (ping, quick_check, ultra_check, acoustic_audit, ui_tree, screenshot_ui, hot_exec, log_tail, etc.)")
    p_cav.add_argument("--args", default="{}", help="JSON object of arguments")
    p_cav.add_argument("--tier", type=int, default=2, choices=(0, 1, 2))

    p_cp = sub.add_parser("checkpoint", help="Anti-Cancellation 3-tier Session Flight Recorder (forwards to flight_recorder.py)")
    p_cp.add_argument("fr_args", nargs=argparse.REMAINDER, help="Arguments passed directly to flight_recorder.py (init|save|recover|restore|status|close)")

    p_ship = sub.add_parser("ship", help="1-shot quota-saving pipeline: verify + file-budget + auto [skip ci] push + Termux sync + Flight Recorder close")
    p_ship.add_argument("--commit", required=True, help="Commit message")
    p_ship.add_argument("--next", dest="next_step", default="", help="Next step saved to Flight Recorder & Antigravity handoff")
    p_ship.add_argument("--workspace", default=str(REPO_ROOT))
    p_ship.add_argument("--owner-repo", default="guumachadooo/Cavern")
    p_ship.add_argument("--branch", default="master")
    p_ship.add_argument("--via", choices=("auto", "github", "phone"), default="auto")
    p_ship.add_argument("--sync-deletes", action="store_true")
    p_ship.add_argument("--auto-skip-ci", action="store_true", help="Automatically append [skip ci] if no Android C#/C++/BuildAndroid.yml files changed")
    p_ship.add_argument("--no-verify", action="store_true")
    p_ship.add_argument("--no-sync-termux", action="store_true")
    p_ship.add_argument("--no-close-fr", action="store_true")

    p_rep = sub.add_parser("repos", help="3-Repo Federation manager (status|hydrate|diff|push|sync-mirrors across Cavern, OmniAgent & Fractal)")
    p_rep.add_argument("fed_args", nargs=argparse.REMAINDER, help="Arguments passed to repo_federation.py")

    args = parser.parse_args()

    if args.action == "repos":
        import repo_federation as rfed
        return rfed.main(list(args.fed_args or []))

    if args.action == "checkpoint":
        import flight_recorder as fr
        forwarded: list[str] = []
        if args.url:
            forwarded += ["--url", args.url]
        if args.pair_secret:
            forwarded += ["--pair-secret", args.pair_secret]
        if args.admin_key:
            forwarded += ["--admin-key", args.admin_key]
        forwarded += list(args.fr_args or [])
        return fr.main(forwarded)

    if args.action == "capabilities":
        report = lcap.inspect_capabilities()
        try:
            import host_context  # noqa: E402

            hid = str((report.get("hostProfile") or {}).get("hostId") or "local-ide")
            report["baton"] = host_context.detect_baton_state(REPO_ROOT, hid)
            aud = host_context.audit_host_matrix(REPO_ROOT)
            report["hostMatrixCoverage"] = {
                "present": aud.get("present", False),
                "ok": aud.get("ok", True),
                "entries": aud.get("entry_count", 0),
                "unclassifiedCount": len(aud.get("unclassified") or []),
                "staleCount": len(aud.get("stale") or []),
            }
        except Exception:
            pass
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return 0
    if args.action == "ci-status":
        res = ghdirect.fetch_ci_runs(owner_repo=args.owner_repo, workflow=args.workflow, limit=args.limit)
        print(json.dumps(res, indent=2, ensure_ascii=False))
        return 0 if res.get("ok") else 1
    if args.action == "sync-parity":
        res = ghdirect.verify_github_parity(
            Path(args.workspace or REPO_ROOT),
            owner_repo=args.owner_repo,
            branch=args.branch,
            clean=bool(args.clean),
        )
        print(json.dumps(res, indent=2, ensure_ascii=False))
        return 0 if res.get("ok") else 1
    if args.action == "connect":
        return cmd_connect(args)
    if args.action == "pull-workspace":
        return cmd_pull_workspace(args)
    if args.action == "push-workspace":
        return cmd_push_workspace(args)
    if args.action == "ship":
        return cmd_ship(args)
    if args.action == "hot-exec":
        return cmd_hot_exec(args)
    if args.action == "swarm":
        return cmd_swarm(args)

    if args.action == "deploy-worker":
        worker_file = Path(args.file).expanduser().resolve()
        if not worker_file.is_file():
            print(json.dumps({"ok": False, "error": f"Worker file not found: {worker_file}"}))
            return 2
        local_cf_token = lcap.get_secret("CLOUDFLARE_API_TOKEN")
        if args.via == "direct" or (args.via == "auto" and local_cf_token):
            import deploy_worker as cf_deploy
            d_argv = ["--file", str(worker_file)]
            if args.worker_name:
                d_argv += ["--worker-name", args.worker_name]
            if args.no_do:
                d_argv.append("--no-do")
            return cf_deploy.main(d_argv)
        relay, secret, admin = _resolve_auth(args, allow_knock=True)
        script_text = worker_file.read_text(encoding="utf-8")
        blob = proto.upload_encrypted_blob(
            relay,
            secret,
            {"script": script_text, "file": worker_file.name},
            admin_key=admin,
            meta={"kind": "cloudflare_worker_js"},
        )
        res = send_agent_command(
            relay,
            secret,
            "cf_deploy_worker",
            {
                "blobId": blob["blobId"],
                "workerName": args.worker_name,
                "enableDurableObject": not args.no_do,
            },
            channel="android",
            admin_key=admin,
            timeout_sec=max(90, args.timeout),
        )
        print(json.dumps(res, indent=2, ensure_ascii=False))
        return 0 if res.get("ok") else 1

    relay, secret, admin = _resolve_auth(args, allow_knock=True)
    if args.action == "status":
        headers = proto.derive_agent_headers(secret, channel="termux", admin_key=admin)
        st, resp = proto.http_json("GET", f"{relay}/v1/agent/status?all=1", headers, timeout=15)
        print(json.dumps(resp, indent=2, ensure_ascii=False))
        return 0 if st == 200 and resp.get("ok") else 1

    if args.action == "cavernize-cmd":
        payload = build_cavernize_cmd_payload(args.name, args.args, args.tier, args.timeout)
        res = send_agent_command(
            relay,
            secret,
            "cavernize_cmd",
            payload,
            channel="android",
            admin_key=admin,
            timeout_sec=max(60, args.timeout),
        )
        print(json.dumps(res, indent=2, ensure_ascii=False))
        return 0 if res.get("ok") else 1

    channel = pick_channel(relay, secret, args.channel, admin)
    if args.action == "ping":
        res = send_agent_command(relay, secret, "ping", {"repo": args.repo}, channel=channel, admin_key=admin, timeout_sec=args.timeout)
    elif args.action == "cmd":
        res = send_agent_command(relay, secret, args.name, json.loads(args.args), channel=channel, admin_key=admin, timeout_sec=args.timeout)
    elif args.action == "shell":
        res = send_agent_command(
            relay, secret, "shell_exec",
            {"command": args.command, "debian": args.debian, "cwd": args.cwd or args.repo, "timeoutSec": args.timeout},
            channel=channel, admin_key=admin, timeout_sec=args.timeout + 15,
        )
    elif args.action == "install-apk":
        cmd_name = "install_apk_url" if channel == "android" else "install_apk"
        res = send_agent_command(
            relay, secret, cmd_name,
            {"url": args.apk_url, "dest": args.dest, "method": args.method},
            channel=channel, admin_key=admin, timeout_sec=max(args.timeout, 180),
        )
    elif args.action == "logcat":
        res = send_agent_command(
            relay, secret, "logcat_tail",
            {"lines": args.lines, "filter": args.filter},
            channel=channel, admin_key=admin, timeout_sec=max(120, args.timeout),
        )
    elif args.action == "ag-discover":
        res = send_agent_command(relay, secret, "ag_discover", {}, channel=channel, admin_key=admin, timeout_sec=max(120, args.timeout))
    elif args.action == "ag-sync":
        res = send_agent_command(
            relay, secret, "ag_sync_session",
            {
                "sessionId": args.session_id,
                "title": args.title,
                "summary": args.summary,
                "commit": args.commit,
                "next": args.next,
                "files": args.files,
                "repo": args.repo,
            },
            channel=channel, admin_key=admin, timeout_sec=max(120, args.timeout),
        )
    else:
        raise SystemExit(f"Unsupported action: {args.action}")

    print(json.dumps(res, indent=2, ensure_ascii=False))
    return 0 if res.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
