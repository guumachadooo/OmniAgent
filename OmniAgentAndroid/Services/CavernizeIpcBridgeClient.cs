using System.Text.Json;
using System.Collections.Concurrent;

using Android.Content;
using Android.Content.PM;

namespace OmniAgentAndroid.Services;

/// <summary>
/// Same-signature local Android IPC client allowing <c>OmniAgentAndroid</c> to dispatch
/// Live Bridge diagnostic &amp; QA commands directly to <c>CavernizeAndroid</c> in &lt;10 ms
/// with zero duplicate cloud polling or background services inside Cavernize.
/// Protected by signature-level permission <c>hu.sbence.cavern.permission.OMNI_BRIDGE</c>
/// AND explicit runtime <see cref="PackageManager.CheckSignatures(string, string)"/> verification.
/// </summary>
public static class CavernizeIpcBridgeClient {
    public const string CavernizePackage = "hu.sbence.cavern.cavernize";
    public const string BridgePermission = "hu.sbence.cavern.permission.OMNI_BRIDGE";
    public const string ActionCommand = "hu.sbence.cavern.ipc.OMNI_BRIDGE_CMD";
    public const string ActionReply = "hu.sbence.cavern.ipc.OMNI_BRIDGE_REPLY";

    public const string ExtraReqId = "req_id";
    public const string ExtraCmd = "cmd";
    public const string ExtraArgsJson = "args_json";
    public const string ExtraMaxTier = "max_tier";
    public const string ExtraReplyPackage = "reply_package";
    public const string ExtraOk = "ok";
    public const string ExtraBody = "body";
    public const string ExtraElapsedMs = "elapsed_ms";

    static readonly ConcurrentDictionary<string, TaskCompletionSource<(bool ok, string body, long elapsedMs)>> pending = new(StringComparer.Ordinal);
    static readonly object regLock = new();
    static ReplyReceiver replyReceiver;
    static long cachedSigCheckedMs;
    static bool cachedSigResult;

    static readonly object watchdogLock = new();
    static int watchdogLastPid;
    static string watchdogLastSessionId = "";
    static string watchdogLastAliveUtc = "";
    static int watchdogConsecutiveTimeouts;
    static int watchdogPidChanges;
    static int watchdogTotalProbes;
    static string watchdogLastError = "";

    /// <summary>
    /// Verifies that <c>CavernizeAndroid</c> is installed and signed by the exact same release keystore certificate.
    /// Cached for 30s to avoid repeated PackageManager Binder IPC on the UI thread.
    /// </summary>
    public static bool IsSignatureVerified(Context context, bool forceRefresh = false) {
        if (context?.PackageManager == null) {
            return false;
        }
        long nowMs = Android.OS.SystemClock.ElapsedRealtime();
        if (!forceRefresh && cachedSigCheckedMs > 0 && nowMs - cachedSigCheckedMs < 30_000L) {
            return cachedSigResult;
        }
        try {
#pragma warning disable CS0618, CA1422
            // Android PackageManager.SIGNATURE_MATCH == 0
            bool ok = (int)context.PackageManager.CheckSignatures(context.PackageName, CavernizePackage) == 0;
#pragma warning restore CS0618, CA1422
            cachedSigResult = ok;
            cachedSigCheckedMs = nowMs;
            return ok;
        } catch {
            return false;
        }
    }

    static void EnsureReceiverRegistered(Context context) {
        lock (regLock) {
            if (replyReceiver != null) {
                return;
            }
            Context appCtx = context.ApplicationContext ?? context;
            ReplyReceiver rx = new();
            IntentFilter filter = new(ActionReply);
            if (OperatingSystem.IsAndroidVersionAtLeast(33)) {
                appCtx.RegisterReceiver(rx, filter, BridgePermission, null, ReceiverFlags.Exported);
            } else {
                appCtx.RegisterReceiver(rx, filter, BridgePermission, null);
            }
            replyReceiver = rx;
        }
    }

