# OmniAgent

> **Zero-Trust E2E Android Companion App, Termux/Debian Agent Daemon & Cloudflare Durable Objects Unified Hub** — bridging cloud AI workspaces (**Google AI Studio**, **Antigravity**) with a live Android device and [**Cavernize Android**](https://github.com/guumachadooo/Cavern) over sub-10 ms same-signature local IPC.

[![Build OmniAgent APK](https://github.com/guumachadooo/OmniAgent/actions/workflows/BuildOmniAgent.yml/badge.svg)](https://github.com/guumachadooo/OmniAgent/actions/workflows/BuildOmniAgent.yml)
[![Latest Release](https://img.shields.io/github/v/release/guumachadooo/OmniAgent?label=Latest%20APK&color=0A84FF)](https://github.com/guumachadooo/OmniAgent/releases/latest)
[![Platform](https://img.shields.io/badge/.NET%2011-Android%2015%2B%20%28API%2035%29-34C759)](#architecture)
[![Relay](https://img.shields.io/badge/Cloudflare-Hub%20v6%20SQLite%20DO-F38020)](#architecture)
[![Security](https://img.shields.io/badge/Crypto-E2E%20SHA256--CTR%20%2B%20ECDH%20P--256-111827)](#security-model)

---

## Architecture

```text
┌─────────────────────────────┐         E2E Encrypted (SHA256-CTR + HMAC)
│  AI Studio / Antigravity    │ ◄──────────────────────────────────────────┐
│  agent/omni_agent_client.py │                                            ▼
│  agent/live_sync_watcher.py │                         ┌──────────────────────────────────┐
└─────────────────────────────┘                         │  Cloudflare Unified Hub v6       │
                                                        │  SQLite Durable Object (HUB_DO)  │
                                                        │  <50 ms strong consistency       │
                                                        └───────────┬──────────┬───────────┘
                                                                    │          │
                      ┌─────────────────────────────────────────────┘          └──────────────────────────┐
                      ▼                                                                                   ▼
┌───────────────────────────────────────────┐                                     ┌──────────────────────────────────────┐
│  OmniAgent Android App (One UI 9)         │   Same-Signature Local IPC (<10ms)  │  Termux / Debian PRoot Daemon        │
│  hu.sbence.cavern.omniagent               │ ◄─────────────────────────────────► │  agent/omni_agent_daemon.py          │
│  • Biometric Smart Knock Overlay          │   hu.sbence.cavern.permission       │  agent/termux_auto_pilot.py          │
│  • Cloudflare Worker Auto-Deploy          │   .OMNI_BRIDGE (CheckSignatures=0)  │  • Zero-Quota Auto-APK Downloader    │
│  • Direct GitHub Push & APK Installer     │                 │                   │  • Live Antigravity File Mirror      │
└───────────────────────────────────────────┘                 ▼                   │  • Background .NET Pre-Verify Cache  │
                                            ┌───────────────────────────────────┐ └──────────────────────────────────────┘
                                            │  Cavernize Android                │
                                            │  Live Bridge, SelfAudit, hot_exec │
                                            └───────────────────────────────────┘
```

---

## Key Features

- **Same-Signature Local IPC (`CavernizeIpcBridgeClient.cs`):** Signed with the exact same release certificate as Cavernize Android (`protectionLevel="signature"` + `CheckSignatures == 0`), forwarding live diagnostics, audio audits, UI inspection, and in-RAM C# `hot_exec` probes in `<10 ms` without duplicate background polling in Cavernize.
- **Cloudflare Unified Hub v6 (`worker/cloudflare_live_relay_worker.js`):** Hybrid SQLite Durable Objects (`HUB_DO`) eliminating KV eventual-consistency delays and write-quota exhaustion (`<50 ms` strong consistency, chunked encrypted blobs, multi-agent Swarm bus).
- **Zero-Quota Automations (`agent/termux_auto_pilot.py` & `agent/live_sync_watcher.py`):**
  - **Auto-APK Watcher:** Polls GitHub Releases via conditional `ETag` (`304 Not Modified` = 0 rate-limit cost) and automatically downloads new `Cavernize-Android.apk` & `OmniAgent-Android.apk` releases straight into `/sdcard/Download/`.
  - **Live Antigravity Mirror:** Debounced `<8 ms` workspace watcher mirroring AI Studio edits into Debian Antigravity with SHA-256 manual-edit conflict protection and 0 LLM tokens.
  - **1-Shot `ship` Pipeline (`agent/omni_agent_client.py`):** Runs static verification, file-budget checks, direct GitHub API push, Termux `git pull --rebase`, and Flight Recorder handoff in a single command.
- **3-Tier Anti-Rollback Flight Recorder (`agent/flight_recorder.py`):** Persists session diaries and verified delta bundles across local `/tmp`, Termux phone storage, and Cloudflare Hub v6 DO.

---

## Repository Layout

| Path | Description |
| :--- | :--- |
| `OmniAgentAndroid/` | Standalone `.NET 11` (`net11.0-android`) One UI 9 Android companion application. |
| `agent/` | Zero-dependency Python CLI, Termux daemon, E2E crypto protocol, Flight Recorder, Live Sync Watcher, Auto-Pilot & unit tests. |
| `worker/` | Cloudflare Workers v6 SQLite Durable Object relay script (`cloudflare_live_relay_worker.js`). |
| `docs/` | Unified Hub architecture & operations documentation (`DIAGNOSTICS_HUB.md`). |
| `.github/workflows/BuildOmniAgent.yml` | Automated signed APK CI publishing auto-advancing `v2.0.<run>` (`Latest`) + rolling `omniagent-latest` releases. |

---

## Security Model

- **Zero-Trust Relay:** Cloudflare Worker routes only opaque authenticated ciphertext (`SHA256-CTR + HMAC-SHA256` Encrypt-then-MAC + P-256 ECDH Smart Knock).
- **Android Hardware Keystore:** Pairing secrets, GitHub PATs, and Cloudflare tokens inside OmniAgent Android are encrypted via Android Keystore `AES-256-GCM` (`OmniSettingsStore.cs`).
- **Biometric Gate:** Remote elevation requests open an instant on-screen biometric approval overlay (`OmniBiometricGateActivity.cs`).

---

## Ecosystem

- [**Cavern / Cavernize Android**](https://github.com/guumachadooo/Cavern) — Spatial audio player, Dolby Atmos/TrueHD/IAMF/HOA3 renderer, neural stem studio, and USB Pro-Audio suite.
- [**Dynamic Fractal Planner**](https://github.com/guumachadooo/dynamic-fractal-planner) — Token-efficient AI planning, SOTA pre-flight, wiring-gate, and file-budget verification framework.

## License

Licensed under [`LICENSE.md`](./LICENSE.md).
