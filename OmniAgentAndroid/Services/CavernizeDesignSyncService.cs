using Android.Content;

using CavernizeAndroid.Theme;

namespace OmniAgentAndroid.Services;

/// <summary>
/// Zero-quota runtime design synchronization client: queries Cavernize's active One UI 9
/// design tokens over &lt;10 ms same-signature local IPC and applies them live via
/// <see cref="OneUiTheme.ApplyRuntimeDesignTokensJson(string, string, out bool)"/>.
/// </summary>
public static class CavernizeDesignSyncService {
    public static void ApplyCachedTokensIfPresent(Context context) {
        if (context == null) {
            return;
        }
        try {
            OmniSettingsStore store = new(context);
            string cached = store.CachedCavernizeDesignTokensJson;
            if (!string.IsNullOrWhiteSpace(cached)) {
                OneUiTheme.ApplyRuntimeDesignTokensJson(cached, "cavernize-cache", out _);
            }
        } catch {
            // Fall back cleanly to synced compile-time OneUiTheme defaults
        }
    }

    public static async Task<(bool ok, bool visualChanged, string detail)> SyncFromCavernizeAsync(
        Context context,
        bool logIfUnchanged = false,
        CancellationToken ct = default) {
        if (context == null) {
            return (false, false, "No context");
        }
        if (!CavernizeIpcBridgeClient.IsSignatureVerified(context)) {
            return (false, false, "Cavernize not installed / signature mismatch");
        }
        try {
            (bool ok, string body, long elapsedMs) = await CavernizeIpcBridgeClient.SendCommandAsync(
                context, "get_design_tokens", "{}", maxTier: 0, timeoutSeconds: 8, ct: ct).ConfigureAwait(false);
            if (!ok || string.IsNullOrWhiteSpace(body)) {
                return (false, false, string.IsNullOrWhiteSpace(body) ? "No design tokens returned" : body);
            }
            if (OneUiTheme.ApplyRuntimeDesignTokensJson(body, "cavernize-ipc", out bool visualChanged)) {
                OmniSettingsStore store = new(context);
                if (visualChanged || string.IsNullOrWhiteSpace(store.CachedCavernizeDesignTokensJson)) {
                    store.CachedCavernizeDesignTokensJson = body;
                }
                store.LastDesignSyncUtc = DateTime.UtcNow.ToString("O");
                if (visualChanged || logIfUnchanged) {
                    string tag = visualChanged ? "updated" : "up to date";
                    OmniSettingsStore.AppendActivity("DESIGN", $"Cavernize One UI 9 tokens {tag} ({elapsedMs} ms).");
                }
                string detail = visualChanged ? $"Updated ({elapsedMs} ms)" : $"Up to date ({elapsedMs} ms)";
                return (true, visualChanged, detail);
            }
            return (false, false, "Invalid token payload");
        } catch (Exception ex) {
            return (false, false, ex.Message);
        }
    }
}