    /// <summary>
    /// Sends a local signature-verified command to CavernizeAndroid and awaits its response.
    /// </summary>
    public static async Task<(bool ok, string body, long elapsedMs)> SendCommandAsync(
        Context context,
        string cmd,
        string argsJson = "{}",
        int maxTier = 2,
        int timeoutSeconds = 45,
        CancellationToken ct = default) {
        if (context == null) {
            return (false, "Missing Android context.", 0);
        }
        if (!OmniAgentForegroundService.IsPackageInstalled(context, CavernizePackage)) {
            return (false, "Cavernize Android is not installed on this device.", 0);
        }
        if (!IsSignatureVerified(context)) {
            return (false, "Cavernize APK signature mismatch — local IPC blocked for security.", 0);
        }

        EnsureReceiverRegistered(context);

        string reqId = Guid.NewGuid().ToString("N")[..16];
        TaskCompletionSource<(bool, string, long)> tcs = new(TaskCreationOptions.RunContinuationsAsynchronously);
        pending[reqId] = tcs;

        using CancellationTokenRegistration reg = ct.Register(() => tcs.TrySetCanceled());
        try {
            Intent intent = new(ActionCommand);
            intent.SetPackage(CavernizePackage);
            intent.PutExtra(ExtraReqId, reqId);
            intent.PutExtra(ExtraCmd, (cmd ?? "ping").Trim().ToLowerInvariant());
            intent.PutExtra(ExtraArgsJson, string.IsNullOrWhiteSpace(argsJson) ? "{}" : argsJson);
            intent.PutExtra(ExtraMaxTier, Math.Clamp(maxTier, 0, 2));
            intent.PutExtra(ExtraReplyPackage, context.PackageName);
            context.SendBroadcast(intent, BridgePermission);

            int clampedTimeout = Math.Clamp(timeoutSeconds, 2, 180);
            Task completed = await Task.WhenAny(tcs.Task, Task.Delay(TimeSpan.FromSeconds(clampedTimeout), ct)).ConfigureAwait(false);
            if (completed == tcs.Task) {
                return await tcs.Task.ConfigureAwait(false);
            }
            return (false, $"Cavernize local IPC timed out after {timeoutSeconds}s (ensure Cavernize Android is updated and signed with the matching release key).", 0);
        } finally {
            pending.TryRemove(reqId, out _);
        }
    }



    /// <summary>
    /// Updates external OmniAgent IPC health counters whenever any <c>cavernize_cmd</c> completes or times out.
    /// </summary>
    public static void NoteExternalIpcOutcome(bool ok, string detail) {
        lock (watchdogLock) {
            if (ok) {
                watchdogConsecutiveTimeouts = 0;
                watchdogLastAliveUtc = DateTime.UtcNow.ToString("O");
                watchdogLastError = "";
            } else if (!string.IsNullOrEmpty(detail) &&
                       detail.Contains("timed out", StringComparison.OrdinalIgnoreCase)) {
                watchdogConsecutiveTimeouts++;
                watchdogLastError = detail.Length > 220 ? detail[..220] : detail;
            }
        }
    }

