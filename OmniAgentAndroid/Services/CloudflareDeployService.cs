using System.Net.Http.Headers;
using System.Text.Json;

namespace OmniAgentAndroid.Services;

/// <summary>
/// Direct Cloudflare Workers REST API deployer running locally on Android using a user-provided
/// Cloudflare API Token encrypted in Android Keystore (AES-256-GCM).
/// Preserves <c>RELAY_KV</c> and existing Worker secrets via <c>keep_bindings</c> while automatically
/// enabling the Free-Tier SQLite Durable Object <c>HUB_DO</c> (<c>HubCoordinator</c>).
/// </summary>
public static class CloudflareDeployService {
    const string CfApiBase = "https://api.cloudflare.com/client/v4";
    const string DoBindingName = "HUB_DO";
    const string DoClassName = "HubCoordinator";
    const string DoMigrationTag = "v6-hub-sqlite";
    static readonly string[] PreservedBindings = ["kv_namespace", "secret_text", "plain_text", "r2_bucket", "d1"];

    static readonly HttpClient http = BuildClient();

    static HttpClient BuildClient() {
        HttpClient c = new() { Timeout = TimeSpan.FromSeconds(45) };
        c.DefaultRequestHeaders.UserAgent.Add(new ProductInfoHeaderValue("OmniAgentCFDeploy", "2.0"));
        c.DefaultRequestHeaders.Accept.Add(new MediaTypeWithQualityHeaderValue("application/json"));
        return c;
    }

    /// <summary>
    /// Verifies a Cloudflare API Token and resolves the first accessible Account ID + Name.
    /// </summary>
    public static async Task<(bool ok, string accountId, string accountName, string message)> TestTokenAsync(
        string apiToken,
        CancellationToken ct = default) {
        string clean = (apiToken ?? "").Trim();
        if (string.IsNullOrEmpty(clean)) {
            return (false, "", "", "Cloudflare API Token is empty.");
        }
        try {
            using HttpRequestMessage req = new(HttpMethod.Get, $"{CfApiBase}/accounts?per_page=10");
            req.Headers.Authorization = new AuthenticationHeaderValue("Bearer", clean);
            using HttpResponseMessage resp = await http.SendAsync(req, ct).ConfigureAwait(false);
            string body = await resp.Content.ReadAsStringAsync(ct).ConfigureAwait(false);
            using JsonDocument doc = JsonDocument.Parse(body);
            JsonElement root = doc.RootElement;
            bool ok = root.TryGetProperty("success", out JsonElement sEl) && sEl.ValueKind == JsonValueKind.True;
            if (!ok || !root.TryGetProperty("result", out JsonElement resEl) || resEl.ValueKind != JsonValueKind.Array || resEl.GetArrayLength() == 0) {
                return (false, "", "", $"Cloudflare API rejected token (HTTP {(int)resp.StatusCode}): {ExtractErrorSummary(root)}");
            }
            JsonElement first = resEl[0];
            string accId = first.TryGetProperty("id", out JsonElement idEl) ? (idEl.GetString() ?? "") : "";
            string accName = first.TryGetProperty("name", out JsonElement nEl) ? (nEl.GetString() ?? "") : "";
            return !string.IsNullOrEmpty(accId)
                ? (true, accId, accName, $"Token valid · Account: {accName} ({accId[..Math.Min(8, accId.Length)]}...)")
                : (false, "", "", "Account ID missing in Cloudflare response.");
        } catch (Exception ex) {
            return (false, "", "", $"Cloudflare connection error: {ex.Message}");
        }
    }

