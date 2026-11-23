using System.Net.Http.Headers;
using System.Text.Json;
using System.Text.RegularExpressions;

using Android.App;
using Android.Content;
using Android.Content.PM;

using Path = System.IO.Path;

namespace OmniAgentAndroid.Services;

public sealed class OmniUpdateCheckResult {
    public bool IsUpdateAvailable { get; init; }
    public string CurrentVersion { get; init; } = "";
    public string RemoteVersion { get; init; } = "";
    public string DownloadUrl { get; init; } = "";
    public long AssetSizeBytes { get; init; }
    public string ReleaseNotes { get; init; } = "";
    public string PublishedAt { get; init; } = "";
    public bool ResolvedViaWebFallback { get; init; }
}

/// <summary>
/// Self-update manager for OmniAgent Companion matching CavernizeAndroid's <c>AppUpdateService</c>.
/// Checks GitHub Releases (<c>guumachadooo/OmniAgent</c> tags <c>omniagent-latest</c> / <c>latest</c>)
/// with automatic zero-rate-limit GitHub Web redirect fallback when unauthenticated API hits 403/429,
/// streams APK downloads in 64 KB chunks with live progress, verifies Unknown Sources install permission,
/// and invokes the Android package installer via <see cref="OmniApkFileProvider"/>.
/// </summary>
public static class OmniUpdateService {
    const string RepoSlug = "guumachadooo/OmniAgent";
    const string OtaTagApiUrl = "https://api.github.com/repos/guumachadooo/OmniAgent/releases/tags/omniagent-latest";
    const string LatestApiUrl = "https://api.github.com/repos/guumachadooo/OmniAgent/releases/latest";
    const string LatestWebReleaseUrl = "https://github.com/guumachadooo/OmniAgent/releases/latest";
    public const string TargetAssetName = "OmniAgent-Android.apk";

    static readonly HttpClient http = BuildGitHubHttpClient();

    static HttpClient BuildGitHubHttpClient() {
        HttpClient client = new() { Timeout = TimeSpan.FromSeconds(30) };
        client.DefaultRequestHeaders.UserAgent.Add(new ProductInfoHeaderValue("OmniAgentAndroid", "2.1"));
        client.DefaultRequestHeaders.Accept.Add(new MediaTypeWithQualityHeaderValue("application/vnd.github+json"));
        return client;
    }

    public static (string versionName, long versionCode) GetInstalledVersion(Context context) {
        try {
            PackageInfo info = context?.PackageManager?.GetPackageInfo(context.PackageName, 0);
            string name = info?.VersionName ?? "2.1.0";
#pragma warning disable CS0618, CA1422
            long code = info != null
                ? (OperatingSystem.IsAndroidVersionAtLeast(28) ? info.LongVersionCode : info.VersionCode)
                : 1;
#pragma warning restore CS0618, CA1422
            return (name, code);
        } catch {
            return ("2.1.0", 1);
        }
    }

