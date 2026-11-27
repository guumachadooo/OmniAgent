#!/usr/bin/env python3
"""Local-First Capability Discovery & AI Studio Runtime Secret Resolver.

AI Studio injects user Secrets (e.g. GITHUB_TOKEN, CLOUDFLARE_API_TOKEN,
CAVERN_BRIDGE_PAIR_SECRET, CAVERN_RELAY_URL) into the preview dev-server
process (`node .agents/preview/server.mjs`), rather than into tool subshells.

This zero-dependency stdlib module:
  1. Resolves whitelisted runtime variables strictly in memory across:
     os.environ -> /tmp/.cavern_runtime_env.json (chmod 0600, written by server.mjs)
     -> live `server.mjs` process environment -> gitignored .agents/.bridge_vault.json.
  2. NEVER prints, logs, or writes secret values into repository files.
  3. Compares live availability against `.agents/env_manifest.json` so any lost
     expected variable is immediately flagged (`missingExpectedVars`) and the AI
     can ask the user to restore it.
  4. Reports a clean Local-First vs. Phone-Fallback routing matrix so AI Studio
     performs GitHub push/CI & Cloudflare deploys directly whenever possible,
     reserving OmniAgent for phone-only actions (S25U hot_exec, Cavernize IPC,
     APK install, logcat, and Antigravity/Termux sync).
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any

AGENT_DIR = Path(__file__).resolve().parent
REPO_ROOT = AGENT_DIR.parents[1] if AGENT_DIR.parent.name == ".agents" else AGENT_DIR.parent
ENV_MANIFEST_PATH = REPO_ROOT / ".agents" / "env_manifest.json"
RUNTIME_ENV_FILE = Path("/tmp/.cavern_runtime_env.json")
LEGACY_WORKSPACE_VAULT_PATH = REPO_ROOT / ".agents" / ".bridge_vault.json"
LOCAL_VAULT_PATH = Path("/tmp/.cavern_bridge_vault.json")
OMNI_LOCAL_CONFIG_PATH = Path.home() / ".omni_agent" / "config.json"

WHITELISTED_KEYS: tuple[str, ...] = (
    "GITHUB_TOKEN",
    "GH_TOKEN",
    "CLOUDFLARE_API_TOKEN",
    "CLOUDFLARE_ACCOUNT_ID",
    "CLOUDFLARE_WORKER_NAME",
    "CAVERN_BRIDGE_PAIR_SECRET",
    "CAVERN_RELAY_URL",
    "CAVERN_ADMIN_KEY",
    "CAVERN_KNOCK_ID",
)

_VAULT_FIELD_MAP: dict[str, str] = {
    "CAVERN_BRIDGE_PAIR_SECRET": "pairSecret",
    "CAVERN_RELAY_URL": "relayUrl",
    "CAVERN_ADMIN_KEY": "adminKey",
    "CAVERN_KNOCK_ID": "knockId",
}

_OMNI_CONFIG_FIELD_MAP: dict[str, str] = {
    "CAVERN_BRIDGE_PAIR_SECRET": "pair_secret",
    "CAVERN_RELAY_URL": "relay_url",
}


def _read_json_safe(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _read_dev_server_proc_env() -> dict[str, str]:
    """Read ONLY whitelisted keys from the running local preview server process."""
    found: dict[str, str] = {}
    proc_root = Path("/proc")
    if not proc_root.is_dir():
        return found
    allowed = set(WHITELISTED_KEYS)
    for proc in proc_root.iterdir():
        if not proc.name.isdigit():
            continue
        try:
            cmd = (proc / "cmdline").read_bytes().decode("utf-8", errors="ignore")
            if "server.mjs" not in cmd:
                continue
            raw = (proc / "environ").read_bytes().decode("utf-8", errors="ignore")
            for entry in raw.split("\0"):
                if "=" not in entry:
                    continue
                k, v = entry.split("=", 1)
                if k in allowed and v.strip():
                    found[k] = v.strip()
            if found:
                break
        except Exception:
            continue
    return found


def resolve_secret_with_source(
    name: str,
    *,
    aliases: tuple[str, ...] = (),
    runtime_file: Path = RUNTIME_ENV_FILE,
    vault_path: Path = LOCAL_VAULT_PATH,
    allow_proc_scan: bool = True,
    omni_config_path: Path | None = None,
) -> tuple[str, str]:
    """Return `(value, source)` for a whitelisted secret without ever logging value."""
    keys = (name,) + tuple(aliases)

    # 1. Direct process environment
    for k in keys:
        val = os.environ.get(k, "").strip()
        if val:
            return val, f"env:{k}"

    # 2. Private runtime env bridge written by local preview server (/tmp, mode 0600)
    rt = _read_json_safe(runtime_file)
    for k in keys:
        val = str(rt.get(k) or "").strip()
        if val:
            return val, f"runtime_file:{k}"

    # 3. Running preview server process environment (in-memory only)
    if allow_proc_scan:
        proc_env = _read_dev_server_proc_env()
        for k in keys:
            val = proc_env.get(k, "").strip()
            if val:
                return val, f"dev_server_proc:{k}"

    # 4. Private /tmp vault (fallback to legacy .agents/.bridge_vault.json if present)
    vault = _read_json_safe(vault_path)
    is_default_vault = Path(vault_path).resolve() == LOCAL_VAULT_PATH.resolve()
    if not vault and is_default_vault:
        vault = _read_json_safe(LEGACY_WORKSPACE_VAULT_PATH)
    for k in keys:
        vf = _VAULT_FIELD_MAP.get(k)
        if vf:
            val = str(vault.get(vf) or "").strip()
            if val:
                return val, f"vault:{vf}"

    # 5. Local Termux/Debian OmniAgent daemon config (~/.omni_agent/config.json, read-only)
    cfg_path = omni_config_path if omni_config_path is not None else (OMNI_LOCAL_CONFIG_PATH if is_default_vault else None)
    if cfg_path is not None:
        omni_cfg = _read_json_safe(cfg_path)
        for k in keys:
            cf = _OMNI_CONFIG_FIELD_MAP.get(k)
            if cf:
                val = str(omni_cfg.get(cf) or "").strip()
                if val:
                    return val, f"omni_local_config:{cf}"

    return "", "none"


def get_secret(
    name: str,
    *,
    aliases: tuple[str, ...] = (),
    runtime_file: Path = RUNTIME_ENV_FILE,
    vault_path: Path = LOCAL_VAULT_PATH,
    allow_proc_scan: bool = True,
    omni_config_path: Path | None = None,
) -> str:
    val, _ = resolve_secret_with_source(
        name,
        aliases=aliases,
        runtime_file=runtime_file,
        vault_path=vault_path,
        allow_proc_scan=allow_proc_scan,
        omni_config_path=omni_config_path,
    )
    return val


def _load_host_context_module() -> Any:
    candidates = (
        REPO_ROOT / ".agents" / "plugins" / "dynamic-fractal-planner" / "scripts",
        Path(os.environ.get("CAVERN_FEDERATION_ROOT", "/tmp/cavern-federation")) / "fractal" / "scripts",
        Path.home() / ".gemini" / "config" / "plugins" / "dynamic-fractal-planner" / "scripts",
    )
    for c in candidates:
        if (c / "host_context.py").is_file():
            if str(c) not in sys.path:
                sys.path.insert(0, str(c))
            try:
                import host_context  # noqa: E402
                return host_context
            except Exception:
                continue
    return None


def detect_runtime_host(
    *,
    runtime_file: Path = RUNTIME_ENV_FILE,
    allow_proc_scan: bool = True,
    host_override: str = "",
) -> dict[str, Any]:
    """Detect effective runtime host while preserving isolated AI Studio unit-test fixtures."""
    if host_override.strip():
        hid = host_override.strip()
        return {"host_id": hid, "reason": "arg:host_override", "is_cloud": hid == "ai-studio-cloud"}
    if Path(runtime_file) != RUNTIME_ENV_FILE and Path(runtime_file).is_file():
        return {"host_id": "ai-studio-cloud", "reason": "isolated_runtime_file", "is_cloud": True}

    hc = _load_host_context_module()
    if hc is not None:
        return hc.detect_host(REPO_ROOT)

    if any(os.environ.get(k, "").strip() for k in ("ANTIGRAVITY_AGENT", "ANTIGRAVITY_CONVERSATION_ID")):
        hid = "antigravity-termux" if (os.environ.get("PROOT_L2S_DIR") or Path("/sdcard").is_dir()) else "antigravity-local"
        return {"host_id": hid, "reason": "env:ANTIGRAVITY", "is_cloud": False}
    if os.environ.get("GITHUB_ACTIONS", "").lower() == "true" or os.environ.get("CI", "").lower() in ("1", "true"):
        return {"host_id": "ci-runner", "reason": "env:CI", "is_cloud": False}
    if str(REPO_ROOT).startswith("/app/applet") or RUNTIME_ENV_FILE.is_file():
        return {"host_id": "ai-studio-cloud", "reason": "fallback:cloud", "is_cloud": True}
    return {"host_id": "local-ide", "reason": "fallback:local-ide", "is_cloud": False}


def load_env_manifest(manifest_path: Path = ENV_MANIFEST_PATH) -> dict[str, Any]:
    data = _read_json_safe(manifest_path)
    if not data:
        return {"schema": 1, "ownerRepo": "guumachadooo/Cavern", "defaultBranch": "master", "variables": []}
    return data


def inspect_capabilities(
    *,
    manifest_path: Path = ENV_MANIFEST_PATH,
    runtime_file: Path = RUNTIME_ENV_FILE,
    vault_path: Path = LOCAL_VAULT_PATH,
    allow_proc_scan: bool = True,
    omni_config_path: Path | None = None,
    host_override: str = "",
) -> dict[str, Any]:
    """Return a 100% secret-free host-aware capability & missing-variable audit report."""
    manifest = load_env_manifest(manifest_path)
    var_entries = manifest.get("variables") or []
    host_info = detect_runtime_host(
        runtime_file=runtime_file,
        allow_proc_scan=allow_proc_scan,
        host_override=host_override,
    )
    host_id = str(host_info.get("host_id") or "local-ide")
    is_cloud = bool(host_info.get("is_cloud"))

    present: list[dict[str, Any]] = []
    raw_missing_expected: list[dict[str, Any]] = []
    optional_absent: list[str] = []
    present_names: set[str] = set()
    omni_local_used = False

    for item in var_entries:
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        aliases = tuple(str(a).strip() for a in (item.get("aliases") or []) if str(a).strip())
        val, src = resolve_secret_with_source(
            name,
            aliases=aliases,
            runtime_file=runtime_file,
            vault_path=vault_path,
            allow_proc_scan=allow_proc_scan,
            omni_config_path=omni_config_path,
        )
        expected = bool(item.get("expectedInAiStudio"))
        if val:
            present_names.add(name)
            if src.startswith("omni_local_config:"):
                omni_local_used = True
            present.append(
                {
                    "name": name,
                    "source": src,
                    "capability": item.get("capability", ""),
                    "expectedInAiStudio": expected,
                }
            )
        elif expected:
            raw_missing_expected.append(
                {
                    "name": name,
                    "capability": item.get("capability", ""),
                    "purpose": item.get("purpose", ""),
                    "lastConfirmedUtc": item.get("lastConfirmedUtc", ""),
                }
            )
        else:
            optional_absent.append(name)

    has_gh = "GITHUB_TOKEN" in present_names
    has_cf = "CLOUDFLARE_API_TOKEN" in present_names
    has_pair = "CAVERN_BRIDGE_PAIR_SECRET" in present_names
    has_relay = "CAVERN_RELAY_URL" in present_names

    dotnet_cached = Path("/root/.cache/cavern-android-ref/dotnet-11/dotnet").is_file() or bool(shutil.which("dotnet"))
    gh_hosts_file = Path.home() / ".config" / "gh" / "hosts.yml"
    has_native_git = (REPO_ROOT / ".git").exists() or (
        bool(host_override.strip()) and host_id.startswith("antigravity-")
    )
    has_gh_cli = (gh_hosts_file.is_file() and gh_hosts_file.stat().st_size > 0) or has_gh
    has_local_dotnet = Path("/root/.dotnet/dotnet").is_file() or dotnet_cached
    has_sdcard = Path("/sdcard").is_dir()
    cfg_probe = omni_config_path if omni_config_path is not None else OMNI_LOCAL_CONFIG_PATH
    has_local_omni_cfg = cfg_probe.is_file()

    fed_mount = Path(os.environ.get("CAVERN_FEDERATION_ROOT", "/tmp/cavern-federation"))
    fed_manifest = _read_json_safe(REPO_ROOT / ".agents" / "federation.json")
    fed_repos: dict[str, Any] = {}
    local_scratch_repos: dict[str, bool] = {}
    for alias, spec in (fed_manifest.get("repos") or {}).items():
        if isinstance(spec, dict):
            termux_p = str(spec.get("termuxPath") or "").strip()
            local_git_ok = bool(termux_p and (Path(termux_p) / ".git").exists())
            local_scratch_repos[alias] = local_git_ok
            mp = Path(termux_p) if (not is_cloud and local_git_ok) else (fed_mount / alias)
            fed_repos[alias] = {
                "ownerRepo": spec.get("ownerRepo", ""),
                "branch": spec.get("branch", "main"),
                "path": str(mp),
                "ready": mp.exists(),
            }

    if is_cloud:
        missing_expected = raw_missing_expected
        alert_str = (
            "MISSING_EXPECTED_ENV_VARS: Ask user to restore: "
            + ", ".join(m["name"] for m in missing_expected)
            if missing_expected
            else ""
        )
        omni_auth = "local_zero_knock" if (has_pair and has_relay) else "smart_knock_biometric"
        routing: dict[str, Any] = {
            "githubPush": "local_github_api" if has_gh else "phone_omniagent",
            "githubCiInspect": "local_github_api" if has_gh else "public_readonly_or_phone",
            "cloudflareWorkerDeploy": "local_cloudflare_api" if has_cf else "phone_omniagent",
            "omniAgentAuth": omni_auth,
            "localBuildAndTests": {
                "mode": "local",
                "python3": bool(shutil.which("python3")),
                "node": bool(shutil.which("node")),
                "gcc": bool(shutil.which("gcc")),
                "dotnet11CiParity": dotnet_cached,
            },
            "phoneOnly": [
                "hot_exec (live S25U Cavernize RAM C# probe)",
                "cavernize-cmd (live S25U audio/UI/SelfAudit IPC)",
                "ag-sync / git_pull (sync Termux Antigravity mirror & repo)",
                "install-apk / logcat / shell (live S25U device ops)",
            ],
        }
    else:
        missing_expected = []
        alert_str = ""
        if has_pair and has_relay:
            omni_auth = "local_omni_config" if omni_local_used else "local_zero_knock"
        else:
            omni_auth = "smart_knock_biometric"
        routing = {
            "githubPush": "native_git" if has_native_git else ("local_github_api" if has_gh else "phone_omniagent"),
            "githubCiInspect": "native_gh_cli" if has_gh_cli else "public_readonly_or_phone",
            "cloudflareWorkerDeploy": "local_cloudflare_api" if has_cf else "phone_omniagent",
            "omniAgentAuth": omni_auth,
            "omniAgentScope": "android_ipc_and_hot_exec_only",
            "federationMode": "local_scratch_clones" if any(local_scratch_repos.values()) else "tarball_mount",
            "flightRecorder": "skip_native_git",
            "previewShim": "skip_preserve_only",
            "localBuildAndTests": {
                "mode": "local",
                "python3": bool(shutil.which("python3")),
                "node": bool(shutil.which("node")),
                "gcc": bool(shutil.which("gcc")),
                "dotnet11CiParity": has_local_dotnet,
            },
            "phoneOnly": [
                "hot_exec (live S25U Cavernize RAM C# probe via OmniAgent IPC)",
                "cavernize-cmd (live S25U audio/UI/SelfAudit IPC via OmniAgent)",
            ],
        }

    out: dict[str, Any] = {
        "ok": True,
        "ownerRepo": manifest.get("ownerRepo", "guumachadooo/Cavern"),
        "defaultBranch": manifest.get("defaultBranch", "master"),
        "hostProfile": {
            "hostId": host_id,
            "reason": host_info.get("reason", ""),
            "ignoreCloudEnvAlerts": not is_cloud,
        },
        "presentCount": len(present),
        "presentVars": present,
        "missingExpectedCount": len(missing_expected),
        "missingExpectedVars": missing_expected,
        "optionalAbsentVars": optional_absent,
        "alert": alert_str,
        "routing": routing,
        "federation": {
            "mountRoot": str(fed_mount),
            "allReady": bool(fed_repos) and all(v["ready"] for v in fed_repos.values()),
            "repos": fed_repos,
        },
    }
    if not is_cloud:
        out["aiStudioExpectedVarsMissing"] = [m["name"] for m in raw_missing_expected]
        out["nativeCapabilities"] = {
            "git": has_native_git,
            "ghCliAuthed": has_gh_cli,
            "localDotnet": has_local_dotnet,
            "sdcard": has_sdcard,
            "localOmniConfig": has_local_omni_cfg,
            "localScratchRepos": local_scratch_repos,
        }
    return out


if __name__ == "__main__":
    print(json.dumps(inspect_capabilities(), indent=2, ensure_ascii=False))
