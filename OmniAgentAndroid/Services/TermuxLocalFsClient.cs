using System.Diagnostics;
using System.Net;
using System.Net.Http.Headers;
using System.Text.Json;

using IOPath = System.IO.Path;

namespace OmniAgentAndroid.Services;

public enum LocalFsFailureKind {
    None = 0,
    MissingPairKey = 1,
    Unauthorized = 2,
    DaemonOffline = 3,
    NotFound = 4,
    PermissionDenied = 5,
    Error = 6
}

public sealed class LocalFsEntry {
    public string Name { get; init; } = "";
    public string FullPath { get; init; } = "";
    public bool IsDir { get; init; }
    public bool IsSymlink { get; init; }
    public long SizeBytes { get; init; }
    public long MtimeUnix { get; init; }
    public string Extension { get; init; } = "";
}

public sealed class LocalFsListResult {
    public bool Ok { get; init; }
    public bool IsLocalAndroidFallback { get; init; }
    public LocalFsFailureKind FailureKind { get; init; } = LocalFsFailureKind.None;
    public int HttpStatusCode { get; init; }
    public string Path { get; init; } = "";
    public string Parent { get; init; } = "";
    public string Error { get; init; } = "";
    public List<LocalFsEntry> Entries { get; init; } = [];
}

public sealed class LocalFsReadResult {
    public bool Ok { get; init; }
    public string Path { get; init; } = "";
    public string Name { get; init; } = "";
    public long SizeBytes { get; init; }
    public int LineCount { get; init; }
    public bool Truncated { get; init; }
    public bool IsBinary { get; init; }
    public string Content { get; init; } = "";
    public string Error { get; init; } = "";
}

public sealed class LocalTriadRepoStatus {
    public string Alias { get; init; } = "";
    public string OwnerRepo { get; init; } = "";
    public string Path { get; init; } = "";
    public bool Exists { get; init; }
    public string Branch { get; init; } = "";
    public string Head { get; init; } = "";
    public string LastSubject { get; init; } = "";
    public int DirtyCount { get; init; }
}

public sealed class LocalTriadSnapshot {
    public bool Ok { get; init; }
    public string Hostname { get; init; } = "";
    public string Wave { get; init; } = "";
    public string Summary { get; init; } = "";
    public string Next { get; init; } = "";
    public int PendingExtra { get; init; }
    public int BuildLessons { get; init; }
    public List<LocalTriadRepoStatus> Repos { get; init; } = [];
}

/// <summary>
/// Ultra-fast (&lt;5 ms) authenticated 127.0.0.1 loopback client talking directly to
/// <c>omni_agent_daemon.py</c> inside Debian PRoot / Termux (using HMAC-SHA256 key derived
/// from the same 256-bit pairSecret), with direct Android <c>/storage/emulated/0</c> fallback
/// and full <see cref="OmniLogger"/> diagnostics.
/// </summary>
public static class TermuxLocalFsClient {
    static readonly HttpClient http = BuildClient();

    public static readonly (string Label, string Path)[] DefaultBookmarks = [
        ("Cavern", "/root/.gemini/antigravity/scratch/Cavern"),
        ("OmniAgent", "/root/.gemini/antigravity/scratch/OmniAgent"),
        ("Fractal", "/root/.gemini/antigravity/scratch/dynamic-fractal-planner"),
        ("Antigravity", "/root/.gemini/antigravity"),
        ("Debian /root", "/root"),
        ("Termux ~", "/data/data/com.termux/files/home"),
        ("Android /sdcard", "/storage/emulated/0")
    ];

    static HttpClient BuildClient() {
        HttpClient c = new() { Timeout = TimeSpan.FromSeconds(8) };
        c.DefaultRequestHeaders.Accept.Add(new MediaTypeWithQualityHeaderValue("application/json"));
        return c;
    }

    static string BaseUrl(OmniSettingsStore store) => $"http://127.0.0.1:{store.LocalFsPort}";

    static HttpRequestMessage CreateAuthed(HttpMethod method, string url, OmniSettingsStore store) {
        string key = OmniCrypto.ComputeLocalFsKey(store.LoadPairSecretHex());
        HttpRequestMessage req = new(method, url);
        if (!string.IsNullOrEmpty(key)) {
            req.Headers.Add("X-Omni-Local-Key", key);
        }
        return req;
    }

