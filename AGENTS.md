# Project Guidelines — OmniAgent (`guumachadooo/OmniAgent`)

> STYLE|lossless symbolic telegraphic EN ONLY (`!articles/copulas/filler`, ops `-> | && || ! w/ w/o`, verbatim paths/symbols). Reply to USER in user's chat language.

## 0. Auto-Loaded AI Studio Boot Context & 3-Repo Federation
- **Real Stack vs. Preview Shim:** Product is **C#/.NET (`net11.0-android`)** (`OmniAgentAndroid/`), **Cloudflare Unified Hub v6 Worker** (`worker/cloudflare_live_relay_worker.js`), & **Python 3 stdlib bridge/daemon** (`agent/`). Root `package.json` + `.agents/preview/server.mjs` is strictly a **zero-dep Node.js preview shim** (`0.0.0.0:3000`, zero `node_modules/`) keeping AI Studio alive, bridging Secrets to `/tmp/.cavern_runtime_env.json`, & auto-hydrating sibling repos at container boot (0 LLM quota). NEVER delete C#/Python/Worker files or add web deps.
- **Instant 3-Repo Access (`/tmp/cavern-federation/`):** On container boot, `server.mjs` automatically symlinks `/tmp/cavern-federation/omniagent -> /app/applet` AND hydrates `/tmp/cavern-federation/cavern` (`guumachadooo/Cavern@master`) & `/tmp/cavern-federation/fractal` (`guumachadooo/dynamic-fractal-planner@main`) outside `/app/applet` (zero workspace bloat).
- **Mandatory Boot Check (1 command):**
  `python3 -B agent/omni_agent_client.py capabilities && python3 -B agent/omni_agent_client.py repos status`
  - If any sibling repo is not hydrated yet -> `python3 -B agent/omni_agent_client.py repos hydrate`.
  - If `GITHUB_TOKEN` is absent in a brand-new AI Studio app -> run `python3 -B agent/omni_agent_client.py connect --scope workspace` (1-tap S25U Biometric Smart Knock via public `.agents/bridge_profile.json`) to push ANY of the 3 repos through phone Termux git!
- **Multi-Repo Edit, Mirror Sync & Push:**
  - Edit `omniagent` directly in `/app/applet`, `cavern` in `/tmp/cavern-federation/cavern`, `fractal` in `/tmp/cavern-federation/fractal`.
  - Sync shared `agent/*.py` & `worker/cloudflare_live_relay_worker.js` -> Cavern: `python3 -B agent/omni_agent_client.py repos sync-mirrors --from omniagent --apply`.
  - Push one or all modified repos (direct `GITHUB_TOKEN` or auto Termux fallback): `python3 -B agent/omni_agent_client.py repos push <omniagent|cavern|fractal|all> --commit "..."`.
  - Deploy Cloudflare Worker v6: `python3 -B agent/omni_agent_client.py deploy-worker`.
  - Verify before push: `python3 -B agent/test_omni_agent.py && node --test agent/test_worker_v4.mjs`.
- **Signing Key & IPC Invariant:** Keep `.github/signing/cavernize.keystore.b64` & `hu.sbence.cavern.omniagent` aligned with `CavernizeAndroid` (`<10 ms` same-signature IPC). Publish auto-advancing `v2.0.<run>` GitHub Releases (`make_latest: true`) + `omniagent-latest` OTA alias (`make_latest: false`).
