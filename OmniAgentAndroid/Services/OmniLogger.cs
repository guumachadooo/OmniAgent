using System.Globalization;
using System.Text;
using System.Text.Json;
using System.Text.RegularExpressions;

using Android.App;
using Android.Content;
using Android.OS;
using Android.Provider;

using Path = System.IO.Path;

namespace OmniAgentAndroid.Services;

/// <summary>
/// Centralized persistent structured diagnostic logger for OmniAgent Companion.
/// Writes timestamped, thread- and category-tagged events, startup hardware/config/permission audits,
/// recursive exception chains, and fatal crash markers to internal storage
/// (<c>FilesDir/logs/omniagent_log.txt</c>) and exports/mirrors cleanly to
/// <c>Downloads/OmniAgent/Logs/omniagent_log.txt</c> with automatic secret redaction.
/// </summary>
public static class OmniLogger {
    const long MaxLogBytes = 2 * 1024 * 1024;
    const int TrimTargetBytes = 1200 * 1024;
    const int CoalesceWindowSeconds = 12;
    const int MaxCoalescedRepeats = 300;
    const int MaxExceptionDepth = 5;
    const int MaxStackFramesPerLevel = 14;
    const int RecentEventsCapacity = 24;

    static readonly object sync = new();
    static readonly Queue<string> recentCriticalEvents = new(RecentEventsCapacity + 2);

    static readonly Regex SecretTokenRegex = new(
        @"(?:\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9_]{20,}\b|\bgithub_pat_[A-Za-z0-9_]{20,}\b|\b(?:CAVPAIR|OMNIPAIR|PAIR)[-_:]?[0-9a-fA-F]{16,}\b|\b[0-9a-fA-F]{64}\b|(?i)(?:Authorization\s*:\s*(?:Bearer|token)\s+)[^\s""']+|(?i)((?:api[_-]?key|access[_-]?token|pair[_-]?secret|x-omni-local-key|x-agent-key|pat|token|password|secret)\s*[=:]\s*)[^\s&""';,]+)",
        RegexOptions.Compiled);

    static string internalLogPath = "";
    static string crashMarkerPath = "";
    static bool initialized;
    static int mainManagedThreadId = -1;

    static string sessionId = Guid.NewGuid().ToString("N")[..8];
    static DateTime sessionStartUtc = DateTime.UtcNow;
    static int sessionInfoCount;
    static int sessionWarnCount;
    static int sessionErrorCount;
    static int sessionFatalCount;
    static string lastErrorSummary = "";
    static DateTime? lastErrorUtc;

    static string lastDedupKey;
    static DateTime lastDedupFirstLocal;
    static DateTime lastDedupLastLocal;
    static int lastDedupCount;

    static string cachedAppVersion = "2.1.0";
    static long cachedAppBuild = 1;

    public static string SessionId => sessionId;
    public static DateTime SessionStartUtc => sessionStartUtc;
    public static int SessionInfoCount => Volatile.Read(ref sessionInfoCount);
    public static int SessionWarnCount => Volatile.Read(ref sessionWarnCount);
    public static int SessionErrorCount => Volatile.Read(ref sessionErrorCount);
    public static int SessionFatalCount => Volatile.Read(ref sessionFatalCount);
    public static string InternalLogPath => internalLogPath;

    public static string LastErrorSummary {
        get {
            lock (sync) {
                return lastErrorSummary;
            }
        }
    }

    public static bool HasPendingCrashReport {
        get {
            lock (sync) {
                try {
                    return !string.IsNullOrEmpty(crashMarkerPath) && File.Exists(crashMarkerPath);
                } catch {
                    return false;
                }
            }
        }
    }