    static bool TryParseJson(string raw, out JsonDocument doc) {
        doc = null;
        if (string.IsNullOrWhiteSpace(raw)) {
            return false;
        }
        try {
            doc = JsonDocument.Parse(raw);
            return true;
        } catch {
            return false;
        }
    }

    public static async Task<(bool online, string detail)> CheckHealthAsync(OmniSettingsStore store, CancellationToken ct = default) {
        if (store == null || !store.HasPairSecret) {
            OmniLogger.Warn("LOCALFS", "Health check skipped: Pair Key not configured in OmniAgent.");
            return (false, "No Pair Key configured");
        }
        Stopwatch sw = Stopwatch.StartNew();
        try {
            string url = $"{BaseUrl(store)}/local/health";
            using HttpRequestMessage req = CreateAuthed(HttpMethod.Get, url, store);
            using HttpResponseMessage resp = await http.SendAsync(req, ct).ConfigureAwait(false);
            sw.Stop();
            if (resp.StatusCode == HttpStatusCode.Unauthorized) {
                OmniLogger.Warn("LOCALFS", $"GET /local/health -> HTTP 401 Unauthorized ({sw.ElapsedMilliseconds} ms): Pair Key in OmniAgent does not match Debian daemon.");
                return (false, "HTTP 401 (Pair Key mismatch)");
            }
            if (!resp.IsSuccessStatusCode) {
                OmniLogger.Warn("LOCALFS", $"GET /local/health -> HTTP {(int)resp.StatusCode} ({sw.ElapsedMilliseconds} ms)");
                return (false, $"HTTP {(int)resp.StatusCode}");
            }
            string raw = await resp.Content.ReadAsStringAsync(ct).ConfigureAwait(false);
            if (!TryParseJson(raw, out JsonDocument doc)) {
                return (false, "Invalid JSON from loopback");
            }
            using (doc) {
                string ver = doc.RootElement.TryGetProperty("version", out JsonElement vEl) ? (vEl.GetString() ?? "") : "";
                long up = doc.RootElement.TryGetProperty("uptimeSec", out JsonElement uEl) && uEl.TryGetInt64(out long u) ? u : 0;
                OmniLogger.Info("LOCALFS", $"GET /local/health OK ({sw.ElapsedMilliseconds} ms): Debian v{ver}, up {up / 60}m");
                return (true, $"Debian v{ver} · up {up / 60}m");
            }
        } catch (OperationCanceledException) when (ct.IsCancellationRequested) {
            throw;
        } catch (Exception ex) {
            sw.Stop();
            OmniLogger.Warn("LOCALFS", $"GET /local/health unreachable ({sw.ElapsedMilliseconds} ms): {ex.GetType().Name}: {ex.Message}");
            return (false, "Loopback asleep");
        }
    }

