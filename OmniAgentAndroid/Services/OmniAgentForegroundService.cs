using System.Net.Http.Headers;
using System.Text.Json;

using Android.App;
using Android.Content;
using Android.OS;

using Path = System.IO.Path;

namespace OmniAgentAndroid.Services;

/// <summary>
/// Standalone background Foreground Service (<c>dataSync</c>) running independently of Cavernize.
/// Connects to Cloudflare Hub v4 on channel <c>android</c> using zero-trust E2E encryption,
/// supporting direct GitHub atomic multi-file push, one-tap APK install, and Termux daemon wake.
/// </summary>
[Service(Exported = false, ForegroundServiceType = Android.Content.PM.ForegroundService.TypeDataSync)]
public sealed class OmniAgentForegroundService : Service {
    public const string TermuxRunCommandPermission = "com.termux.permission.RUN_COMMAND";
    const int NotifId = 9188;
    const string ChannelId = "omni_agent_foreground";
    const string ActionStop = "hu.sbence.cavern.omniagent.ACTION_STOP";

    const string AgentVersion = "2.0.0";

    static readonly HashSet<string> CriticalCommands = new(StringComparer.OrdinalIgnoreCase) {
        "github_push_bundle",
        "cf_deploy_worker",
        "install_apk_url",
        "termux_run"
    };

    static readonly string[] Capabilities = [
        "ping",
        "status",
        "device_info",
        "github_push_bundle",
        "cf_status",
        "cf_deploy_worker",
        "cavernize_cmd",
        "install_apk_url",
        "launch_termux_daemon",
        "termux_run",
        "open_cavernize"
    ];

    static readonly HttpClient http = BuildClient();
    static volatile bool isRunning;
    static volatile int commandsHandled;

    public static bool IsRunning => isRunning;
    public static int CommandsHandled => commandsHandled;
    public static event Action StateChanged;

    CancellationTokenSource cts;

    static HttpClient BuildClient() {
        HttpClient c = new() { Timeout = TimeSpan.FromSeconds(30) };
        c.DefaultRequestHeaders.UserAgent.Add(new ProductInfoHeaderValue("OmniAgentAndroid", "2.0"));
        c.DefaultRequestHeaders.Accept.Add(new MediaTypeWithQualityHeaderValue("application/json"));
        return c;
    }

    public static bool IsBatteryUnrestricted(Context context) {
        if (context == null || !OperatingSystem.IsAndroidVersionAtLeast(23)) {
            return true;
        }
        try {
            PowerManager pm = (PowerManager)context.GetSystemService(PowerService);
            return pm == null || pm.IsIgnoringBatteryOptimizations(context.PackageName);
        } catch {
            return false;
        }
    }

    public static void Start(Context context) {
        if (context == null) {
            return;
        }
        Context appCtx = context.ApplicationContext ?? context;
        Intent intent = new(appCtx, typeof(OmniAgentForegroundService));
        if (OperatingSystem.IsAndroidVersionAtLeast(26)) {
            appCtx.StartForegroundService(intent);
        } else {
            appCtx.StartService(intent);
        }
    }

    public static void Stop(Context context) {
        if (context == null) {
            return;
        }
        Context appCtx = context.ApplicationContext ?? context;
        try {
            appCtx.StopService(new Intent(appCtx, typeof(OmniAgentForegroundService)));
        } catch {
            // Ignore
        }
    }

    public override IBinder OnBind(Intent intent) => null;

    public override StartCommandResult OnStartCommand(Intent intent, StartCommandFlags flags, int startId) {
        if (string.Equals(intent?.Action, ActionStop, StringComparison.Ordinal)) {
            OmniSettingsStore store = new(this);
            store.ServiceEnabled = false;
            StopForegroundCompat(true);
            StopSelf();
            return StartCommandResult.NotSticky;
        }

        EnsureChannel();
        StartForegroundCompat(NotifId, BuildNotification());

        if (cts == null || cts.IsCancellationRequested) {
            cts?.Dispose();
            cts = new CancellationTokenSource();
            isRunning = true;
            NotifyState();
            OmniSettingsStore.AppendActivity("AGENT", $"Foreground service v{AgentVersion} started (channel=android).");
            CancellationToken token = cts.Token;
            _ = Task.Run(() => PollLoopAsync(token));
        }
        return StartCommandResult.Sticky;
    }

    [System.Runtime.Versioning.SupportedOSPlatform("android34.0")]
    public override void OnTimeout(int startId) {
        if (OperatingSystem.IsAndroidVersionAtLeast(34)) {
            base.OnTimeout(startId);
        }
        HandleTimeout(startId);
    }

    [System.Runtime.Versioning.SupportedOSPlatform("android35.0")]
    public override void OnTimeout(int startId, Android.Content.PM.ForegroundService fgsType) {
        if (OperatingSystem.IsAndroidVersionAtLeast(35)) {
            base.OnTimeout(startId, fgsType);
        }
        HandleTimeout(startId);
    }

    void HandleTimeout(int startId) {
        OmniSettingsStore.AppendActivity("AGENT", $"Foreground timeout reached (startId={startId}); stopping cleanly.");
        StopForegroundCompat(true);
        try {
            StopSelf(startId);
        } catch {
            StopSelf();
        }
    }

