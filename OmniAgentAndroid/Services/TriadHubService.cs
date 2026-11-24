using System.Net.Http.Headers;
using System.Text.Json;
using System.Text.RegularExpressions;

using Android.Content;

using IOPath = System.IO.Path;

namespace OmniAgentAndroid.Services;

public sealed class TriadRepoCardModel {
    public string Alias { get; init; } = "";
    public string DisplayName { get; init; } = "";
    public string OwnerRepo { get; init; } = "";
    public string Branch { get; init; } = "";
    public string LatestTag { get; init; } = "";
    public string LatestApkUrl { get; init; } = "";
    public string LatestCommitSha { get; init; } = "";
    public string LatestCommitMsg { get; init; } = "";
    public string CiStatus { get; init; } = "idle"; // pass | fail | running | idle
    public int LocalDirtyCount { get; init; } = -1; // -1 = Debian loopback offline
    public string LocalBranch { get; init; } = "";
}

public sealed class TriadSyncEntry {
    public string Date { get; init; } = "";
    public string Scope { get; init; } = "";
    public string Summary { get; init; } = "";
}

public sealed class TriadHubSnapshot {
    public string FetchedAtLocal { get; init; } = "";
    public bool DebianLoopbackOnline { get; init; }
    public string ActiveWave { get; init; } = "—";
    public string HandoffSummary { get; init; } = "";
    public string NextMilestone { get; init; } = "";
    public int PendingExtraCount { get; init; }
    public int BuildLessonsCount { get; init; }
    public List<TriadRepoCardModel> Repos { get; init; } = [];
    public List<TriadSyncEntry> RecentSync { get; init; } = [];
}

/// <summary>
/// Zero-quota Triad Command Hub aggregator combining live GitHub REST/Raw metadata for
/// <c>Cavern</c>, <c>OmniAgent</c>, and <c>dynamic-fractal-planner</c> with local &lt;5 ms
/// Debian PRoot git state and 1-tap signed APK release installation.
/// </summary>
public static class TriadHubService {
    static readonly HttpClient http = BuildClient();

    public static readonly (string Key, string Label, string RawUrl)[] ProjectDocs = [
        ("handoff", "HANDOFF.md", "https://raw.githubusercontent.com/guumachadooo/Cavern/master/.agents/docs/HANDOFF.md"),
        ("master", "MASTER PLAN W46–W51", "https://raw.githubusercontent.com/guumachadooo/Cavern/master/.agents/docs/MASTER_AUDIT_PLAN_W46_W51.md"),
        ("sync", "SYNC LEDGER", "https://raw.githubusercontent.com/guumachadooo/Cavern/master/.agents/docs/EXTERNAL_AI_SYNC.md"),
        ("extra", "EXTRA REQUESTS", "https://raw.githubusercontent.com/guumachadooo/Cavern/master/.agents/docs/EXTRA_REQUESTS.md"),
        ("lessons", "BUILD LESSONS", "https://raw.githubusercontent.com/guumachadooo/Cavern/master/.agents/docs/BUILD_LESSONS.md"),
        ("agents", "AGENTS.md", "https://raw.githubusercontent.com/guumachadooo/Cavern/master/AGENTS.md")
    ];

    static HttpClient BuildClient() {
        HttpClient c = new() { Timeout = TimeSpan.FromSeconds(18) };
        c.DefaultRequestHeaders.UserAgent.Add(new ProductInfoHeaderValue("OmniAgentHub", "2.1"));
        c.DefaultRequestHeaders.Accept.Add(new MediaTypeWithQualityHeaderValue("application/vnd.github+json"));
        return c;
    }