    public static async Task<LocalTriadSnapshot> GetLocalTriadAsync(OmniSettingsStore store, CancellationToken ct = default) {
        if (store == null || !store.HasPairSecret) {
            return new LocalTriadSnapshot { Ok = false };
        }
        Stopwatch sw = Stopwatch.StartNew();
        try {
            using HttpRequestMessage req = CreateAuthed(HttpMethod.Get, $"{BaseUrl(store)}/local/triad", store);
            using HttpResponseMessage resp = await http.SendAsync(req, ct).ConfigureAwait(false);
            string raw = await resp.Content.ReadAsStringAsync(ct).ConfigureAwait(false);
            sw.Stop();
            if (!resp.IsSuccessStatusCode || !TryParseJson(raw, out JsonDocument doc)) {
                OmniLogger.Warn("LOCALFS", $"GET /local/triad failed: HTTP {(int)resp.StatusCode} ({sw.ElapsedMilliseconds} ms) body={raw[..Math.Min(160, raw.Length)]}");
                return new LocalTriadSnapshot { Ok = false };
            }
            using (doc) {
                JsonElement root = doc.RootElement;
                List<LocalTriadRepoStatus> repos = [];
                if (root.TryGetProperty("repos", out JsonElement rArr) && rArr.ValueKind == JsonValueKind.Array) {
                    foreach (JsonElement r in rArr.EnumerateArray()) {
                        repos.Add(new LocalTriadRepoStatus {
                            Alias = Str(r, "alias"),
                            OwnerRepo = Str(r, "ownerRepo"),
                            Path = Str(r, "path"),
                            Exists = r.TryGetProperty("exists", out JsonElement exEl) && exEl.ValueKind == JsonValueKind.True,
                            Branch = Str(r, "branch"),
                            Head = Str(r, "head"),
                            LastSubject = Str(r, "lastSubject"),
                            DirtyCount = r.TryGetProperty("dirtyCount", out JsonElement dEl) && dEl.TryGetInt32(out int dc) ? dc : 0
                        });
                    }
                }
                string wave = "", summary = "", next = "";
                if (root.TryGetProperty("handoff", out JsonElement hEl) && hEl.ValueKind == JsonValueKind.Object) {
                    wave = Str(hEl, "wave");
                    summary = Str(hEl, "summary");
                    next = Str(hEl, "next");
                }
                OmniLogger.Info("LOCALFS", $"GET /local/triad OK ({sw.ElapsedMilliseconds} ms, repos={repos.Count})");
                return new LocalTriadSnapshot {
                    Ok = true,
                    Hostname = Str(root, "hostname"),
                    Wave = wave,
                    Summary = summary,
                    Next = next,
                    PendingExtra = root.TryGetProperty("pendingExtraRequests", out JsonElement peEl) && peEl.TryGetInt32(out int pe) ? pe : 0,
                    BuildLessons = root.TryGetProperty("buildLessonsCount", out JsonElement blEl) && blEl.TryGetInt32(out int bl) ? bl : 0,
                    Repos = repos
                };
            }
        } catch (OperationCanceledException) when (ct.IsCancellationRequested) {
            throw;
        } catch (Exception ex) {
            sw.Stop();
            OmniLogger.Warn("LOCALFS", $"GET /local/triad error ({sw.ElapsedMilliseconds} ms): {ex.GetType().Name}: {ex.Message}");
            return new LocalTriadSnapshot { Ok = false };
        }
    }

