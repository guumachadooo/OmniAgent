# Timeless Build & Commit Error Lessons (`BUILD_LESSONS.md`)

> STYLE|lossless symbolic telegraphic EN ONLY (`!articles/copulas/filler`, ops `-> | && || ! w/ w/o`, verbatim diagnostic codes & symbols).
> LAW|ANTI-OBSOLESCENCE (MANDATORY): NEVER hardcode ephemeral target versions (OS/API level, .NET/TFM, JDK, Actions `@vN`, NDK/lib versions, or calendar dates) as permanent rules. ALWAYS state timeless root cause + version-agnostic rule + dynamic live-lookup instruction (`official docs` / active `.csproj` / workflow / `verify_universal.py --all`).
> LEARNER|dynamic auto-learning CLI: `python3 -B .agents/plugins/dynamic-fractal-planner/scripts/plan_tool.py learn --from-log <file> --apply` | `--fetch-live-ci --apply` | `--audit-lessons`.

---

## 1. Core Timeless Build & Commit Lessons (`BL-001` .. `BL-003`)

### `BL-001` | Signing Keystore, Package ID & IPC Parity (`OmniAgent` <-> `Cavernize`)
- `Category`: `android-ipc`
- `Codes`: `IPC_SIG_MISMATCH, XA4211`
- `Keywords`: `keystore, signing, ApplicationId, hu.sbence.cavern, permission, protectionLevel`
- `RootCause`: Divergent signing certificates or mismatched `ApplicationDisplayVersion` prefixes between `OmniAgentAndroid.csproj` and CI workflows break signature-protected Android IPC (`<10 ms` same-signature broadcast/service) and OTA monotonic comparisons.
- `TimelessRule`: Keep `.github/signing/cavernize.keystore.b64` synchronized with `CavernizeAndroid`, never change `hu.sbence.cavern.omniagent` signature or `<SupportedOSPlatformVersion>` / `targetSdkVersion` W^X constraints blindly, and keep `ApplicationDisplayVersion` prefix synced between `.csproj` and `.github/workflows/BuildOmniAgent.yml`.
- `DynamicLookup`: Inspect `OmniAgentAndroid/OmniAgentAndroid.csproj`, `AndroidManifest.xml`, and `.github/workflows/BuildOmniAgent.yml` before modifying packaging or IPC permissions.
- `AutoGuard`: `agent/test_omni_agent.py` + `verify_universal.py --all`

### `BL-002` | Stdlib-Only Python Bridge/Daemon & Zero-Dep Preview Shim
- `Category`: `python-node-runtime`
- `Codes`: `IMPORT_ERR, MODULE_NOT_FOUND`
- `Keywords`: `stdlib, server.mjs, package.json, node_modules, agent, worker`
- `RootCause`: Adding pip or npm dependencies breaks zero-setup execution on Termux PRoot, AI Studio containers, and CI runners; deleting `.agents/preview/server.mjs` breaks AI Studio preview healthchecks and secret bridging.
- `TimelessRule`: Keep `agent/*.py` 100% Python stdlib and `.agents/preview/*.mjs` 100% Node built-in modules (`zero node_modules`); verify Cloudflare Worker syntax and protocol parity via `node --test agent/test_worker_v4.mjs`.
- `DynamicLookup`: Run `python3 -B agent/test_omni_agent.py && node --test agent/test_worker_v4.mjs` after any bridge/worker edit.
- `AutoGuard`: `agent/test_omni_agent.py` + `agent/test_worker_v4.mjs`

### `BL-003` | Host-Matrix Classification & Cross-Host Routing (`antigravity-termux` <-> `ai-studio-cloud`)
- `Category`: `multi-host`
- `Codes`: `HOST_MATRIX_UNCLASSIFIED`
- `Keywords`: `host_matrix.json, host-classify, host-audit, antigravity-termux, ai-studio-cloud`
- `RootCause`: Unclassified scripts/configs under `agent/`, `worker/`, or `.agents/` cause cloud-only daemons or alerts to misfire on Termux PRoot or leave new tools unrouted across hosts.
- `TimelessRule`: Classify every new script/config via `plan_tool.py host-classify <path> --apply` (`universal` | `ai-studio-only` | `dual-adapted` | `phone-daemon`) and pass `plan_tool.py host-audit` before commit/push.
- `DynamicLookup`: Run `python3 -B .agents/plugins/dynamic-fractal-planner/scripts/plan_tool.py host-audit` and `verify_universal.py --all`.
- `AutoGuard`: `verify_universal.py:check_host_matrix_coverage`

---

## Auto-Learned Lessons

<!-- AUTO-LEARNED-LESSONS:START -->

(No new uncatalogued structural lessons pending.)

<!-- AUTO-LEARNED-LESSONS:END -->
