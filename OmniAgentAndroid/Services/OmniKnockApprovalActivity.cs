using System.Collections.Concurrent;

using Android.App;
using Android.Content;
using Android.Hardware.Biometrics;
using Android.OS;
using Android.Provider;

using OmniAgentAndroid.Language;

namespace OmniAgentAndroid.Services;

/// <summary>
/// Completely invisible translucent Activity host that invokes Android / Samsung One UI's
/// native system <see cref="BiometricPrompt"/> bottom sheet directly over whatever app the
/// user is currently using — NEVER opening OmniAgent's UI window and backed by a hardware
/// StrongBox/TEE ECDSA-P256 <see cref="BiometricPrompt.CryptoObject"/> signature over the
/// Smart Knock challenge.
/// </summary>
[Activity(
    Exported = false,
    Theme = "@style/OmniSystemPromptTheme",
    TaskAffinity = "hu.sbence.cavern.omniagent.biometric",
    LaunchMode = Android.Content.PM.LaunchMode.SingleInstance,
    ExcludeFromRecents = true,
    NoHistory = true)]
public sealed class OmniKnockApprovalActivity : Activity {
    public const string ExtraKnockReqId = "omni_knock_req_id";
    public const string ExtraSasCode = "omni_knock_sas";
    public const string ExtraRequester = "omni_knock_requester";
    public const string ExtraReason = "omni_knock_reason";
    public const string ExtraClientPub = "omni_knock_client_pub";

    const int KnockNotifId = 9192;
    const string KnockChannelId = "omni_agent_knock_urgent";

    static readonly ConcurrentDictionary<string, TaskCompletionSource<(bool approved, bool bioVerified, string hwSig)>> pendingKnocks = new(StringComparer.Ordinal);

    string currentReqId = "";
    string currentChallenge = "";
    CancellationSignal cancelSignal;
    bool resolved;

    public static bool CanDrawInstantOverlay(Context context) {
        if (context == null) {
            return false;
        }
        return !OperatingSystem.IsAndroidVersionAtLeast(23) || Settings.CanDrawOverlays(context);
    }

    public static async Task<(bool approved, bool biometricVerified, string hardwareSigB64)> RequestApprovalAsync(
        Context context,
        string knockReqId,
        string sasCode,
        string requester,
        string reason,
        string clientPubB64,
        CancellationToken ct) {
        if (context == null || string.IsNullOrEmpty(knockReqId)) {
            return (false, false, "");
        }
        TaskCompletionSource<(bool, bool, string)> tcs = new(TaskCreationOptions.RunContinuationsAsynchronously);
        pendingKnocks[knockReqId] = tcs;
        using CancellationTokenRegistration reg = ct.Register(() => tcs.TrySetResult((false, false, "")));
        try {
            Intent intent = new(context, typeof(OmniKnockApprovalActivity));
            intent.PutExtra(ExtraKnockReqId, knockReqId);
            intent.PutExtra(ExtraSasCode, sasCode ?? "---");
            intent.PutExtra(ExtraRequester, requester ?? "AI Studio");
            intent.PutExtra(ExtraReason, reason ?? "Zero-paste auto-connect");
            intent.PutExtra(ExtraClientPub, clientPubB64 ?? "");
            intent.AddFlags(
                ActivityFlags.NewTask |
                ActivityFlags.MultipleTask |
                ActivityFlags.NoAnimation |
                ActivityFlags.ExcludeFromRecents);

            bool hasOverlay = CanDrawInstantOverlay(context);
            if (!hasOverlay) {
                PostFallbackNotification(context, intent, sasCode ?? "---", requester ?? "AI Studio", reason ?? "");
            }
            try {
                context.StartActivity(intent);
            } catch {
                PostFallbackNotification(context, intent, sasCode ?? "---", requester ?? "AI Studio", reason ?? "");
            }

            Task completed = await Task.WhenAny(tcs.Task, Task.Delay(TimeSpan.FromSeconds(110), ct)).ConfigureAwait(false);
            return completed == tcs.Task ? await tcs.Task.ConfigureAwait(false) : (false, false, "");
        } catch {
            return (false, false, "");
        } finally {
            pendingKnocks.TryRemove(knockReqId, out _);
            CancelFallbackNotification(context);
        }
    }