    public static async Task<LocalFsListResult> ListDirectoryAsync(OmniSettingsStore store, string path, CancellationToken ct = default) {
        string cleanPath = string.IsNullOrWhiteSpace(path) ? "/root" : path.Trim();
        bool isSdcardPath = cleanPath.StartsWith("/storage/emulated/0", StringComparison.Ordinal) ||
            cleanPath.StartsWith("/sdcard", StringComparison.Ordinal);

        if (store == null || !store.HasPairSecret) {
            if (isSdcardPath) {
                LocalFsListResult directNoKey = ListAndroidDirectoryDirect(cleanPath);
                if (directNoKey.Ok) {
                    return directNoKey;
                }
            }
            OmniLogger.Warn("LOCALFS", $"ListDirectory('{cleanPath}') blocked: Pair Key not configured.");
            return new LocalFsListResult {
                Ok = false,
                FailureKind = LocalFsFailureKind.MissingPairKey,
                Path = cleanPath,
                Error = "Pair Key not configured in OmniAgent. Sync from Cavernize or configure Worker & Key."
            };
        }

        Stopwatch sw = Stopwatch.StartNew();
        try {
            string url = $"{BaseUrl(store)}/local/fs/list?path={Uri.EscapeDataString(cleanPath)}&hidden=1";
            using HttpRequestMessage req = CreateAuthed(HttpMethod.Get, url, store);
            using HttpResponseMessage resp = await http.SendAsync(req, ct).ConfigureAwait(false);
            string raw = await resp.Content.ReadAsStringAsync(ct).ConfigureAwait(false);
            sw.Stop();

            int code = (int)resp.StatusCode;
            if (!TryParseJson(raw, out JsonDocument doc)) {
                string err = $"HTTP {code}: invalid response from 127.0.0.1:{store.LocalFsPort}";
                OmniLogger.Error("LOCALFS", $"ListDirectory('{cleanPath}') failed ({sw.ElapsedMilliseconds} ms): {err}");
                return new LocalFsListResult {
                    Ok = false,
                    FailureKind = code == 401 ? LocalFsFailureKind.Unauthorized : LocalFsFailureKind.Error,
                    HttpStatusCode = code,
                    Path = cleanPath,
                    Error = err
                };
            }

            using (doc) {
                JsonElement root = doc.RootElement;
                if (!resp.IsSuccessStatusCode || !(root.TryGetProperty("ok", out JsonElement okEl) && okEl.ValueKind == JsonValueKind.True)) {
                    string apiErr = Str(root, "error");
                    if (string.IsNullOrWhiteSpace(apiErr)) {
                        apiErr = $"HTTP {code}";
                    }
                    LocalFsFailureKind kind = code switch {
                        401 => LocalFsFailureKind.Unauthorized,
                        403 => LocalFsFailureKind.PermissionDenied,
                        404 => LocalFsFailureKind.NotFound,
                        _ => LocalFsFailureKind.Error
                    };
                    OmniLogger.Warn("LOCALFS", $"ListDirectory('{cleanPath}') -> HTTP {code} ({kind}, {sw.ElapsedMilliseconds} ms): {apiErr}");
                    return new LocalFsListResult {
                        Ok = false,
                        FailureKind = kind,
                        HttpStatusCode = code,
                        Path = cleanPath,
                        Error = code == 401
                            ? $"HTTP 401: {apiErr} (Pair Key in OmniAgent differs from Debian /root/.omni_agent/config.json)"
                            : apiErr
                    };
                }

                List<LocalFsEntry> list = [];
                if (root.TryGetProperty("entries", out JsonElement arr) && arr.ValueKind == JsonValueKind.Array) {
                    foreach (JsonElement e in arr.EnumerateArray()) {
                        list.Add(new LocalFsEntry {
                            Name = Str(e, "name"),
                            FullPath = Str(e, "path"),
                            IsDir = e.TryGetProperty("isDir", out JsonElement dEl) && dEl.ValueKind == JsonValueKind.True,
                            IsSymlink = e.TryGetProperty("isSymlink", out JsonElement sEl) && sEl.ValueKind == JsonValueKind.True,
                            SizeBytes = e.TryGetProperty("sizeBytes", out JsonElement szEl) && szEl.TryGetInt64(out long sz) ? sz : 0L,
                            MtimeUnix = e.TryGetProperty("mtimeUnix", out JsonElement mtEl) && mtEl.TryGetInt64(out long mt) ? mt : 0L,
                            Extension = Str(e, "ext")
                        });
                    }
                }
                string resolvedPath = Str(root, "path");
                OmniLogger.Info("LOCALFS", $"ListDirectory('{resolvedPath}') OK ({list.Count} items, {sw.ElapsedMilliseconds} ms)");
                return new LocalFsListResult {
                    Ok = true,
                    FailureKind = LocalFsFailureKind.None,
                    HttpStatusCode = code,
                    Path = resolvedPath,
                    Parent = Str(root, "parent"),
                    Entries = list
                };
            }
        } catch (OperationCanceledException) when (ct.IsCancellationRequested) {
            throw;
        } catch (Exception ex) {
            sw.Stop();
            // If browsing Android /sdcard and Debian loopback is asleep, try direct Android I/O fallback
            if (isSdcardPath) {
                LocalFsListResult direct = ListAndroidDirectoryDirect(cleanPath);
                if (direct.Ok) {
                    OmniLogger.Info("LOCALFS", $"ListDirectory('{cleanPath}') fell back to direct Android I/O ({direct.Entries.Count} items)");
                    return direct;
                }
            }
            string detail = $"{ex.GetType().Name}: {ex.Message} (127.0.0.1:{store.LocalFsPort})";
            OmniLogger.Warn("LOCALFS", $"ListDirectory('{cleanPath}') loopback unreachable ({sw.ElapsedMilliseconds} ms): {detail}");
            return new LocalFsListResult {
                Ok = false,
                FailureKind = LocalFsFailureKind.DaemonOffline,
                Path = cleanPath,
                Error = detail
            };
        }
    }