    public static async Task<OmniUpdateCheckResult> CheckForUpdateAsync(
        Context context,
        OmniSettingsStore store = null,
        CancellationToken token = default) {
        (string currentVer, _) = GetInstalledVersion(context);
        string pat = store?.LoadGitHubPat() ?? "";

        Exception lastApiError = null;
        foreach (string apiUrl in new[] { LatestApiUrl, OtaTagApiUrl }) {
            try {
                using HttpRequestMessage req = new(HttpMethod.Get, apiUrl);
                if (!string.IsNullOrWhiteSpace(pat)) {
                    req.Headers.Authorization = new AuthenticationHeaderValue("Bearer", pat);
                }
                using HttpResponseMessage resp = await http.SendAsync(req, token).ConfigureAwait(false);
                if ((int)resp.StatusCode == 403 || (int)resp.StatusCode == 429) {
                    OmniLogger.Warn("UPDATE", $"GitHub API rate-limited ({(int)resp.StatusCode}) on {apiUrl}; switching to web redirect fallback.");
                    break;
                }
                if (!resp.IsSuccessStatusCode) {
                    OmniLogger.Warn("UPDATE", $"GitHub API returned HTTP {(int)resp.StatusCode} for {apiUrl}");
                    continue;
                }

                string json = await resp.Content.ReadAsStringAsync(token).ConfigureAwait(false);
                OmniUpdateCheckResult parsed = ParseReleaseApiJson(currentVer, json);
                if (parsed != null && !string.IsNullOrEmpty(parsed.DownloadUrl)) {
                    OmniLogger.Info("UPDATE", $"Checked update via API: installed=v{currentVer}, remote=v{parsed.RemoteVersion}, available={parsed.IsUpdateAvailable}");
                    return parsed;
                }
            } catch (Exception ex) when (ex is not OperationCanceledException) {
                lastApiError = ex;
                OmniLogger.Warn("UPDATE", $"API check error on {apiUrl}: {ex.Message}");
            }
        }

        // Zero-rate-limit GitHub web redirect fallback (/releases/latest -> /releases/tag/v2.1.X)
        OmniUpdateCheckResult webRes = await CheckForUpdateViaWebRedirectAsync(RepoSlug, TargetAssetName, currentVer, token).ConfigureAwait(false);
        if (webRes != null) {
            OmniLogger.Info("UPDATE", $"Checked update via web fallback: installed=v{currentVer}, remote=v{webRes.RemoteVersion}, available={webRes.IsUpdateAvailable}");
            return webRes;
        }

        if (lastApiError != null) {
            throw lastApiError;
        }
        throw new InvalidOperationException("Unable to reach GitHub Releases for OmniAgent.");
    }

    static OmniUpdateCheckResult ParseReleaseApiJson(string currentVer, string json) {
        using JsonDocument doc = JsonDocument.Parse(json);
        JsonElement root = doc.RootElement;

        string tagName = root.TryGetProperty("tag_name", out JsonElement tEl) ? (tEl.GetString() ?? "") : "";
        string releaseName = root.TryGetProperty("name", out JsonElement nEl) ? (nEl.GetString() ?? "") : "";
        string releaseBody = root.TryGetProperty("body", out JsonElement bEl) ? (bEl.GetString() ?? "") : "";
        string publishedAt = root.TryGetProperty("published_at", out JsonElement pEl) ? (pEl.GetString() ?? "") : "";

        string downloadUrl = "";
        long assetSize = 0;
        if (root.TryGetProperty("assets", out JsonElement assetsEl) && assetsEl.ValueKind == JsonValueKind.Array) {
            foreach (JsonElement asset in assetsEl.EnumerateArray()) {
                string assetName = asset.TryGetProperty("name", out JsonElement an) ? (an.GetString() ?? "") : "";
                if (string.Equals(assetName, TargetAssetName, StringComparison.OrdinalIgnoreCase)) {
                    downloadUrl = asset.TryGetProperty("browser_download_url", out JsonElement du) ? (du.GetString() ?? "") : "";
                    if (asset.TryGetProperty("size", out JsonElement sz) && sz.TryGetInt64(out long s)) {
                        assetSize = s;
                    }
                    break;
                }
            }
            if (string.IsNullOrEmpty(downloadUrl)) {
                foreach (JsonElement asset in assetsEl.EnumerateArray()) {
                    string assetName = asset.TryGetProperty("name", out JsonElement an) ? (an.GetString() ?? "") : "";
                    if (assetName.EndsWith(".apk", StringComparison.OrdinalIgnoreCase)) {
                        downloadUrl = asset.TryGetProperty("browser_download_url", out JsonElement du) ? (du.GetString() ?? "") : "";
                        if (asset.TryGetProperty("size", out JsonElement sz) && sz.TryGetInt64(out long s)) {
                            assetSize = s;
                        }
                        break;
                    }
                }
            }
        }

        string remoteVer = ExtractVersionString(tagName) ?? ExtractVersionString(releaseName) ?? ExtractVersionString(releaseBody) ?? "";
        bool updateAvailable = !string.IsNullOrEmpty(downloadUrl) && IsRemoteNewer(currentVer, remoteVer);

        return new OmniUpdateCheckResult {
            IsUpdateAvailable = updateAvailable,
            CurrentVersion = currentVer,
            RemoteVersion = string.IsNullOrEmpty(remoteVer) ? currentVer : remoteVer,
            DownloadUrl = downloadUrl,
            AssetSizeBytes = assetSize,
            ReleaseNotes = releaseBody,
            PublishedAt = publishedAt,
            ResolvedViaWebFallback = false
        };
    }

