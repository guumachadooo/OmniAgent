using System.Collections.Concurrent;

using Android.App;
using Android.Content;
using Android.Hardware.Biometrics;
using Android.OS;

using OmniAgentAndroid.Language;

namespace OmniAgentAndroid.Services;

/// <summary>
/// Invisible native system BiometricPrompt gate invoked ONLY when the user selects
/// <c>biometric</c> mode for critical remote operations. Never opens OmniAgent's UI.
/// </summary>
[Activity(
    Exported = false,
    Theme = "@style/OmniSystemPromptTheme",
    TaskAffinity = "hu.sbence.cavern.omniagent.biometric",
    LaunchMode = Android.Content.PM.LaunchMode.SingleInstance,
    ExcludeFromRecents = true,
    NoHistory = true)]
public sealed class OmniBiometricGateActivity : Activity {
    public const string ExtraRequestId = "omni_gate_req_id";
    public const string ExtraTitle = "omni_gate_title";
    public const string ExtraSubtitle = "omni_gate_subtitle";
    public const string ExtraDescription = "omni_gate_desc";

    static readonly ConcurrentDictionary<string, TaskCompletionSource<bool>> pendingGates = new(StringComparer.Ordinal);

    string currentReqId = "";
    bool resolved;
    const int GateNotifId = 9193;
    const string GateChannelId = "omni_agent_knock_urgent";

    public static async Task<bool> RequestApprovalAsync(
        Context context,
        string requestId,
        string title,
        string subtitle,
        string description,
        CancellationToken ct) {
        if (context == null || string.IsNullOrEmpty(requestId)) {
            return false;
        }
        TaskCompletionSource<bool> tcs = new(TaskCreationOptions.RunContinuationsAsynchronously);
        pendingGates[requestId] = tcs;
        using CancellationTokenRegistration reg = ct.Register(() => tcs.TrySetResult(false));
        try {
            Intent intent = new(context, typeof(OmniBiometricGateActivity));
            intent.PutExtra(ExtraRequestId, requestId);
            intent.PutExtra(ExtraTitle, title ?? "OmniAgent");
            intent.PutExtra(ExtraSubtitle, subtitle ?? "Confirm remote agent action");
            intent.PutExtra(ExtraDescription, description ?? "");
            intent.AddFlags(
                ActivityFlags.NewTask |
                ActivityFlags.MultipleTask |
                ActivityFlags.NoAnimation |
                ActivityFlags.ExcludeFromRecents);

            bool hasOverlay = OmniKnockApprovalActivity.CanDrawInstantOverlay(context);
            if (!hasOverlay) {
                PostGateNotification(context, intent, title ?? "OmniAgent", subtitle ?? "Confirm remote agent action");
            }
            try {
                context.StartActivity(intent);
            } catch {
                PostGateNotification(context, intent, title ?? "OmniAgent", subtitle ?? "Confirm remote agent action");
            }

            Task completed = await Task.WhenAny(tcs.Task, Task.Delay(TimeSpan.FromSeconds(75), ct)).ConfigureAwait(false);
            return completed == tcs.Task && await tcs.Task.ConfigureAwait(false);
        } catch {
            return false;
        } finally {
            pendingGates.TryRemove(requestId, out _);
            try {
                ((NotificationManager)context.GetSystemService(NotificationService))?.Cancel(GateNotifId);
            } catch { }
        }
    }

