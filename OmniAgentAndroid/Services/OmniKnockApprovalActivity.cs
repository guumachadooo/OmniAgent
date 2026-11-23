using System.Collections.Concurrent;

using Android.App;
using Android.Content;
using Android.Graphics;
using Android.Graphics.Drawables;
using Android.Hardware.Biometrics;
using Android.OS;
using Android.Util;
using Android.Views;
using Android.Widget;

namespace OmniAgentAndroid.Services;

/// <summary>
/// Translucent One UI 9 AMOLED biometric approval overlay for Zero-Secret Ephemeral P-256 ECDH Smart Knocks.
/// Shows the independently computed 6-character Visual Verification Code (SAS) and requires native Android
/// <see cref="BiometricPrompt"/> before granting encrypted workspace credentials to an unpaired AI Studio chat.
/// </summary>
[Activity(
    Exported = false,
    Theme = "@style/OmniBiometricOverlayTheme",
    ExcludeFromRecents = true,
    NoHistory = true)]
public sealed class OmniKnockApprovalActivity : Activity {
    public const string ExtraKnockReqId = "omni_knock_req_id";
    public const string ExtraSasCode = "omni_knock_sas";
    public const string ExtraRequester = "omni_knock_requester";
    public const string ExtraReason = "omni_knock_reason";

    static readonly ConcurrentDictionary<string, TaskCompletionSource<(bool approved, bool biometricVerified)>> pendingKnocks = new(StringComparer.Ordinal);

    string currentReqId = "";
    bool resolved;
    TextView statusTv;
    Button approveBtn;
    Button denyBtn;
    CancellationSignal cancelSignal;

    const int KnockNotifId = 9192;
    const string KnockChannelId = "omni_agent_knock_urgent";

    public static bool CanDrawInstantOverlay(Context context) {
        if (context == null) {
            return false;
        }
        return !OperatingSystem.IsAndroidVersionAtLeast(23) || Android.Provider.Settings.CanDrawOverlays(context);
    }

    public static async Task<(bool approved, bool biometricVerified)> RequestApprovalAsync(
        Context context,
        string knockReqId,
        string sasCode,
        string requester,
        string reason,
        CancellationToken ct) {
        if (context == null || string.IsNullOrEmpty(knockReqId)) {
            return (false, false);
        }
        TaskCompletionSource<(bool, bool)> tcs = new(TaskCreationOptions.RunContinuationsAsynchronously);
        pendingKnocks[knockReqId] = tcs;

        using CancellationTokenRegistration reg = ct.Register(() => tcs.TrySetResult((false, false)));
        try {
            Intent intent = new(context, typeof(OmniKnockApprovalActivity));
            intent.PutExtra(ExtraKnockReqId, knockReqId);
            intent.PutExtra(ExtraSasCode, sasCode ?? "");
            intent.PutExtra(ExtraRequester, requester ?? "AI Studio");
            intent.PutExtra(ExtraReason, reason ?? "Zero-paste connection");
            intent.AddFlags(ActivityFlags.NewTask | ActivityFlags.ClearTop | ActivityFlags.SingleTop | ActivityFlags.ReorderToFront);

            bool hasOverlay = CanDrawInstantOverlay(context);
            if (hasOverlay) {
                try {
                    context.StartActivity(intent);
                } catch {
                    PostFullScreenFallbackNotification(context, intent, sasCode ?? "---", requester ?? "AI Studio", reason ?? "");
                }
            } else {
                // Always post urgent FullScreenIntent notification AND try direct launch (works if app is visible or screen unlocked via BAL)
                PostFullScreenFallbackNotification(context, intent, sasCode ?? "---", requester ?? "AI Studio", reason ?? "");
                try {
                    context.StartActivity(intent);
                } catch {
                    // User can tap the urgent heads-up notification
                }
            }

            Task completed = await Task.WhenAny(tcs.Task, Task.Delay(TimeSpan.FromSeconds(105), ct)).ConfigureAwait(false);
            return completed == tcs.Task ? await tcs.Task.ConfigureAwait(false) : (false, false);
        } catch {
            return (false, false);
        } finally {
            pendingKnocks.TryRemove(knockReqId, out _);
            CancelFallbackNotification(context);
        }
    }