    public static async Task<(string tag, string apkUrl)> ResolveLatestReleaseViaWebRedirectAsync(
        string ownerRepo,
        string assetFileName,
        CancellationToken token = default) {
        try {
            string webUrl = $"https://github.com/{ownerRepo}/releases/latest";
            using HttpRequestMessage req = new(HttpMethod.Get, webUrl);
            req.Headers.Accept.Clear();
            req.Headers.Accept.Add(new MediaTypeWithQualityHeaderValue("text/html"));
            using HttpResponseMessage resp = await http.SendAsync(req, HttpCompletionOption.ResponseHeadersRead, token).ConfigureAwait(false);
            string finalUrl = resp.RequestMessage?.RequestUri?.ToString() ?? "";
            Match m = Regex.Match(finalUrl, @"/releases/tag/([^/?#]+)", RegexOptions.IgnoreCase);
            if (!m.Success) {
                return ("", "");
            }
            string tag = Uri.UnescapeDataString(m.Groups[1].Value);
            string dl = string.IsNullOrWhiteSpace(assetFileName)
                ? ""
                : $"https://github.com/{ownerRepo}/releases/download/{Uri.EscapeDataString(tag)}/{Uri.EscapeDataString(assetFileName)}";
            return (tag, dl);
        } catch (Exception ex) when (ex is not OperationCanceledException) {
            OmniLogger.Warn("UPDATE", $"Web redirect fallback failed for {ownerRepo}: {ex.Message}");
            return ("", "");
        }
    }

    static async Task<OmniUpdateCheckResult> CheckForUpdateViaWebRedirectAsync(
        string ownerRepo,
        string assetFileName,
        string currentVer,
        CancellationToken token) {
        (string tag, string dlUrl) = await ResolveLatestReleaseViaWebRedirectAsync(ownerRepo, assetFileName, token).ConfigureAwait(false);
        if (string.IsNullOrEmpty(tag) || string.IsNullOrEmpty(dlUrl)) {
            return null;
        }
        string remoteVer = ExtractVersionString(tag) ?? "";
        if (string.IsNullOrEmpty(remoteVer)) {
            return null;
        }
        return new OmniUpdateCheckResult {
            IsUpdateAvailable = IsRemoteNewer(currentVer, remoteVer),
            CurrentVersion = currentVer,
            RemoteVersion = remoteVer,
            DownloadUrl = dlUrl,
            AssetSizeBytes = 0,
            ReleaseNotes = $"Release {tag}",
            PublishedAt = "",
            ResolvedViaWebFallback = true
        };
    }