    static void PostGateNotification(Context context, Intent fullScreenIntent, string title, string subtitle) {
        try {
            NotificationManager nm = (NotificationManager)context.GetSystemService(NotificationService);
            if (nm == null) {
                return;
            }
            if (OperatingSystem.IsAndroidVersionAtLeast(26)) {
                NotificationChannel ch = new(GateChannelId, "Smart Knock Biometric Prompt", NotificationImportance.High);
                nm.CreateNotificationChannel(ch);
            }
            PendingIntentFlags piFlags = OperatingSystem.IsAndroidVersionAtLeast(23)
                ? PendingIntentFlags.UpdateCurrent | PendingIntentFlags.Immutable
                : PendingIntentFlags.UpdateCurrent;
            PendingIntent pi = PendingIntent.GetActivity(context, GateNotifId, fullScreenIntent, piFlags);

            Notification.Builder b = OperatingSystem.IsAndroidVersionAtLeast(26)
                ? new Notification.Builder(context, GateChannelId)
                : new Notification.Builder(context);
            b.SetSmallIcon(Resource.Drawable.omni_icon)
                .SetContentTitle(title)
                .SetContentText(subtitle)
                .SetAutoCancel(true)
                .SetCategory(Notification.CategoryCall)
                .SetContentIntent(pi)
                .SetFullScreenIntent(pi, true);
            nm.Notify(GateNotifId, b.Build());
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

        currentReqId = Intent?.GetStringExtra(ExtraRequestId) ?? "";
        string title = Intent?.GetStringExtra(ExtraTitle) ?? OmniStrings.Active["GatePromptTitle"];
        string subtitle = Intent?.GetStringExtra(ExtraSubtitle) ?? "Approve action";
        string desc = Intent?.GetStringExtra(ExtraDescription) ?? "";

        if (!OperatingSystem.IsAndroidVersionAtLeast(28)) {
            Complete(true);
            return;
        }
        Window?.DecorView?.Post(() => ShowBiometricPromptApi28(title, subtitle, desc));
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

    [System.Runtime.Versioning.SupportedOSPlatform("android28.0")]
    void ShowBiometricPromptApi28(string title, string subtitle, string desc) {
        try {
            var hwMode = OmniHardwareBiometric.ProbeCapability(this);
            BiometricPrompt.CryptoObject cryptoObj = null;
            if (hwMode != OmniHardwareBiometric.HardwareBacking.DeviceCredentialFallback) {
                (cryptoObj, _) = OmniHardwareBiometric.TryCreateCryptoObject();
            }

            BiometricPrompt.Builder builder = new BiometricPrompt.Builder(this)
                .SetTitle(title)
                .SetSubtitle(subtitle);
            if (!string.IsNullOrWhiteSpace(desc)) {
                builder.SetDescription(desc);
            }
            if (OperatingSystem.IsAndroidVersionAtLeast(29)) {
                builder.SetConfirmationRequired(false);
            }

            bool useStrongBio = hwMode != OmniHardwareBiometric.HardwareBacking.DeviceCredentialFallback;
            if (OperatingSystem.IsAndroidVersionAtLeast(30)) {
                if (useStrongBio) {
                    builder.SetAllowedAuthenticators(0x000F);
                    builder.SetNegativeButton(OmniStrings.Active["CancelBtn"], MainExecutor, new NegativeClick(() => Complete(false)));
                } else {
                    builder.SetAllowedAuthenticators(0x000F | 0x8000);
                    cryptoObj = null;
                }
            } else {
                builder.SetNegativeButton(OmniStrings.Active["CancelBtn"], MainExecutor, new NegativeClick(() => Complete(false)));
            }

            CancellationSignal cancelSignal = new();
            BiometricPrompt prompt = builder.Build();
            AuthCallback cb = new(this);
            if (cryptoObj != null) {
                prompt.Authenticate(cryptoObj, cancelSignal, MainExecutor, cb);
            } else {
                prompt.Authenticate(cancelSignal, MainExecutor, cb);
            }
        } catch {
            Complete(false);
        }
    }

    void Complete(bool approved) {
        if (resolved) {
            return;
        }
        resolved = true;
        if (!string.IsNullOrEmpty(currentReqId) && pendingGates.TryRemove(currentReqId, out TaskCompletionSource<bool> tcs)) {
            tcs.TrySetResult(approved);
        }
        Finish();
        SuppressTransitions();
    }

    protected override void OnDestroy() {
        if (!resolved) {
            Complete(false);
        }
        base.OnDestroy();
    }

    sealed class NegativeClick(Action onDeny) : Java.Lang.Object, IDialogInterfaceOnClickListener {
        public void OnClick(IDialogInterface dialog, int which) => onDeny();
    }

    [System.Runtime.Versioning.SupportedOSPlatform("android28.0")]
    sealed class AuthCallback(OmniBiometricGateActivity owner) : BiometricPrompt.AuthenticationCallback {
        public override void OnAuthenticationSucceeded(BiometricPrompt.AuthenticationResult result) =>
            owner.Complete(true);

        public override void OnAuthenticationError(BiometricErrorCode errorCode, Java.Lang.ICharSequence errString) =>
            owner.Complete(false);
    }
}
