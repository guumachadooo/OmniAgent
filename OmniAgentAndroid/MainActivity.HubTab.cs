using Android.Content;
using Android.Graphics;
using Android.Text;
using Android.Views;
using Android.Widget;

using CavernizeAndroid.Theme;
using CavernizeAndroid.Views;

using OmniAgentAndroid.Services;

namespace OmniAgentAndroid;

public sealed partial class MainActivity {
    TextView hubWaveChipTv;
    TextView hubSummaryTv;
    TextView hubNextTv;
    TextView hubStatsTv;
    TextView hubFetchedAtTv;
    Button hubRefreshBtn;
    LinearLayout hubReposContainer;
    LinearLayout hubSyncContainer;
    TriadHubSnapshot lastHubSnapshot;
    bool hubLoading;

    void BuildHubTab(LinearLayout parent) {
        hubRefreshBtn = MakeActionPill(S["HubRefreshBtn"], primary: false);
        hubRefreshBtn.Click += (_, _) => _ = LoadHubSnapshotAsync(silent: false);
        parent.AddView(MakeTabHeader(
            S["HubTitle"],
            S["HubSubtitle"],
            OneUiIconType.Sparkles,
            OneUiTheme.IconAtmosUpmix,
            hubRefreshBtn));

        // 1. Active Wave & Next Milestone Card
        LinearLayout waveCard = MakeGlassCard();
        hubWaveChipTv = OneUiTheme.CreateStatusChip(this, "—", OneUiTheme.Primary);
        waveCard.AddView(MakeCardHeader(
            S["HubActiveWaveTitle"],
            OneUiIconType.Bolt,
            OneUiTheme.IconSpatial,
            trailingView: hubWaveChipTv));

        hubSummaryTv = new TextView(this) {
            Text = S["HubRefreshing"],
            TextSize = 13.5f,
            Typeface = FontRegular,
        };
        hubSummaryTv.SetTextColor(OneUiTheme.TextSecondary);
        hubSummaryTv.SetLineSpacing(Dp(2), 1.05f);
        hubSummaryTv.SetPadding(0, 0, 0, Dp(10));
        waveCard.AddView(hubSummaryTv);

        LinearLayout nextBox = new(this) { Orientation = Orientation.Vertical };
        nextBox.Background = OneUiTheme.CreateRoundedDrawable(
            this,
            OneUiTheme.WithAlpha(OneUiTheme.Primary, 24),
            16f,
            OneUiTheme.WithAlpha(OneUiTheme.Primary, 72),
            1f);
        nextBox.SetPadding(Dp(12), Dp(10), Dp(12), Dp(10));

        TextView nextLabelTv = new(this) {
            Text = S["HubWaveNextLabel"],
            TextSize = 11f,
            Typeface = FontBold,
        };
        nextLabelTv.SetTextColor(OneUiTheme.Primary);
        nextBox.AddView(nextLabelTv);

        hubNextTv = new TextView(this) {
            Text = "—",
            TextSize = 13f,
            Typeface = FontMedium,
        };
        hubNextTv.SetTextColor(OneUiTheme.TextPrimary);
        hubNextTv.SetPadding(0, Dp(2), 0, 0);
        nextBox.AddView(hubNextTv);
        waveCard.AddView(nextBox, new LinearLayout.LayoutParams(
            ViewGroup.LayoutParams.MatchParent, ViewGroup.LayoutParams.WrapContent));

        LinearLayout footerRow = MakeHRow();
        footerRow.SetPadding(0, Dp(10), 0, 0);
        hubStatsTv = new TextView(this) {
            Text = "",
            TextSize = 11.5f,
            Typeface = FontMedium,
        };
        hubStatsTv.SetTextColor(OneUiTheme.TextMuted);
        hubStatsTv.SetSingleLine(true);
        hubStatsTv.Ellipsize = TextUtils.TruncateAt.End;
        footerRow.AddView(hubStatsTv, new LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WrapContent, 1f));