    public override void OnDestroy() {
        isRunning = false;
        try {
            cts?.Cancel();
            cts?.Dispose();
        } catch {
            // Ignore
        }
        cts = null;
        NotifyState();
        OmniSettingsStore.AppendActivity("AGENT", "Foreground service stopped.");
        base.OnDestroy();
    }

    static async Task<int> ProbeRecommendedHeartbeatSecAsync(string baseUrl, CancellationToken ct) {
        try {
            using HttpResponseMessage resp = await http.GetAsync($"{baseUrl}/health", ct).ConfigureAwait(false);
            if (resp.IsSuccessStatusCode) {
                string body = await resp.Content.ReadAsStringAsync(ct).ConfigureAwait(false);
                using JsonDocument doc = JsonDocument.Parse(body);
                JsonElement root = doc.RootElement;
                if (root.TryGetProperty("recommendedHeartbeatSec", out JsonElement hbEl) && hbEl.TryGetInt32(out int rec) && rec >= 30 && rec <= 600) {
                    return rec;
                }
                if (root.TryGetProperty("supportsDurableObjects", out JsonElement doEl) && doEl.ValueKind == JsonValueKind.True) {
                    return 60;
                }
            }
        } catch {
            // Fall back to economical KV interval
        }
        return 180;
    }

    async Task PollLoopAsync(CancellationToken ct) {
        DateTime lastHb = DateTime.MinValue;
        DateTime lastHealthProbe = DateTime.MinValue;
        int heartbeatSec = 180;
        int idleTicks = 0;
        try {
            while (!ct.IsCancellationRequested) {
                OmniSettingsStore store = new(this);
                if (!store.ServiceEnabled) {
                    break;
                }
                string pairHex = store.LoadPairSecretHex();
                if (string.IsNullOrEmpty(pairHex) || !OmniSettingsStore.TryNormalizeRelayUrl(store.RelayUrl, out string baseUrl)) {
                    await Task.Delay(12_000, ct).ConfigureAwait(false);
                    continue;
                }

                string devHash = OmniCrypto.ComputeDeviceHash(pairHex);
                string agentKey = OmniCrypto.ComputeAgentKey(pairHex);

                if ((DateTime.UtcNow - lastHealthProbe).TotalMinutes >= 30) {
                    heartbeatSec = await ProbeRecommendedHeartbeatSecAsync(baseUrl, ct).ConfigureAwait(false);
                    lastHealthProbe = DateTime.UtcNow;
                }

                if ((DateTime.UtcNow - lastHb).TotalSeconds >= heartbeatSec) {
                    try {
                        await SendHeartbeatAsync(baseUrl, devHash, agentKey, store, ct).ConfigureAwait(false);
                        lastHb = DateTime.UtcNow;
                    } catch {
                        // Retry on next loop
                    }
                }

                try {
                    using HttpRequestMessage pollReq = CreateAgentReq(HttpMethod.Get, $"{baseUrl}/v1/agent/poll", devHash, agentKey);
                    using HttpResponseMessage pollResp = await http.SendAsync(pollReq, ct).ConfigureAwait(false);
                    if (pollResp.IsSuccessStatusCode) {
                        string raw = await pollResp.Content.ReadAsStringAsync(ct).ConfigureAwait(false);
                        using JsonDocument doc = JsonDocument.Parse(raw);
                        if (doc.RootElement.TryGetProperty("hasCommand", out JsonElement hasEl) &&
                            hasEl.GetBoolean() &&
                            doc.RootElement.TryGetProperty("command", out JsonElement cmdWrap)) {
                            idleTicks = 0;
                            string reqId = cmdWrap.TryGetProperty("requestId", out JsonElement idEl) ? (idEl.GetString() ?? "") : "";
                            JsonElement envEl = cmdWrap.GetProperty("envelope");
                            object resultObj = await HandleEncryptedCommandAsync(baseUrl, devHash, agentKey, pairHex, store, reqId, envEl, ct).ConfigureAwait(false);
                            object resEnv = OmniCrypto.EncryptEnvelope(pairHex, $"res:{reqId}", resultObj);
                            using HttpRequestMessage postRes = CreateAgentReq(HttpMethod.Post, $"{baseUrl}/v1/agent/result", devHash, agentKey);
                            postRes.Content = new StringContent(
                                JsonSerializer.Serialize(new { requestId = reqId, envelope = resEnv }),
                                System.Text.Encoding.UTF8,
                                "application/json");
                            using HttpResponseMessage _ = await http.SendAsync(postRes, ct).ConfigureAwait(false);
                            Interlocked.Increment(ref commandsHandled);
                            NotifyState();
                            continue;
                        }
                        idleTicks = Math.Min(idleTicks + 1, 15);
                    }

                    // Poll Zero-Secret Ephemeral P-256 Smart Knock inbox when enabled
                    if (store.SmartKnockEnabled) {
                        using HttpRequestMessage kReq = new(HttpMethod.Get, $"{baseUrl}/v1/knock/poll");
                        kReq.Headers.Add("X-Device-Hash", devHash);
                        using HttpResponseMessage kResp = await http.SendAsync(kReq, ct).ConfigureAwait(false);
                        if (kResp.IsSuccessStatusCode) {
                            string kRaw = await kResp.Content.ReadAsStringAsync(ct).ConfigureAwait(false);
                            using JsonDocument kDoc = JsonDocument.Parse(kRaw);
                            if (kDoc.RootElement.TryGetProperty("hasKnock", out JsonElement hkEl) &&
                                hkEl.GetBoolean() &&
                                kDoc.RootElement.TryGetProperty("knock", out JsonElement knockEl)) {
                                idleTicks = 0;
                                await HandleSmartKnockAsync(baseUrl, devHash, pairHex, knockEl, ct).ConfigureAwait(false);
                                continue;
                            }
                        }
                    }
                } catch (System.OperationCanceledException) {
                    break;
                } catch {
                    idleTicks = Math.Min(idleTicks + 1, 15);
                }

                await Task.Delay(idleTicks >= 4 ? 7_000 : 2_500, ct).ConfigureAwait(false);
            }
        } catch (System.OperationCanceledException) {
            // Normal cancel
        } finally {
            isRunning = false;
            NotifyState();
            StopForegroundCompat(true);
            StopSelf();
        }
    }

