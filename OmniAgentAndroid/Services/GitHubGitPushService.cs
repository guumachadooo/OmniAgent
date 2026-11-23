using System.Net.Http.Headers;
using System.Text.Json;

namespace OmniAgentAndroid.Services;

/// <summary>
/// Atomic multi-file GitHub Git Data API commit &amp; push client running directly on Android.
/// Uses the Keystore-encrypted Fine-Grained PAT so OmniAgent can push real commits to GitHub
/// even when Termux is closed.
/// </summary>
public static class GitHubGitPushService {
    static readonly HttpClient http = BuildClient();

    static HttpClient BuildClient() {
        HttpClient c = new() { Timeout = TimeSpan.FromSeconds(60) };
        c.DefaultRequestHeaders.UserAgent.Add(new ProductInfoHeaderValue("OmniAgentAndroid", "1.0"));
        c.DefaultRequestHeaders.Accept.Add(new MediaTypeWithQualityHeaderValue("application/vnd.github+json"));
        c.DefaultRequestHeaders.Add("X-GitHub-Api-Version", "2022-11-28");
        return c;
    }

    public static async Task<(bool ok, string headSha, string message)> PushDeltaBundleAsync(
        string pat,
        string ownerRepo,
        string branch,
        string commitMessage,
        JsonElement bundleEl,
        CancellationToken ct) {
        if (string.IsNullOrWhiteSpace(pat)) {
            return (false, "", "GitHub Fine-Grained PAT not configured in OmniAgent.");
        }
        if (string.IsNullOrWhiteSpace(ownerRepo) || !ownerRepo.Contains('/')) {
            return (false, "", "Invalid GitHub repository (expected owner/repo).");
        }
        if (string.IsNullOrWhiteSpace(commitMessage)) {
            return (false, "", "Commit message is required.");
        }

        string apiBase = $"https://api.github.com/repos/{ownerRepo.Trim()}";
        string cleanBranch = string.IsNullOrWhiteSpace(branch) ? "master" : branch.Trim();

        // 1. Resolve current branch HEAD commit SHA
        using JsonDocument refDoc = await SendJsonAsync(HttpMethod.Get, $"{apiBase}/git/ref/heads/{cleanBranch}", pat, null, ct).ConfigureAwait(false);
        string headCommitSha = refDoc.RootElement.GetProperty("object").GetProperty("sha").GetString();

        // 2. Resolve base tree SHA
        using JsonDocument commitDoc = await SendJsonAsync(HttpMethod.Get, $"{apiBase}/git/commits/{headCommitSha}", pat, null, ct).ConfigureAwait(false);
        string baseTreeSha = commitDoc.RootElement.GetProperty("tree").GetProperty("sha").GetString();

        // 3. Create Git blobs for each modified/added file in bundle
        List<object> treeEntries = [];
        int fileCount = 0;
        if (bundleEl.TryGetProperty("files", out JsonElement filesEl) && filesEl.ValueKind == JsonValueKind.Array) {
            foreach (JsonElement f in filesEl.EnumerateArray()) {
                string path = (f.GetProperty("path").GetString() ?? "").TrimStart('/');
                if (string.IsNullOrEmpty(path)) {
                    continue;
                }
                byte[] gz = Convert.FromBase64String(f.GetProperty("gz_b64").GetString() ?? "");
                byte[] raw = OmniCrypto.GzipDecompress(gz);
                bool isExec = f.TryGetProperty("exec", out JsonElement exEl) && exEl.GetBoolean();

                using JsonDocument blobDoc = await SendJsonAsync(
                    HttpMethod.Post,
                    $"{apiBase}/git/blobs",
                    pat,
                    new { content = Convert.ToBase64String(raw), encoding = "base64" },
                    ct).ConfigureAwait(false);
                string blobSha = blobDoc.RootElement.GetProperty("sha").GetString();

                treeEntries.Add(new {
                    path,
                    mode = isExec ? "100755" : "100644",
                    type = "blob",
                    sha = blobSha
                });
                fileCount++;
            }
        }

        if (bundleEl.TryGetProperty("deleted", out JsonElement delEl) && delEl.ValueKind == JsonValueKind.Array) {
            foreach (JsonElement d in delEl.EnumerateArray()) {
                string delPath = (d.GetString() ?? "").TrimStart('/');
                if (string.IsNullOrEmpty(delPath)) {
                    continue;
                }
                treeEntries.Add(new {
                    path = delPath,
                    mode = "100644",
                    type = "blob",
                    sha = (string)null
                });
            }
        }

        if (treeEntries.Count == 0) {
            return (true, headCommitSha, "No file changes to commit.");
        }

        // 4. Create new Git tree anchored to base_tree
        using JsonDocument treeDoc = await SendJsonAsync(
            HttpMethod.Post,
            $"{apiBase}/git/trees",
            pat,
            new { base_tree = baseTreeSha, tree = treeEntries },
            ct).ConfigureAwait(false);
        string newTreeSha = treeDoc.RootElement.GetProperty("sha").GetString();

        // 5. Create new Git commit
        using JsonDocument newCommitDoc = await SendJsonAsync(
            HttpMethod.Post,
            $"{apiBase}/git/commits",
            pat,
            new {
                message = commitMessage,
                tree = newTreeSha,
                parents = new[] { headCommitSha }
            },
            ct).ConfigureAwait(false);
        string newCommitSha = newCommitDoc.RootElement.GetProperty("sha").GetString();

        // 6. Fast-forward branch ref
        using JsonDocument _ = await SendJsonAsync(
            HttpMethod.Patch,
            $"{apiBase}/git/refs/heads/{cleanBranch}",
            pat,
            new { sha = newCommitSha, force = false },
            ct).ConfigureAwait(false);

        return (true, newCommitSha, $"Pushed {fileCount} file(s) to {ownerRepo}@{cleanBranch} ({newCommitSha[..Math.Min(8, newCommitSha.Length)]}).");
    }

    static async Task<JsonDocument> SendJsonAsync(
        HttpMethod method,
        string url,
        string pat,
        object body,
        CancellationToken ct) {
        using HttpRequestMessage req = new(method, url);
        req.Headers.Authorization = new AuthenticationHeaderValue("Bearer", pat);
        if (body != null) {
            req.Content = new StringContent(JsonSerializer.Serialize(body), System.Text.Encoding.UTF8, "application/json");
        }
        using HttpResponseMessage resp = await http.SendAsync(req, ct).ConfigureAwait(false);
        string text = await resp.Content.ReadAsStringAsync(ct).ConfigureAwait(false);
        if (!resp.IsSuccessStatusCode) {
            throw new InvalidOperationException($"GitHub API HTTP {(int)resp.StatusCode}: {(text.Length > 300 ? text[..300] : text)}");
        }
        return JsonDocument.Parse(string.IsNullOrWhiteSpace(text) ? "{}" : text);
    }
}
