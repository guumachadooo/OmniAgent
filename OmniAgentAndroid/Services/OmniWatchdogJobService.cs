using Android.App;
using Android.App.Job;
using Android.Content;

namespace OmniAgentAndroid.Services;

/// <summary>
/// Persisted Android <see cref="JobScheduler"/> watchdog ensuring OmniAgent's Always-On bridge
/// automatically recovers even after Samsung One UI aggressive background kills, Doze, or reboot.
/// </summary>
[Service(
    Exported = false,
    Permission = "android.permission.BIND_JOB_SERVICE")]
public sealed class OmniWatchdogJobService : JobService {
    const int WatchdogJobId = 9199;

    public static void Schedule(Context context) {
        if (context == null) {
            return;
        }
        try {
            JobScheduler js = context.GetSystemService(JobSchedulerService) as JobScheduler;
            if (js == null) {
                return;
            }
            ComponentName comp = new(context, Java.Lang.Class.FromType(typeof(OmniWatchdogJobService)));
            JobInfo job = new JobInfo.Builder(WatchdogJobId, comp)
                .SetRequiredNetworkType(NetworkType.Any)
                .SetPeriodic(15 * 60 * 1000L)
                .SetPersisted(true)
                .Build();
            js.Schedule(job);
        } catch {
            // Best effort
        }
    }

    public static void Cancel(Context context) {
        try {
            (context?.GetSystemService(JobSchedulerService) as JobScheduler)?.Cancel(WatchdogJobId);
        } catch {
            // Best effort
        }
    }

    public override bool OnStartJob(JobParameters @params) {
        try {
            OmniSettingsStore store = new(this);
            if (store.ServiceEnabled &&
                !OmniAgentForegroundService.IsRunning &&
                store.HasPairSecret &&
                OmniSettingsStore.TryNormalizeRelayUrl(store.RelayUrl, out _)) {
                OmniSettingsStore.AppendActivity("WATCH", "Watchdog recovered Always-On service.");
                OmniAgentForegroundService.Start(this);
            }
        } catch {
            // Best effort
        }
        JobFinished(@params, false);
        return false;
    }

    public override bool OnStopJob(JobParameters @params) => false;
}