    static void PostFullScreenFallbackNotification(Context context, Intent fullScreenIntent, string sas, string requester, string reason) {
        try {
            NotificationManager nm = (NotificationManager)context.GetSystemService(NotificationService);
            if (nm == null) {
                return;
            }
            if (OperatingSystem.IsAndroidVersionAtLeast(26)) {
                NotificationChannel ch = new(KnockChannelId, "Smart Knock Approval", NotificationImportance.High) {
                    Description = "Urgent biometric approval requests from AI Studio"
                };
                ch.LockscreenVisibility = NotificationVisibility.Public;
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
                .SetContentTitle($"Smart Knock ({sas}) — {requester}")
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

    int Dp(int v) => (int)Math.Round(TypedValue.ApplyDimension(ComplexUnitType.Dip, v, Resources.DisplayMetrics));

    GradientDrawable Pill(Color fill, int radiusDp, Color? stroke = null) {
        GradientDrawable g = new();
        g.SetColor(fill);
        g.SetCornerRadius(Dp(radiusDp));
        if (stroke.HasValue) {
            g.SetStroke(Dp(1), stroke.Value);
        }
        return g;
    }

    protected override void OnCreate(Bundle savedInstanceState) {
        base.OnCreate(savedInstanceState);
        if (OperatingSystem.IsAndroidVersionAtLeast(27)) {
            SetShowWhenLocked(true);
            SetTurnScreenOn(true);
        }

        currentReqId = Intent?.GetStringExtra(ExtraKnockReqId) ?? "";
        string sas = Intent?.GetStringExtra(ExtraSasCode) ?? "---";
        string requester = Intent?.GetStringExtra(ExtraRequester) ?? "AI Studio";
        string reason = Intent?.GetStringExtra(ExtraReason) ?? "Zero-paste connection";
        if (string.IsNullOrEmpty(currentReqId)) {
            Finish();
            return;
        }

        bool isPt = string.Equals(Java.Util.Locale.Default?.Language, "pt", StringComparison.OrdinalIgnoreCase);

        FrameLayout root = new(this);
        root.SetBackgroundColor(Color.Argb(190, 9, 9, 11));
        root.SetPadding(Dp(20), Dp(28), Dp(20), Dp(28));

        LinearLayout card = new(this) { Orientation = Orientation.Vertical };
        card.SetPadding(Dp(22), Dp(22), Dp(22), Dp(20));
        card.Background = Pill(Color.ParseColor("#16161B"), 24, Color.ParseColor("#2A2A35"));

        TextView title = new(this) {
            Text = isPt ? "Smart Knock — Conectar AI Studio?" : "Smart Knock — Connect AI Studio?",
            TextSize = 18f
        };
        title.SetTextColor(Color.ParseColor("#F4F4F6"));
        title.SetTypeface(Typeface.DefaultBold, TypefaceStyle.Bold);
        card.AddView(title);

        TextView sasBadge = new(this) {
            Text = isPt ? $"Código: {sas}" : $"Code: {sas}",
            TextSize = 21f,
            Gravity = GravityFlags.Center
        };
        sasBadge.SetTypeface(Typeface.Monospace, TypefaceStyle.Bold);
        sasBadge.SetTextColor(Color.ParseColor("#2F7CF6"));
        sasBadge.SetPadding(Dp(14), Dp(10), Dp(14), Dp(10));
        sasBadge.Background = Pill(Color.ParseColor("#202028"), 16, Color.ParseColor("#2F7CF6"));
        card.AddView(sasBadge, new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MatchParent, ViewGroup.LayoutParams.WrapContent) {
            TopMargin = Dp(12),
            BottomMargin = Dp(10)
        });

        TextView info = new(this) {
            Text = isPt
                ? $"Solicitante: {requester}\nMotivo: {reason}\nConfirme o código acima e use sua digital para conectar automaticamente sem enviar tokens."
                : $"Requester: {requester}\nReason: {reason}\nConfirm the code above and verify with biometrics to connect automatically without pasting tokens.",
            TextSize = 13f
        };
        info.SetTextColor(Color.ParseColor("#9CA3AF"));
        info.SetPadding(0, 0, 0, Dp(10));
        card.AddView(info);

        statusTv = new TextView(this) {
            Text = isPt ? "Toque na digital para aprovar..." : "Touch fingerprint to approve...",
            TextSize = 12.5f
        };
        statusTv.SetTextColor(Color.ParseColor("#22C55E"));
        statusTv.SetPadding(0, 0, 0, Dp(14));
        card.AddView(statusTv);

        LinearLayout row = new(this) {
            Orientation = Orientation.Horizontal,
            BaselineAligned = false
        };
        row.SetGravity(GravityFlags.CenterVertical);

        denyBtn = MakeBtn(isPt ? "Recusar (15m)" : "Deny (15m)", Color.ParseColor("#202028"), Color.ParseColor("#F4F4F6"));
        denyBtn.Click += (_, _) => Complete(false, false);

        approveBtn = MakeBtn(isPt ? "Usar Digital" : "Verify Fingerprint", Color.ParseColor("#2F7CF6"), Color.White);
        approveBtn.Click += (_, _) => TriggerBiometric(title.Text, $"SAS {sas} · {reason}");

        row.AddView(denyBtn, new LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WrapContent, 1f) { RightMargin = Dp(8) });
        row.AddView(approveBtn, new LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WrapContent, 1.2f));
        card.AddView(row);

        root.AddView(card, new FrameLayout.LayoutParams(ViewGroup.LayoutParams.MatchParent, ViewGroup.LayoutParams.WrapContent) {
            Gravity = GravityFlags.Center
        });
        SetContentView(root);
        Window?.DecorView?.Post(() => TriggerBiometric(title.Text, $"SAS {sas} · {reason}"));
    }