    /// <summary>
    /// External Q2 Cavernize PID, heartbeat &amp; ANR timeout watchdog executed inside OmniAgent's isolated process.
    /// Detects frozen/ANR main-thread deadlocks (bounded ping timeout) and silent crash-restarts (PID/sessionId change)
    /// even when Cavernize itself cannot emit logs or telemetry.
    /// </summary>
    public static async Task<object> ProbeCavernizeWatchdogAsync(
        Context context,
        int timeoutSeconds = 6,
        bool resetBaseline = false,
        CancellationToken ct = default) {
        if (resetBaseline) {
            lock (watchdogLock) {
                watchdogLastPid = 0;
                watchdogLastSessionId = "";
                watchdogConsecutiveTimeouts = 0;
                watchdogPidChanges = 0;
                watchdogLastError = "";
            }
        }
        if (context == null || !OmniAgentForegroundService.IsPackageInstalled(context, CavernizePackage)) {
            return new {
                ok = false,
                state = "NOT_INSTALLED",
                alive = false,
                pid = 0,
                prevPid = 0,
                sessionId = "",
                prevSessionId = "",
                pidRestarted = false,
                consecutiveTimeouts = 0,
                pidChanges = 0,
                elapsedMs = 0L,
                detail = "Cavernize Android is not installed."
            };
        }
        if (!IsSignatureVerified(context)) {
            return new {
                ok = false,
                state = "SIGNATURE_MISMATCH",
                alive = false,
                pid = 0,
                prevPid = 0,
                sessionId = "",
                prevSessionId = "",
                pidRestarted = false,
                consecutiveTimeouts = 0,
                pidChanges = 0,
                elapsedMs = 0L,
                detail = "Cavernize APK signature mismatch."
            };
        }

        int probeTimeout = Math.Clamp(timeoutSeconds, 2, 15);
        System.Diagnostics.Stopwatch sw = System.Diagnostics.Stopwatch.StartNew();
        (bool ipcOk, string body, long ipcElapsedMs) = await SendCommandAsync(
            context, "ping", "{}", maxTier: 0, timeoutSeconds: probeTimeout, ct).ConfigureAwait(false);
        sw.Stop();
        long elapsedMs = ipcElapsedMs > 0 ? ipcElapsedMs : sw.ElapsedMilliseconds;

        int curPid = 0;
        string curSid = "";
        string appVer = "";
        bool uiHost = false;
        if (ipcOk && !string.IsNullOrWhiteSpace(body)) {
            try {
                using JsonDocument doc = JsonDocument.Parse(body);
                JsonElement root = doc.RootElement;
                if (root.TryGetProperty("pid", out JsonElement pidEl) && pidEl.TryGetInt32(out int parsedPid)) {
                    curPid = parsedPid;
                }
                if (root.TryGetProperty("sessionId", out JsonElement sidEl)) {
                    curSid = sidEl.GetString() ?? "";
                }
                if (root.TryGetProperty("appVersion", out JsonElement verEl)) {
                    appVer = verEl.GetString() ?? "";
                }
                if (root.TryGetProperty("uiHostAttached", out JsonElement uiEl) &&
                    (uiEl.ValueKind == JsonValueKind.True || uiEl.ValueKind == JsonValueKind.False)) {
                    uiHost = uiEl.GetBoolean();
                }
            } catch {
                // Non-JSON reply still treated via ipcOk
            }
        }

        int prevPid;
        string prevSid;
        bool pidRestarted = false;
        int consecTimeouts;
        int totalChanges;
        int totalProbes;
        string lastAlive;
        string state;
        string detail;

        lock (watchdogLock) {
            watchdogTotalProbes++;
            totalProbes = watchdogTotalProbes;
            prevPid = watchdogLastPid;
            prevSid = watchdogLastSessionId;

            if (!ipcOk) {
                watchdogConsecutiveTimeouts++;
                consecTimeouts = watchdogConsecutiveTimeouts;
                totalChanges = watchdogPidChanges;
                lastAlive = watchdogLastAliveUtc;
                state = "UNRESPONSIVE_ANR";
                detail = string.IsNullOrWhiteSpace(body)
                    ? $"Cavernize failed to reply to external watchdog ping within {probeTimeout}s"
                    : (body.Length > 260 ? body[..260] : body);
                watchdogLastError = detail;
            } else {
                watchdogConsecutiveTimeouts = 0;
                consecTimeouts = 0;
                bool pidChanged = prevPid > 0 && curPid > 0 && curPid != prevPid;
                bool sidChanged = !string.IsNullOrEmpty(prevSid) && !string.IsNullOrEmpty(curSid) &&
                                  !string.Equals(prevSid, curSid, StringComparison.Ordinal);
                if (pidChanged || sidChanged) {
                    pidRestarted = true;
                    watchdogPidChanges++;
                }
                if (curPid > 0) {
                    watchdogLastPid = curPid;
                }
                if (!string.IsNullOrEmpty(curSid)) {
                    watchdogLastSessionId = curSid;
                }
                watchdogLastAliveUtc = DateTime.UtcNow.ToString("O");
                watchdogLastError = "";
                totalChanges = watchdogPidChanges;
                lastAlive = watchdogLastAliveUtc;
                state = pidRestarted ? "PID_RESTARTED_CRASH" : "ALIVE";
                detail = pidRestarted
                    ? $"Cavernize process restarted externally (pid {prevPid}->{curPid}, sid '{prevSid}'->'{curSid}')"
                    : $"Cavernize alive (pid={curPid}, sid={curSid}, uiHost={uiHost}, {elapsedMs}ms)";
            }
        }

        OmniLogger.Info("WATCHDOG", $"cavernize_watchdog state={state} pid={curPid} sid={curSid} elapsed={elapsedMs}ms");
        return new {
            ok = ipcOk && !pidRestarted,
            state,
            alive = ipcOk,
            pid = curPid,
            prevPid,
            sessionId = curSid,
            prevSessionId = prevSid,
            pidRestarted,
            consecutiveTimeouts = consecTimeouts,
            pidChanges = totalChanges,
            totalProbes,
            lastAliveUtc = lastAlive,
            uiHostAttached = uiHost,
            appVersion = appVer,
            elapsedMs,
            detail
        };
    }