    public static void Initialize(Context context) {
        if (context == null) {
            return;
        }
        Context appCtx = context.ApplicationContext ?? context;
        lock (sync) {
            if (mainManagedThreadId < 0 && Looper.MainLooper?.Thread == Java.Lang.Thread.CurrentThread()) {
                mainManagedThreadId = System.Environment.CurrentManagedThreadId;
            }

            try {
                string intDir = Path.Combine(appCtx.FilesDir.AbsolutePath, "logs");
                Directory.CreateDirectory(intDir);
                internalLogPath = Path.Combine(intDir, "omniagent_log.txt");
                crashMarkerPath = Path.Combine(intDir, "last_crash_marker.txt");
            } catch {
                // Best effort
            }

            if (initialized) {
                return;
            }
            initialized = true;
            sessionId = Guid.NewGuid().ToString("N")[..8];
            sessionStartUtc = DateTime.UtcNow;

            AppDomain.CurrentDomain.UnhandledException += (_, e) => {
                if (e.ExceptionObject is Exception ex) {
                    WriteFatalException("AppDomain.UnhandledException", ex, appCtx);
                } else {
                    WriteFatalMessage($"AppDomain.UnhandledException: {e.ExceptionObject}", appCtx);
                }
            };
            TaskScheduler.UnobservedTaskException += (_, e) => {
                Error("SYS", "TaskScheduler.UnobservedTaskException", e.Exception);
            };
            Android.Runtime.AndroidEnvironment.UnhandledExceptionRaiser += (_, e) => {
                WriteFatalException("AndroidEnvironment.UnhandledException", e.Exception, appCtx);
            };

            (string ver, long code) = OmniUpdateService.GetInstalledVersion(appCtx);
            cachedAppVersion = ver;
            cachedAppBuild = code;

            string abis = Build.SupportedAbis != null && Build.SupportedAbis.Count > 0
                ? string.Join(",", Build.SupportedAbis)
                : "unknown";
            int cores = System.Environment.ProcessorCount;

            WriteSessionBannerLocked(
                $"Session started [sid={sessionId}] | OmniAgent v{ver} (build {code}) | {Build.Manufacturer} {Build.Model} | Android {Build.VERSION.Release} (SDK {(int)Build.VERSION.SdkInt}) | ABI={abis} | CPU={cores}");

            WriteRawLocked("INFO", "SYS", $"Startup Env: {BuildMemorySnapshotText(appCtx)}", allowCoalesce: false);

            if (HasPendingCrashReport) {
                string marker = ReadPendingCrashMarkerLocked();
                WriteRawLocked("WARN", "SYS", $"Previous session ended with fatal crash marker: {marker}", allowCoalesce: false);
            }
        }
    }

    public static void LogConfigSnapshot(Context context, OmniSettingsStore store) {
        if (context == null || store == null) {
            return;
        }
        try {
            string pairHex = store.LoadPairSecretHex();
            string pairState = string.IsNullOrEmpty(pairHex)
                ? "MISSING"
                : $"SET(devHash={OmniCrypto.ComputeDeviceHash(pairHex)[..10]}...)";
            string relayHost = OmniSettingsStore.TryNormalizeRelayUrl(store.RelayUrl, out string normRelay)
                ? new Uri(normRelay).Host
                : "(invalid/unset)";
            bool overlayOk = !OperatingSystem.IsAndroidVersionAtLeast(23) || Settings.CanDrawOverlays(context);
            bool batOk = OmniAgentForegroundService.IsBatteryUnrestricted(context);
            bool termuxInst = OmniAgentForegroundService.IsPackageInstalled(context, "com.termux");
            bool termuxPerm = OmniAgentForegroundService.HasTermuxPermission(context);
            bool cavSig = CavernizeIpcBridgeClient.IsSignatureVerified(context);
            Info("CONFIG",
                $"Snapshot: PairKey={pairState}, Relay={relayHost}, Mode={store.SecurityMode}, " +
                $"AlwaysOn={store.ServiceEnabled}, SmartKnock={store.SmartKnockEnabled}, LocalFsPort={store.LocalFsPort}, " +
                $"HasGhPat={store.HasGitHubPat}, HasCfToken={store.HasCloudflareToken}, " +
                $"Overlay={overlayOk}, BatteryFree={batOk}, TermuxInst={termuxInst}, TermuxPerm={termuxPerm}, CavernizeSig={cavSig}");
        } catch {
            // Never disrupt startup
        }
    }

    public static void Info(string category, string message) => WriteRaw("INFO", NormalizeCategory(category), message);
    public static void Warn(string category, string message) => WriteRaw("WARN", NormalizeCategory(category), message);
    public static void Error(string category, string message) => WriteRaw("ERROR", NormalizeCategory(category), message, allowCoalesce: false);