    static LocalFsListResult ListAndroidDirectoryDirect(string rawPath) {
        try {
            string actual = rawPath.StartsWith("/sdcard", StringComparison.Ordinal)
                ? "/storage/emulated/0" + rawPath[7..]
                : rawPath;
            if (!Directory.Exists(actual)) {
                return new LocalFsListResult { Ok = false, FailureKind = LocalFsFailureKind.NotFound, Path = actual, Error = "Folder not accessible" };
            }
            List<LocalFsEntry> entries = [];
            foreach (string dir in Directory.EnumerateDirectories(actual)) {
                DirectoryInfo di = new(dir);
                entries.Add(new LocalFsEntry {
                    Name = di.Name,
                    FullPath = di.FullName,
                    IsDir = true,
                    MtimeUnix = new DateTimeOffset(di.LastWriteTimeUtc).ToUnixTimeSeconds()
                });
            }
            foreach (string file in Directory.EnumerateFiles(actual)) {
                FileInfo fi = new(file);
                entries.Add(new LocalFsEntry {
                    Name = fi.Name,
                    FullPath = fi.FullName,
                    IsDir = false,
                    SizeBytes = fi.Length,
                    MtimeUnix = new DateTimeOffset(fi.LastWriteTimeUtc).ToUnixTimeSeconds(),
                    Extension = fi.Extension.ToLowerInvariant()
                });
            }
            entries.Sort((a, b) => a.IsDir != b.IsDir ? (a.IsDir ? -1 : 1) : string.Compare(a.Name, b.Name, StringComparison.OrdinalIgnoreCase));
            string trimmed = actual.TrimEnd('/');
            string parent = trimmed.Length > "/storage/emulated/0".Length ? (IOPath.GetDirectoryName(trimmed) ?? "") : "";
            return new LocalFsListResult {
                Ok = true,
                IsLocalAndroidFallback = true,
                Path = actual,
                Parent = parent,
                Entries = entries.Take(500).ToList()
            };
        } catch (Exception ex) {
            return new LocalFsListResult { Ok = false, FailureKind = LocalFsFailureKind.PermissionDenied, Path = rawPath, Error = ex.Message };
        }
    }

    public static async Task<LocalFsReadResult> ReadFileAsync(OmniSettingsStore store, string path, int maxBytes = 196608, CancellationToken ct = default) {
        try {
            string url = $"{BaseUrl(store)}/local/fs/read?path={Uri.EscapeDataString(path)}&maxBytes={maxBytes}";
            using HttpRequestMessage req = CreateAuthed(HttpMethod.Get, url, store);
            using HttpResponseMessage resp = await http.SendAsync(req, ct).ConfigureAwait(false);
            string raw = await resp.Content.ReadAsStringAsync(ct).ConfigureAwait(false);
            if (!TryParseJson(raw, out JsonDocument doc)) {
                return new LocalFsReadResult { Ok = false, Path = path, Error = $"HTTP {(int)resp.StatusCode}" };
            }
            using (doc) {
                JsonElement root = doc.RootElement;
                if (!resp.IsSuccessStatusCode || !(root.TryGetProperty("ok", out JsonElement okEl) && okEl.ValueKind == JsonValueKind.True)) {
                    string err = Str(root, "error");
                    OmniLogger.Warn("LOCALFS", $"ReadFile('{path}') failed: HTTP {(int)resp.StatusCode} {err}");
                    return new LocalFsReadResult { Ok = false, Path = path, Error = string.IsNullOrEmpty(err) ? $"HTTP {(int)resp.StatusCode}" : err };
                }
                return new LocalFsReadResult {
                    Ok = true,
                    Path = Str(root, "path"),
                    Name = Str(root, "name"),
                    SizeBytes = root.TryGetProperty("sizeBytes", out JsonElement szEl) && szEl.TryGetInt64(out long sz) ? sz : 0L,
                    LineCount = root.TryGetProperty("lineCount", out JsonElement lcEl) && lcEl.TryGetInt32(out int lc) ? lc : 0,
                    Truncated = root.TryGetProperty("truncated", out JsonElement trEl) && trEl.ValueKind == JsonValueKind.True,
                    IsBinary = root.TryGetProperty("isBinary", out JsonElement bnEl) && bnEl.ValueKind == JsonValueKind.True,
                    Content = Str(root, "content")
                };
            }
        } catch (Exception ex) {
            if ((path.StartsWith("/storage/emulated/0", StringComparison.Ordinal) || path.StartsWith("/sdcard", StringComparison.Ordinal)) && File.Exists(path)) {
                try {
                    FileInfo fi = new(path);
                    byte[] buf = new byte[Math.Min(fi.Length, maxBytes)];
                    using FileStream fs = File.OpenRead(path);
                    int read = await fs.ReadAsync(buf.AsMemory(0, buf.Length), ct).ConfigureAwait(false);
                    bool bin = false;
                    for (int i = 0; i < Math.Min(read, 4096); i++) {
                        if (buf[i] == 0) { bin = true; break; }
                    }
                    string text = bin ? "" : System.Text.Encoding.UTF8.GetString(buf, 0, read);
                    return new LocalFsReadResult {
                        Ok = true,
                        Path = path,
                        Name = fi.Name,
                        SizeBytes = fi.Length,
                        Truncated = fi.Length > read,
                        IsBinary = bin,
                        LineCount = bin ? 0 : text.Split('\n').Length,
                        Content = text
                    };
                } catch {
                    // Fall through to error return
                }
            }
            OmniLogger.Warn("LOCALFS", $"ReadFile('{path}') error: {ex.Message}");
            return new LocalFsReadResult { Ok = false, Path = path, Error = ex.Message };
        }
    }