    Button MakeBtn(string label, Color bg, Color fg) {
        Button b = new(this) { Text = label, TextSize = 13f };
        b.SetAllCaps(false);
        b.SetSingleLine(true);
        b.Ellipsize = Android.Text.TextUtils.TruncateAt.End;
        b.SetTextColor(fg);
        b.SetPadding(Dp(12), Dp(11), Dp(12), Dp(11));
        b.Background = Pill(bg, 16);
        return b;
    }

    void TriggerBiometric(string title, string subtitle) {
        if (resolved) {
            return;
        }
        if (!OperatingSystem.IsAndroidVersionAtLeast(28)) {
            Complete(true, false);
            return;
        }
        try {
            cancelSignal?.Cancel();
            cancelSignal = new CancellationSignal();
            BiometricPrompt.Builder builder = new BiometricPrompt.Builder(this)
                .SetTitle(title)
                .SetSubtitle(subtitle);
            if (OperatingSystem.IsAndroidVersionAtLeast(30)) {
                builder.SetAllowedAuthenticators(0x000F | 0x8000);
            } else {
                builder.SetNegativeButton("Deny", MainExecutor, new NegClick(() => Complete(false, false)));
            }
            builder.Build().Authenticate(cancelSignal, MainExecutor, new KnockAuthCallback(this));
        } catch {
            Complete(false, false);
        }
    }

    void Complete(bool approved, bool biometricVerified) {
        if (resolved) {
            return;
        }
        resolved = true;
        if (!string.IsNullOrEmpty(currentReqId) && pendingKnocks.TryRemove(currentReqId, out TaskCompletionSource<(bool, bool)> tcs)) {
            tcs.TrySetResult((approved, biometricVerified));
        }
        Finish();
    }

    protected override void OnDestroy() {
        try {
            cancelSignal?.Cancel();
        } catch {
            // Ignore
        }
        if (!resolved) {
            Complete(false, false);
        }
        base.OnDestroy();
    }

    sealed class NegClick(Action fn) : Java.Lang.Object, IDialogInterfaceOnClickListener {
        public void OnClick(IDialogInterface dialog, int which) => fn();
    }

    [System.Runtime.Versioning.SupportedOSPlatform("android28.0")]
    sealed class KnockAuthCallback(OmniKnockApprovalActivity owner) : BiometricPrompt.AuthenticationCallback {
        public override void OnAuthenticationSucceeded(BiometricPrompt.AuthenticationResult result) =>
            owner.Complete(true, true);

        public override void OnAuthenticationError(BiometricErrorCode errorCode, Java.Lang.ICharSequence errString) {
            int code = (int)errorCode;
            if (code == 13 || errorCode == BiometricErrorCode.UserCanceled) {
                owner.Complete(false, false);
                return;
            }
            owner.RunOnUiThread(() => {
                if (owner.statusTv != null) {
                    owner.statusTv.Text = errString?.ToString() ?? errorCode.ToString();
                }
            });
        }
    }
}