    public static void Error(string category, string context, Exception ex) {
        string cat = NormalizeCategory(category);
        if (ex == null) {
            WriteRaw("ERROR", cat, context, allowCoalesce: false);
            return;
        }
        WriteRaw("ERROR", cat, $"{context}: {FormatExceptionChain(ex)}", allowCoalesce: false);
    }

    public static void Metric(string category, string operation, long elapsedMs, string details = null) {
        string tail = string.IsNullOrWhiteSpace(details) ? "" : $" | {details}";
        WriteRaw("METRIC", NormalizeCategory(category), $"{operation} took {elapsedMs} ms{tail}", allowCoalesce: false);
    }

    public static string SanitizeSensitiveText(string text) {
        if (string.IsNullOrEmpty(text)) {
            return "";
        }
        return SecretTokenRegex.Replace(text, match => {
            if (match.Groups[1].Success && !string.IsNullOrEmpty(match.Groups[1].Value)) {
                return match.Groups[1].Value + "[REDACTED]";
            }
            string v = match.Value;
            if (v.StartsWith("Authorization", StringComparison.OrdinalIgnoreCase)) {
                return "Authorization: [REDACTED]";
            }
            return "[REDACTED_SECRET]";
        });
    }

    public static string ReadRecentLines(int maxLines = 450) {
        string tail = ReadTailUtf8(192 * 1024);
        if (string.IsNullOrEmpty(tail)) {
            return "(Log empty / no entries yet)";
        }
        string[] lines = tail.Split('\n');
        if (lines.Length <= maxLines) {
            return tail;
        }
        int start = lines.Length - maxLines;
        StringBuilder sb = new();
        sb.AppendLine($"... (showing last {maxLines} lines; use Export / Share for full log) ...");
        for (int i = start; i < lines.Length; i++) {
            sb.AppendLine(lines[i].TrimEnd('\r'));
        }
        return sb.ToString();
    }

    public static string ReadTailUtf8(int maxBytes = 512 * 1024) {
        lock (sync) {
            FlushCoalescedLocked();
            try {
                if (string.IsNullOrEmpty(internalLogPath) || !File.Exists(internalLogPath)) {
                    return "";
                }
                FileInfo fi = new(internalLogPath);
                if (fi.Length <= maxBytes) {
                    return SanitizeSensitiveText(File.ReadAllText(internalLogPath, Encoding.UTF8));
                }
                byte[] buf = new byte[maxBytes];
                using FileStream fs = new(internalLogPath, FileMode.Open, FileAccess.Read, FileShare.ReadWrite);
                fs.Seek(-maxBytes, SeekOrigin.End);
                int read = fs.Read(buf, 0, maxBytes);
                string raw = Encoding.UTF8.GetString(buf, 0, read);
                int nl = raw.IndexOf('\n');
                string aligned = nl >= 0 && nl + 1 < raw.Length ? raw[(nl + 1)..] : raw;
                string header = $"[TRUNCATED: newest {maxBytes / 1024} KB of {fi.Length / 1024} KB | sid={sessionId} | v{cachedAppVersion} (build {cachedAppBuild})]{System.Environment.NewLine}";
                return header + SanitizeSensitiveText(aligned);
            } catch {
                return "";
            }
        }
    }