    public static async Task<(bool ok, string exportedPath, string error)> ExportToDownloadsAsync(OmniSettingsStore store, string path, CancellationToken ct = default) {
        try {
            using HttpRequestMessage req = CreateAuthed(HttpMethod.Post, $"{BaseUrl(store)}/local/fs/export", store);
            req.Content = new StringContent(JsonSerializer.Serialize(new { path }), System.Text.Encoding.UTF8, "application/json");
            using HttpResponseMessage resp = await http.SendAsync(req, ct).ConfigureAwait(false);
            string raw = await resp.Content.ReadAsStringAsync(ct).ConfigureAwait(false);
            if (!TryParseJson(raw, out JsonDocument doc)) {
                return (false, "", $"HTTP {(int)resp.StatusCode}");
            }
            using (doc) {
                JsonElement root = doc.RootElement;
                bool ok = resp.IsSuccessStatusCode && root.TryGetProperty("ok", out JsonElement okEl) && okEl.ValueKind == JsonValueKind.True;
                return (ok, Str(root, "exportedPath"), Str(root, "error"));
            }
        } catch (Exception ex) {
            OmniLogger.Warn("LOCALFS", $"ExportToDownloads('{path}') error: {ex.Message}");
            return (false, "", ex.Message);
        }
    }

    public static async Task<(bool ok, string savedPath, string error)> SaveLogToLoopbackAsync(
        OmniSettingsStore store,
        string fileName,
        string content,
        CancellationToken ct = default) {
        if (store == null || !store.HasPairSecret) {
            return (false, "", "No Pair Key");
        }
        try {
            using HttpRequestMessage req = CreateAuthed(HttpMethod.Post, $"{BaseUrl(store)}/local/logs/save", store);
            req.Content = new StringContent(
                JsonSerializer.Serialize(new { fileName, content }),
                System.Text.Encoding.UTF8,
                "application/json");
            using HttpResponseMessage resp = await http.SendAsync(req, ct).ConfigureAwait(false);
            string raw = await resp.Content.ReadAsStringAsync(ct).ConfigureAwait(false);
            if (!TryParseJson(raw, out JsonDocument doc)) {
                return (false, "", $"HTTP {(int)resp.StatusCode}");
            }
            using (doc) {
                JsonElement root = doc.RootElement;
                bool ok = resp.IsSuccessStatusCode && root.TryGetProperty("ok", out JsonElement okEl) && okEl.ValueKind == JsonValueKind.True;
                return (ok, Str(root, "savedPath"), Str(root, "error"));
            }
        } catch (Exception ex) {
            return (false, "", ex.Message);
        }
    }

    public static async Task<(bool ok, string error)> CreateDirectoryAsync(OmniSettingsStore store, string fullPath, CancellationToken ct = default) {
        try {
            using HttpRequestMessage req = CreateAuthed(HttpMethod.Post, $"{BaseUrl(store)}/local/fs/mkdir", store);
            req.Content = new StringContent(JsonSerializer.Serialize(new { path = fullPath }), System.Text.Encoding.UTF8, "application/json");
            using HttpResponseMessage resp = await http.SendAsync(req, ct).ConfigureAwait(false);
            string raw = await resp.Content.ReadAsStringAsync(ct).ConfigureAwait(false);
            if (!TryParseJson(raw, out JsonDocument doc)) {
                return (false, $"HTTP {(int)resp.StatusCode}");
            }
            using (doc) {
                bool ok = resp.IsSuccessStatusCode && doc.RootElement.TryGetProperty("ok", out JsonElement okEl) && okEl.ValueKind == JsonValueKind.True;
                return (ok, Str(doc.RootElement, "error"));
            }
        } catch (Exception ex) {
            OmniLogger.Warn("LOCALFS", $"CreateDirectory('{fullPath}') error: {ex.Message}");
            return (false, ex.Message);
        }
    }