    public static async Task<TriadHubSnapshot> FetchSnapshotAsync(OmniSettingsStore store, CancellationToken ct = default) {
        string pat = store?.LoadGitHubPat() ?? "";

        Task<LocalTriadSnapshot> localTask = TermuxLocalFsClient.GetLocalTriadAsync(store, ct);
        Task<string> handoffTask = FetchTextSafeAsync(ProjectDocs[0].RawUrl, ct);
        Task<string> syncTask = FetchTextSafeAsync(ProjectDocs[2].RawUrl, ct);
        Task<string> extraTask = FetchTextSafeAsync(ProjectDocs[3].RawUrl, ct);
        Task<string> lessonsTask = FetchTextSafeAsync(ProjectDocs[4].RawUrl, ct);

        Task<TriadRepoCardModel> cavTask = FetchRepoMetaAsync("cavern", "Cavernize", "guumachadooo/Cavern", "master", "Cavernize-Android.apk", pat, ct);
        Task<TriadRepoCardModel> omniTask = FetchRepoMetaAsync("omniagent", "OmniAgent", "guumachadooo/OmniAgent", "main", "OmniAgent-Android.apk", pat, ct);
        Task<TriadRepoCardModel> fracTask = FetchRepoMetaAsync("fractal", "Fractal Planner", "guumachadooo/dynamic-fractal-planner", "main", "", pat, ct);

        await Task.WhenAll(localTask, handoffTask, syncTask, extraTask, lessonsTask, cavTask, omniTask, fracTask).ConfigureAwait(false);

        LocalTriadSnapshot local = await localTask.ConfigureAwait(false);
        string handoffMd = await handoffTask.ConfigureAwait(false);
        string syncMd = await syncTask.ConfigureAwait(false);
        string extraMd = await extraTask.ConfigureAwait(false);
        string lessonsMd = await lessonsTask.ConfigureAwait(false);

        (string wave, string summary, string next) = ParseHandoff(handoffMd);
        if (string.IsNullOrWhiteSpace(wave) && !string.IsNullOrWhiteSpace(local.Wave)) {
            wave = local.Wave;
            summary = local.Summary;
            next = local.Next;
        }

        int pendingExtra = ParsePendingExtraCount(extraMd);
        int lessonsCount = ParseBuildLessonsCount(lessonsMd);
        if (lessonsCount == 0 && local.BuildLessons > 0) {
            lessonsCount = local.BuildLessons;
        }

        List<TriadRepoCardModel> repos = [
            MergeLocal(await cavTask.ConfigureAwait(false), local),
            MergeLocal(await omniTask.ConfigureAwait(false), local),
            MergeLocal(await fracTask.ConfigureAwait(false), local)
        ];

        return new TriadHubSnapshot {
            FetchedAtLocal = DateTime.Now.ToString("HH:mm:ss"),
            DebianLoopbackOnline = local.Ok,
            ActiveWave = string.IsNullOrWhiteSpace(wave) ? "—" : wave,
            HandoffSummary = summary,
            NextMilestone = next,
            PendingExtraCount = pendingExtra,
            BuildLessonsCount = lessonsCount,
            Repos = repos,
            RecentSync = ParseSyncEntries(syncMd, 5)
        };
    }

    static TriadRepoCardModel MergeLocal(TriadRepoCardModel gh, LocalTriadSnapshot local) {
        if (!local.Ok) {
            return gh;
        }
        LocalTriadRepoStatus match = local.Repos.FirstOrDefault(r => string.Equals(r.Alias, gh.Alias, StringComparison.OrdinalIgnoreCase));
        if (match == null || !match.Exists) {
            return gh;
        }
        return new TriadRepoCardModel {
            Alias = gh.Alias,
            DisplayName = gh.DisplayName,
            OwnerRepo = gh.OwnerRepo,
            Branch = gh.Branch,
            LatestTag = gh.LatestTag,
            LatestApkUrl = gh.LatestApkUrl,
            LatestCommitSha = string.IsNullOrEmpty(gh.LatestCommitSha) ? match.Head : gh.LatestCommitSha,
            LatestCommitMsg = string.IsNullOrEmpty(gh.LatestCommitMsg) ? match.LastSubject : gh.LatestCommitMsg,
            CiStatus = gh.CiStatus,
            LocalDirtyCount = match.DirtyCount,
            LocalBranch = match.Branch
        };
    }