    public static string BuildDiagnosticSummaryJson(Context context, OmniSettingsStore store = null) {
        string[] recentErrors;
        string lastErr;
        DateTime? lastErrTime;
        lock (sync) {
            FlushCoalescedLocked();
            recentErrors = [.. recentCriticalEvents];
            lastErr = lastErrorSummary;
            lastErrTime = lastErrorUtc;
        }

        (string ver, long buildCode) = context != null
            ? OmniUpdateService.GetInstalledVersion(context)
            : (cachedAppVersion, cachedAppBuild);

        var payload = new {
            schemaVersion = 1,
            exportedAtUtc = DateTime.UtcNow.ToString("O", CultureInfo.InvariantCulture),
            sessionId,
            sessionStartUtc = sessionStartUtc.ToString("O", CultureInfo.InvariantCulture),
            app = new {
                name = "OmniAgent",
                version = ver,
                build = buildCode
            },
            device = new {
                manufacturer = Build.Manufacturer ?? "",
                model = Build.Model ?? "",
                androidRelease = Build.VERSION.Release ?? "",
                sdkInt = (int)Build.VERSION.SdkInt,
                abis = Build.SupportedAbis != null ? string.Join(",", Build.SupportedAbis) : "",
                cpuCores = System.Environment.ProcessorCount
            },
            memory = BuildMemorySnapshotText(context),
            logSizeBytes = GetLogSizeBytes(),
            counts = new {
                info = SessionInfoCount,
                warn = SessionWarnCount,
                error = SessionErrorCount,
                fatal = SessionFatalCount
            },
            hasPendingCrash = HasPendingCrashReport,
            lastErrorUtc = lastErrTime?.ToString("O", CultureInfo.InvariantCulture) ?? "",
            lastErrorSummary = SanitizeSensitiveText(lastErr),
            config = store != null ? new {
                hasPairSecret = store.HasPairSecret,
                relayUrl = store.RelayUrl,
                securityMode = store.SecurityMode,
                serviceEnabled = store.ServiceEnabled,
                smartKnockEnabled = store.SmartKnockEnabled,
                localFsPort = store.LocalFsPort,
                hasGitHubPat = store.HasGitHubPat,
                hasCloudflareToken = store.HasCloudflareToken,
                autoCheckUpdates = store.AutoCheckUpdates
            } : null,
            recentCriticalEvents = recentErrors
        };
        return JsonSerializer.Serialize(payload, new JsonSerializerOptions { WriteIndented = true });
    }

    public static void Clear(Context context) {
        lock (sync) {
            lastDedupKey = null;
            lastDedupCount = 0;
            Interlocked.Exchange(ref sessionInfoCount, 0);
            Interlocked.Exchange(ref sessionWarnCount, 0);
            Interlocked.Exchange(ref sessionErrorCount, 0);
            Interlocked.Exchange(ref sessionFatalCount, 0);
            lastErrorSummary = "";
            lastErrorUtc = null;
            recentCriticalEvents.Clear();

            try {
                if (!string.IsNullOrEmpty(internalLogPath) && File.Exists(internalLogPath)) {
                    File.Delete(internalLogPath);
                }
                if (!string.IsNullOrEmpty(crashMarkerPath) && File.Exists(crashMarkerPath)) {
                    File.Delete(crashMarkerPath);
                }
            } catch {
                // Ignore
            }
            (string ver, long code) = context != null
                ? OmniUpdateService.GetInstalledVersion(context)
                : (cachedAppVersion, cachedAppBuild);
            WriteSessionBannerLocked(
                $"Session started [sid={sessionId}] (Log reset) | OmniAgent v{ver} (build {code}) | {Build.Manufacturer} {Build.Model}");
        }
    }

    public static long GetLogSizeBytes() {
        lock (sync) {
            try {
                return !string.IsNullOrEmpty(internalLogPath) && File.Exists(internalLogPath)
                    ? new FileInfo(internalLogPath).Length
                    : 0;
            } catch {
                return 0;
            }
        }
    }

    /// <summary>
    /// Writes a sanitized snapshot of the log to <c>CacheDir/shares/omniagent_log.txt</c> and returns
    /// its shareable <c>content://</c> URI via <see cref="OmniApkFileProvider"/>.
    /// </summary>
    public static Android.Net.Uri CreateShareableLogUri(Context context) {
        if (context == null) {
            return null;
        }
        try {
            string text = ReadTailUtf8(1024 * 1024);
            string shareDir = OmniApkFileProvider.GetShareDirectory(context);
            string outFile = Path.Combine(shareDir, "omniagent_log.txt");
            File.WriteAllText(outFile, text, Encoding.UTF8);
            return OmniApkFileProvider.GetShareUri(context, "omniagent_log.txt");
        } catch {
            return null;
        }
    }

