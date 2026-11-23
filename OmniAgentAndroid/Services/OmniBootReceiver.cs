using Android.App;
using Android.Content;

namespace OmniAgentAndroid.Services;

/// <summary>
/// Automatically restarts <see cref="OmniAgentForegroundService"/> after device reboot or APK update
/// when the user previously enabled the agent, preventing background bridge downtime.
/// </summary>
[BroadcastReceiver(Enabled = true, Exported = true)]
[IntentFilter([Intent.ActionBootCompleted, Intent.ActionMyPackageReplaced])]
public sealed class OmniBootReceiver : BroadcastReceiver {
    public override void OnReceive(Context context, Intent intent) {
        if (context == null || intent == null) {
            return;
        }
        string action = intent.Action ?? "";
        if (!string.Equals(action, Intent.ActionBootCompleted, StringComparison.Ordinal) &&
            !string.Equals(action, Intent.ActionMyPackageReplaced, StringComparison.Ordinal)) {
            return;
        }
        try {
            OmniSettingsStore store = new(context);
            if (store.ServiceEnabled && store.HasPairSecret && OmniSettingsStore.TryNormalizeRelayUrl(store.RelayUrl, out _)) {
                OmniSettingsStore.AppendActivity("BOOT", $"Auto-restarting OmniAgent after {action}.");
                OmniAgentForegroundService.Start(context);
            }
        } catch (Exception ex) {
            OmniSettingsStore.AppendActivity("ERR", $"Boot auto-start skipped: {ex.Message}");
        }
    }
}