    async Task SendHeartbeatAsync(string baseUrl, string devHash, string agentKey, OmniSettingsStore store, CancellationToken ct) {
        using HttpRequestMessage req = CreateAgentReq(HttpMethod.Post, $"{baseUrl}/v1/agent/heartbeat", devHash, agentKey);
        object payload = new {
            agentVersion = AgentVersion,
            hostEnv = "android-companion",
            device = $"{Build.Manufacturer} {Build.Model}".Trim(),
            securityMode = store.SecurityMode,
            repoPath = store.GitHubRepo,
            tools = new {
                githubDirectPush = store.HasGitHubPat,
                cloudflareDeployReady = store.HasCloudflareToken,
                overlayPermissionGranted = OmniKnockApprovalActivity.CanDrawInstantOverlay(this),
                batteryUnrestricted = IsBatteryUnrestricted(this),
                apkInstaller = true,
                termuxInstalled = IsPackageInstalled(this, "com.termux"),
                termuxPermissionGranted = HasTermuxPermission(this),
                cavernizeInstalled = IsPackageInstalled(this, "hu.sbence.cavern.cavernize"),
                cavernizeSignatureMatch = CavernizeIpcBridgeClient.IsSignatureVerified(this)
            },
            capabilities = Capabilities
        };
        req.Content = new StringContent(JsonSerializer.Serialize(payload), System.Text.Encoding.UTF8, "application/json");
        using HttpResponseMessage _ = await http.SendAsync(req, ct).ConfigureAwait(false);
    }

    async Task HandleSmartKnockAsync(string baseUrl, string devHash, string pairHex, JsonElement knockEl, CancellationToken ct) {
        string knockReqId = knockEl.TryGetProperty("knockReqId", out JsonElement idEl) ? (idEl.GetString() ?? "") : "";
        string clientPub = knockEl.TryGetProperty("clientPubKey", out JsonElement pkEl) ? (pkEl.GetString() ?? "") : "";
        string requester = knockEl.TryGetProperty("requester", out JsonElement rqEl) ? (rqEl.GetString() ?? "AI Studio") : "AI Studio";
        string reason = knockEl.TryGetProperty("reason", out JsonElement rEl) ? (rEl.GetString() ?? "Zero-paste auto-connect") : "Zero-paste auto-connect";
        if (string.IsNullOrEmpty(knockReqId) || string.IsNullOrEmpty(clientPub)) {
            return;
        }
        string knockId = OmniCrypto.ComputeKnockChannelId(pairHex);
        string sas = OmniCrypto.ComputeKnockSasCode(knockId, knockReqId, clientPub);
        OmniSettingsStore.AppendActivity("KNOCK", $"Incoming Smart Knock (SAS={sas}) from {requester}...");

        (bool approved, bool bioOk) = await OmniKnockApprovalActivity.RequestApprovalAsync(this, knockReqId, sas, requester, reason, ct).ConfigureAwait(false);
        if (!approved || !bioOk) {
            using HttpRequestMessage denyReq = new(HttpMethod.Post, $"{baseUrl}/v1/knock/deny");
            denyReq.Headers.Add("X-Device-Hash", devHash);
            denyReq.Content = new StringContent(
                JsonSerializer.Serialize(new { knockReqId, reason = "Denied by OmniAgent user", cooldownSeconds = 900 }),
                System.Text.Encoding.UTF8,
                "application/json");
            using HttpResponseMessage _ = await http.SendAsync(denyReq, ct).ConfigureAwait(false);
            OmniSettingsStore.AppendActivity("KNOCK", $"Denied Smart Knock (SAS={sas}, 15m cooldown).");
            return;
        }

        object grantPayload = new {
            mode = "workspace",
            relayUrl = baseUrl,
            pairSecret = pairHex,
            knockId,
            biometricVerified = true,
            device = $"{Build.Manufacturer} {Build.Model}".Trim(),
            approvedAtUtc = DateTime.UtcNow.ToString("O")
        };
        (string serverPubB64, object encGrant) = OmniCrypto.CreateEcdhP256GrantEnvelope(clientPub, knockReqId, grantPayload);
        using HttpRequestMessage appReq = new(HttpMethod.Post, $"{baseUrl}/v1/knock/approve");
        appReq.Headers.Add("X-Device-Hash", devHash);
        appReq.Content = new StringContent(
            JsonSerializer.Serialize(new { knockReqId, serverPubKey = serverPubB64, encryptedGrant = encGrant, scope = "workspace" }),
            System.Text.Encoding.UTF8,
            "application/json");
        using HttpResponseMessage resp = await http.SendAsync(appReq, ct).ConfigureAwait(false);
        OmniSettingsStore.AppendActivity("KNOCK", resp.IsSuccessStatusCode
            ? $"Approved Smart Knock (SAS={sas}) -> E2E P-256 workspace connected!"
            : $"Smart Knock approve failed (HTTP {(int)resp.StatusCode}).");
    }

