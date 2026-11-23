using Android.Content;
using Android.Graphics;
using Android.OS;
using Android.Provider;
using Android.Views;
using Android.Widget;

using CavernizeAndroid.Theme;
using CavernizeAndroid.Views;

using OmniAgentAndroid.Language;
using OmniAgentAndroid.Services;
using OmniAgentAndroid.SharedOneUi;

namespace OmniAgentAndroid;

public sealed partial class MainActivity {
    OneUiInlineSelector approvalSelector;
    TextView approvalModeDescTv;
    TextView hwBioChipTv;
    TextView overlayChipTv;
    TextView batteryChipTv;
    TextView designSyncInfoTv;
    OneUiSwitch alwaysOnSwitch;
    OneUiSwitch smartKnockSwitch;
    bool suppressSettingsEvents;

    void BuildSettingsTab(LinearLayout parent) {
        parent.AddView(MakeTabHeader(
            S["SettingsTitle"],
            $"OmniAgent v2.1 · Kit {OneUiDesignKitManifest.KitFingerprint}",
            OneUiIconType.Gear,
            OneUiTheme.IconSystemInfo));

        // 1. Approval Policy & Hardware Biometrics Card (with '?' guided help explaining Smart/Biometric/Trust & Passkeys!)
        LinearLayout secCard = MakeGlassCard();
        secCard.AddView(MakeCardHeader(
            S["SettingsApprovalTitle"],
            OneUiIconType.Lock,
            OneUiTheme.IconAtmosUpmix,
            onHelp: () => OneUiModalDialog.Show(
                this,
                S["SettingsApprovalHelpTitle"],
                S["SettingsApprovalHelpBody"],
                icon: OneUiIconType.Lock,
                iconBadgeColor: OneUiTheme.IconAtmosUpmix,
                positiveText: S["OkBtn"])));

        approvalSelector = new OneUiInlineSelector(this);
        approvalSelector.SetItems([
            S["SettingsModeSmart"],
            S["SettingsModeBiometric"],
            S["SettingsModeTrusted"],
        ], initialIndex: ModeToIndex(store.SecurityMode), notify: false);
        approvalSelector.SelectionChanged += idx => {
            if (suppressSettingsEvents) {
                return;
            }
            store.SecurityMode = IndexToMode(idx);
            OmniSettingsStore.AppendActivity("SEC", $"Approval policy -> {store.SecurityMode}");
            RefreshSettingsTabState();
        };
        secCard.AddView(approvalSelector);

        approvalModeDescTv = new TextView(this) {
            Text = "",
            TextSize = 12.5f,
            Typeface = FontRegular,
        };
        approvalModeDescTv.SetTextColor(OneUiTheme.TextSub);
        approvalModeDescTv.SetPadding(Dp(2), Dp(8), Dp(2), Dp(10));
        secCard.AddView(approvalModeDescTv);

        hwBioChipTv = OneUiTheme.CreateStatusChip(this, S["SettingsHwBioFallback"], OneUiTheme.FunctionalGreen);
        secCard.AddView(hwBioChipTv, new LinearLayout.LayoutParams(
            ViewGroup.LayoutParams.WrapContent, ViewGroup.LayoutParams.WrapContent) { BottomMargin = Dp(8) });

        secCard.AddView(OneUiTheme.CreateDivider(this));
        (LinearLayout knockRow, OneUiSwitch knockSw) = MakeSwitchRow(
            S["SettingsSmartKnockSwitch"],
            null,
            store.SmartKnockEnabled,
            isChecked => {
                if (suppressSettingsEvents) {
                    return;
                }
                store.SmartKnockEnabled = isChecked;
                OmniSettingsStore.AppendActivity("SEC", $"Smart Knock overlay -> {(isChecked ? "ON" : "OFF")}");
            });
        smartKnockSwitch = knockSw;
        secCard.AddView(knockRow);
        parent.AddView(secCard);

        // 2. Always-On & System Overlay Resilience Card
        LinearLayout resCard = MakeGlassCard();
        resCard.AddView(MakeCardHeader(
            S["SettingsResilienceTitle"],
            OneUiIconType.Bolt,
            OneUiTheme.FunctionalGreen));

        TextView resDesc = new(this) {
            Text = S["SettingsResilienceDesc"],
            TextSize = 12.5f,
            Typeface = FontRegular,
        };
        resDesc.SetTextColor(OneUiTheme.TextSub);
        resDesc.SetPadding(0, 0, 0, Dp(8));
        resCard.AddView(resDesc);

        (LinearLayout aoRow, OneUiSwitch aoSw) = MakeSwitchRow(
            S["SettingsAlwaysOnSwitch"],
            null,
            store.ServiceEnabled,
            isChecked => {
                if (suppressSettingsEvents) {
                    return;
                }
                store.ServiceEnabled = isChecked;
                if (isChecked) {
                    OmniAgentForegroundService.EnsureAlwaysOn(this);
                } else {
                    OmniAgentForegroundService.Stop(this);
                }
                RefreshBridgeTabState();
            });
        alwaysOnSwitch = aoSw;
        resCard.AddView(aoRow);

        LinearLayout chipRow = MakeHRow();
        chipRow.SetPadding(0, Dp(8), 0, Dp(10));
        overlayChipTv = OneUiTheme.CreateStatusChip(this, S["SettingsOverlayNeed"], OneUiTheme.FunctionalAmber);
        batteryChipTv = OneUiTheme.CreateStatusChip(this, S["SettingsBatteryNeed"], OneUiTheme.FunctionalAmber);
        chipRow.AddView(overlayChipTv, new LinearLayout.LayoutParams(
            ViewGroup.LayoutParams.WrapContent, ViewGroup.LayoutParams.WrapContent) { RightMargin = Dp(8) });
        chipRow.AddView(batteryChipTv);
        resCard.AddView(chipRow);

        LinearLayout permRow = MakeHRow();
        Button overlayBtn = MakeActionPill("Overlay", primary: false);
        overlayBtn.Click += (_, _) => OpenOverlayPermissionSettings();
        Button batteryBtn = MakeActionPill("Battery", primary: false);
        batteryBtn.Click += (_, _) => OpenBatteryOptimizationSettings();
        permRow.AddView(overlayBtn, new LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WrapContent, 1f) { RightMargin = Dp(8) });
        permRow.AddView(batteryBtn, new LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WrapContent, 1f));
        resCard.AddView(permRow);
        parent.AddView(resCard);

        // 3. Connections & Credentials Card (Cloudflare Relay, Cloudflare Auto-Deploy, GitHub Push, Termux)
        LinearLayout connCard = MakeGlassCard();
        connCard.AddView(MakeCardHeader(
            S["SettingsConnectionsTitle"],
            OneUiIconType.Globe,
            OneUiTheme.IconSpatial));

        LinearLayout cRow1 = MakeHRow();
        Button relayBtn = MakeActionPill(S["SettingsRelayBtn"], primary: true);
        relayBtn.Click += (_, _) => ShowRelayConfigModal();
        Button cfBtn = MakeActionPill(S["SettingsCfDeployBtn"], primary: false);
        cfBtn.Click += (_, _) => ShowCloudflareConfigModal();
        cRow1.AddView(relayBtn, new LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WrapContent, 1f) { RightMargin = Dp(8) });
        cRow1.AddView(cfBtn, new LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WrapContent, 1f));
        connCard.AddView(cRow1, new LinearLayout.LayoutParams(
            ViewGroup.LayoutParams.MatchParent, ViewGroup.LayoutParams.WrapContent) { BottomMargin = Dp(8) });

        LinearLayout cRow2 = MakeHRow();
        Button ghBtn = MakeActionPill(S["SettingsGithubBtn"], primary: false);
        ghBtn.Click += (_, _) => ShowGitHubConfigModal();
        Button txBtn = MakeActionPill(S["SettingsTermuxBtn"], primary: false);
        txBtn.Click += (_, _) => ShowTermuxConfigModal();
        cRow2.AddView(ghBtn, new LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WrapContent, 1f) { RightMargin = Dp(8) });
        cRow2.AddView(txBtn, new LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WrapContent, 1f));
        connCard.AddView(cRow2);
        parent.AddView(connCard);

        // 4. Zero-Quota Cavernize Design Inheritance Card (with '?' guided help)
        LinearLayout designCard = MakeGlassCard();
        designCard.AddView(MakeCardHeader(
            S["SettingsDesignSyncTitle"],
            OneUiIconType.Sparkles,
            OneUiTheme.IconStemsCalib,
            onHelp: () => OneUiModalDialog.Show(
                this,
                S["SettingsDesignSyncHelpTitle"],
                S["SettingsDesignSyncHelpBody"],
                icon: OneUiIconType.Sparkles,
                iconBadgeColor: OneUiTheme.IconStemsCalib,
                positiveText: S["OkBtn"])));

        designSyncInfoTv = new TextView(this) {
            Text = "",
            TextSize = 12.5f,
            Typeface = FontRegular,
        };
        designSyncInfoTv.SetTextColor(OneUiTheme.TextSub);
        designSyncInfoTv.SetPadding(0, 0, 0, Dp(10));
        designCard.AddView(designSyncInfoTv);

        Button syncDesignBtn = MakeActionPill(S["SettingsDesignSyncNowBtn"], primary: false);
        syncDesignBtn.Click += (_, _) => _ = RunManualDesignSyncAsync(syncDesignBtn);
        designCard.AddView(syncDesignBtn, new LinearLayout.LayoutParams(
            ViewGroup.LayoutParams.MatchParent, ViewGroup.LayoutParams.WrapContent));
        parent.AddView(designCard);

        // 5. Language Selector Card
        LinearLayout langCard = MakeGlassCard();
        langCard.AddView(MakeCardHeader(
            S["SettingsLanguageLabel"],
            OneUiIconType.Info,
            OneUiTheme.IconSystemInfo));

        OneUiInlineSelector langSelector = new(this);
        langSelector.SetItems([
            S["SettingsLangAuto"],
            S["SettingsLangPt"],
            S["SettingsLangEn"],
        ], initialIndex: LangToIndex(store.UiLanguage), notify: false);
        langSelector.SelectionChanged += idx => {
            string chosen = idx switch { 1 => "pt", 2 => "en", _ => "auto" };
            if (!string.Equals(store.UiLanguage, chosen, StringComparison.OrdinalIgnoreCase)) {
                store.UiLanguage = chosen;
                OmniStrings.Refresh(chosen);
                Recreate();
            }
        };
        langCard.AddView(langSelector);
        parent.AddView(langCard);
    }

    void RefreshSettingsTabState() {
        if (approvalModeDescTv == null || IsFinishing || IsDestroyed) {
            return;
        }
        suppressSettingsEvents = true;
        try {
            string mode = store.SecurityMode;
            int targetModeIdx = ModeToIndex(mode);
            if (approvalSelector != null && approvalSelector.SelectedIndex != targetModeIdx) {
                approvalSelector.SetSelection(targetModeIdx, notify: false);
            }
            approvalModeDescTv.Text = mode switch {
                "biometric" => S["SettingsModeBioDesc"],
                "trusted" => S["SettingsModeTrustedDesc"],
                _ => S["SettingsModeSmartDesc"],
            };

            OmniHardwareBiometric.HardwareBacking hw = OmniHardwareBiometric.ProbeCapability(this);
            (string hwLabel, Color hwColor) = hw switch {
                OmniHardwareBiometric.HardwareBacking.StrongBox => (S["SettingsHwBioStrongBox"], OneUiTheme.FunctionalGreen),
                OmniHardwareBiometric.HardwareBacking.Tee => (S["SettingsHwBioTee"], OneUiTheme.FunctionalCyan),
                _ => (S["SettingsHwBioFallback"], OneUiTheme.FunctionalAmber),
            };
            OneUiTheme.UpdateStatusChip(this, hwBioChipTv, hwLabel, hwColor);

            if (alwaysOnSwitch != null) {
                alwaysOnSwitch.Checked = store.ServiceEnabled;
            }
            if (smartKnockSwitch != null) {
                smartKnockSwitch.Checked = store.SmartKnockEnabled;
            }

            bool overlayOk = !OperatingSystem.IsAndroidVersionAtLeast(23) || Settings.CanDrawOverlays(this);
            OneUiTheme.UpdateStatusChip(
                this,
                overlayChipTv,
                overlayOk ? S["SettingsOverlayOk"] : S["SettingsOverlayNeed"],
                overlayOk ? OneUiTheme.FunctionalGreen : OneUiTheme.FunctionalAmber);

            bool batOk = OmniAgentForegroundService.IsBatteryUnrestricted(this);
            OneUiTheme.UpdateStatusChip(
                this,
                batteryChipTv,
                batOk ? S["SettingsBatteryOk"] : S["SettingsBatteryNeed"],
                batOk ? OneUiTheme.FunctionalGreen : OneUiTheme.FunctionalAmber);

            string lastSync = string.IsNullOrWhiteSpace(store.LastDesignSyncUtc) ? OneUiDesignKitManifest.SyncedAtUtc : store.LastDesignSyncUtc;
            designSyncInfoTv.Text = string.Format(
                S["SettingsDesignSyncInfoFmt"],
                OneUiTheme.ActiveDesignTokensSource,
                OneUiDesignKitManifest.KitFingerprint,
                lastSync);
        } finally {
            suppressSettingsEvents = false;
        }
    }

    async Task RunManualDesignSyncAsync(Button btn) {
        btn.Enabled = false;
        try {
            (bool ok, bool visualChanged, string detail) = await CavernizeDesignSyncService.SyncFromCavernizeAsync(
                this, logIfUnchanged: true).ConfigureAwait(false);
            ShowToast(detail);
            RunOnUiThread(() => {
                if (IsFinishing || IsDestroyed) {
                    return;
                }
                RefreshSettingsTabState();
                if (ok && visualChanged) {
                    Recreate();
                }
            });
        } finally {
            RunOnUiThread(() => {
                if (!IsFinishing && !IsDestroyed) {
                    btn.Enabled = true;
                }
            });
        }
    }

    // ── One UI 9 Configuration Modals ────────────────────────────────────────────────────

    void ShowRelayConfigModal() {
        LinearLayout body = new(this) { Orientation = Orientation.Vertical };
        body.AddView(MakeFieldLabel(S["RelayUrlLabel"]));
        EditText urlEt = MakeInput("https://your-worker.workers.dev", store.RelayUrl, isSecret: false);
        body.AddView(urlEt);

        body.AddView(MakeFieldLabel(S["PairKeyLabel"]));
        EditText keyEt = MakeInput("64 hex chars", OmniCrypto.FormatPairKeyDisplay(store.LoadPairSecretHex()), isSecret: false);
        body.AddView(keyEt);

        LinearLayout keyBtns = MakeHRow();
        keyBtns.SetPadding(0, Dp(10), 0, 0);
        Button genBtn = MakeActionPill(S["GenerateBtn"], primary: false);
        genBtn.Click += (_, _) => keyEt.Text = OmniCrypto.FormatPairKeyDisplay(OmniCrypto.GeneratePairSecretHex());
        Button copyBtn = MakeActionPill(S["CopyKeyBtn"], primary: false);
        copyBtn.Click += (_, _) => CopyToClipboard("OmniAgent Pair Key", OmniCrypto.NormalizePairSecretHex(keyEt.Text));
        Button testBtn = MakeActionPill(S["TestBtn"], primary: false);
        TextView testStatusTv = new(this) { Text = "", TextSize = 12f, Typeface = FontRegular };
        testStatusTv.SetTextColor(OneUiTheme.TextSub);
        testStatusTv.SetPadding(0, Dp(8), 0, 0);

        testBtn.Click += async (_, _) => {
            testStatusTv.Text = "...";
            (bool ok, int ver, long ms, string detail) = await OmniAgentForegroundService.TestRelayHealthAsync(urlEt.Text).ConfigureAwait(false);
            RunOnUiThread(() => {
                testStatusTv.Text = ok ? $"Hub v{ver} OK ({ms} ms)" : detail;
                testStatusTv.SetTextColor(ok ? OneUiTheme.FunctionalGreen : OneUiTheme.FunctionalRed);
            });
        };

        keyBtns.AddView(genBtn, new LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WrapContent, 1f) { RightMargin = Dp(6) });
        keyBtns.AddView(copyBtn, new LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WrapContent, 1f) { RightMargin = Dp(6) });
        keyBtns.AddView(testBtn, new LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WrapContent, 1f));
        body.AddView(keyBtns);
        body.AddView(testStatusTv);

        OneUiModalDialog.Show(
            this,
            S["RelayModalTitle"],
            customContent: body,
            icon: OneUiIconType.Globe,
            iconBadgeColor: OneUiTheme.IconSpatial,
            positiveText: S["SaveBtn"],
            onPositive: () => {
                if (OmniSettingsStore.TryNormalizeRelayUrl(urlEt.Text, out string norm)) {
                    store.RelayUrl = norm;
                }
                string normKey = OmniCrypto.NormalizePairSecretHex(keyEt.Text);
                if (!string.IsNullOrEmpty(normKey)) {
                    store.SavePairSecretHex(normKey);
                }
                OmniSettingsStore.AppendActivity("CFG", "Saved Cloudflare Hub URL & Keystore pairing key.");
                OmniAgentForegroundService.EnsureAlwaysOn(this);
                RefreshAllVisibleState();
            },
            negativeText: S["CancelBtn"]);
    }

    void ShowCloudflareConfigModal() {
        LinearLayout body = new(this) { Orientation = Orientation.Vertical };
        body.AddView(MakeFieldLabel(S["CfTokenLabel"]));
        EditText tokEt = MakeInput(store.HasCloudflareToken ? "•••••••• (saved)" : "Cloudflare API Token", "", isSecret: true);
        body.AddView(tokEt);

        body.AddView(MakeFieldLabel(S["CfWorkerLabel"]));
        EditText workerEt = MakeInput(store.ResolveEffectiveWorkerName(), store.CloudflareWorkerName, isSecret: false);
        body.AddView(workerEt);

        body.AddView(MakeFieldLabel(S["CfAccountLabel"]));
        EditText accEt = MakeInput("Auto-detected", store.CloudflareAccountId, isSecret: false);
        body.AddView(accEt);

        LinearLayout btnRow = MakeHRow();
        btnRow.SetPadding(0, Dp(10), 0, 0);
        Button openBtn = MakeActionPill(S["OpenCloudflareTokenBtn"], primary: false);
        openBtn.Click += (_, _) => OpenUrl("https://dash.cloudflare.com/profile/api-tokens");
        Button testBtn = MakeActionPill(S["TestBtn"], primary: false);
        TextView testTv = new(this) { Text = "", TextSize = 12f, Typeface = FontRegular };
        testTv.SetTextColor(OneUiTheme.TextSub);
        testTv.SetPadding(0, Dp(8), 0, 0);

        testBtn.Click += async (_, _) => {
            string tok = string.IsNullOrWhiteSpace(tokEt.Text) ? store.LoadCloudflareToken() : tokEt.Text.Trim();
            testTv.Text = "...";
            (bool ok, string accId, _, string msg) = await CloudflareDeployService.TestTokenAsync(tok).ConfigureAwait(false);
            RunOnUiThread(() => {
                if (ok && string.IsNullOrWhiteSpace(accEt.Text) && !string.IsNullOrWhiteSpace(accId)) {
                    accEt.Text = accId;
                }
                testTv.Text = msg;
                testTv.SetTextColor(ok ? OneUiTheme.FunctionalGreen : OneUiTheme.FunctionalRed);
            });
        };

        btnRow.AddView(openBtn, new LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WrapContent, 1.4f) { RightMargin = Dp(8) });
        btnRow.AddView(testBtn, new LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WrapContent, 0.8f));
        body.AddView(btnRow);
        body.AddView(testTv);

        OneUiModalDialog.Show(
            this,
            S["CfModalTitle"],
            customContent: body,
            icon: OneUiIconType.Bolt,
            iconBadgeColor: OneUiTheme.IconStudioExport,
            positiveText: S["SaveBtn"],
            onPositive: () => {
                if (!string.IsNullOrWhiteSpace(tokEt.Text)) {
                    store.SaveCloudflareToken(tokEt.Text);
                }
                store.CloudflareWorkerName = workerEt.Text;
                store.CloudflareAccountId = accEt.Text;
                OmniSettingsStore.AppendActivity("CFG", $"Saved Cloudflare config (worker={store.ResolveEffectiveWorkerName()}).");
                RefreshAllVisibleState();
            },
            negativeText: S["CancelBtn"]);
    }

    void ShowGitHubConfigModal() {
        LinearLayout body = new(this) { Orientation = Orientation.Vertical };
        body.AddView(MakeFieldLabel(S["GithubRepoLabel"]));
        EditText repoEt = MakeInput("guumachadooo/Cavern", store.GitHubRepo, isSecret: false);
        body.AddView(repoEt);

        body.AddView(MakeFieldLabel(S["GithubBranchLabel"]));
        EditText branchEt = MakeInput("master", store.GitHubBranch, isSecret: false);
        body.AddView(branchEt);

        body.AddView(MakeFieldLabel(S["GithubPatLabel"]));
        EditText patEt = MakeInput(store.HasGitHubPat ? "•••••••• (saved)" : "github_pat_...", "", isSecret: true);
        body.AddView(patEt);

        Button openGhBtn = MakeActionPill(S["OpenGithubTokenBtn"], primary: false);
        openGhBtn.Click += (_, _) => OpenUrl("https://github.com/settings/personal-access-tokens/new");
        body.AddView(openGhBtn, new LinearLayout.LayoutParams(
            ViewGroup.LayoutParams.MatchParent, ViewGroup.LayoutParams.WrapContent) { TopMargin = Dp(10) });

        OneUiModalDialog.Show(
            this,
            S["GithubModalTitle"],
            customContent: body,
            icon: OneUiIconType.LibraryGrid,
            iconBadgeColor: OneUiTheme.IconStemsCalib,
            positiveText: S["SaveBtn"],
            onPositive: () => {
                store.GitHubRepo = repoEt.Text;
                store.GitHubBranch = branchEt.Text;
                if (!string.IsNullOrWhiteSpace(patEt.Text)) {
                    store.SaveGitHubPat(patEt.Text);
                }
                OmniSettingsStore.AppendActivity("CFG", $"Saved GitHub config ({store.GitHubRepo}@{store.GitHubBranch}).");
                RefreshAllVisibleState();
            },
            negativeText: S["CancelBtn"]);
    }

    void ShowTermuxConfigModal() {
        LinearLayout body = new(this) { Orientation = Orientation.Vertical };
        body.AddView(MakeFieldLabel(S["TermuxScriptLabel"]));
        EditText scriptEt = MakeInput("/data/data/com.termux/files/home/.omni_agent/start.sh", store.TermuxStartScript, isSecret: false);
        body.AddView(scriptEt);

        LinearLayout btnRow = MakeHRow();
        btnRow.SetPadding(0, Dp(10), 0, 0);
        Button copySetupBtn = MakeActionPill(S["TermuxCopyCmdBtn"], primary: false);
        copySetupBtn.Click += (_, _) => {
            string cmd = "mkdir -p ~/.termux && grep -qxF 'allow-external-apps=true' ~/.termux/termux.properties 2>/dev/null || echo 'allow-external-apps=true' >> ~/.termux/termux.properties && termux-reload-settings";
            CopyToClipboard("Termux Setup", cmd);
        };
        Button permBtn = MakeActionPill(S["TermuxOpenPermsBtn"], primary: false);
        permBtn.Click += (_, _) => RequestPermissions([OmniAgentForegroundService.TermuxRunCommandPermission], ReqTermuxRunCommand);

        btnRow.AddView(copySetupBtn, new LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WrapContent, 1f) { RightMargin = Dp(8) });
        btnRow.AddView(permBtn, new LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WrapContent, 1f));
        body.AddView(btnRow);

        OneUiModalDialog.Show(
            this,
            S["TermuxModalTitle"],
            customContent: body,
            icon: OneUiIconType.Wrench,
            iconBadgeColor: OneUiTheme.FunctionalAmber,
            positiveText: S["SaveBtn"],
            onPositive: () => {
                store.TermuxStartScript = scriptEt.Text;
                OmniSettingsStore.AppendActivity("CFG", "Updated Termux/Debian daemon script path.");
            },
            negativeText: S["CancelBtn"]);
    }

    void OpenOverlayPermissionSettings() {
        if (!OperatingSystem.IsAndroidVersionAtLeast(23)) {
            return;
        }
        try {
            Intent intent = new(Settings.ActionManageOverlayPermission, Android.Net.Uri.Parse($"package:{PackageName}"));
            StartActivity(intent);
        } catch (Exception ex) {
            ShowToast(ex.Message);
        }
    }

    void OpenBatteryOptimizationSettings() {
        if (!OperatingSystem.IsAndroidVersionAtLeast(23)) {
            return;
        }
        try {
            Intent intent = new(Settings.ActionRequestIgnoreBatteryOptimizations, Android.Net.Uri.Parse($"package:{PackageName}"));
            StartActivity(intent);
        } catch {
            try {
                StartActivity(new Intent(Settings.ActionIgnoreBatteryOptimizationSettings));
            } catch {
                StartActivity(new Intent(Settings.ActionApplicationDetailsSettings, Android.Net.Uri.FromParts("package", PackageName, null)));
            }
        }
    }

    static int ModeToIndex(string mode) => (mode ?? "smart").Trim().ToLowerInvariant() switch {
        "biometric" => 1,
        "trusted" => 2,
        _ => 0,
    };

    static string IndexToMode(int index) => index switch {
        1 => "biometric",
        2 => "trusted",
        _ => "smart",
    };

    static int LangToIndex(string lang) => (lang ?? "auto").Trim().ToLowerInvariant() switch {
        "pt" => 1,
        "en" => 2,
        _ => 0,
    };
}
