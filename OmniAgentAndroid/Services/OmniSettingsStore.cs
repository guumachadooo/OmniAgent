using Android.Content;
using Android.Security.Keystore;

using Java.Security;

using Javax.Crypto;
using Javax.Crypto.Spec;

namespace OmniAgentAndroid.Services;

/// <summary>
/// Android Keystore AES-256-GCM encrypted persistence for OmniAgent Companion.
/// Defaults to Always-On (<see cref="ServiceEnabled"/> = <c>true</c>) so the user never has to manually click Start.
/// </summary>
public sealed class OmniSettingsStore {
    const string PrefsName = "omni_agent_prefs_v1";
    const string KeyAlias = "omni_agent_master_aes_v1";
    const string KsPrefix = "ks1:";

    static readonly object logLock = new();
    static readonly List<string> activityLog = [];
    static readonly object secretCacheLock = new();
    static readonly Dictionary<string, string> decryptedSecretCache = new(StringComparer.Ordinal);
    static volatile IKey cachedKeystoreKey;

    public static event Action ActivityChanged;

    readonly ISharedPreferences prefs;

    public OmniSettingsStore(Context context) {
        prefs = (context.ApplicationContext ?? context).GetSharedPreferences(PrefsName, FileCreationMode.Private);
    }

    public string RelayUrl {
        get => prefs.GetString("relay_url", "https://cavernize-diag-relay.gustavomponciano2.workers.dev") ?? "";
        set => prefs.Edit().PutString("relay_url", (value ?? "").Trim().TrimEnd('/')).Apply();
    }

    public string GitHubRepo {
        get => prefs.GetString("github_repo", "guumachadooo/Cavern") ?? "guumachadooo/Cavern";
        set => prefs.Edit().PutString("github_repo", string.IsNullOrWhiteSpace(value) ? "guumachadooo/Cavern" : value.Trim()).Apply();
    }

    public string GitHubBranch {
        get => prefs.GetString("github_branch", "master") ?? "master";
        set => prefs.Edit().PutString("github_branch", string.IsNullOrWhiteSpace(value) ? "master" : value.Trim()).Apply();
    }

    public string SecurityMode {
        get => prefs.GetString("security_mode", "smart") ?? "smart";
        set => prefs.Edit().PutString("security_mode", string.IsNullOrWhiteSpace(value) ? "smart" : value.Trim().ToLowerInvariant()).Apply();
    }

    public string TermuxStartScript {
        get => prefs.GetString("termux_start_script", "/data/data/com.termux/files/home/.omni_agent/start.sh") ?? "";
        set => prefs.Edit().PutString("termux_start_script", (value ?? "").Trim()).Apply();
    }

    /// <summary>Defaults to true so OmniAgent starts &amp; stays alive automatically with zero manual Start tap.</summary>
    public bool ServiceEnabled {
        get => prefs.GetBoolean("service_enabled", true);
        set => prefs.Edit().PutBoolean("service_enabled", value).Apply();
    }

    /// <summary>Enables Zero-Secret P-256 ECDH Smart Knock native system biometric prompt.</summary>
    public bool SmartKnockEnabled {
        get => prefs.GetBoolean("smart_knock_enabled", true);
        set => prefs.Edit().PutBoolean("smart_knock_enabled", value).Apply();
    }

    /// <summary>UI language preference: "auto" (follows device locale), "pt", or "en".</summary>
    public string UiLanguage {
        get => prefs.GetString("ui_language", "auto") ?? "auto";
        set => prefs.Edit().PutString("ui_language", string.IsNullOrWhiteSpace(value) ? "auto" : value.Trim().ToLowerInvariant()).Apply();
    }

    public string CloudflareAccountId {
        get => prefs.GetString("cf_account_id", "") ?? "";
        set => prefs.Edit().PutString("cf_account_id", (value ?? "").Trim()).Apply();
    }

    public string CloudflareWorkerName {
        get => prefs.GetString("cf_worker_name", "") ?? "";
        set => prefs.Edit().PutString("cf_worker_name", (value ?? "").Trim()).Apply();
    }