    async Task<object> HandleEncryptedCommandAsync(
        string baseUrl,
        string devHash,
        string agentKey,
        string pairHex,
        OmniSettingsStore store,
        string reqId,
        JsonElement envelope,
        CancellationToken ct) {
        string cmd = "";
        try {
            using JsonDocument cmdDoc = OmniCrypto.DecryptEnvelope(pairHex, $"cmd:{reqId}", envelope);
            JsonElement root = cmdDoc.RootElement;
            cmd = root.TryGetProperty("cmd", out JsonElement cEl) ? (cEl.GetString() ?? "").Trim().ToLowerInvariant() : "";
            JsonElement argsEl = root.TryGetProperty("args", out JsonElement aEl) && aEl.ValueKind == JsonValueKind.Object ? aEl : default;

            OmniSettingsStore.AppendActivity("CMD", $"{cmd} (#{reqId[..Math.Min(8, reqId.Length)]})");

            if (string.Equals(store.SecurityMode, "biometric", StringComparison.OrdinalIgnoreCase) && CriticalCommands.Contains(cmd)) {
                bool approved = await OmniBiometricGateActivity.RequestApprovalAsync(
                    this, reqId, "OmniAgent Approval", $"Allow remote '{cmd}'?", ct).ConfigureAwait(false);
                if (!approved) {
                    OmniSettingsStore.AppendActivity("DENY", $"User denied {cmd} via biometric prompt.");
                    return new { ok = false, cmd, error = "Denied by user via BiometricPrompt." };
                }
            }

            switch (cmd) {
                case "ping":
                case "status":
                case "device_info":
                    return new {
                        ok = true,
                        cmd,
                        agentVersion = AgentVersion,
                        channel = "android",
                        securityMode = store.SecurityMode,
                        device = $"{Build.Manufacturer} {Build.Model}".Trim(),
                        android = $"{Build.VERSION.Release} (SDK {(int)Build.VERSION.SdkInt})",
                        githubDirectPushReady = store.HasGitHubPat,
                        githubRepo = store.GitHubRepo,
                        cloudflareDeployReady = store.HasCloudflareToken,
                        cloudflareWorkerName = store.ResolveEffectiveWorkerName(),
                        overlayPermissionGranted = OmniKnockApprovalActivity.CanDrawInstantOverlay(this),
                        batteryUnrestricted = IsBatteryUnrestricted(this),
                        termuxInstalled = IsPackageInstalled(this, "com.termux"),
                        termuxPermissionGranted = HasTermuxPermission(this),
                        cavernizeInstalled = IsPackageInstalled(this, "hu.sbence.cavern.cavernize"),
                        cavernizeSignatureMatch = CavernizeIpcBridgeClient.IsSignatureVerified(this),
                        commandsHandled,
                        utc = DateTime.UtcNow.ToString("O")
                    };

                case "cf_status": {
                    (bool healthOk, int workerVer, long healthMs, string healthDetail) = await TestRelayHealthAsync(baseUrl, ct).ConfigureAwait(false);
                    return new {
                        ok = true,
                        cmd,
                        cloudflareDeployReady = store.HasCloudflareToken,
                        accountId = store.CloudflareAccountId,
                        workerName = store.ResolveEffectiveWorkerName(),
                        relayUrl = baseUrl,
                        relayHealthy = healthOk,
                        workerVersion = workerVer,
                        healthLatencyMs = healthMs,
                        healthDetail
                    };
                }

                case "cf_deploy_worker": {
                    if (!store.HasCloudflareToken) {
                        return new { ok = false, cmd, error = "Cloudflare API Token not configured in OmniAgent yet. Open OmniAgent -> Deploy CF to paste it once." };
                    }
                    string workerScript = GetStr(argsEl, "script");
                    string blobId = GetStr(argsEl, "blobId");
                    if (string.IsNullOrWhiteSpace(workerScript) && !string.IsNullOrWhiteSpace(blobId)) {
                        using JsonDocument blobDoc = await DownloadEncryptedBlobAsync(baseUrl, devHash, agentKey, pairHex, blobId, ct).ConfigureAwait(false);
                        if (blobDoc.RootElement.ValueKind == JsonValueKind.Object &&
                            blobDoc.RootElement.TryGetProperty("script", out JsonElement sEl) &&
                            sEl.ValueKind == JsonValueKind.String) {
                            workerScript = sEl.GetString() ?? "";
                        }
                    }
                    if (string.IsNullOrWhiteSpace(workerScript)) {
                        return new { ok = false, cmd, error = "Worker script payload (script or blobId) is required." };
                    }
                    string targetWorkerName = GetStr(argsEl, "workerName");
                    if (string.IsNullOrWhiteSpace(targetWorkerName)) {
                        targetWorkerName = store.ResolveEffectiveWorkerName();
                    }
                    string targetAccountId = GetStr(argsEl, "accountId");
                    if (string.IsNullOrWhiteSpace(targetAccountId)) {
                        targetAccountId = store.CloudflareAccountId;
                    }
                    (bool depOk, string depMode, string resolvedAcc, string depDetail) = await CloudflareDeployService.DeployWorkerAsync(
                        store.LoadCloudflareToken(),
                        targetAccountId,
                        targetWorkerName,
                        workerScript,
                        enableDurableObject: true,
                        mainModule: "worker.js",
                        ct: ct).ConfigureAwait(false);
                    if (depOk && !string.IsNullOrWhiteSpace(resolvedAcc) && string.IsNullOrWhiteSpace(store.CloudflareAccountId)) {
                        store.CloudflareAccountId = resolvedAcc;
                    }
                    OmniSettingsStore.AppendActivity(depOk ? "CF" : "ERR", $"cf_deploy_worker ({targetWorkerName}, {depMode}): {depDetail}");
                    (bool healthOk, int workerVer, long healthMs, string healthDetail) = await TestRelayHealthAsync(baseUrl, ct).ConfigureAwait(false);
                    return new {
                        ok = depOk,
                        cmd,
                        workerName = targetWorkerName,
                        accountId = resolvedAcc,
                        deployMode = depMode,
                        message = depDetail,
                        healthOk,
                        workerVersion = workerVer,
                        healthLatencyMs = healthMs,
                        healthDetail
                    };
                }

                case "cavernize_cmd": {
                    string subCmd = GetStr(argsEl, "cmd");
                    if (string.IsNullOrWhiteSpace(subCmd)) {
                        return new { ok = false, cmd, error = "args.cmd is required for cavernize_cmd." };
                    }
                    string subArgsJson = "{}";
                    if (argsEl.ValueKind == JsonValueKind.Object && argsEl.TryGetProperty("args", out JsonElement subArgsEl)) {
                        subArgsJson = subArgsEl.GetRawText();
                    }
                    int maxTier = argsEl.ValueKind == JsonValueKind.Object && argsEl.TryGetProperty("tier", out JsonElement tEl) && tEl.TryGetInt32(out int parsedTier)
                        ? Math.Clamp(parsedTier, 0, 2)
                        : 2;
                    int timeoutSec = argsEl.ValueKind == JsonValueKind.Object && argsEl.TryGetProperty("timeoutSec", out JsonElement toEl) && toEl.TryGetInt32(out int parsedTo)
                        ? Math.Clamp(parsedTo, 5, 120)
                        : 45;
                    (bool ipcOk, string ipcBody, long ipcElapsedMs) = await CavernizeIpcBridgeClient.SendCommandAsync(
                        this, subCmd, subArgsJson, maxTier, timeoutSec, ct).ConfigureAwait(false);
                    OmniSettingsStore.AppendActivity(ipcOk ? "IPC" : "ERR", $"cavernize_cmd '{subCmd}' ({ipcElapsedMs}ms, ok={ipcOk})");
                    return new {
                        ok = ipcOk,
                        cmd,
                        subCmd,
                        elapsedMs = ipcElapsedMs,
                        body = ipcBody
                    };
                }

                case "github_push_bundle": {
                    string blobId = GetStr(argsEl, "blobId");
                    string msg = GetStr(argsEl, "message");
                    string branch = GetStr(argsEl, "branch");
                    if (string.IsNullOrEmpty(branch)) {
                        branch = store.GitHubBranch;
                    }
                    string repo = GetStr(argsEl, "repo");
                    if (string.IsNullOrEmpty(repo) || !repo.Contains('/')) {
                        repo = store.GitHubRepo;
                    }
                    if (string.IsNullOrEmpty(blobId)) {
                        return new { ok = false, cmd, error = "blobId is required." };
                    }
                    using JsonDocument bundleDoc = await DownloadEncryptedBlobAsync(baseUrl, devHash, agentKey, pairHex, blobId, ct).ConfigureAwait(false);
                    (bool ok, string sha, string detail) = await GitHubGitPushService.PushDeltaBundleAsync(
                        store.LoadGitHubPat(), repo, branch, msg, bundleDoc.RootElement, ct).ConfigureAwait(false);
                    OmniSettingsStore.AppendActivity(ok ? "PUSH" : "ERR", detail);
                    return new { ok, cmd, head = sha, message = detail };
                }

                case "install_apk_url": {
                    string url = GetStr(argsEl, "url");
                    if (string.IsNullOrWhiteSpace(url) || !url.StartsWith("https://", StringComparison.OrdinalIgnoreCase)) {
                        return new { ok = false, cmd, error = "Valid https:// APK URL is required." };
                    }
                    byte[] apkBytes = await http.GetByteArrayAsync(url, ct).ConfigureAwait(false);
                    if (apkBytes.Length < 1024 || apkBytes[0] != 0x50 || apkBytes[1] != 0x4B) {
                        return new { ok = false, cmd, error = "Downloaded file is not a valid APK archive." };
                    }
                    string fileName = $"omni_install_{DateTime.UtcNow:yyyyMMdd_HHmmss}.apk";
                    string path = Path.Combine(OmniApkFileProvider.GetApkDirectory(this), fileName);
                    await File.WriteAllBytesAsync(path, apkBytes, ct).ConfigureAwait(false);
                    Android.Net.Uri contentUri = OmniApkFileProvider.GetApkUri(this, fileName);
                    Intent installIntent = new(Intent.ActionView);
                    installIntent.SetDataAndType(contentUri, "application/vnd.android.package-archive");
                    installIntent.AddFlags(ActivityFlags.GrantReadUriPermission | ActivityFlags.NewTask);
                    StartActivity(installIntent);
                    OmniSettingsStore.AppendActivity("APK", $"Launched installer ({apkBytes.Length / 1024} KB).");
                    return new { ok = true, cmd, bytes = apkBytes.Length, file = fileName };
                }

                case "launch_termux_daemon": {
                    string scriptPath = GetStr(argsEl, "script");
                    if (string.IsNullOrEmpty(scriptPath)) {
                        scriptPath = store.TermuxStartScript;
                    }
                    (bool ok, bool needsPermission, bool needsExternalApps, string detail) = LaunchTermuxSmartDaemon(this, scriptPath);
                    OmniSettingsStore.AppendActivity(ok ? "TERMUX" : "ERR", detail);
                    return new {
                        ok,
                        cmd,
                        needsPermission,
                        needsExternalApps,
                        termuxInstalled = IsPackageInstalled(this, "com.termux"),
                        permissionGranted = HasTermuxPermission(this),
                        message = detail
                    };
                }

                case "termux_run": {
                    string execPath = GetStr(argsEl, "path");
                    if (string.IsNullOrEmpty(execPath)) {
                        execPath = "/data/data/com.termux/files/usr/bin/bash";
                    }
                    string command = GetStr(argsEl, "command");
                    (bool ok, string detail) = LaunchTermuxCommand(this, execPath, string.IsNullOrEmpty(command) ? [] : ["-lc", command]);
                    OmniSettingsStore.AppendActivity("TERMUX", detail);
                    return new { ok, cmd, message = detail };
                }

                case "open_cavernize": {
                    Intent launch = PackageManager?.GetLaunchIntentForPackage("hu.sbence.cavern.cavernize");
                    if (launch == null) {
                        return new { ok = false, cmd, error = "Cavernize Android is not installed." };
                    }
                    launch.AddFlags(ActivityFlags.NewTask);
                    StartActivity(launch);
                    OmniSettingsStore.AppendActivity("APP", "Brought Cavernize Android to foreground.");
                    return new { ok = true, cmd, message = "Opened Cavernize Android." };
                }

                default:
                    return new { ok = false, cmd, error = $"Unknown Android companion command '{cmd}'." };
            }
        } catch (Exception ex) {
            OmniSettingsStore.AppendActivity("ERR", $"{cmd}: {ex.Message}");
            return new { ok = false, cmd, error = $"{ex.GetType().Name}: {ex.Message}" };
        }
    }