    static async Task<TriadRepoCardModel> FetchRepoMetaAsync(
        string alias,
        string displayName,
        string ownerRepo,
        string branch,
        string preferredApkName,
        string pat,
        CancellationToken ct) {
        string latestTag = "—";
        string latestApkUrl = "";
        string commitSha = "";
        string commitMsg = "";
        string ciStatus = "idle";

        bool rateLimited = false;
        try {
            GitHubCachedResponse relResp = await OmniUpdateService.SendCachedGitHubGetAsync(
                $"https://api.github.com/repos/{ownerRepo}/releases/latest", pat, token: ct).ConfigureAwait(false);
            if (relResp.Ok && !string.IsNullOrWhiteSpace(relResp.Body)) {
                using JsonDocument doc = JsonDocument.Parse(relResp.Body);
                latestTag = Str(doc.RootElement, "tag_name");
                if (!string.IsNullOrEmpty(preferredApkName) &&
                    doc.RootElement.TryGetProperty("assets", out JsonElement assets) &&
                    assets.ValueKind == JsonValueKind.Array) {
                    foreach (JsonElement a in assets.EnumerateArray()) {
                        string name = Str(a, "name");
                        string dl = Str(a, "browser_download_url");
                        if (string.Equals(name, preferredApkName, StringComparison.OrdinalIgnoreCase)) {
                            latestApkUrl = dl;
                            break;
                        }
                        if (string.IsNullOrEmpty(latestApkUrl) && name.EndsWith(".apk", StringComparison.OrdinalIgnoreCase)) {
                            latestApkUrl = dl;
                        }
                    }
                }
            } else if (relResp.RateLimited) {
                rateLimited = true;
                OmniLogger.Warn("HUB", $"GitHub API HTTP {relResp.StatusCode} for {ownerRepo} releases; using cached web redirect fallback.");
            }
        } catch (Exception ex) {
            OmniLogger.Warn("HUB", $"GitHub release lookup failed for {ownerRepo}: {ex.Message}");
        }

        if (string.IsNullOrWhiteSpace(latestTag) || latestTag == "—" ||
            (!string.IsNullOrEmpty(preferredApkName) && string.IsNullOrWhiteSpace(latestApkUrl))) {
            (string webTag, string webApkUrl) = await OmniUpdateService.ResolveLatestReleaseViaWebRedirectAsync(
                ownerRepo, preferredApkName, ct).ConfigureAwait(false);
            if (!string.IsNullOrWhiteSpace(webTag)) {
                latestTag = webTag;
            }
            if (!string.IsNullOrWhiteSpace(webApkUrl) && string.IsNullOrWhiteSpace(latestApkUrl)) {
                latestApkUrl = webApkUrl;
            }
        }

        if (!rateLimited) {
            try {
                GitHubCachedResponse cResp = await OmniUpdateService.SendCachedGitHubGetAsync(
                    $"https://api.github.com/repos/{ownerRepo}/commits/{branch}", pat, token: ct).ConfigureAwait(false);
                if (cResp.Ok && !string.IsNullOrWhiteSpace(cResp.Body)) {
                    using JsonDocument doc = JsonDocument.Parse(cResp.Body);
                    string sha = Str(doc.RootElement, "sha");
                    commitSha = sha.Length >= 7 ? sha[..7] : sha;
                    if (doc.RootElement.TryGetProperty("commit", out JsonElement cObj)) {
                        string fullMsg = Str(cObj, "message");
                        int nl = fullMsg.IndexOf('\n');
                        commitMsg = (nl > 0 ? fullMsg[..nl] : fullMsg).Trim();
                    }
                } else if (cResp.RateLimited) {
                    rateLimited = true;
                }
            } catch (Exception ex) {
                OmniLogger.Warn("HUB", $"GitHub commit lookup failed for {ownerRepo}: {ex.Message}");
            }
        }

        if (!rateLimited) {
            try {
                GitHubCachedResponse runResp = await OmniUpdateService.SendCachedGitHubGetAsync(
                    $"https://api.github.com/repos/{ownerRepo}/actions/runs?per_page=1", pat, token: ct).ConfigureAwait(false);
                if (runResp.Ok && !string.IsNullOrWhiteSpace(runResp.Body)) {
                    using JsonDocument doc = JsonDocument.Parse(runResp.Body);
                    if (doc.RootElement.TryGetProperty("workflow_runs", out JsonElement arr) &&
                        arr.ValueKind == JsonValueKind.Array &&
                        arr.GetArrayLength() > 0) {
                        JsonElement first = arr[0];
                        string st = Str(first, "status");
                        string conc = Str(first, "conclusion");
                        if (st is "in_progress" or "queued") ciStatus = "running";
                        else if (conc == "success") ciStatus = "pass";
                        else if (conc is "failure" or "timed_out") ciStatus = "fail";
                    }
                }
            } catch (Exception ex) {
                OmniLogger.Warn("HUB", $"GitHub CI lookup failed for {ownerRepo}: {ex.Message}");
            }
        }

        return new TriadRepoCardModel {
            Alias = alias,
            DisplayName = displayName,
            OwnerRepo = ownerRepo,
            Branch = branch,
            LatestTag = string.IsNullOrWhiteSpace(latestTag) ? "—" : latestTag,
            LatestApkUrl = latestApkUrl,
            LatestCommitSha = commitSha,
            LatestCommitMsg = commitMsg,
            CiStatus = ciStatus
        };
    }

    public static async Task<string> FetchDocContentAsync(string rawUrl, CancellationToken ct = default) {
        string txt = await FetchTextSafeAsync(rawUrl, ct).ConfigureAwait(false);
        return string.IsNullOrWhiteSpace(txt) ? "(Could not fetch document — check network connection.)" : txt;
    }

