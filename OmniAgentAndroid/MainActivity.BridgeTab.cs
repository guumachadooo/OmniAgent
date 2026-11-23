using Android.Graphics;
using Android.Text;
using Android.Views;
using Android.Widget;

using CavernizeAndroid.Theme;
using CavernizeAndroid.Views;

using OmniAgentAndroid.Services;

namespace OmniAgentAndroid;

public sealed partial class MainActivity {
    TextView bridgeStateChipTv;
    TextView bridgeHeadlineTv;
    TextView bridgeDetailTv;
    Button bridgeToggleBtn;
    TextView bridgeCavernizeStatusTv;
    TextView bridgeDebianStatusTv;
    TextView bridgeRelayStatusTv;
    TextView bridgeActivityTv;
    string lastRenderedActivityText = "";
    bool debianHealthChecking;
    long lastDebianProbeMs;

    void BuildBridgeTab(LinearLayout parent) {
        parent.AddView(MakeTabHeader(
            S["BridgeTitle"],
            S["AppSubtitle"],
            OneUiIconType.Bolt,
            OneUiTheme.FunctionalGreen));

        // 1. Always-On Hero Status Card (Zero manual Start click required!)
        LinearLayout statusCard = MakeGlassCard();
        bridgeStateChipTv = OneUiTheme.CreateStatusChip(this, S["BridgeAlwaysOnOnline"], OneUiTheme.FunctionalGreen);
        statusCard.AddView(MakeCardHeader(
            S["AppTitle"],
            OneUiIconType.Bolt,
            OneUiTheme.FunctionalGreen,
            trailingView: bridgeStateChipTv));

        bridgeHeadlineTv = new TextView(this) {
            Text = S["BridgeAlwaysOnOnline"],
            TextSize = 15f,
            Typeface = FontBold,
        };
        bridgeHeadlineTv.SetTextColor(OneUiTheme.TextPrimary);
        statusCard.AddView(bridgeHeadlineTv);

        bridgeDetailTv = new TextView(this) {
            Text = "",
            TextSize = 12.5f,
            Typeface = FontRegular,
        };
        bridgeDetailTv.SetTextColor(OneUiTheme.TextSub);
        bridgeDetailTv.SetPadding(0, Dp(4), 0, Dp(12));
        statusCard.AddView(bridgeDetailTv);

        LinearLayout heroBtns = MakeHRow();
        bridgeToggleBtn = MakeActionPill(S["BridgePauseBtn"], primary: false);
        bridgeToggleBtn.Click += (_, _) => ToggleBridgePauseResume();
        Button knockBtn = MakeActionPill(S["BridgeCopyKnockBtn"], primary: true);
        knockBtn.Click += (_, _) => CopyPublicKnockId();

        heroBtns.AddView(bridgeToggleBtn, new LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WrapContent, 1f) { RightMargin = Dp(8) });
        heroBtns.AddView(knockBtn, new LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WrapContent, 1f));
        statusCard.AddView(heroBtns);
        parent.AddView(statusCard);

        // 2. Ecosystem Links Card (Cavernize Automatic IPC + Debian Loopback + Cloudflare Hub v6)
        LinearLayout linksCard = MakeGlassCard();
        linksCard.AddView(MakeCardHeader(
            S["BridgeLinksCardTitle"],
            OneUiIconType.Globe,
            OneUiTheme.IconSpatial));

        // Row A: Cavernize Automatic Same-Signature IPC
        LinearLayout cavRow = MakeHRow();
        cavRow.SetPadding(0, Dp(6), 0, Dp(8));
        bridgeCavernizeStatusTv = new TextView(this) {
            Text = S["BridgeCavernizeLinked"],
            TextSize = 13f,
            Typeface = FontMedium,
        };
        bridgeCavernizeStatusTv.SetTextColor(OneUiTheme.TextPrimary);
        cavRow.AddView(bridgeCavernizeStatusTv, new LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WrapContent, 1f) { RightMargin = Dp(8) });
        Button pingCavBtn = MakeActionPill("Ping", primary: false);
        pingCavBtn.Click += (_, _) => _ = PingCavernizeIpcAsync(pingCavBtn);
        cavRow.AddView(pingCavBtn);
        linksCard.AddView(cavRow);

        linksCard.AddView(OneUiTheme.CreateDivider(this));

        // Row B: Debian / Termux Local Daemon
        LinearLayout debRow = MakeHRow();
        debRow.SetPadding(0, Dp(8), 0, Dp(8));
        bridgeDebianStatusTv = new TextView(this) {
            Text = S["BridgeDebianOffline"],
            TextSize = 13f,
            Typeface = FontMedium,
        };
        bridgeDebianStatusTv.SetTextColor(OneUiTheme.TextPrimary);
        debRow.AddView(bridgeDebianStatusTv, new LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WrapContent, 1f) { RightMargin = Dp(8) });
        Button wakeTermuxBtn = MakeActionPill(S["BridgeWakeTermuxBtn"], primary: false);
        wakeTermuxBtn.Click += (_, _) => TriggerTermuxWake();
        debRow.AddView(wakeTermuxBtn);
        linksCard.AddView(debRow);

        linksCard.AddView(OneUiTheme.CreateDivider(this));

        // Row C: Cloudflare Hub v6 Relay
        LinearLayout relRow = MakeHRow();
        relRow.SetPadding(0, Dp(8), 0, Dp(4));
        bridgeRelayStatusTv = new TextView(this) {
            Text = store.RelayUrl,
            TextSize = 12.5f,
            Typeface = FontRegular,
        };
        bridgeRelayStatusTv.SetTextColor(OneUiTheme.TextSub);
        bridgeRelayStatusTv.SetSingleLine(true);
        bridgeRelayStatusTv.Ellipsize = TextUtils.TruncateAt.Middle;
        relRow.AddView(bridgeRelayStatusTv, new LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WrapContent, 1f) { RightMargin = Dp(8) });
        Button testRelayBtn = MakeActionPill(S["BridgeTestRelayBtn"], primary: false);
        testRelayBtn.Click += (_, _) => _ = TestRelayFromBridgeTabAsync(testRelayBtn);
        relRow.AddView(testRelayBtn);
        linksCard.AddView(relRow);
        parent.AddView(linksCard);

        // 3. Live Activity Journal Card
        LinearLayout logCard = MakeGlassCard();
        Button clearBtn = MakeActionPill(S["BridgeClearBtn"], primary: false);
        clearBtn.SetPadding(Dp(10), Dp(6), Dp(10), Dp(6));
        clearBtn.SetMinimumHeight(Dp(32));
        clearBtn.SetMinHeight(Dp(32));
        clearBtn.Click += (_, _) => {
            OmniSettingsStore.ClearActivity();
            RefreshBridgeTabState();
        };
        logCard.AddView(MakeCardHeader(
            S["BridgeActivityTitle"],
            OneUiIconType.Logs,
            OneUiTheme.IconSystemInfo,
            trailingView: clearBtn));

        bridgeActivityTv = new TextView(this) {
            Text = S["BridgeActivityEmpty"],
            TextSize = 11.5f,
            Typeface = Typeface.Monospace,
        };
        bridgeActivityTv.SetTextColor(OneUiTheme.TextSecondary);
        bridgeActivityTv.SetTextIsSelectable(true);
        bridgeActivityTv.SetLineSpacing(Dp(2), 1.05f);
        logCard.AddView(bridgeActivityTv);
        parent.AddView(logCard);
    }

    void RefreshBridgeTabState(bool probeDebian = false) {
        if (bridgeStateChipTv == null || IsFinishing || IsDestroyed) {
            return;
        }
        bool configured = store.HasPairSecret && !string.IsNullOrWhiteSpace(store.RelayUrl);
        bool running = OmniAgentForegroundService.IsRunning;

        if (!configured) {
            OneUiTheme.UpdateStatusChip(this, bridgeStateChipTv, S["BridgeNeedsSetup"], OneUiTheme.FunctionalAmber);
            bridgeHeadlineTv.Text = S["BridgeNeedsSetup"];
            bridgeDetailTv.Text = S["BridgeStatusNeedsSetup"];
            bridgeToggleBtn.Text = S["SettingsRelayBtn"];
        } else if (running) {
            OneUiTheme.UpdateStatusChip(this, bridgeStateChipTv, S["BridgeAlwaysOnOnline"], OneUiTheme.FunctionalGreen);
            bridgeHeadlineTv.Text = S["BridgeAlwaysOnOnline"];
            bridgeDetailTv.Text = string.Format(S["BridgeStatusOnlineFmt"], OmniAgentForegroundService.CommandsHandled, store.SecurityMode);
            bridgeToggleBtn.Text = S["BridgePauseBtn"];
        } else {
            OneUiTheme.UpdateStatusChip(this, bridgeStateChipTv, S["BridgeAlwaysOnStandby"], OneUiTheme.FunctionalAmber);
            bridgeHeadlineTv.Text = S["BridgeAlwaysOnStandby"];
            bridgeDetailTv.Text = store.RelayUrl;
            bridgeToggleBtn.Text = S["BridgeResumeBtn"];
        }

        bool cavSigOk = CavernizeIpcBridgeClient.IsSignatureVerified(this);
        bridgeCavernizeStatusTv.Text = cavSigOk ? S["BridgeCavernizeLinked"] : S["BridgeCavernizeMissing"];
        bridgeCavernizeStatusTv.SetTextColor(cavSigOk ? OneUiTheme.FunctionalGreen : OneUiTheme.TextMuted);

        bridgeRelayStatusTv.Text = string.IsNullOrWhiteSpace(store.RelayUrl) ? "—" : store.RelayUrl;

        List<string> logs = OmniSettingsStore.GetActivitySnapshot();
        string nextLogText = logs.Count == 0 ? S["BridgeActivityEmpty"] : string.Join("\n", logs);
        if (!string.Equals(lastRenderedActivityText, nextLogText, StringComparison.Ordinal)) {
            lastRenderedActivityText = nextLogText;
            bridgeActivityTv.Text = nextLogText;
        }

        if (probeDebian) {
            _ = RefreshDebianLoopbackStatusAsync();
        }
    }

    async Task RefreshDebianLoopbackStatusAsync() {
        long nowMs = Android.OS.SystemClock.ElapsedRealtime();
        if (debianHealthChecking || (lastDebianProbeMs > 0 && nowMs - lastDebianProbeMs < 5_000L)) {
            return;
        }
        debianHealthChecking = true;
        lastDebianProbeMs = nowMs;
        try {
            (bool online, _) = await TermuxLocalFsClient.CheckHealthAsync(store).ConfigureAwait(false);
            RunOnUiThread(() => {
                if (bridgeDebianStatusTv == null || IsFinishing || IsDestroyed) {
                    return;
                }
                bridgeDebianStatusTv.Text = online ? S["BridgeDebianOnline"] : S["BridgeDebianOffline"];
                bridgeDebianStatusTv.SetTextColor(online ? OneUiTheme.FunctionalGreen : OneUiTheme.FunctionalAmber);
            });
        } finally {
            debianHealthChecking = false;
        }
    }

    void ToggleBridgePauseResume() {
        if (!store.HasPairSecret || string.IsNullOrWhiteSpace(store.RelayUrl)) {
            ShowRelayConfigModal();
            return;
        }
        if (OmniAgentForegroundService.IsRunning) {
            store.ServiceEnabled = false;
            OmniAgentForegroundService.Stop(this);
        } else {
            store.ServiceEnabled = true;
            OmniAgentForegroundService.Start(this);
        }
        RefreshBridgeTabState();
    }

    void CopyPublicKnockId() {
        string pairHex = store.LoadPairSecretHex();
        if (string.IsNullOrEmpty(pairHex)) {
            ShowToast(S["BridgeStatusNeedsSetup"]);
            ShowRelayConfigModal();
            return;
        }
        string knockId = OmniCrypto.ComputeKnockChannelId(pairHex);
        CopyToClipboard("OmniAgent Knock ID", knockId);
        OmniSettingsStore.AppendActivity("KNOCK", $"Copied Public Knock ID ({knockId[..Math.Min(12, knockId.Length)]}...).");
    }

    async Task PingCavernizeIpcAsync(Button btn) {
        btn.Enabled = false;
        try {
            (bool ok, string body, long ms) = await CavernizeIpcBridgeClient.SendCommandAsync(
                this, "ping", "{}", maxTier: 0, timeoutSeconds: 10).ConfigureAwait(false);
            string msg = ok ? $"Cavernize IPC OK ({ms} ms)" : body;
            OmniSettingsStore.AppendActivity("IPC", msg);
            ShowToast(msg);
        } finally {
            RunOnUiThread(() => btn.Enabled = true);
        }
    }

    void TriggerTermuxWake() {
        (bool ok, bool needsPerm, _, string msg) = OmniAgentForegroundService.LaunchTermuxSmartDaemon(
            this, store.TermuxStartScript);
        OmniSettingsStore.AppendActivity("TERMUX", msg);
        ShowToast(msg);
        if (needsPerm) {
            RequestPermissions([OmniAgentForegroundService.TermuxRunCommandPermission], ReqTermuxRunCommand);
        } else if (ok) {
            _ = DelayedRefreshAfterTermuxWakeAsync();
        }
    }

    async Task TestRelayFromBridgeTabAsync(Button btn) {
        btn.Enabled = false;
        try {
            (bool ok, int ver, long ms, string detail) = await OmniAgentForegroundService.TestRelayHealthAsync(store.RelayUrl).ConfigureAwait(false);
            string msg = ok ? $"Hub v{ver} OK ({ms} ms)" : detail;
            OmniSettingsStore.AppendActivity("RELAY", msg);
            ShowToast(msg);
        } finally {
            RunOnUiThread(() => btn.Enabled = true);
        }
    }
}
