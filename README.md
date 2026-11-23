# OmniAgent Companion (`v2.1`)

Standalone **Samsung One UI 9** Android Companion, **Triad Command Hub**, **Local Debian/Termux File Explorer**, and **Zero-Click Always-On Bridge** for the **Cavern / OmniAgent / Dynamic Fractal Planner** Triad.

## What's New in v2.1
- **Zero-Quota Cavernize One UI 9 Design Inheritance:** Compiles CavernizeAndroid's exact `OneUiTheme.cs`, `OneUiIconView.cs`, `OneUiComponents.cs`, and SF Pro variable font directly via `.agents/federation.json` mirrors + `BuildOmniAgent.yml` pre-build sync, AND pulls live design tokens over `<10 ms` same-signature IPC (`get_design_tokens`) with zero AI quota spent.
- **Zero-Click Always-On Automation & Cold-Wake:** Auto-starts on launch, boot, and package update with `OnTaskRemoved` quick-restart (`AlarmManager`), a persisted 15-minute `JobScheduler` watchdog (`OmniWatchdogJobService`), automatic wake when Cavernize launches, and Debian daemon Smart Knock wake fallback.
- **Native System Biometric Overlay (Zero App Window) + StrongBox/TEE Hardware ECDSA:** Invisible translucent host (`OmniSystemPromptTheme`) invokes Android/Samsung's native `BiometricPrompt` bottom sheet directly over any app with hardware-bound ECDSA-P256 `CryptoObject` signing (Samsung Knox Vault StrongBox / TEE).
- **Zero-Toggle Automatic Cavernize Local IPC:** Automatically linked to same-signature **Cavernize Android** (`hu.sbence.cavern.permission.OMNI_BRIDGE`, `protectionLevel="signature"`).
- **Triad Command Hub:** 4-tab floating frosted glass dock (`Hub · Files · Bridge · Settings`) showing live Wave/Next Milestone, CI status, GitHub Releases, local Debian dirty state, 1-tap Pull Triad / Cavernize Self-Check / APK Updater, and a zero-quota Project Doc Reader.
- **Local Debian / Termux & Android File Explorer:** Authenticated `127.0.0.1:47921` loopback bridge (`omni_agent_daemon.py v4.1.0`) with HMAC auth and `os.path.realpath` root confinement — browse PRoot Debian (`/root`), Termux (`~`), Triad workspaces, and `/sdcard`, preview code/logs, copy, share, or export to `/sdcard/Download/OmniExport/`.

## Approval Policy Modes
- **Smart (Recommended):** Automatically allows routine read-only/diagnostic tasks and prompts with the native biometric overlay only for sensitive writes, pushes, or new Smart Knock connections.
- **Biometric (Strict):** Requires fresh hardware-backed biometric verification for every remote command.
- **Trusted (Free):** Auto-approves commands from already paired sessions without interrupting you (Smart Knock pairing for brand-new unpaired sessions still requires your fingerprint).