    /// <summary>
    /// Publishes <c>omniagent_log.txt</c> into public <c>Downloads/OmniAgent/Logs/</c> via Android
    /// MediaStore (API 29+) or direct file write, plus optional Debian loopback mirror so Termux and
    /// external tools can inspect the log directly.
    /// </summary>
    public static async Task<(bool ok, string location)> ExportLogToDownloadsAsync(Context context, OmniSettingsStore store = null) {
        string text = ReadTailUtf8(1024 * 1024);
        string publishedPath = "";

        // 1. Prefer Debian loopback save if online (overwrites /sdcard/Download/OmniAgent/Logs/omniagent_log.txt cleanly without duplicate "(1)" MediaStore suffixes)
        if (store != null && store.HasPairSecret) {
            (bool lbOk, string lbPath, _) = await TermuxLocalFsClient.SaveLogToLoopbackAsync(store, "omniagent_log.txt", text).ConfigureAwait(false);
            if (lbOk && !string.IsNullOrWhiteSpace(lbPath)) {
                publishedPath = lbPath;
            }
        }

        // 2. Also publish via Android MediaStore Downloads (works even when Debian daemon is asleep!)
        if (string.IsNullOrEmpty(publishedPath) && context != null) {
            try {
                if (OperatingSystem.IsAndroidVersionAtLeast(29)) {
                    ContentResolver resolver = context.ContentResolver;
                    if (resolver != null) {
                        string relPath = $"{Android.OS.Environment.DirectoryDownloads}/OmniAgent/Logs/";
                        try {
                            resolver.Delete(
                                MediaStore.Downloads.ExternalContentUri,
                                $"{MediaStore.IMediaColumns.DisplayName}=? AND {MediaStore.IMediaColumns.RelativePath}=?",
                                ["omniagent_log.txt", relPath]);
                        } catch {
                            // Ignore if none existed or owned by another install
                        }
                        ContentValues values = new();
                        values.Put(MediaStore.IMediaColumns.DisplayName, "omniagent_log.txt");
                        values.Put(MediaStore.IMediaColumns.MimeType, "text/plain");
                        values.Put(MediaStore.IMediaColumns.RelativePath, relPath);
                        Android.Net.Uri uri = resolver.Insert(MediaStore.Downloads.ExternalContentUri, values);
                        if (uri != null) {
                            using Stream s = resolver.OpenOutputStream(uri, "wt");
                            if (s != null) {
                                byte[] bytes = Encoding.UTF8.GetBytes(text);
                                await s.WriteAsync(bytes).ConfigureAwait(false);
                                publishedPath = "Downloads/OmniAgent/Logs/omniagent_log.txt";
                            }
                        }
                    }
                } else {
#pragma warning disable CA1422
                    Java.IO.File dlDir = Android.OS.Environment.GetExternalStoragePublicDirectory(Android.OS.Environment.DirectoryDownloads);
#pragma warning restore CA1422
                    if (dlDir != null) {
                        string dir = Path.Combine(dlDir.AbsolutePath, "OmniAgent", "Logs");
                        Directory.CreateDirectory(dir);
                        string dst = Path.Combine(dir, "omniagent_log.txt");
                        await File.WriteAllTextAsync(dst, text, Encoding.UTF8).ConfigureAwait(false);
                        publishedPath = dst;
                    }
                }
            } catch (Exception ex) {
                Warn("LOG", $"MediaStore export fallback failed: {ex.Message}");
            }
        }

        return (!string.IsNullOrEmpty(publishedPath), string.IsNullOrEmpty(publishedPath) ? internalLogPath : publishedPath);
    }

    static void WriteFatalException(string source, Exception ex, Context context) {
        string summary = ex != null ? $"{source}: {FormatExceptionChain(ex)}" : source;
        WriteFatalMessage(summary, context);
    }

    static void WriteFatalMessage(string summary, Context context) {
        string mem = BuildMemorySnapshotText(context);
        lock (sync) {
            FlushCoalescedLocked();
            WriteRawLocked("FATAL", "CRASH", $"{summary}\n  {mem}", allowCoalesce: false);
            WriteCrashMarkerLocked(summary);
        }
    }

