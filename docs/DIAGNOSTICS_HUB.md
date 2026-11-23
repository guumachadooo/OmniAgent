# Diagnostics, Live Bridge & OmniAgent Full-Agent Architecture (`DIAGNOSTICS_HUB.md`)

> STYLE|lossless symbolic telegraphic EN ONLY (`!articles/copulas/filler`, ops `-> | && || ! w/ w/o`, verbatim paths/symbols).

## 1. Unified Zero-Trust Hub v6 (Free Cloudflare Worker + SQLite Durable Object `HUB_DO` + `RELAY_KV`)
- **Hybrid Storage Architecture (`HubCoordinator` DO + `RELAY_KV`):**
  - Real-time paths (`/v1/agent/*`, `/v1/wake/*`, `/v1/knock/*`, `/v1/session/*`, `/v1/blob/*`, `/v1/swarm/*`) execute inside a single strongly-consistent Free-Tier SQLite Durable Object (`HUB_DO`) w/ hot RAM TTL cache -> `<50ms` latency, **zero 60s KV negative cache timeouts**, **zero daily KV write-quota burn** (100k free DO req/day), && graceful fallback to `RELAY_KV` when `HUB_DO` is unbound.
  - Runtime device telemetry (`/v1/telemetry/*`, 14-day TTL) stays on `RELAY_KV`. `/health` exposes `version: 6`, `supportsDurableObjects`, `storageMode` (`durable-object` | `kv-fallback`), && `recommendedHeartbeatSec` (`60` DO / `180` KV).
  - One-command / automated Worker deploy: `.agents/agent/deploy_worker.py` (3-step SQLite DO migration w/ `keep_bindings` preserving `RELAY_KV` & secrets, or via `omni_agent.py deploy-worker` using Keystore-encrypted Cloudflare API Token inside `OmniAgentAndroid`).
- **CI Build Diagnostics (GitHub `diagnostics` branch):**
  - Published exclusively by `.github/workflows/BuildAndroid.yml`: `diagnostics:ci/latest-build-diagnostics.{md,json}` + `diagnostics:ci/latest-learned-lessons.md`.
  - Query live: `python3 -B .agents/scripts/fetch_live_diagnostics.py --ci --lessons`
- **OmniConnect v5/v6 Zero-Paste Auto-Connect (`bridge_auth.py` + `/v1/knock/*`):**
  - Priority: `CLI > Env Secrets (CAVERN_RELAY_URL / CAVERN_BRIDGE_PAIR_SECRET, .env.example) > Gitignored Local Vault (.agents/.bridge_vault.json, chmod 0600, excluded from OmniAgent sync) > Public Non-Secret Repo Profile (.agents/bridge_profile.json)`.
  - **Zero-Secret Ephemeral NIST P-256 ECDH Smart Knock (`/v1/knock/*`):** public `knockId = sha256("knock-channel-v5:" + sha256("wake-channel-v2:" + pairSecret))` contains zero secret material; client generates ephemeral P-256 keypair, displays 6-char SAS (`XXX-XXX`), phone polls/approves/denies using private `X-Device-Hash` (server derives `ownerKnockId` preventing spoofing), rate-limited `3/15m` + single-slot `409` lock + `15m` deny cooldown + native `BiometricPrompt`, & encrypts grant (`session` or biometric-verified `workspace`) E2E via P-256 ECDH + v4 Encrypt-then-MAC.
  - **Vault Session Persistence:** `live_phone_bridge.py` + plugin `live_bridge_client.py` immediately persist approved wake session tokens to `.bridge_vault.json` via `on_session_acquired` BEFORE running `--cmd`, and only evict tokens on HTTP 404 (`EXIT_SESSION_EXPIRED = 4`, never on transient network timeout).
  - Zero-flag CLI entrypoints: `python3 -B .agents/scripts/omni_agent.py connect [--status-only|--force-knock|--scope session|workspace]`, `python3 -B .agents/scripts/live_phone_bridge.py --auto --cmd ping`, `python3 -B .agents/scripts/fetch_live_diagnostics.py --cloudflare --all`.
- **Runtime Device Logs, 12-Layer Self-Audit & Live Bridge v3+ (`hot_exec`) (Cloudflare Hub v6):**
  - Worker `.agents/docs/cloudflare_live_relay_worker.js` (v6, 100% backwards-compatible w/ Cavernize v3–v5).
  - Fetch telemetry: `python3 -B .agents/scripts/fetch_live_diagnostics.py --cloudflare --all`
  - Connect Cavernize Live Bridge v3+: `python3 -B .agents/scripts/live_phone_bridge.py --auto --tier 2 --cmd <cmd> --args '{...}'` OR route locally via OmniAgent same-signature IPC (`omni_agent.py cavernize-cmd <cmd> --args '{...}'`).
  - **Live C# RAM Hot-Execution (`hot_exec`, zero APK rebuild):** `LiveHotPatchHost.cs` verifies HMAC-SHA256 against Android Keystore pairing key & runs locally compiled IL `.dll` (`.agents/agent/hot_probe_compiler.py`) purely in RAM via collectible `AssemblyLoadContext.LoadFromStream` (100% W^X compliant; defaults to `include_android=False` + `--via auto` routing over online `android` IPC channel; access `CavernizeAndroid` via reflection on device):
    `python3 -B .agents/scripts/omni_agent.py hot-exec --name MyProbe --code '...'`