    static void PostFallbackNotification(Context context, Intent fullScreenIntent, string sas, string requester, string reason) {
        try {
            NotificationManager nm = (NotificationManager)context.GetSystemService(NotificationService);
            if (nm == null) {
                return;
            }
            if (OperatingSystem.IsAndroidVersionAtLeast(26)) {
                NotificationChannel ch = new(KnockChannelId, "Smart Knock Biometric Prompt", NotificationImportance.High) {
                    Description = "Native system fingerprint approval for AI Studio connections"
                };
                nm.CreateNotificationChannel(ch);
            }
            PendingIntentFlags piFlags = OperatingSystem.IsAndroidVersionAtLeast(23)
                ? PendingIntentFlags.UpdateCurrent | PendingIntentFlags.Immutable
                : PendingIntentFlags.UpdateCurrent;
            PendingIntent pi = PendingIntent.GetActivity(context, KnockNotifId, fullScreenIntent, piFlags);

            Notification.Builder b = OperatingSystem.IsAndroidVersionAtLeast(26)
                ? new Notification.Builder(context, KnockChannelId)
                : new Notification.Builder(context);
            b.SetSmallIcon(Resource.Drawable.omni_icon)
                .SetContentTitle($"OmniAgent ({sas}) · {requester}")
                .SetContentText(string.IsNullOrWhiteSpace(reason) ? "Toque para aprovar com sua digital" : reason)
                .SetAutoCancel(true)
                .SetCategory(Notification.CategoryCall)
                .SetContentIntent(pi)
                .SetFullScreenIntent(pi, true);
            nm.Notify(KnockNotifId, b.Build());
        } catch {
            // Best effort
        }
    }

    static void CancelFallbackNotification(Context context) {
        try {
            NotificationManager nm = (NotificationManager)context?.GetSystemService(NotificationService);
            nm?.Cancel(KnockNotifId);
        } catch {
            // Best effort
        }
    }

    protected override void OnCreate(Bundle savedInstanceState) {
        base.OnCreate(savedInstanceState);
        SuppressTransitions();
        if (OperatingSystem.IsAndroidVersionAtLeast(27)) {
            SetShowWhenLocked(true);
            SetTurnScreenOn(true);
        }

        OmniSettingsStore store = new(this);
        OmniStrings.Refresh(store.UiLanguage);
        OmniStrings S = OmniStrings.Active;

        currentReqId = Intent?.GetStringExtra(ExtraKnockReqId) ?? "";
        string sas = Intent?.GetStringExtra(ExtraSasCode) ?? "---";
        string requester = Intent?.GetStringExtra(ExtraRequester) ?? "AI Studio";
        string reason = Intent?.GetStringExtra(ExtraReason) ?? "Zero-paste connection";
        string clientPub = Intent?.GetStringExtra(ExtraClientPub) ?? "";
        currentChallenge = $"knock-hw-v1|{currentReqId}|{sas}|{clientPub}";

        if (string.IsNullOrEmpty(currentReqId)) {
            FinishNoAnim();
            return;
        }

        // ZERO SetContentView! Keep host 100% invisible and launch Samsung/Android native system BiometricPrompt directly.
        Window?.DecorView?.Post(() => TriggerNativeSystemBiometric(
            S["KnockPromptTitle"],
            string.Format(S["KnockPromptSubFmt"], sas, requester),
            string.Format(S["KnockPromptDescFmt"], reason),
            S["KnockDenyBtn"]));
    }

    void SuppressTransitions() {
        try {
            if (OperatingSystem.IsAndroidVersionAtLeast(34)) {
                OverrideActivityTransition(OverrideTransition.Open, 0, 0);
                OverrideActivityTransition(OverrideTransition.Close, 0, 0);
            } else {
#pragma warning disable CS0618, CA1422
                OverridePendingTransition(0, 0);
#pragma warning restore CS0618, CA1422
            }
        } catch {
            // Best effort
        }
    }