    static void WriteCrashMarkerLocked(string summary) {
        if (string.IsNullOrEmpty(crashMarkerPath)) {
            return;
        }
        try {
            string oneLine = (summary ?? "Unknown fatal crash").Replace('\r', ' ').Replace('\n', ' ');
            if (oneLine.Length > 260) {
                oneLine = oneLine[..260] + "...";
            }
            string payload = $"{DateTime.UtcNow:O}|sid={sessionId}|v{cachedAppVersion}|{SanitizeSensitiveText(oneLine)}";
            File.WriteAllText(crashMarkerPath, payload, Encoding.UTF8);
        } catch {
            // Best effort
        }
    }

    static string ReadPendingCrashMarkerLocked() {
        try {
            if (!string.IsNullOrEmpty(crashMarkerPath) && File.Exists(crashMarkerPath)) {
                return File.ReadAllText(crashMarkerPath, Encoding.UTF8).Trim();
            }
        } catch {
            // Ignore
        }
        return "";
    }

    static void WriteRaw(string level, string category, string message, bool allowCoalesce = true) {
        string safeMsg = SanitizeSensitiveText(message ?? "");
        lock (sync) {
            WriteRawLocked(level, category, safeMsg, allowCoalesce);
        }
    }

    static void WriteRawLocked(string level, string category, string safeMsg, bool allowCoalesce) {
        switch (level) {
            case "WARN":
                Interlocked.Increment(ref sessionWarnCount);
                break;
            case "ERROR":
                Interlocked.Increment(ref sessionErrorCount);
                RecordCriticalEventLocked(level, category, safeMsg);
                break;
            case "FATAL":
                Interlocked.Increment(ref sessionFatalCount);
                RecordCriticalEventLocked(level, category, safeMsg);
                break;
            default:
                Interlocked.Increment(ref sessionInfoCount);
                break;
        }

        DateTime now = DateTime.Now;
        string dedupKey = allowCoalesce && level != "FATAL" ? $"{level}|{category}|{safeMsg}" : null;

        if (dedupKey != null &&
            string.Equals(dedupKey, lastDedupKey, StringComparison.Ordinal) &&
            (now - lastDedupLastLocal).TotalSeconds <= CoalesceWindowSeconds &&
            lastDedupCount < MaxCoalescedRepeats) {
            lastDedupCount++;
            lastDedupLastLocal = now;
            return;
        }

        FlushCoalescedLocked();

        if (dedupKey != null) {
            lastDedupKey = dedupKey;
            lastDedupFirstLocal = now;
            lastDedupLastLocal = now;
            lastDedupCount = 1;
        } else {
            lastDedupKey = null;
            lastDedupCount = 0;
        }

        string threadTag = GetThreadTag();
        string line = $"[{now:yyyy-MM-dd HH:mm:ss.fff}] [{level}] [{threadTag}] [{category}] {safeMsg}{System.Environment.NewLine}";
        AppendSafe(internalLogPath, line);
    }

    static void WriteSessionBannerLocked(string bannerText) {
        FlushCoalescedLocked();
        Interlocked.Increment(ref sessionInfoCount);
        string line = $"[{DateTime.Now:yyyy-MM-dd HH:mm:ss.fff}] === {bannerText} ==={System.Environment.NewLine}";
        AppendSafe(internalLogPath, line);
    }

    static void FlushCoalescedLocked() {
        if (lastDedupCount > 1) {
            double spanSec = Math.Max(0.0, (lastDedupLastLocal - lastDedupFirstLocal).TotalSeconds);
            string line = $"[{lastDedupLastLocal:yyyy-MM-dd HH:mm:ss.fff}] [INFO] [SYS] (previous message repeated {lastDedupCount}x in {spanSec:0.0}s){System.Environment.NewLine}";
            AppendSafe(internalLogPath, line);
        }
        lastDedupKey = null;
        lastDedupCount = 0;
    }

    static void RecordCriticalEventLocked(string level, string category, string safeMsg) {
        string firstLine = safeMsg;
        int nl = firstLine.IndexOf('\n');
        if (nl >= 0) {
            firstLine = firstLine[..nl].Trim();
        }
        if (firstLine.Length > 200) {
            firstLine = firstLine[..200] + "...";
        }
        lastErrorSummary = $"[{category}] {firstLine}";
        lastErrorUtc = DateTime.UtcNow;

        string item = $"[{DateTime.UtcNow:HH:mm:ss}Z] [{level}] [{category}] {firstLine}";
        recentCriticalEvents.Enqueue(item);
        while (recentCriticalEvents.Count > RecentEventsCapacity) {
            recentCriticalEvents.Dequeue();
        }
    }