    async Task<JsonDocument> DownloadEncryptedBlobAsync(
        string baseUrl,
        string devHash,
        string agentKey,
        string pairHex,
        string blobId,
        CancellationToken ct) {
        using HttpRequestMessage metaReq = CreateAgentReq(HttpMethod.Get, $"{baseUrl}/v1/blob/meta?blobId={Uri.EscapeDataString(blobId)}", devHash, agentKey);
        using HttpResponseMessage metaResp = await http.SendAsync(metaReq, ct).ConfigureAwait(false);
        string metaRaw = await metaResp.Content.ReadAsStringAsync(ct).ConfigureAwait(false);
        using JsonDocument metaDoc = JsonDocument.Parse(metaRaw);
        int totalChunks = metaDoc.RootElement.GetProperty("meta").GetProperty("totalChunks").GetInt32();

        System.Text.StringBuilder sb = new();
        for (int i = 0; i < totalChunks; i++) {
            using HttpRequestMessage chReq = CreateAgentReq(HttpMethod.Get, $"{baseUrl}/v1/blob/chunk?blobId={Uri.EscapeDataString(blobId)}&index={i}", devHash, agentKey);
            using HttpResponseMessage chResp = await http.SendAsync(chReq, ct).ConfigureAwait(false);
            string chRaw = await chResp.Content.ReadAsStringAsync(ct).ConfigureAwait(false);
            using JsonDocument chDoc = JsonDocument.Parse(chRaw);
            sb.Append(chDoc.RootElement.GetProperty("data").GetString() ?? "");
        }

        try {
            using HttpRequestMessage delReq = CreateAgentReq(HttpMethod.Delete, $"{baseUrl}/v1/blob/delete?blobId={Uri.EscapeDataString(blobId)}", devHash, agentKey);
            using HttpResponseMessage _ = await http.SendAsync(delReq, ct).ConfigureAwait(false);
        } catch {
            // Best effort cleanup
        }

        using JsonDocument envDoc = JsonDocument.Parse(sb.ToString());
        return OmniCrypto.DecryptEnvelope(pairHex, $"blob:{blobId}", envDoc.RootElement);
    }

