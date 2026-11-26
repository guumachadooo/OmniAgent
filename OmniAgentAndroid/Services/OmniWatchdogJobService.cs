using Android.App;
using Android.App.Job;
using Android.Content;

namespace OmniAgentAndroid.Services;

/// <summary>
/// Persisted Android <see cref="JobScheduler"/> watchdog and Android 14+ <c>UserInitiated</c>
/// bridge runner ensuring OmniAgent's Always-On bridge recovers cleanly after Samsung One UI
/// background kills, Doze, reboot, or Android 15–17 <c>dataSync</c> 6-hour FGS timeout without
/// triggering <c>ForegroundServiceStartNotAllowedException</c>.
/// </summary>
[Service(
    Exported = false,
    Permission = "android.permission.BIND_JOB_SERVICE")]
public sealed class OmniWatchdogJobService : JobService {
    const int WatchdogJobId = 9199;
    const int UserInitiatedJobId = 9200;

    CancellationTokenSource jobCts;

    public static void Schedule(Context context) {
        if (context == null) {
            return;
        }
        try {
            JobScheduler js = context.GetSystemService(JobSchedulerService) as JobScheduler;
            if (js == null || js.GetPendingJob(WatchdogJobId) != null) {
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

    public static void ScheduleUserInitiated(Context context) {
        if (context == null) {
            return;
        }
        try {
            JobScheduler js = context.GetSystemService(JobSchedulerService) as JobScheduler;
            if (js == null || js.GetPendingJob(UserInitiatedJobId) != null) {
                return;
            }
            ComponentName comp = new(context, Java.Lang.Class.FromType(typeof(OmniWatchdogJobService)));
            if (OperatingSystem.IsAndroidVersionAtLeast(34) && js.CanRunUserInitiatedJobs()) {
                try {
                    JobInfo uiJob = new JobInfo.Builder(UserInitiatedJobId, comp)
                        .SetRequiredNetworkType(NetworkType.Any)
                        .SetUserInitiated(true)
                        .Build();
                    js.Schedule(uiJob);
                    return;
                } catch {
                    // Fall back to expedited / standard one-shot job when UIJT eligibility is unavailable
                }
            }
            if (OperatingSystem.IsAndroidVersionAtLeast(31)) {
                try {
                    JobInfo expJob = new JobInfo.Builder(UserInitiatedJobId, comp)
                        .SetRequiredNetworkType(NetworkType.Any)
                        .SetExpedited(true)
                        .Build();
                    js.Schedule(expJob);
                    return;
                } catch {
                    // Fall back to standard immediate job when expedited quota is exhausted
                }
            }
            JobInfo fallbackJob = new JobInfo.Builder(UserInitiatedJobId, comp)
                .SetRequiredNetworkType(NetworkType.Any)
                .SetMinimumLatency(1500L)
                .Build();
            js.Schedule(fallbackJob);
        } catch {
            // Best effort
        }
    }

    public static void Cancel(Context context) {
        try {
            if (context?.GetSystemService(JobSchedulerService) is JobScheduler js) {
                js.Cancel(WatchdogJobId);
                js.Cancel(UserInitiatedJobId);
            }
        } catch {
            // Best effort
        }
    }

    public override bool OnStartJob(JobParameters @params) {
        try {
            OmniSettingsStore store = new(this);
            if (!store.ServiceEnabled ||
                !store.HasPairSecret ||
                !OmniSettingsStore.TryNormalizeRelayUrl(store.RelayUrl, out _)) {
                JobFinished(@params, false);
                return false;
            }

            bool isUserInitiated = OperatingSystem.IsAndroidVersionAtLeast(34) && @params != null && @params.JobId == UserInitiatedJobId;
            if (isUserInitiated && OperatingSystem.IsAndroidVersionAtLeast(34)) {
                try {
                    OmniAgentForegroundService.EnsureNotificationChannel(this);
                    SetNotification(
                        @params,
                        OmniAgentForegroundService.NotifId,
                        OmniAgentForegroundService.BuildServiceNotification(this, isJobMode: true),
                        JobEndNotificationPolicy.Detach);
                } catch {
                    // Best effort
                }
            }

            Context appCtx = ApplicationContext ?? this;
            if (OmniAgentForegroundService.IsRunning) {
                JobFinished(@params, false);
                return false;
            }
            if (OmniAgentForegroundService.IsBatteryUnrestricted(appCtx)) {
                OmniSettingsStore.AppendActivity("WATCH", "Battery unrestricted — reviving unlimited remoteMessaging FGS.");
                OmniAgentForegroundService.Start(appCtx);
                JobFinished(@params, false);
                return false;
            }

            jobCts?.Cancel();
            jobCts?.Dispose();
            jobCts = new CancellationTokenSource();
            CancellationToken ct = jobCts.Token;
            TimeSpan maxRun = isUserInitiated ? TimeSpan.FromMinutes(8) : TimeSpan.FromSeconds(75);

            OmniSettingsStore.AppendActivity(
                "WATCH",
                isUserInitiated
                    ? "UserInitiated JobScheduler running bridge session."
                    : "Watchdog JobScheduler running background bridge check.");

            _ = Task.Run(async () => {
                try {
                    await OmniAgentForegroundService.RunBridgeLoopAsync(appCtx, ct, maxRun).ConfigureAwait(false);
                } catch {
                    // Best effort
                } finally {
                    try {
                        JobFinished(@params, false);
                    } catch {
                        // Ignore
                    }
                }
            });
            return true;
        } catch {
            try {
                JobFinished(@params, false);
            } catch {
                // Ignore
            }
            return false;
        }
    }

    public override bool OnStopJob(JobParameters @params) {
        try {
            jobCts?.Cancel();
            jobCts?.Dispose();
            jobCts = null;
        } catch {
            // Ignore
        }
        return false;
    }
}
