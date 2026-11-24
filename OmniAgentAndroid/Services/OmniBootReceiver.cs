using Android.App;
using Android.Content;

namespace OmniAgentAndroid.Services;

/// <summary>
/// Always-On auto-wake receiver handling device boot, APK upgrades, Recents task-removal restarts,
/// Cavernize startup wake pings (<c>OMNI_ENSURE_ALIVE</c>), and Termux Smart Knock wakes
/// (<c>OMNI_WAKE_KNOCK</c>) so OmniAgent wakes automatically even when completely closed.
/// </summary>
[BroadcastReceiver(Enabled = true, Exported = true)]
[IntentFilter([
    Intent.ActionBootCompleted,
    Intent.ActionLockedBootCompleted,
    Intent.ActionMyPackageReplaced,
    "android.intent.action.QUICKBOOT_POWERON",
    ActionWatchdogRestart,
    ActionEnsureOmniAlive,
    ActionWakeKnock
])]
public sealed class OmniBootReceiver : BroadcastReceiver {
    public const string ActionWatchdogRestart = "hu.sbence.cavern.omniagent.WATCHDOG_RESTART";
    public const string ActionEnsureOmniAlive = "hu.sbence.cavern.ipc.OMNI_ENSURE_ALIVE";
    public const string ActionWakeKnock = "hu.sbence.cavern.ipc.OMNI_WAKE_KNOCK";

    public override void OnReceive(Context context, Intent intent) {
        if (context == null || intent == null) {
            return;
        }
        string action = intent.Action ?? "";
        try {
            OmniSettingsStore store = new(context);
            bool forceWake = string.Equals(action, ActionWakeKnock, StringComparison.Ordinal);
            if ((store.ServiceEnabled || forceWake) &&
                store.HasPairSecret &&
                OmniSettingsStore.TryNormalizeRelayUrl(store.RelayUrl, out _)) {
                OmniSettingsStore.AppendActivity("WAKE", $"Auto-waking bridge via JobScheduler ({action}).");
                Context appCtx = context.ApplicationContext ?? context;
                OmniWatchdogJobService.Schedule(appCtx);
                OmniWatchdogJobService.ScheduleUserInitiated(appCtx);
            }
        } catch (Exception ex) {
            OmniSettingsStore.AppendActivity("ERR", $"Auto-wake skipped: {ex.Message}");
        }
    }
}