    public static async Task<(bool ok, string apkPath, string detail)> DownloadApkAsync(
        Context context,
        string apkUrl,
        string fileName,
        Action<double, string> onProgress = null,
        CancellationToken ct = default) {
        if (context == null || string.IsNullOrWhiteSpace(apkUrl)) {
            return (false, "", "Missing APK download URL.");
        }
        try {
            string safeName = IOPath.GetFileName(string.IsNullOrWhiteSpace(fileName) ? "update.apk" : fileName);
            string outPath = await OmniUpdateService.DownloadUpdateAsync(
                context,
                apkUrl,
                expectedBytes: 0,
                onProgress: onProgress,
                fileName: safeName,
                token: ct).ConfigureAwait(false);
            long sizeBytes = File.Exists(outPath) ? new FileInfo(outPath).Length : 0;
            OmniSettingsStore.AppendActivity("APK", $"Downloaded {safeName} ({sizeBytes / (1024 * 1024.0):0.1} MB).");
            return (true, outPath, $"{sizeBytes / (1024 * 1024.0):0.1} MB");
        } catch (Exception ex) {
            OmniLogger.Error("APK", $"DownloadApkAsync({fileName})", ex);
            return (false, "", ex.Message);
        }
    }

    public static async Task<(bool ok, string detail)> DownloadAndInstallApkAsync(
        Context context,
        string apkUrl,
        string fileName,
        CancellationToken ct = default) {
        (bool ok, string outPath, string detail) = await DownloadApkAsync(context, apkUrl, fileName, null, ct).ConfigureAwait(false);
        if (!ok) {
            return (false, detail);
        }
        try {
            string safeName = IOPath.GetFileName(outPath);
            Android.Net.Uri apkUri = OmniApkFileProvider.GetApkUri(context, safeName);
            Intent installIntent = new(Intent.ActionView);
            installIntent.SetDataAndType(apkUri, "application/vnd.android.package-archive");
            installIntent.AddFlags(ActivityFlags.NewTask | ActivityFlags.GrantReadUriPermission);
            context.StartActivity(installIntent);
            return (true, $"Launching installer ({detail})");
        } catch (Exception ex) {
            OmniLogger.Error("APK", "DownloadAndInstallApkAsync launch", ex);
            return (false, ex.Message);
        }
    }

    static async Task<string> FetchTextSafeAsync(string url, CancellationToken ct) {
        try {
            using HttpResponseMessage resp = await http.GetAsync(url, ct).ConfigureAwait(false);
            return resp.IsSuccessStatusCode ? await resp.Content.ReadAsStringAsync(ct).ConfigureAwait(false) : "";
        } catch {
            return "";
        }
    }

    static (string wave, string summary, string next) ParseHandoff(string md) {
        string wave = "", summary = "", next = "";
        if (string.IsNullOrWhiteSpace(md)) {
            return (wave, summary, next);
        }
        foreach (string raw in md.Split('\n')) {
            string line = raw.Trim();
            if (line.StartsWith("WAVE|", StringComparison.Ordinal)) wave = line[5..].Trim();
            else if (line.StartsWith("SUMMARY|", StringComparison.Ordinal)) summary = line[8..].Trim();
            else if (line.StartsWith("NEXT|", StringComparison.Ordinal)) next = line[5..].Trim();
        }
        return (wave, summary, next);
    }

    static int ParsePendingExtraCount(string md) {
        if (string.IsNullOrWhiteSpace(md)) return 0;
        string pendingBlk = md.Split("## Completed Requests", StringSplitOptions.None)[0];
        int count = 0;
        foreach (string ln in pendingBlk.Split('\n')) {
            if (ln.TrimStart().StartsWith("- REQ|", StringComparison.Ordinal)) count++;
        }
        return count;
    }

    static int ParseBuildLessonsCount(string md) {
        if (string.IsNullOrWhiteSpace(md)) return 0;
        return Regex.Matches(md, @"^###\s+`?BL-\d+`?", RegexOptions.Multiline).Count;
    }

    static List<TriadSyncEntry> ParseSyncEntries(string md, int maxItems) {
        List<TriadSyncEntry> list = [];
        if (string.IsNullOrWhiteSpace(md)) return list;
        foreach (string raw in md.Split('\n')) {
            string line = raw.Trim();
            if (!line.StartsWith("- ", StringComparison.Ordinal) || !line.Contains(" | ", StringComparison.Ordinal)) {
                continue;
            }
            string[] parts = line[2..].Split(" | ");
            if (parts.Length >= 5) {
                list.Add(new TriadSyncEntry {
                    Date = parts[0].Trim(),
                    Scope = parts[2].Trim(),
                    Summary = parts[4].Trim()
                });
                if (list.Count >= maxItems) break;
            }
        }
        return list;
    }

    static string Str(JsonElement obj, string prop) =>
        obj.ValueKind == JsonValueKind.Object && obj.TryGetProperty(prop, out JsonElement el) && el.ValueKind == JsonValueKind.String
            ? (el.GetString() ?? "")
            : "";
}