    public int LocalFsPort {
        get => prefs.GetInt("local_fs_port", 47921);
        set => prefs.Edit().PutInt("local_fs_port", Math.Clamp(value, 1024, 65535)).Apply();
    }

    public string LastExplorerPath {
        get => prefs.GetString("last_explorer_path", "/root/.gemini/antigravity/scratch/Cavern") ?? "/root/.gemini/antigravity/scratch/Cavern";
        set => prefs.Edit().PutString("last_explorer_path", string.IsNullOrWhiteSpace(value) ? "/root" : value.Trim()).Apply();
    }

    public string CachedCavernizeDesignTokensJson {
        get => prefs.GetString("cavernize_design_tokens_json", "") ?? "";
        set => prefs.Edit().PutString("cavernize_design_tokens_json", value ?? "").Apply();
    }

    public string LastDesignSyncUtc {
        get => prefs.GetString("last_design_sync_utc", "") ?? "";
        set => prefs.Edit().PutString("last_design_sync_utc", value ?? "").Apply();
    }

    public bool HasPairSecret => !string.IsNullOrEmpty(LoadPairSecretHex());
    public bool HasGitHubPat => !string.IsNullOrEmpty(LoadGitHubPat());
    public bool HasCloudflareToken => !string.IsNullOrEmpty(LoadCloudflareToken());

    public void SavePairSecretHex(string rawKey) {
        string norm = OmniCrypto.NormalizePairSecretHex(rawKey);
        if (string.IsNullOrEmpty(norm)) {
            prefs.Edit().Remove("pair_secret_enc").Apply();
            return;
        }
        prefs.Edit().PutString("pair_secret_enc", EncryptSecret(norm)).Apply();
    }

    public string LoadPairSecretHex() =>
        OmniCrypto.NormalizePairSecretHex(DecryptSecret(prefs.GetString("pair_secret_enc", "") ?? ""));

    public void SaveGitHubPat(string pat) {
        string clean = (pat ?? "").Trim();
        if (string.IsNullOrEmpty(clean)) {
            prefs.Edit().Remove("github_pat_enc").Apply();
            return;
        }
        prefs.Edit().PutString("github_pat_enc", EncryptSecret(clean)).Apply();
    }

    public string LoadGitHubPat() =>
        DecryptSecret(prefs.GetString("github_pat_enc", "") ?? "");

    public void SaveCloudflareToken(string token) {
        string clean = (token ?? "").Trim();
        if (string.IsNullOrEmpty(clean)) {
            prefs.Edit().Remove("cf_token_enc").Apply();
            return;
        }
        prefs.Edit().PutString("cf_token_enc", EncryptSecret(clean)).Apply();
    }

    public string LoadCloudflareToken() =>
        DecryptSecret(prefs.GetString("cf_token_enc", "") ?? "");

    public string ResolveEffectiveWorkerName() {
        string explicitName = CloudflareWorkerName;
        if (!string.IsNullOrWhiteSpace(explicitName)) {
            return explicitName;
        }
        if (TryNormalizeRelayUrl(RelayUrl, out string norm) && Uri.TryCreate(norm, UriKind.Absolute, out Uri uri)) {
            string host = (uri.Host ?? "").ToLowerInvariant();
            if (host.EndsWith(".workers.dev", StringComparison.Ordinal)) {
                int dot = host.IndexOf('.');
                if (dot > 0) {
                    return host[..dot];
                }
            }
        }
        return "cavernize-diag-relay";
    }

    public static bool TryNormalizeRelayUrl(string raw, out string normalized) {
        normalized = "";
        if (string.IsNullOrWhiteSpace(raw)) {
            return false;
        }
        string clean = raw.Trim().TrimEnd('/');
        if (!Uri.TryCreate(clean, UriKind.Absolute, out Uri uri) ||
            !string.Equals(uri.Scheme, Uri.UriSchemeHttps, StringComparison.OrdinalIgnoreCase) ||
            string.IsNullOrWhiteSpace(uri.Host)) {
            return false;
        }
        string host = uri.Host.ToLowerInvariant();
        if (host == "localhost" || host == "127.0.0.1" || host == "::1" || host.EndsWith(".local", StringComparison.Ordinal)) {
            return false;
        }
        normalized = clean;
        return true;
    }