    /// <summary>
    /// Queries same-signature CavernizeAndroid over local Broadcast IPC (<c>get_omni_bootstrap</c>)
    /// to automatically import its Cloudflare Worker URL and 256-bit Pair Key without manual pasting.
    /// </summary>
    public static async Task<(bool imported, string detail)> TryBootstrapFromCavernizeAsync(
        Context context,
        OmniSettingsStore store,
        bool overwriteExisting = false,
        CancellationToken ct = default) {
        if (context == null || store == null) {
            return (false, "Missing context/store.");
        }
        if (!IsSignatureVerified(context)) {
            return (false, "Cavernize not installed or signature mismatch.");
        }
        (bool ok, string body, long ms) = await SendCommandAsync(
            context, "get_omni_bootstrap", "{}", maxTier: 0, timeoutSeconds: 8, ct).ConfigureAwait(false);
        if (!ok || string.IsNullOrWhiteSpace(body)) {
            OmniLogger.Warn("IPC", $"Cavernize bootstrap failed ({ms} ms): {body}");
            return (false, string.IsNullOrWhiteSpace(body) ? "Cavernize did not reply (update Cavernize to latest APK)." : body);
        }
        try {
            using JsonDocument doc = JsonDocument.Parse(body);
            JsonElement root = doc.RootElement;
            string relayUrl = root.TryGetProperty("relayUrl", out JsonElement rEl) ? (rEl.GetString() ?? "") : "";
            string pairHex = root.TryGetProperty("pairSecretHex", out JsonElement pEl) ? (pEl.GetString() ?? "") : "";
            string normPair = OmniCrypto.NormalizePairSecretHex(pairHex);

            bool changed = false;
            if (OmniSettingsStore.TryNormalizeRelayUrl(relayUrl, out string normRelay) &&
                (overwriteExisting || string.IsNullOrWhiteSpace(store.RelayUrl) ||
                 store.RelayUrl.Contains("your-worker", StringComparison.OrdinalIgnoreCase))) {
                store.RelayUrl = normRelay;
                changed = true;
            }
            if (!string.IsNullOrEmpty(normPair) && (overwriteExisting || !store.HasPairSecret)) {
                store.SavePairSecretHex(normPair);
                changed = true;
            }
            if (changed) {
                OmniSettingsStore.AppendActivity("IPC", $"Synced Relay URL & 256-bit Pair Key from Cavernize ({ms} ms).");
                return (true, $"Synced from Cavernize ({ms} ms)");
            }
            if (string.IsNullOrEmpty(normPair)) {
                return (false, "Cavernize has no Pair Key saved yet.");
            }
            return (false, "Already configured.");
        } catch (Exception ex) {
            OmniLogger.Warn("IPC", $"Bootstrap parse error: {ex.Message}");
            return (false, ex.Message);
        }
    }

    sealed class ReplyReceiver : BroadcastReceiver {
        public override void OnReceive(Context context, Intent intent) {
            if (intent == null || !string.Equals(intent.Action, ActionReply, StringComparison.Ordinal)) {
                return;
            }
            string reqId = intent.GetStringExtra(ExtraReqId) ?? "";
            if (string.IsNullOrEmpty(reqId)) {
                return;
            }
            bool ok = intent.GetBooleanExtra(ExtraOk, false);
            string body = intent.GetStringExtra(ExtraBody) ?? "";
            long elapsedMs = intent.GetLongExtra(ExtraElapsedMs, 0L);
            if (pending.TryRemove(reqId, out TaskCompletionSource<(bool, string, long)> tcs)) {
                tcs.TrySetResult((ok, body, elapsedMs));
            }
        }
    }
}