    public static bool HasTermuxPermission(Context context) {
        if (context == null) {
            return false;
        }
        return context.CheckSelfPermission(TermuxRunCommandPermission) == Android.Content.PM.Permission.Granted;
    }

    public static (bool ok, bool needsPermission, bool needsExternalApps, string message) LaunchTermuxSmartDaemon(
        Context context,
        string customScriptPath = null) {
        if (context == null) {
            return (false, false, false, "Missing Android context.");
        }
        if (!IsPackageInstalled(context, "com.termux")) {
            return (false, false, false, "Termux (com.termux) is not installed on this device.");
        }
        if (!HasTermuxPermission(context)) {
            return (false, true, false, "Termux RUN_COMMAND permission not granted yet. Tap Wake Termux to allow.");
        }

        string safeCustom = string.IsNullOrWhiteSpace(customScriptPath)
            ? "/data/data/com.termux/files/home/.omni_agent/start.sh"
            : customScriptPath.Trim().Replace("'", "");

        // Hybrid auto-detect launcher:
        // 1) Ensures Termux usr/bin is in PATH (Termux RUN_COMMAND often has minimal PATH);
        // 2) Runs native Termux start.sh via setsid -f if present;
        // 3) Otherwise detaches proot-distro on the OUTSIDE via setsid -f while running the daemon
        //    in foreground inside Debian PRoot (preventing PRoot from exiting and killing child processes!).
        string smartCmd =
            $"export PATH=/data/data/com.termux/files/usr/bin:$PATH; " +
            $"if [ -x '{safeCustom}' ]; then " +
            $"setsid -f bash '{safeCustom}' >\"$HOME/.omni_agent_termux.log\" 2>&1 < /dev/null; " +
            $"elif command -v proot-distro >/dev/null 2>&1; then " +
            $"setsid -f /data/data/com.termux/files/usr/bin/proot-distro login debian --shared-tmp -- bash -lc " +
            $"'if [ -x /root/.omni_agent/start.sh ]; then exec bash /root/.omni_agent/start.sh; " +
            $"elif [ -f /root/.omni_agent/config.json ]; then exec python3 -B /root/.gemini/antigravity/scratch/Cavern/.agents/agent/omni_agent_daemon.py --config /root/.omni_agent/config.json; fi' " +
            $">\"$HOME/.omni_agent_debian.log\" 2>&1 < /dev/null; " +
            $"fi";

        (bool ok, string msg) = LaunchTermuxCommand(context, "/data/data/com.termux/files/usr/bin/bash", ["-lc", smartCmd]);
        if (ok) {
            return (true, false, false, "Smart wake sent to Termux / Debian PRoot daemon.");
        }
        bool permErr = msg.Contains("RUN_COMMAND", StringComparison.OrdinalIgnoreCase) &&
            msg.Contains("permission", StringComparison.OrdinalIgnoreCase);
        bool extErr = msg.Contains("allow-external-apps", StringComparison.OrdinalIgnoreCase) ||
            msg.Contains("not allowed", StringComparison.OrdinalIgnoreCase);
        return (false, permErr, !permErr && extErr, msg);
    }

