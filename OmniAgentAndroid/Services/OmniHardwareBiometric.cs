using Android.Content;
using Android.Hardware.Biometrics;
using Android.Security.Keystore;

using Java.Security;
using Java.Security.Spec;

namespace OmniAgentAndroid.Services;

/// <summary>
/// Passkey-grade hardware-backed biometric signing engine backed by Android Keystore
/// (Samsung Knox Vault StrongBox on S25 Ultra, with automatic TEE fallback).
///
/// The ECDSA P-256 private key never leaves secure hardware and CANNOT sign a challenge
/// unless Android's biometric sensor has just verified the user inside the same
/// <see cref="BiometricPrompt.CryptoObject"/> transaction — achieving FIDO2/Passkey-level
/// cryptographic assurance inside a single instant native One UI system prompt with zero
/// external account-picker friction or assetlinks.json dependency.
/// </summary>
public static class OmniHardwareBiometric {
    const string KeyAlias = "omni_agent_hw_bio_ecdsa_v1";

    public enum HardwareBacking {
        StrongBox,
        Tee,
        DeviceCredentialFallback
    }

    static long cachedProbeMs;
    static HardwareBacking cachedBacking = HardwareBacking.DeviceCredentialFallback;

    public static HardwareBacking ProbeCapability(Context context, bool forceRefresh = false) {
        if (!OperatingSystem.IsAndroidVersionAtLeast(29) || context == null) {
            return HardwareBacking.DeviceCredentialFallback;
        }
        long nowMs = Android.OS.SystemClock.ElapsedRealtime();
        if (!forceRefresh && cachedProbeMs > 0 && nowMs - cachedProbeMs < 15_000L) {
            return cachedBacking;
        }
        try {
            BiometricManager bm = context.GetSystemService(Java.Lang.Class.FromType(typeof(BiometricManager))) as BiometricManager;
            if (bm == null) {
                return HardwareBacking.DeviceCredentialFallback;
            }
            int can = OperatingSystem.IsAndroidVersionAtLeast(30)
                ? (int)bm.CanAuthenticate(0x000F) // BIOMETRIC_STRONG
#pragma warning disable CS0618, CA1422
                : (int)bm.CanAuthenticate();
#pragma warning restore CS0618, CA1422
            HardwareBacking result;
            if (can != 0) { // BIOMETRIC_SUCCESS == 0
                result = HardwareBacking.DeviceCredentialFallback;
            } else {
                bool hasStrongBox = context.PackageManager?.HasSystemFeature("android.hardware.strongbox_keystore") ?? false;
                result = hasStrongBox ? HardwareBacking.StrongBox : HardwareBacking.Tee;
            }
            cachedBacking = result;
            cachedProbeMs = nowMs;
            return result;
        } catch {
            return HardwareBacking.DeviceCredentialFallback;
        }
    }

    [System.Runtime.Versioning.SupportedOSPlatform("android28.0")]
    public static (BiometricPrompt.CryptoObject cryptoObj, bool isStrongBox) TryCreateCryptoObject() {
        try {
            (IPrivateKey privKey, bool strongBox) = GetOrCreateHardwareSigningKey(recreate: false);
            if (privKey == null) {
                return (null, false);
            }
            Signature sig = Signature.GetInstance("SHA256withECDSA");
            try {
                sig.InitSign(privKey);
            } catch (KeyPermanentlyInvalidatedException) {
                // User enrolled/removed a fingerprint -> cleanly rotate hardware key
                (privKey, strongBox) = GetOrCreateHardwareSigningKey(recreate: true);
                if (privKey == null) {
                    return (null, false);
                }
                sig = Signature.GetInstance("SHA256withECDSA");
                sig.InitSign(privKey);
            }
            return (new BiometricPrompt.CryptoObject(sig), strongBox);
        } catch {
            return (null, false);
        }
    }

    [System.Runtime.Versioning.SupportedOSPlatform("android28.0")]
    public static string TrySignChallenge(BiometricPrompt.AuthenticationResult result, string challengeText) {
        try {
            Signature sig = result?.CryptoObject?.Signature;
            if (sig == null || string.IsNullOrEmpty(challengeText)) {
                return "";
            }
            sig.Update(System.Text.Encoding.UTF8.GetBytes(challengeText));
            byte[] rawSig = sig.Sign();
            return rawSig is { Length: > 0 } ? Convert.ToBase64String(rawSig) : "";
        } catch {
            return "";
        }
    }

    [System.Runtime.Versioning.SupportedOSPlatform("android28.0")]
    static (IPrivateKey key, bool strongBox) GetOrCreateHardwareSigningKey(bool recreate) {
        KeyStore ks = KeyStore.GetInstance("AndroidKeyStore");
        ks.Load(null);
        if (recreate && ks.ContainsAlias(KeyAlias)) {
            ks.DeleteEntry(KeyAlias);
        }
        if (!recreate && ks.ContainsAlias(KeyAlias)) {
            if (ks.GetKey(KeyAlias, null) is IPrivateKey existing) {
                return (existing, true);
            }
        }
        // Try StrongBox first (Samsung Knox Vault), fall back to TEE cleanly
        try {
            IPrivateKey sb = GenerateKey(useStrongBox: true);
            return (sb, true);
        } catch {
            try {
                IPrivateKey tee = GenerateKey(useStrongBox: false);
                return (tee, false);
            } catch {
                return (null, false);
            }
        }
    }

    [System.Runtime.Versioning.SupportedOSPlatform("android28.0")]
    static IPrivateKey GenerateKey(bool useStrongBox) {
        KeyPairGenerator kpg = KeyPairGenerator.GetInstance(KeyProperties.KeyAlgorithmEc, "AndroidKeyStore");
        KeyGenParameterSpec.Builder b = new KeyGenParameterSpec.Builder(KeyAlias, KeyStorePurpose.Sign)
            .SetAlgorithmParameterSpec(new ECGenParameterSpec("secp256r1"))
            .SetDigests(KeyProperties.DigestSha256)
            .SetUserAuthenticationRequired(true)
            .SetInvalidatedByBiometricEnrollment(true);

        // -1 requires fresh biometric confirmation per CryptoObject signature across API 28-36
#pragma warning disable CS0618, CA1422
        b.SetUserAuthenticationValidityDurationSeconds(-1);
#pragma warning restore CS0618, CA1422
        if (useStrongBox) {
            b.SetIsStrongBoxBacked(true);
        }
        kpg.Initialize(b.Build());
        KeyPair kp = kpg.GenerateKeyPair();
        return kp?.Private;
    }
}