    public static async Task<(bool ok, string error)> DeletePathAsync(OmniSettingsStore store, string fullPath, CancellationToken ct = default) {
        try {
            using HttpRequestMessage req = CreateAuthed(HttpMethod.Post, $"{BaseUrl(store)}/local/fs/delete", store);
            req.Content = new StringContent(JsonSerializer.Serialize(new { path = fullPath }), System.Text.Encoding.UTF8, "application/json");
            using HttpResponseMessage resp = await http.SendAsync(req, ct).ConfigureAwait(false);
            string raw = await resp.Content.ReadAsStringAsync(ct).ConfigureAwait(false);
            if (!TryParseJson(raw, out JsonDocument doc)) {
                return (false, $"HTTP {(int)resp.StatusCode}");
            }
            using (doc) {
                bool ok = resp.IsSuccessStatusCode && doc.RootElement.TryGetProperty("ok", out JsonElement okEl) && okEl.ValueKind == JsonValueKind.True;
                return (ok, Str(doc.RootElement, "error"));
            }
        } catch (Exception ex) {
            OmniLogger.Warn("LOCALFS", $"DeletePath('{fullPath}') error: {ex.Message}");
            return (false, ex.Message);
        }
    }

    public static async Task<(bool ok, string summary)> PullAllTriadReposAsync(OmniSettingsStore store, CancellationToken ct = default) {
        try {
            using HttpClient longHttp = new() { Timeout = TimeSpan.FromSeconds(65) };
            using HttpRequestMessage req = CreateAuthed(HttpMethod.Post, $"{BaseUrl(store)}/local/git/pull_all", store);
            req.Content = new StringContent("{}", System.Text.Encoding.UTF8, "application/json");
            using HttpResponseMessage resp = await longHttp.SendAsync(req, ct).ConfigureAwait(false);
            string raw = await resp.Content.ReadAsStringAsync(ct).ConfigureAwait(false);
            if (!TryParseJson(raw, out JsonDocument doc)) {
                return (false, $"HTTP {(int)resp.StatusCode}: invalid response");
            }
            using (doc) {
                JsonElement root = doc.RootElement;
                bool ok = resp.IsSuccessStatusCode && root.TryGetProperty("ok", out JsonElement okEl) && okEl.ValueKind == JsonValueKind.True;
                List<string> lines = [];
                if (root.TryGetProperty("results", out JsonElement arr) && arr.ValueKind == JsonValueKind.Array) {
                    foreach (JsonElement r in arr.EnumerateArray()) {
                        string a = Str(r, "alias");
                        bool rok = r.TryGetProperty("ok", out JsonElement e) && e.ValueKind == JsonValueKind.True;
                        lines.Add($"{a}: {(rok ? "OK" : "ERR")} {Str(r, "output")}");
                    }
                }
                string summary = lines.Count > 0 ? string.Join("\n", lines) : Str(root, "error");
                OmniLogger.Info("LOCALFS", $"PullAllTriad -> ok={ok}: {summary.Replace('\n', ' ')}");
                return (ok, summary);
            }
        } catch (Exception ex) {
            OmniLogger.Error("LOCALFS", "PullAllTriadReposAsync", ex);
            return (false, ex.Message);
        }
    }

    public static string FormatSize(long bytes) {
        if (bytes < 1024) return $"{bytes} B";
        if (bytes < 1024 * 1024) return $"{bytes / 1024.0:0.0} KB";
        if (bytes < 1024L * 1024L * 1024L) return $"{bytes / (1024.0 * 1024.0):0.0} MB";
        return $"{bytes / (1024.0 * 1024.0 * 1024.0):0.00} GB";
    }

    static string Str(JsonElement obj, string prop) =>
        obj.ValueKind == JsonValueKind.Object && obj.TryGetProperty(prop, out JsonElement el) && el.ValueKind == JsonValueKind.String
            ? (el.GetString() ?? "")
            : "";
}