    public static (bool ok, string message) LaunchTermuxCommand(Context context, string executablePath, string[] arguments) {
        try {
            Intent intent = new("com.termux.RUN_COMMAND");
            intent.SetClassName("com.termux", "com.termux.app.RunCommandService");
            intent.PutExtra("com.termux.RUN_COMMAND_PATH", executablePath);
            if (arguments is { Length: > 0 }) {
                intent.PutExtra("com.termux.RUN_COMMAND_ARGUMENTS", arguments);
            }
            intent.PutExtra("com.termux.RUN_COMMAND_BACKGROUND", true);
            if (OperatingSystem.IsAndroidVersionAtLeast(26)) {
                context.StartForegroundService(intent);
            } else {
                context.StartService(intent);
            }
            return (true, $"Dispatched Termux RUN_COMMAND -> {executablePath}");
        } catch (Java.Lang.SecurityException secEx) {
            return (false, $"Termux permission or allow-external-apps blocked: {secEx.Message}");
        } catch (Exception ex) {
            return (false, $"Termux RUN_COMMAND failed (check allow-external-apps=true in ~/.termux/termux.properties): {ex.Message}");
        }
    }

    public static async Task<(bool ok, int version, long elapsedMs, string detail)> TestRelayHealthAsync(string rawUrl, CancellationToken ct = default) {
        if (!OmniSettingsStore.TryNormalizeRelayUrl(rawUrl, out string baseUrl)) {
            return (false, 0, 0, "Invalid https:// Worker URL.");
        }
        System.Diagnostics.Stopwatch sw = System.Diagnostics.Stopwatch.StartNew();
        try {
            using HttpResponseMessage resp = await http.GetAsync($"{baseUrl}/health", ct).ConfigureAwait(false);
            sw.Stop();
            string body = await resp.Content.ReadAsStringAsync(ct).ConfigureAwait(false);
            if (!resp.IsSuccessStatusCode) {
                return (false, 0, sw.ElapsedMilliseconds, $"HTTP {(int)resp.StatusCode}");
            }
            using JsonDocument doc = JsonDocument.Parse(body);
            JsonElement root = doc.RootElement;
            int version = root.TryGetProperty("version", out JsonElement vEl) && vEl.ValueKind == JsonValueKind.Number
                ? vEl.GetInt32()
                : 0;
            bool omni = root.TryGetProperty("supportsOmniAgent", out JsonElement oEl) && oEl.ValueKind == JsonValueKind.True;
            bool hasDo = root.TryGetProperty("supportsDurableObjects", out JsonElement doEl) && doEl.ValueKind == JsonValueKind.True;
            string modeTag = hasDo ? "DO-SQLite" : "KV";
            return (omni && version >= 4, version, sw.ElapsedMilliseconds, omni
                ? $"Worker v{version} ({modeTag}) ready ({sw.ElapsedMilliseconds} ms)"
                : $"Worker v{version} (needs v6)");
        } catch (Exception ex) {
            sw.Stop();
            return (false, 0, sw.ElapsedMilliseconds, ex.Message);
        }
    }