    static void AppendSafe(string path, string line) {
        if (string.IsNullOrEmpty(path)) {
            return;
        }
        try {
            string dir = Path.GetDirectoryName(path) ?? ".";
            if (!Directory.Exists(dir)) {
                Directory.CreateDirectory(dir);
            }
            if (File.Exists(path) && new FileInfo(path).Length > MaxLogBytes) {
                byte[] tailBuf = new byte[TrimTargetBytes];
                int read;
                using (FileStream fs = new(path, FileMode.Open, FileAccess.Read, FileShare.ReadWrite)) {
                    fs.Seek(-TrimTargetBytes, SeekOrigin.End);
                    read = fs.Read(tailBuf, 0, TrimTargetBytes);
                }
                string text = Encoding.UTF8.GetString(tailBuf, 0, read);
                int nl = text.IndexOf('\n');
                string trimmed = nl >= 0 && nl + 1 < text.Length ? text[(nl + 1)..] : text;
                string tmpPath = path + ".trim.tmp";
                File.WriteAllText(tmpPath, trimmed, Encoding.UTF8);
                File.Move(tmpPath, path, overwrite: true);
            }
            File.AppendAllText(path, line, Encoding.UTF8);
        } catch {
            // Avoid secondary failures during logging
        }
    }

    static string GetThreadTag() {
        int tid = System.Environment.CurrentManagedThreadId;
        return tid == mainManagedThreadId ? "UI" : $"T#{tid}";
    }

    static string NormalizeCategory(string category) {
        if (string.IsNullOrWhiteSpace(category)) {
            return "APP";
        }
        string clean = category.Trim().ToUpperInvariant();
        return clean.Length > 12 ? clean[..12] : clean;
    }

    static string FormatExceptionChain(Exception root) {
        StringBuilder sb = new();
        FormatExceptionRecursive(sb, root, 0);
        return sb.ToString();
    }

    static void FormatExceptionRecursive(StringBuilder sb, Exception ex, int depth) {
        if (ex == null || depth > MaxExceptionDepth) {
            return;
        }
        if (depth > 0) {
            sb.Append($"\n  ---> [Inner #{depth}] ");
        }
        sb.Append($"{ex.GetType().Name} (0x{ex.HResult:X8}): {ex.Message}");
        if (!string.IsNullOrWhiteSpace(ex.StackTrace)) {
            string[] frames = ex.StackTrace.Split('\n');
            int take = Math.Min(frames.Length, MaxStackFramesPerLevel);
            for (int i = 0; i < take; i++) {
                string f = frames[i].TrimEnd('\r');
                if (!string.IsNullOrWhiteSpace(f)) {
                    sb.Append('\n').Append(f);
                }
            }
            if (frames.Length > take) {
                sb.Append($"\n  ... (+{frames.Length - take} more frames)");
            }
        }
        if (ex is System.AggregateException agg) {
            var flat = agg.Flatten().InnerExceptions;
            for (int i = 0; i < Math.Min(flat.Count, 3); i++) {
                FormatExceptionRecursive(sb, flat[i], depth + 1);
            }
        } else if (ex.InnerException != null) {
            FormatExceptionRecursive(sb, ex.InnerException, depth + 1);
        }
    }

    static string BuildMemorySnapshotText(Context context) {
        try {
            long gcMb = GC.GetTotalMemory(false) / (1024 * 1024);
            if (context?.GetSystemService(Context.ActivityService) is ActivityManager am) {
                ActivityManager.MemoryInfo mi = new();
                am.GetMemoryInfo(mi);
                long availMb = mi.AvailMem / (1024 * 1024);
                long totalMb = mi.TotalMem / (1024 * 1024);
                return $"RAM avail={availMb}/{totalMb} MB (lowMem={mi.LowMemory}), GC={gcMb} MB";
            }
            return $"GC={gcMb} MB";
        } catch {
            return "RAM=unavailable";
        }
    }
}