- **OmniAgent v2.0.0 Full-Agent Ecosystem ("Antigravity 2", `/v1/agent/*`, `/v1/blob/*`, `/v1/swarm/*`, `/v1/knock/*`):**
  - **Phase 1 — Termux / Debian PRoot Daemon (`.agents/agent/omni_agent_daemon.py`, `channel=termux`):**
    - Install/start: `bash .agents/agent/install_termux_agent.sh --bg` (adaptive heartbeat `60s` DO / `180s` KV).
    - Provides true **2-way Git sync/push** (`push-workspace`, `pull-workspace`, auto-excluding `public/assets/aistudio/`), Termux/Debian shell + parallel workers, Wireless ADB/Shizuku APK install & `logcat`, Antigravity local Debian integration (`ag-discover`, `ag-sync` compact token-diet mirror never touching binary `.pb`), && optional official local `gemini`/`ollama` CLI subagents.
  - **Phase 2 — Standalone Android Companion APK (`CavernSamples/OmniAgentAndroid/`, `hu.sbence.cavern.omniagent`, `channel=android`):**
    - Built via `.github/workflows/BuildOmniAgent.yml` -> `omniagent-latest` (and bundled alongside Cavernize in `BuildAndroid.yml` -> `android-latest` / `v2.9.X`).
    - Survives Cavernize updates/crashes; features One UI 9 AMOLED dashboard, `Smart` (zero fingerprint nagging) / `Biometric` / `Trusted` policy, **Instant On-Screen Biometric Overlay** (`SYSTEM_ALERT_WINDOW` BAL exemption + `USE_FULL_SCREEN_INTENT` lockscreen fallback), `OmniBootReceiver` boot auto-start, Keystore AES-256-GCM **direct GitHub Git Data API atomic multi-file push**, Keystore AES-256-GCM **Cloudflare Workers Auto-Deploy (`cf_deploy_worker` / `cf_status`)**, cryptographically verified **same-signature local IPC** (`hu.sbence.cavern.permission.OMNI_BRIDGE` `protectionLevel="signature"` + `CheckSignatures`) to Cavernize (`cavernize_cmd` w/ JSON object `args` + int `tier`, stopping duplicate Cavernize standby polling), `OmniApkFileProvider` one-tap APK installer, && Termux `RUN_COMMAND` daemon launcher.
  - **Zero-API-Cost Multi-Agent Swarm Bus (`/v1/swarm/*`):**
    - Coordinates tasks between AI Studio <-> Antigravity (Debian) <-> secondary chats via `omni_agent.py swarm <enqueue|list|claim|complete|task|clear>`.

## 2. Anti-Cancellation / Anti-Rollback 3-Tier Session Flight Recorder (`.agents/scripts/flight_recorder.py`)
- **Problem Solved:** Canceling an AI Studio turn discards chat context & auto-rolls back `/app/applet` edits. The Flight Recorder continuously mirrors structured session memory + verified `omni-delta-v1` file snapshots to **3 independent tiers outside AI Studio's rollback boundary**:
  1. **Tier 1 — Local Container Cache (`/tmp/cavern-flight-recorder/`):** `<5ms`, survives same-container workspace rollbacks even offline (`baseline_manifest.json`, `latest.json`, `LATEST_DIARY.md`, `latest_bundle.json.gz`, `history/cp_*.json`).
  2. **Tier 2 — Phone Termux Permanent Storage (`/root/.gemini/antigravity/ai_studio_bridge/flight_recorder/`):** survives container resets, new AI Studio chats, >24h gaps, & phone reboots; strictly outside git repo tree so `git status` stays clean.
  3. **Tier 3 — Cloudflare Hub v6 SQLite DO (`/v1/swarm/enqueue`, `taskId="fr_latest"`, `role="flight_recorder"`):** E2E-encrypted (`omni_protocol.encrypt_envelope`, ctx `fr:latest`, 0 Worker redeploy, 0 KV writes, inline gzipped bundle when `<=140 KB`), works across brand-new containers even if Termux is momentarily offline.
- **Commands (`flight_recorder.py` or `omni_agent.py checkpoint`):**
  - `init --session <id> --wave <W> --goal "..."`: captures workspace SHA-256 baseline & initializes seq #1 across all tiers.
  - `save --auto-diff --phase <p> --summary "..." --finding "..." --done "..." --pending "..." --next "..."`: merges incremental session diary, auto-diffs against baseline manifest, packs verified `omni-delta-v1` bundle, & writes to all 3 tiers.
  - `recover [--json]`: probes all 3 tiers, picks freshest by `(timestampUnix, seq)`, detects exact rolled-back/missing files via SHA-256 (`ROLLBACK_DETECTED`), & prints compact recovery brief + exact `restore` command.
  - `restore [--dry-run] [--source auto|local|cloud|termux] [--allow-delete]`: verifies SHA-256 & reapplies rolled-back workspace edits in 1 command.
  - `close [--commit <sha>] [--note "..."]`: cleanly closes completed session after commit/push.
