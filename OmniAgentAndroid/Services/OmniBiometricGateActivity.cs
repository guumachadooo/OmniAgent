using System.Collections.Concurrent;

using Android.App;
using Android.Content;
using Android.Hardware.Biometrics;
using Android.OS;

namespace OmniAgentAndroid.Services;

/// <summary>
/// Translucent native BiometricPrompt gate invoked ONLY when the user explicitly selects
/// <c>biometric</c> security mode for critical remote operations (<c>github_push_bundle</c>,
/// <c>install_apk_url</c>, <c>termux_run</c>). Never interrupts in <c>smart</c> or <c>trusted</c> mode.
/// </summary>
[Activity(
    Exported = false,
    Theme = "@style/OmniBiometricOverlayTheme",
    ExcludeFromRecents = true,
    NoHistory = true)]
public sealed class OmniBiometricGateActivity : Activity {
    public const string ExtraRequestId = "omni_gate_req_id";
    public const string ExtraTitle = "omni_gate_title";
    public const string ExtraSubtitle = "omni_gate_subtitle";

    static readonly ConcurrentDictionary<string, TaskCompletionSource<bool>> pendingGates = new(StringComparer.Ordinal);
    string currentReqId = "";

    const int GateNotifId = 9193;
    const string GateChannelId = "omni_agent_knock_urgent";

    public static async Task<bool> RequestApprovalAsync(Context context, string requestId, string title, string subtitle, CancellationToken ct) {
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
            intent.AddFlags(ActivityFlags.NewTask | ActivityFlags.ClearTop | ActivityFlags.SingleTop | ActivityFlags.ReorderToFront);

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
                NotificationChannel ch = new(GateChannelId, "Smart Knock Approval", NotificationImportance.High);
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
        if (OperatingSystem.IsAndroidVersionAtLeast(27)) {
            SetShowWhenLocked(true);
            SetTurnScreenOn(true);
        }
        currentReqId = Intent?.GetStringExtra(ExtraRequestId) ?? "";
        string title = Intent?.GetStringExtra(ExtraTitle) ?? "OmniAgent";
        string subtitle = Intent?.GetStringExtra(ExtraSubtitle) ?? "Approve critical action";

        if (!OperatingSystem.IsAndroidVersionAtLeast(28)) {
            Complete(true);
            return;
        }

        try {
            ShowBiometricPromptApi28(title, subtitle);
        } catch {
            Complete(false);
        }
    }

    [System.Runtime.Versioning.SupportedOSPlatform("android28.0")]
    void ShowBiometricPromptApi28(string title, string subtitle) {
        BiometricPrompt.Builder builder = new BiometricPrompt.Builder(this)
            .SetTitle(title)
            .SetSubtitle(subtitle);

        if (OperatingSystem.IsAndroidVersionAtLeast(30)) {
            const int AuthenticatorBiometricStrong = 0x000F;
            const int AuthenticatorDeviceCredential = 0x8000;
            builder.SetAllowedAuthenticators(AuthenticatorBiometricStrong | AuthenticatorDeviceCredential);
        } else {
            builder.SetNegativeButton("Deny", MainExecutor, new NegativeClick(() => Complete(false)));
        }

        CancellationSignal cancelSignal = new();
        BiometricPrompt prompt = builder.Build();
        prompt.Authenticate(cancelSignal, MainExecutor, new AuthCallback(this));
    }

    void Complete(bool approved) {
        if (!string.IsNullOrEmpty(currentReqId) && pendingGates.TryRemove(currentReqId, out TaskCompletionSource<bool> tcs)) {
            tcs.TrySetResult(approved);
        }
        Finish();
    }

    sealed class NegativeClick(Action onDeny) : Java.Lang.Object, IDialogInterfaceOnClickListener {
        public void OnClick(IDialogInterface dialog, int which) => onDeny();
    }

    [System.Runtime.Versioning.SupportedOSPlatform("android28.0")]
    sealed class AuthCallback(OmniBiometricGateActivity owner) : BiometricPrompt.AuthenticationCallback {
        public override void OnAuthenticationSucceeded(BiometricPrompt.AuthenticationResult result) =>
            owner.Complete(true);

        public override void OnAuthenticationError(BiometricErrorCode errorCode, Java.Lang.ICharSequence errString) {
            // Note (BL-005): AndroidX BiometricErrorCode.NegativeButton maps to numeric code 13 on platform bindings
            int code = (int)errorCode;
            if (code == 13 || code == (int)BiometricErrorCode.UserCanceled) {
                owner.Complete(false);
                return;
            }
            owner.Complete(false);
        }
    }
}