    public static async Task<string> DownloadUpdateAsync(
        Context context,
        string downloadUrl,
        long expectedBytes,
        Action<double, string> onProgress,
        string fileName = TargetAssetName,
        CancellationToken token = default) {
        if (context == null) {
            throw new InvalidOperationException("Missing Android context.");
        }
        if (string.IsNullOrWhiteSpace(downloadUrl)) {
            throw new InvalidOperationException("Download URL not found in GitHub release.");
        }

        string safeName = Path.GetFileName(string.IsNullOrWhiteSpace(fileName) ? TargetAssetName : fileName);
        string apkDir = OmniApkFileProvider.GetApkDirectory(context);
        string finalApk = Path.Combine(apkDir, safeName);
        string tempApk = finalApk + ".part";
        if (File.Exists(tempApk)) {
            File.Delete(tempApk);
        }

        OmniLogger.Info("UPDATE", $"Downloading {safeName} from {downloadUrl}");
        using HttpClient dlClient = new() { Timeout = TimeSpan.FromMinutes(10) };
        dlClient.DefaultRequestHeaders.UserAgent.Add(new ProductInfoHeaderValue("OmniAgentAndroid", "2.1"));

        using HttpResponseMessage resp = await dlClient.GetAsync(downloadUrl, HttpCompletionOption.ResponseHeadersRead, token).ConfigureAwait(false);
        resp.EnsureSuccessStatusCode();

        long totalBytes = resp.Content.Headers.ContentLength ?? expectedBytes;
        long received = 0;
        await using (Stream src = await resp.Content.ReadAsStreamAsync(token).ConfigureAwait(false))
        await using (FileStream dst = File.Create(tempApk)) {
            byte[] buffer = new byte[65536];
            long nextUiBytes = 0;
            int read;
            while ((read = await src.ReadAsync(buffer, token).ConfigureAwait(false)) > 0) {
                await dst.WriteAsync(buffer.AsMemory(0, read), token).ConfigureAwait(false);
                received += read;
                if (received >= nextUiBytes || (totalBytes > 0 && received >= totalBytes)) {
                    double frac = totalBytes > 0 ? Math.Clamp(received / (double)totalBytes, 0, 1) : 0;
                    double rxMb = received / (1024.0 * 1024.0);
                    double totMb = totalBytes / (1024.0 * 1024.0);
                    string text = totalBytes > 0
                        ? $"{rxMb:0.0} MB / {totMb:0.0} MB ({frac:0%})"
                        : $"{rxMb:0.0} MB";
                    onProgress?.Invoke(frac, text);
                    nextUiBytes = received + 200_000;
                }
            }
        }

        if (File.Exists(finalApk)) {
            File.Delete(finalApk);
        }
        File.Move(tempApk, finalApk);
        OmniLogger.Info("UPDATE", $"Downloaded {safeName} ({received / (1024.0 * 1024.0):0.1} MB) -> {finalApk}");
        return finalApk;
    }

    public static bool CanInstallPackages(Context context) {
        if (context == null) {
            return false;
        }
        if (OperatingSystem.IsAndroidVersionAtLeast(26)) {
            return context.PackageManager?.CanRequestPackageInstalls() == true;
        }
        return true;
    }

    public static void OpenUnknownSourcesSettings(Activity activity) {
        if (activity == null) {
            return;
        }
        if (OperatingSystem.IsAndroidVersionAtLeast(26)) {
            Intent intent = new(
                Android.Provider.Settings.ActionManageUnknownAppSources,
                Android.Net.Uri.Parse("package:" + activity.PackageName));
            activity.StartActivity(intent);
        }
    }

    public static void LaunchInstaller(Activity activity, string apkPath) {
        if (activity == null || string.IsNullOrWhiteSpace(apkPath) || !File.Exists(apkPath)) {
            return;
        }
        Android.Net.Uri apkUri = OmniApkFileProvider.GetApkUri(activity, Path.GetFileName(apkPath));
        Intent install = new(Intent.ActionView);
        install.SetDataAndType(apkUri, "application/vnd.android.package-archive");
        install.AddFlags(ActivityFlags.GrantReadUriPermission | ActivityFlags.NewTask);
        OmniLogger.Info("UPDATE", $"Launching Android package installer for {Path.GetFileName(apkPath)}");
        activity.StartActivity(install);
    }

    public static string ExtractVersionString(string text) {
        if (string.IsNullOrWhiteSpace(text)) {
            return null;
        }
        Match m = Regex.Match(text, @"\bv?(\d+\.\d+\.\d+(?:\.\d+)?)\b");
        return m.Success ? m.Groups[1].Value : null;
    }

    public static bool IsRemoteNewer(string localVer, string remoteVer) {
        string cleanLocal = ExtractVersionString(localVer);
        string cleanRemote = ExtractVersionString(remoteVer);
        if (string.IsNullOrWhiteSpace(cleanRemote) || string.IsNullOrWhiteSpace(cleanLocal)) {
            return false;
        }
        if (Version.TryParse(cleanLocal, out Version lv) && Version.TryParse(cleanRemote, out Version rv)) {
            return rv > lv;
        }
        return false;
    }
}
