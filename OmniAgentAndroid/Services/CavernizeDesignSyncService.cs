using Android.Content;

using CavernizeAndroid.Theme;

namespace OmniAgentAndroid.Services;

/// <summary>
/// Zero-quota runtime design synchronization client: queries Cavernize's active One UI 9
/// design tokens over &lt;10 ms same-signature local IPC and applies them live via
/// <see cref="OneUiTheme.ApplyRuntimeDesignTokensJson(string, string)"/>.
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
                OneUiTheme.ApplyRuntimeDesignTokensJson(cached, "cavernize-cache");
            }
        } catch {
            // Fall back cleanly to synced compile-time OneUiTheme defaults
        }
    }

    public static async Task<(bool ok, string detail)> SyncFromCavernizeAsync(Context context, CancellationToken ct = default) {
        if (context == null) {
            return (false, "No context");
        }
        if (!CavernizeIpcBridgeClient.IsSignatureVerified(context)) {
            return (false, "Cavernize not installed / signature mismatch");
        }
        try {
            (bool ok, string body, long elapsedMs) = await CavernizeIpcBridgeClient.SendCommandAsync(
                context, "get_design_tokens", "{}", maxTier: 0, timeoutSeconds: 8, ct: ct).ConfigureAwait(false);
            if (!ok || string.IsNullOrWhiteSpace(body)) {
                return (false, string.IsNullOrWhiteSpace(body) ? "No design tokens returned" : body);
            }
            if (OneUiTheme.ApplyRuntimeDesignTokensJson(body, "cavernize-ipc")) {
                OmniSettingsStore store = new(context);
                store.CachedCavernizeDesignTokensJson = body;
                store.LastDesignSyncUtc = DateTime.UtcNow.ToString("O");
                OmniSettingsStore.AppendActivity("DESIGN", $"Synced live One UI 9 tokens from Cavernize ({elapsedMs} ms).");
                return (true, $"Synced ({elapsedMs} ms)");
            }
            return (false, "Invalid token payload");
        } catch (Exception ex) {
            return (false, ex.Message);
        }
    }
}