    /// <summary>
    /// Uploads the Cloudflare Module Worker script directly via Cloudflare REST API.
    /// Automatically handles first-time SQLite Durable Object migration, subsequent updates,
    /// and graceful KV-only fallback while preserving <c>RELAY_KV</c> and Worker secrets.
    /// </summary>
    public static async Task<(bool ok, string mode, string accountId, string detail)> DeployWorkerAsync(
        string apiToken,
        string accountId,
        string workerName,
        string scriptSource,
        bool enableDurableObject = true,
        string mainModule = "worker.js",
        CancellationToken ct = default) {
        string cleanToken = (apiToken ?? "").Trim();
        if (string.IsNullOrEmpty(cleanToken)) {
            return (false, "", "", "Cloudflare API Token not configured in OmniAgent.");
        }
        string cleanWorker = (workerName ?? "").Trim();
        if (string.IsNullOrEmpty(cleanWorker)) {
            return (false, "", "", "Worker name is required (e.g. cavernize-diag-relay).");
        }
        if (string.IsNullOrWhiteSpace(scriptSource) || scriptSource.Length < 256) {
            return (false, "", "", "Worker script source is empty or too short.");
        }

        string resolvedAccId = (accountId ?? "").Trim();
        if (string.IsNullOrEmpty(resolvedAccId)) {
            (bool tokOk, string foundId, _, string tokMsg) = await TestTokenAsync(cleanToken, ct).ConfigureAwait(false);
            if (!tokOk) {
                return (false, "", "", tokMsg);
            }
            resolvedAccId = foundId;
        }

        byte[] sourceBytes = System.Text.Encoding.UTF8.GetBytes(scriptSource);
        string url = $"{CfApiBase}/accounts/{Uri.EscapeDataString(resolvedAccId)}/workers/scripts/{Uri.EscapeDataString(cleanWorker)}";
        string entryMod = string.IsNullOrWhiteSpace(mainModule) ? "worker.js" : mainModule.Trim();

        if (enableDurableObject) {
            // Attempt 1: Attach HUB_DO + run SQLite Durable Object migration (first-time v6 upgrade)
            (bool ok1, string err1) = await UploadOnceAsync(cleanToken, url, sourceBytes, entryMod, includeDo: true, includeMigration: true, ct).ConfigureAwait(false);
            if (ok1) {
                return (true, "durable_object_sqlite_migrated", resolvedAccId, $"Deployed '{cleanWorker}' with SQLite Durable Object HUB_DO (RELAY_KV preserved).");
            }

            string lowerErr = err1.ToLowerInvariant();
            // Attempt 2: Class/tag already migrated -> update script preserving HUB_DO binding without re-running migration
            if (lowerErr.Contains("already") || lowerErr.Contains("migration") || lowerErr.Contains("tag") ||
                lowerErr.Contains("exists") || lowerErr.Contains("10074") || lowerErr.Contains("10075")) {
                (bool ok2, string err2) = await UploadOnceAsync(cleanToken, url, sourceBytes, entryMod, includeDo: true, includeMigration: false, ct).ConfigureAwait(false);
                if (ok2) {
                    return (true, "durable_object_sqlite_updated", resolvedAccId, $"Updated '{cleanWorker}' with SQLite Durable Object HUB_DO (RELAY_KV preserved).");
                }
                err1 = err2;
            }

            // Attempt 3: Graceful KV-only fallback so the Worker still upgrades to v6 even if DO migration is blocked
            (bool ok3, string err3) = await UploadOnceAsync(cleanToken, url, sourceBytes, entryMod, includeDo: false, includeMigration: false, ct).ConfigureAwait(false);
            if (ok3) {
                return (true, "kv_fallback", resolvedAccId, $"Deployed '{cleanWorker}' in KV fallback mode (DO notice: {err1}).");
            }
            return (false, "", resolvedAccId, $"Cloudflare deploy failed: {err1} | fallback: {err3}");
        }

        (bool okKv, string errKv) = await UploadOnceAsync(cleanToken, url, sourceBytes, entryMod, includeDo: false, includeMigration: false, ct).ConfigureAwait(false);
        return okKv
            ? (true, "kv_only", resolvedAccId, $"Deployed '{cleanWorker}' (KV mode, RELAY_KV preserved).")
            : (false, "", resolvedAccId, $"Cloudflare deploy failed: {errKv}");
    }

    static async Task<(bool ok, string err)> UploadOnceAsync(
        string token,
        string url,
        byte[] scriptBytes,
        string mainModule,
        bool includeDo,
        bool includeMigration,
        CancellationToken ct) {
        Dictionary<string, object> meta = new() {
            ["main_module"] = mainModule,
            ["compatibility_date"] = "2026-04-01",
            ["keep_bindings"] = PreservedBindings
        };
        if (includeDo) {
            meta["bindings"] = new object[] {
                new {
                    name = DoBindingName,
                    type = "durable_object_namespace",
                    class_name = DoClassName
                }
            };
            if (includeMigration) {
                meta["migrations"] = new {
                    new_tag = DoMigrationTag,
                    new_sqlite_classes = new[] { DoClassName }
                };
            }
        }

        string metaJson = JsonSerializer.Serialize(meta);
        using MultipartFormDataContent form = new($"----OmniCF{Guid.NewGuid():N}");

        StringContent metaPart = new(metaJson, System.Text.Encoding.UTF8, "application/json");
        form.Add(metaPart, "metadata");

        ByteArrayContent scriptPart = new(scriptBytes);
        scriptPart.Headers.ContentType = new MediaTypeHeaderValue("application/javascript+module");
        form.Add(scriptPart, mainModule, mainModule);

        using HttpRequestMessage req = new(HttpMethod.Put, url) { Content = form };
        req.Headers.Authorization = new AuthenticationHeaderValue("Bearer", token);
        using HttpResponseMessage resp = await http.SendAsync(req, ct).ConfigureAwait(false);
        string body = await resp.Content.ReadAsStringAsync(ct).ConfigureAwait(false);
        try {
            using JsonDocument doc = JsonDocument.Parse(body);
            JsonElement root = doc.RootElement;
            bool ok = resp.IsSuccessStatusCode && root.TryGetProperty("success", out JsonElement sEl) && sEl.ValueKind == JsonValueKind.True;
            return ok ? (true, "") : (false, $"HTTP {(int)resp.StatusCode}: {ExtractErrorSummary(root)}");
        } catch {
            return resp.IsSuccessStatusCode ? (true, "") : (false, $"HTTP {(int)resp.StatusCode}: {body[..Math.Min(240, body.Length)]}");
        }
    }

    static string ExtractErrorSummary(JsonElement root) {
        if (root.TryGetProperty("errors", out JsonElement errs) && errs.ValueKind == JsonValueKind.Array && errs.GetArrayLength() > 0) {
            List<string> parts = [];
            for (int i = 0; i < Math.Min(3, errs.GetArrayLength()); i++) {
                JsonElement e = errs[i];
                int code = e.TryGetProperty("code", out JsonElement cEl) && cEl.TryGetInt32(out int cv) ? cv : 0;
                string msg = e.TryGetProperty("message", out JsonElement mEl) ? (mEl.GetString() ?? "") : e.ToString();
                parts.Add(code > 0 ? $"[{code}] {msg}" : msg);
            }
            return string.Join("; ", parts);
        }
        return "Unknown Cloudflare API error";
    }
}