    public static bool IsPackageInstalled(Context context, string pkg) {
        try {
            if (context?.PackageManager == null) {
                return false;
            }
            if (OperatingSystem.IsAndroidVersionAtLeast(33)) {
                return context.PackageManager.GetPackageInfo(pkg, Android.Content.PM.PackageManager.PackageInfoFlags.Of(0L)) != null;
            }
#pragma warning disable CS0618, CA1422
            return context.PackageManager.GetPackageInfo(pkg, 0) != null;
#pragma warning restore CS0618, CA1422
        } catch {
            return false;
        }
    }

    static HttpRequestMessage CreateAgentReq(HttpMethod method, string url, string devHash, string agentKey) {
        HttpRequestMessage req = new(method, url);
        req.Headers.Add("X-Device-Hash", devHash);
        req.Headers.Add("X-Agent-Key", agentKey);
        req.Headers.Add("X-Agent-Channel", "android");
        return req;
    }

    static string GetStr(JsonElement el, string name) =>
        el.ValueKind == JsonValueKind.Object && el.TryGetProperty(name, out JsonElement p) && p.ValueKind == JsonValueKind.String
            ? (p.GetString() ?? "")
            : "";

    void EnsureChannel() {
        if (!OperatingSystem.IsAndroidVersionAtLeast(26)) {
            return;
        }
        NotificationManager nm = (NotificationManager)GetSystemService(NotificationService);
        if (nm == null) {
            return;
        }
        NotificationChannel ch = new(ChannelId, "OmniAgent Bridge", NotificationImportance.Low) {
            Description = "Zero-trust background bridge for AI Studio Full-Agent commands"
        };
        ch.SetShowBadge(false);
        nm.CreateNotificationChannel(ch);
    }

    Notification BuildNotification() {
        PendingIntentFlags piFlags = OperatingSystem.IsAndroidVersionAtLeast(23)
            ? PendingIntentFlags.UpdateCurrent | PendingIntentFlags.Immutable
            : PendingIntentFlags.UpdateCurrent;
        Intent openIntent = new(this, typeof(MainActivity));
        openIntent.AddFlags(ActivityFlags.NewTask | ActivityFlags.SingleTop);
        PendingIntent openPi = PendingIntent.GetActivity(this, NotifId, openIntent, piFlags);

        Intent stopIntent = new(this, typeof(OmniAgentForegroundService));
        stopIntent.SetAction(ActionStop);
        PendingIntent stopPi = PendingIntent.GetService(this, NotifId + 1, stopIntent, piFlags);

        Notification.Builder b = new(this, ChannelId);
        b.SetSmallIcon(Resource.Drawable.omni_icon)
            .SetContentTitle("OmniAgent Online")
            .SetContentText($"Channel: android • {commandsHandled} cmd(s) handled")
            .SetOngoing(true)
            .SetContentIntent(openPi);
        if (OperatingSystem.IsAndroidVersionAtLeast(23)) {
            Notification.Action act = new Notification.Action.Builder(
                Android.Graphics.Drawables.Icon.CreateWithResource(this, Resource.Drawable.omni_icon),
                "Stop",
                stopPi).Build();
            b.AddAction(act);
        }
        return b.Build();
    }

    void StartForegroundCompat(int id, Notification n) {
        if (OperatingSystem.IsAndroidVersionAtLeast(29)) {
            StartForeground(id, n, Android.Content.PM.ForegroundService.TypeDataSync);
        } else {
            StartForeground(id, n);
        }
    }

    void StopForegroundCompat(bool remove) {
        try {
            if (OperatingSystem.IsAndroidVersionAtLeast(24)) {
                StopForeground(remove ? StopForegroundFlags.Remove : StopForegroundFlags.Detach);
            } else {
#pragma warning disable CS0618, CA1422
                StopForeground(remove);
#pragma warning restore CS0618, CA1422
            }
        } catch {
            // Ignore
        }
    }

    static void NotifyState() {
        try {
            StateChanged?.Invoke();
        } catch {
            // Ignore
        }
    }
}