    public static void AppendActivity(string tag, string message) {
        string line = $"[{DateTime.Now:HH:mm:ss}] [{tag}] {message}";
        lock (logLock) {
            activityLog.Insert(0, line);
            if (activityLog.Count > 140) {
                activityLog.RemoveRange(140, activityLog.Count - 140);
            }
        }
        try {
            ActivityChanged?.Invoke();
        } catch {
            // Ignore listener errors
        }
    }

    public static List<string> GetActivitySnapshot() {
        lock (logLock) {
            return [.. activityLog];
        }
    }

    public static void ClearActivity() {
        lock (logLock) {
            activityLog.Clear();
        }
        try {
            ActivityChanged?.Invoke();
        } catch {
            // Ignore
        }
    }

    static IKey GetOrCreateKeystoreKey() {
        IKey fast = cachedKeystoreKey;
        if (fast != null) {
            return fast;
        }
        lock (secretCacheLock) {
            if (cachedKeystoreKey != null) {
                return cachedKeystoreKey;
            }
            KeyStore ks = KeyStore.GetInstance("AndroidKeyStore");
            ks.Load(null);
            if (ks.ContainsAlias(KeyAlias)) {
                IKey existing = ks.GetKey(KeyAlias, null);
                if (existing != null) {
                    cachedKeystoreKey = existing;
                    return existing;
                }
            }
            KeyGenerator kg = KeyGenerator.GetInstance(KeyProperties.KeyAlgorithmAes, "AndroidKeyStore");
            KeyGenParameterSpec spec = new KeyGenParameterSpec.Builder(
                    KeyAlias,
                    KeyStorePurpose.Encrypt | KeyStorePurpose.Decrypt)
                .SetBlockModes(KeyProperties.BlockModeGcm)
                .SetEncryptionPaddings(KeyProperties.EncryptionPaddingNone)
                .SetKeySize(256)
                .Build();
            kg.Init(spec);
            cachedKeystoreKey = kg.GenerateKey();
            return cachedKeystoreKey;
        }
    }

    static string EncryptSecret(string plainText) {
        IKey key = GetOrCreateKeystoreKey();
        Cipher cipher = Cipher.GetInstance("AES/GCM/NoPadding");
        cipher.Init(CipherMode.EncryptMode, key);
        byte[] iv = cipher.GetIV();
        byte[] enc = cipher.DoFinal(System.Text.Encoding.UTF8.GetBytes(plainText));
        string ciphertext = $"{KsPrefix}{Convert.ToBase64String(iv)}:{Convert.ToBase64String(enc)}";
        lock (secretCacheLock) {
            if (decryptedSecretCache.Count > 16) {
                decryptedSecretCache.Clear();
            }
            decryptedSecretCache[ciphertext] = plainText;
        }
        return ciphertext;
    }

    static string DecryptSecret(string stored) {
        if (string.IsNullOrEmpty(stored) || !stored.StartsWith(KsPrefix, StringComparison.Ordinal)) {
            return "";
        }
        lock (secretCacheLock) {
            if (decryptedSecretCache.TryGetValue(stored, out string cached)) {
                return cached;
            }
        }
        try {
            string rest = stored[KsPrefix.Length..];
            int sep = rest.IndexOf(':');
            if (sep <= 0 || sep >= rest.Length - 1) {
                return "";
            }
            byte[] iv = Convert.FromBase64String(rest[..sep]);
            byte[] cipherBytes = Convert.FromBase64String(rest[(sep + 1)..]);
            IKey key = GetOrCreateKeystoreKey();
            Cipher cipher = Cipher.GetInstance("AES/GCM/NoPadding");
            cipher.Init(CipherMode.DecryptMode, key, new GCMParameterSpec(128, iv));
            byte[] plain = cipher.DoFinal(cipherBytes);
            string plainText = System.Text.Encoding.UTF8.GetString(plain);
            lock (secretCacheLock) {
                if (decryptedSecretCache.Count > 16) {
                    decryptedSecretCache.Clear();
                }
                decryptedSecretCache[stored] = plainText;
            }
            return plainText;
        } catch {
            return "";
        }
    }
}
