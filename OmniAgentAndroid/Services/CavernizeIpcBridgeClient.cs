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

    /// <summary>
    /// Verifies that <c>CavernizeAndroid</c> is installed and signed by the exact same release keystore certificate.
    /// </summary>
    public static bool IsSignatureVerified(Context context) {
        if (context?.PackageManager == null) {
            return false;
        }
        try {
#pragma warning disable CS0618, CA1422
            // Android PackageManager.SIGNATURE_MATCH == 0
            return (int)context.PackageManager.CheckSignatures(context.PackageName, CavernizePackage) == 0;
#pragma warning restore CS0618, CA1422
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

            Task completed = await Task.WhenAny(tcs.Task, Task.Delay(TimeSpan.FromSeconds(Math.Clamp(timeoutSeconds, 5, 180)), ct)).ConfigureAwait(false);
            if (completed == tcs.Task) {
                return await tcs.Task.ConfigureAwait(false);
            }
            return (false, $"Cavernize local IPC timed out after {timeoutSeconds}s (ensure Cavernize is updated and 'Permitir ponte local com OmniAgent' is enabled).", 0);
        } finally {
            pending.TryRemove(reqId, out _);
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