    void TriggerNativeSystemBiometric(string title, string subtitle, string description, string denyLabel) {
        if (resolved) {
            return;
        }
        if (!OperatingSystem.IsAndroidVersionAtLeast(28)) {
            Complete(true, false, "");
            return;
        }
        try {
            cancelSignal?.Cancel();
            cancelSignal = new CancellationSignal();

            var hwMode = OmniHardwareBiometric.ProbeCapability(this);
            BiometricPrompt.CryptoObject cryptoObj = null;
            if (hwMode != OmniHardwareBiometric.HardwareBacking.DeviceCredentialFallback) {
                (cryptoObj, _) = OmniHardwareBiometric.TryCreateCryptoObject();
            }

            BiometricPrompt.Builder builder = new BiometricPrompt.Builder(this)
                .SetTitle(title)
                .SetSubtitle(subtitle)
                .SetDescription(description);

            if (OperatingSystem.IsAndroidVersionAtLeast(29)) {
                builder.SetConfirmationRequired(false);
            }

            bool useStrongBio = hwMode != OmniHardwareBiometric.HardwareBacking.DeviceCredentialFallback;
            if (OperatingSystem.IsAndroidVersionAtLeast(30)) {
                if (useStrongBio) {
                    // BIOMETRIC_STRONG allows clean native NegativeButton ("Recusar (15m)") AND CryptoObject!
                    builder.SetAllowedAuthenticators(0x000F);
                    builder.SetNegativeButton(denyLabel, MainExecutor, new NegClick(() => Complete(false, false, "")));
                } else {
                    // DEVICE_CREDENTIAL fallback (API 30+ forbids SetNegativeButton with DEVICE_CREDENTIAL)
                    builder.SetAllowedAuthenticators(0x000F | 0x8000);
                    cryptoObj = null;
                }
            } else {
                builder.SetNegativeButton(denyLabel, MainExecutor, new NegClick(() => Complete(false, false, "")));
            }

            BiometricPrompt prompt = builder.Build();
            KnockAuthCallback cb = new(this);
            if (cryptoObj != null) {
                prompt.Authenticate(cryptoObj, cancelSignal, MainExecutor, cb);
            } else {
                prompt.Authenticate(cancelSignal, MainExecutor, cb);
            }
        } catch {
            Complete(false, false, "");
        }
    }

    void Complete(bool approved, bool biometricVerified, string hwSig) {
        if (resolved) {
            return;
        }
        resolved = true;
        if (!string.IsNullOrEmpty(currentReqId) && pendingKnocks.TryRemove(currentReqId, out TaskCompletionSource<(bool, bool, string)> tcs)) {
            tcs.TrySetResult((approved, biometricVerified, hwSig ?? ""));
        }
        FinishNoAnim();
    }

    void FinishNoAnim() {
        Finish();
        SuppressTransitions();
    }

    protected override void OnDestroy() {
        try {
            cancelSignal?.Cancel();
        } catch {
            // Ignore
        }
        if (!resolved) {
            Complete(false, false, "");
        }
        base.OnDestroy();
    }

    sealed class NegClick(Action fn) : Java.Lang.Object, IDialogInterfaceOnClickListener {
        public void OnClick(IDialogInterface dialog, int which) => fn();
    }

    [System.Runtime.Versioning.SupportedOSPlatform("android28.0")]
    sealed class KnockAuthCallback(OmniKnockApprovalActivity owner) : BiometricPrompt.AuthenticationCallback {
        public override void OnAuthenticationSucceeded(BiometricPrompt.AuthenticationResult result) {
            string hwSig = OmniHardwareBiometric.TrySignChallenge(result, owner.currentChallenge);
            owner.Complete(true, true, hwSig);
        }

        public override void OnAuthenticationError(BiometricErrorCode errorCode, Java.Lang.ICharSequence errString) {
            owner.Complete(false, false, "");
        }
    }
}