        hubFetchedAtTv = new TextView(this) {
            Text = "",
            TextSize = 11f,
            Typeface = FontRegular,
        };
        hubFetchedAtTv.SetTextColor(OneUiTheme.TextMuted);
        footerRow.AddView(hubFetchedAtTv);
        waveCard.AddView(footerRow);
        parent.AddView(waveCard);

        // 2. Triad Repositories Section
        parent.AddView(MakeSectionLabel(S["HubReposTitle"]));
        hubReposContainer = new LinearLayout(this) { Orientation = Orientation.Vertical };
        parent.AddView(hubReposContainer);

        // 3. Smart 1-Tap Actions Card
        LinearLayout actionsCard = MakeGlassCard();
        actionsCard.AddView(MakeCardHeader(
            S["HubActionsTitle"],
            OneUiIconType.Wrench,
            OneUiTheme.IconStudioExport));

        LinearLayout row1 = MakeHRow();
        Button pullBtn = MakeActionPill(S["HubPullTriadBtn"], primary: true);
        pullBtn.Click += (_, _) => _ = RunPullAllTriadAsync(pullBtn);
        Button selfCheckBtn = MakeActionPill(S["HubSelfCheckCavernizeBtn"], primary: false);
        selfCheckBtn.Click += (_, _) => _ = RunCavernizeSelfCheckAsync(selfCheckBtn);
        row1.AddView(pullBtn, new LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WrapContent, 1f) { RightMargin = Dp(8) });
        row1.AddView(selfCheckBtn, new LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WrapContent, 1f));
        actionsCard.AddView(row1, new LinearLayout.LayoutParams(
            ViewGroup.LayoutParams.MatchParent, ViewGroup.LayoutParams.WrapContent) { BottomMargin = Dp(8) });

        LinearLayout row2 = MakeHRow();
        Button updCavBtn = MakeActionPill(S["HubUpdateCavernizeBtn"], primary: false);
        updCavBtn.Click += (_, _) => _ = RunApkUpdateAsync("cavern", "Cavernize-Android.apk", updCavBtn);
        Button updOmniBtn = MakeActionPill(S["HubUpdateOmniBtn"], primary: false);
        updOmniBtn.Click += (_, _) => _ = RunApkUpdateAsync("omniagent", "OmniAgent-Android.apk", updOmniBtn);
        row2.AddView(updCavBtn, new LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WrapContent, 1f) { RightMargin = Dp(8) });
        row2.AddView(updOmniBtn, new LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WrapContent, 1f));
        actionsCard.AddView(row2, new LinearLayout.LayoutParams(
            ViewGroup.LayoutParams.MatchParent, ViewGroup.LayoutParams.WrapContent) { BottomMargin = Dp(8) });

        Button openCavBtn = MakeActionPill(S["HubOpenCavernizeBtn"], primary: false);
        openCavBtn.Click += (_, _) => OpenCavernizeApp();
        actionsCard.AddView(openCavBtn, new LinearLayout.LayoutParams(
            ViewGroup.LayoutParams.MatchParent, ViewGroup.LayoutParams.WrapContent));
        parent.AddView(actionsCard);

        // 4. Zero-Quota Project Doc Reader
        LinearLayout docsCard = MakeGlassCard();
        docsCard.AddView(MakeCardHeader(
            S["HubDocsTitle"],
            OneUiIconType.Logs,
            OneUiTheme.IconStemsCalib));

        HorizontalScrollView docScroll = new(this) { HorizontalScrollBarEnabled = false };
        LinearLayout docRow = MakeHRow();
        foreach ((string _, string label, string rawUrl) in TriadHubService.ProjectDocs) {
            string docTitle = label;
            string docUrl = rawUrl;
            Button pill = MakeActionPill(docTitle, primary: false);
            pill.SetPadding(Dp(12), Dp(8), Dp(12), Dp(8));
            pill.SetMinimumHeight(Dp(36));
            pill.SetMinHeight(Dp(36));
            pill.Click += (_, _) => _ = OpenZeroQuotaDocAsync(docTitle, docUrl);
            docRow.AddView(pill, new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.WrapContent, ViewGroup.LayoutParams.WrapContent) { RightMargin = Dp(8) });
        }
        docScroll.AddView(docRow);
        docsCard.AddView(docScroll);
        parent.AddView(docsCard);

        // 5. Recent Sync Ledger Card
        LinearLayout syncCard = MakeGlassCard();
        syncCard.AddView(MakeCardHeader(
            S["HubRecentSyncTitle"],
            OneUiIconType.Queue,
            OneUiTheme.IconSystemInfo));
        hubSyncContainer = new LinearLayout(this) { Orientation = Orientation.Vertical };
        syncCard.AddView(hubSyncContainer);
        parent.AddView(syncCard);
    }

    async Task LoadHubSnapshotAsync(bool silent) {
        if (hubLoading) {
            return;
        }
        hubLoading = true;
        RunOnUiThread(() => {
            if (!silent && hubRefreshBtn != null) {
                hubRefreshBtn.Enabled = false;
                hubRefreshBtn.Text = "...";
            }
        });
        try {
            TriadHubSnapshot snap = await TriadHubService.FetchSnapshotAsync(store).ConfigureAwait(false);
            lastHubSnapshot = snap;
            RunOnUiThread(() => {
                if (!IsFinishing && !IsDestroyed) {
                    RenderHubSnapshot(snap);
                }
            });
        } finally {
            hubLoading = false;
            RunOnUiThread(() => {
                if (!IsFinishing && !IsDestroyed && hubRefreshBtn != null) {
                    hubRefreshBtn.Enabled = true;
                    hubRefreshBtn.Text = S["HubRefreshBtn"];
                }
            });
        }
    }

    void RenderHubSnapshot(TriadHubSnapshot snap) {
        if (snap == null) {
            return;
        }
        OneUiTheme.UpdateStatusChip(this, hubWaveChipTv, snap.ActiveWave, OneUiTheme.Primary);
        hubSummaryTv.Text = string.IsNullOrWhiteSpace(snap.HandoffSummary) ? "—" : snap.HandoffSummary;
        hubNextTv.Text = string.IsNullOrWhiteSpace(snap.NextMilestone) ? "—" : snap.NextMilestone;
        string debianTag = snap.DebianLoopbackOnline ? S["BridgeDebianOnline"] : S["HubLocalOffline"];
        hubStatsTv.Text = string.Format(S["HubStatsFmt"], snap.PendingExtraCount, snap.BuildLessonsCount) + $" · {debianTag}";
        hubFetchedAtTv.Text = snap.FetchedAtLocal;

        hubReposContainer.RemoveAllViews();
        foreach (TriadRepoCardModel repo in snap.Repos) {
            hubReposContainer.AddView(BuildRepoGlassCard(repo));
        }

        hubSyncContainer.RemoveAllViews();
        if (snap.RecentSync.Count == 0) {
            TextView emptyTv = new(this) {
                Text = S["HubNoSyncEntries"],
                TextSize = 12.5f,
                Typeface = FontRegular,
            };
            emptyTv.SetTextColor(OneUiTheme.TextMuted);
            hubSyncContainer.AddView(emptyTv);
        } else {
            for (int i = 0; i < snap.RecentSync.Count; i++) {
                TriadSyncEntry entry = snap.RecentSync[i];
                if (i > 0) {
                    hubSyncContainer.AddView(OneUiTheme.CreateDivider(this));
                }
                LinearLayout row = new(this) { Orientation = Orientation.Vertical };
                row.SetPadding(0, Dp(8), 0, Dp(8));

                TextView headTv = new(this) {
                    Text = $"{entry.Date} · {entry.Scope}",
                    TextSize = 12f,
                    Typeface = FontBold,
                };
                headTv.SetTextColor(OneUiTheme.Primary);
                row.AddView(headTv);

                TextView bodyTv = new(this) {
                    Text = entry.Summary,
                    TextSize = 12.5f,
                    Typeface = FontRegular,
                };
                bodyTv.SetTextColor(OneUiTheme.TextSecondary);
                bodyTv.SetPadding(0, Dp(2), 0, 0);
                row.AddView(bodyTv);
                hubSyncContainer.AddView(row);
            }
        }
        bottomDock?.RefreshBlur(force: true);
    }

    LinearLayout BuildRepoGlassCard(TriadRepoCardModel repo) {
        LinearLayout card = MakeGlassCard(22f);
        card.SetPadding(Dp(16), Dp(14), Dp(16), Dp(14));

        LinearLayout topRow = MakeHRow();
        Color accent = repo.Alias switch {
            "cavern" => OneUiTheme.IconSpatial,
            "omniagent" => OneUiTheme.IconAtmosUpmix,
            _ => OneUiTheme.IconStemsCalib,
        };
        topRow.AddView(OneUiTheme.CreateVectorIconBadge(this, OneUiIconType.LibraryGrid, accent, 28));

        LinearLayout titleCol = new(this) { Orientation = Orientation.Vertical };
        titleCol.SetPadding(Dp(10), 0, Dp(6), 0);
        TextView nameTv = new(this) {
            Text = repo.DisplayName,
            TextSize = 14.5f,
            Typeface = FontBold,
        };
        nameTv.SetTextColor(OneUiTheme.TextPrimary);
        nameTv.SetSingleLine(true);
        nameTv.Ellipsize = TextUtils.TruncateAt.End;
        titleCol.AddView(nameTv);

        TextView branchTv = new(this) {
            Text = $"{repo.OwnerRepo} · {repo.Branch}",
            TextSize = 11.5f,
            Typeface = FontRegular,
        };
        branchTv.SetTextColor(OneUiTheme.TextMuted);
        branchTv.SetSingleLine(true);
        branchTv.Ellipsize = TextUtils.TruncateAt.End;
        titleCol.AddView(branchTv);
        topRow.AddView(titleCol, new LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WrapContent, 1f));

        (string ciLabel, Color ciColor) = repo.CiStatus switch {
            "pass" => (S["HubCiPass"], OneUiTheme.FunctionalGreen),
            "fail" => (S["HubCiFail"], OneUiTheme.FunctionalRed),
            "running" => (S["HubCiRunning"], OneUiTheme.FunctionalAmber),
            _ => (S["HubCiIdle"], OneUiTheme.TextMuted),
        };
        topRow.AddView(OneUiTheme.CreateStatusChip(this, ciLabel, ciColor));
        card.AddView(topRow);

        // Second row: Release tag + Local Debian working tree status
        LinearLayout chipsRow = MakeHRow();
        chipsRow.SetPadding(0, Dp(10), 0, Dp(6));
        if (!string.IsNullOrWhiteSpace(repo.LatestTag)) {
            TextView tagChip = OneUiTheme.CreateStatusChip(this, repo.LatestTag, OneUiTheme.FunctionalCyan);
            chipsRow.AddView(tagChip, new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.WrapContent, ViewGroup.LayoutParams.WrapContent) { RightMargin = Dp(6) });
        }
        if (repo.LocalDirtyCount == 0) {
            chipsRow.AddView(OneUiTheme.CreateStatusChip(this, S["HubCleanLocal"], OneUiTheme.FunctionalGreen));
        } else if (repo.LocalDirtyCount > 0) {
            chipsRow.AddView(OneUiTheme.CreateStatusChip(this, string.Format(S["HubDirtyFmt"], repo.LocalDirtyCount), OneUiTheme.FunctionalAmber));
        } else {
            chipsRow.AddView(OneUiTheme.CreateStatusChip(this, S["HubLocalOffline"], OneUiTheme.TextMuted));
        }
        card.AddView(chipsRow);

        // Latest commit line
        if (!string.IsNullOrWhiteSpace(repo.LatestCommitSha) || !string.IsNullOrWhiteSpace(repo.LatestCommitMsg)) {
            TextView commitTv = new(this) {
                Text = $"{repo.LatestCommitSha}  {repo.LatestCommitMsg}".Trim(),
                TextSize = 12f,
                Typeface = Typeface.Monospace,
            };
            commitTv.SetTextColor(OneUiTheme.TextSub);
            commitTv.SetMaxLines(2);
            commitTv.Ellipsize = TextUtils.TruncateAt.End;
            card.AddView(commitTv);
        }
        return card;
    }

    async Task RunPullAllTriadAsync(Button btn) {
        btn.Enabled = false;
        try {
            (bool ok, string summary) = await TermuxLocalFsClient.PullAllTriadReposAsync(store).ConfigureAwait(false);
            OmniSettingsStore.AppendActivity("TRIAD", summary);
            RunOnUiThread(() => {
                OneUiModalDialog.Show(
                    this,
                    S["HubPullTriadBtn"],
                    summary,
                    icon: ok ? OneUiIconType.Check : OneUiIconType.Info,
                    iconBadgeColor: ok ? OneUiTheme.FunctionalGreen : OneUiTheme.FunctionalAmber,
                    positiveText: S["OkBtn"],
                    monospaceBody: true);
            });
            await LoadHubSnapshotAsync(silent: true).ConfigureAwait(false);
        } finally {
            RunOnUiThread(() => btn.Enabled = true);
        }
    }

    async Task RunCavernizeSelfCheckAsync(Button btn) {
        btn.Enabled = false;
        try {
            (bool ok, string body, long ms) = await CavernizeIpcBridgeClient.SendCommandAsync(
                this, "self_check", "{}", maxTier: 1, timeoutSeconds: 30).ConfigureAwait(false);
            string display = ok ? $"({ms} ms)\n\n{body}" : body;
            OmniSettingsStore.AppendActivity("CAVERN", ok ? $"Self-check OK ({ms} ms)" : $"Self-check: {body}");
            RunOnUiThread(() => {
                OneUiModalDialog.Show(
                    this,
                    S["HubSelfCheckCavernizeBtn"],
                    display,
                    icon: ok ? OneUiIconType.Check : OneUiIconType.Info,
                    iconBadgeColor: ok ? OneUiTheme.FunctionalGreen : OneUiTheme.FunctionalAmber,
                    positiveText: S["OkBtn"],
                    negativeText: S["FilesPreviewCopyBtn"],
                    onNegative: () => CopyToClipboard("Cavernize Self-Check", display),
                    monospaceBody: true);
            });
        } finally {
            RunOnUiThread(() => btn.Enabled = true);
        }
    }

    async Task RunApkUpdateAsync(string alias, string fileName, Button btn) {
        btn.Enabled = false;
        try {
            if (lastHubSnapshot == null) {
                lastHubSnapshot = await TriadHubService.FetchSnapshotAsync(store).ConfigureAwait(false);
            }
            TriadRepoCardModel repo = lastHubSnapshot?.Repos?.FirstOrDefault(r => r.Alias == alias);
            if (repo == null || string.IsNullOrWhiteSpace(repo.LatestApkUrl)) {
                ShowToast(S["HubApkNotFound"]);
                return;
            }
            ShowToast(S["HubApkDownloading"]);
            (bool ok, string detail) = await TriadHubService.DownloadAndInstallApkAsync(
                this, repo.LatestApkUrl, fileName).ConfigureAwait(false);
            ShowToast(ok ? $"{S["HubApkReady"]}: {detail}" : detail);
        } finally {
            RunOnUiThread(() => btn.Enabled = true);
        }
    }

    void OpenCavernizeApp() {
        Intent launch = PackageManager?.GetLaunchIntentForPackage(CavernizeIpcBridgeClient.CavernizePackage);
        if (launch != null) {
            launch.AddFlags(ActivityFlags.NewTask);
            StartActivity(launch);
        } else {
            ShowToast(S["BridgeCavernizeMissing"]);
        }
    }

    async Task OpenZeroQuotaDocAsync(string title, string rawUrl) {
        ShowToast(S["HubRefreshing"]);
        string content = await TriadHubService.FetchDocContentAsync(rawUrl).ConfigureAwait(false);
        RunOnUiThread(() => {
            OneUiModalDialog.Show(
                this,
                title,
                content,
                icon: OneUiIconType.Logs,
                iconBadgeColor: OneUiTheme.IconStemsCalib,
                positiveText: S["CloseBtn"],
                negativeText: S["FilesPreviewCopyBtn"],
                onNegative: () => CopyToClipboard(title, content),
                monospaceBody: true);
        });
    }
}
