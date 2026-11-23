using Android.Content;
using Android.Graphics;
using Android.Text;
using Android.Views;
using Android.Widget;

using CavernizeAndroid.Theme;
using CavernizeAndroid.Views;

using OmniAgentAndroid.Services;

using IOPath = System.IO.Path;

namespace OmniAgentAndroid;

public sealed partial class MainActivity {
    bool filesInitialized;
    bool filesLoading;
    string currentExplorerPath = "/root/.gemini/antigravity/scratch/Cavern";
    string currentExplorerParent = "/root/.gemini/antigravity/scratch";
    List<LocalFsEntry> currentExplorerEntries = [];

    TextView filesPathTv;
    TextView filesCountChipTv;
    EditText filesSearchEt;
    LinearLayout filesOfflineBanner;
    LinearLayout filesListContainer;
    Button filesUpBtn;
    Button filesNewFolderBtn;

    void BuildFilesTab(LinearLayout parent) {
        parent.AddView(MakeTabHeader(
            S["FilesTitle"],
            S["FilesSubtitle"],
            OneUiIconType.Folder,
            OneUiTheme.IconStudioExport));

        // 1. Quick Bookmark Pills (Triad repos + Debian /root + Termux ~ + Android /sdcard)
        HorizontalScrollView bmScroll = new(this) { HorizontalScrollBarEnabled = false };
        LinearLayout bmRow = MakeHRow();
        foreach ((string label, string path) in TermuxLocalFsClient.DefaultBookmarks) {
            string targetPath = path;
            Button pill = MakeActionPill(label, primary: false);
            pill.SetPadding(Dp(12), Dp(7), Dp(12), Dp(7));
            pill.SetMinimumHeight(Dp(34));
            pill.SetMinHeight(Dp(34));
            pill.Click += (_, _) => _ = LoadDirectoryAsync(targetPath, showSpinner: true);
            bmRow.AddView(pill, new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.WrapContent, ViewGroup.LayoutParams.WrapContent) { RightMargin = Dp(8) });
        }
        bmScroll.AddView(bmRow);
        parent.AddView(bmScroll, new LinearLayout.LayoutParams(
            ViewGroup.LayoutParams.MatchParent, ViewGroup.LayoutParams.WrapContent) { BottomMargin = Dp(12) });

        // 2. Offline Debian Banner (auto-hidden when loopback online or browsing /sdcard)
        filesOfflineBanner = MakeGlassCard(22f);
        filesOfflineBanner.Visibility = ViewStates.Gone;
        filesOfflineBanner.AddView(MakeCardHeader(
            S["FilesDaemonOfflineTitle"],
            OneUiIconType.Bolt,
            OneUiTheme.FunctionalAmber));

        TextView offBody = new(this) {
            Text = S["FilesDaemonOfflineBody"],
            TextSize = 12.5f,
            Typeface = FontRegular,
        };
        offBody.SetTextColor(OneUiTheme.TextSub);
        offBody.SetPadding(0, 0, 0, Dp(10));
        filesOfflineBanner.AddView(offBody);

        LinearLayout offBtns = MakeHRow();
        Button wakeBtn = MakeActionPill(S["FilesWakeNowBtn"], primary: true);
        wakeBtn.Click += (_, _) => TriggerTermuxWake();
        Button sdBtn = MakeActionPill(S["FilesBrowseSdcardBtn"], primary: false);
        sdBtn.Click += (_, _) => _ = LoadDirectoryAsync("/storage/emulated/0", showSpinner: true);
        offBtns.AddView(wakeBtn, new LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WrapContent, 1f) { RightMargin = Dp(8) });
        offBtns.AddView(sdBtn, new LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WrapContent, 1f));
        filesOfflineBanner.AddView(offBtns);
        parent.AddView(filesOfflineBanner);

        // 3. Breadcrumb + Toolbar + Search Card
        LinearLayout navCard = MakeGlassCard(22f);
        LinearLayout pathRow = MakeHRow();
        filesPathTv = new TextView(this) {
            Text = currentExplorerPath,
            TextSize = 12f,
            Typeface = Typeface.Monospace,
        };
        filesPathTv.SetTextColor(OneUiTheme.Primary);
        filesPathTv.SetMaxLines(2);
        filesPathTv.Ellipsize = TextUtils.TruncateAt.Start;
        pathRow.AddView(filesPathTv, new LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WrapContent, 1f) { RightMargin = Dp(8) });

        filesCountChipTv = OneUiTheme.CreateStatusChip(this, "0", OneUiTheme.IconSystemInfo);
        pathRow.AddView(filesCountChipTv);
        navCard.AddView(pathRow, new LinearLayout.LayoutParams(
            ViewGroup.LayoutParams.MatchParent, ViewGroup.LayoutParams.WrapContent) { BottomMargin = Dp(10) });

        LinearLayout toolRow = MakeHRow();
        filesUpBtn = MakeActionPill(S["FilesUpBtn"], primary: false);
        filesUpBtn.Click += (_, _) => TryNavigateFilesUp();
        Button reloadBtn = MakeActionPill(S["FilesRefreshBtn"], primary: false);
        reloadBtn.Click += (_, _) => _ = LoadDirectoryAsync(currentExplorerPath, showSpinner: true);
        filesNewFolderBtn = MakeActionPill(S["FilesNewFolderBtn"], primary: false);
        filesNewFolderBtn.Click += (_, _) => ShowCreateFolderModal();

        toolRow.AddView(filesUpBtn, new LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WrapContent, 1f) { RightMargin = Dp(6) });
        toolRow.AddView(reloadBtn, new LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WrapContent, 1f) { RightMargin = Dp(6) });
        toolRow.AddView(filesNewFolderBtn, new LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WrapContent, 1f));
        navCard.AddView(toolRow, new LinearLayout.LayoutParams(
            ViewGroup.LayoutParams.MatchParent, ViewGroup.LayoutParams.WrapContent) { BottomMargin = Dp(10) });

        filesSearchEt = MakeInput(S["FilesSearchHint"], "", isSecret: false);
        filesSearchEt.TextChanged += (_, _) => RenderFilteredExplorerEntries();
        navCard.AddView(filesSearchEt);
        parent.AddView(navCard);

        // 4. Directory Listing Glass Card
        LinearLayout listCard = MakeGlassCard(22f);
        listCard.SetPadding(Dp(12), Dp(10), Dp(12), Dp(10));
        filesListContainer = new LinearLayout(this) { Orientation = Orientation.Vertical };
        listCard.AddView(filesListContainer);
        parent.AddView(listCard);
    }

    bool TryNavigateFilesUp() {
        if (string.IsNullOrWhiteSpace(currentExplorerParent) ||
            string.Equals(currentExplorerParent, currentExplorerPath, StringComparison.Ordinal)) {
            return false;
        }
        _ = LoadDirectoryAsync(currentExplorerParent, showSpinner: true);
        return true;
    }

    async Task LoadDirectoryAsync(string path, bool showSpinner) {
        if (filesLoading) {
            return;
        }
        filesLoading = true;
        filesInitialized = true;
        string target = string.IsNullOrWhiteSpace(path) ? "/root/.gemini/antigravity/scratch/Cavern" : path.Trim();

        if (showSpinner) {
            RunOnUiThread(() => {
                if (filesPathTv != null) {
                    filesPathTv.Text = target;
                }
            });
        }

        try {
            LocalFsListResult res = await TermuxLocalFsClient.ListDirectoryAsync(store, target).ConfigureAwait(false);
            RunOnUiThread(() => {
                if (res.Ok) {
                    currentExplorerPath = res.Path;
                    currentExplorerParent = res.Parent;
                    currentExplorerEntries = res.Entries ?? [];
                    store.LastExplorerPath = currentExplorerPath;
                    filesPathTv.Text = currentExplorerPath;
                    filesOfflineBanner.Visibility = ViewStates.Gone;
                    filesUpBtn.Enabled = !string.IsNullOrEmpty(currentExplorerParent) && currentExplorerParent != currentExplorerPath;
                    RenderFilteredExplorerEntries();
                } else {
                    currentExplorerPath = target;
                    currentExplorerEntries = [];
                    filesPathTv.Text = target;
                    filesOfflineBanner.Visibility = ViewStates.Visible;
                    OneUiTheme.UpdateStatusChip(this, filesCountChipTv, "0", OneUiTheme.FunctionalAmber);
                    filesListContainer.RemoveAllViews();
                    TextView errTv = new(this) {
                        Text = res.Error,
                        TextSize = 12.5f,
                        Typeface = FontRegular,
                    };
                    errTv.SetTextColor(OneUiTheme.TextMuted);
                    errTv.SetPadding(Dp(6), Dp(10), Dp(6), Dp(10));
                    filesListContainer.AddView(errTv);
                }
                bottomDock?.RefreshBlur(force: true);
            });
        } finally {
            filesLoading = false;
        }
    }

    void RenderFilteredExplorerEntries() {
        if (filesListContainer == null) {
            return;
        }
        string q = (filesSearchEt?.Text ?? "").Trim();
        List<LocalFsEntry> filtered = string.IsNullOrEmpty(q)
            ? currentExplorerEntries
            : currentExplorerEntries.Where(e => e.Name.Contains(q, StringComparison.OrdinalIgnoreCase)).ToList();

        OneUiTheme.UpdateStatusChip(
            this,
            filesCountChipTv,
            string.Format(S["FilesCountFmt"], filtered.Count),
            OneUiTheme.Primary);

        filesListContainer.RemoveAllViews();
        if (filtered.Count == 0) {
            TextView emptyTv = new(this) {
                Text = S["FilesEmptyDir"],
                TextSize = 13f,
                Typeface = FontRegular,
                Gravity = GravityFlags.Center,
            };
            emptyTv.SetTextColor(OneUiTheme.TextMuted);
            emptyTv.SetPadding(Dp(12), Dp(20), Dp(12), Dp(20));
            filesListContainer.AddView(emptyTv);
            return;
        }

        int cap = Math.Min(filtered.Count, 260);
        for (int i = 0; i < cap; i++) {
            LocalFsEntry entry = filtered[i];
            if (i > 0) {
                filesListContainer.AddView(OneUiTheme.CreateDivider(this));
            }
            filesListContainer.AddView(BuildFileEntryRow(entry));
        }
    }

    LinearLayout BuildFileEntryRow(LocalFsEntry entry) {
        LinearLayout row = MakeHRow();
        row.Clickable = true;
        row.Focusable = true;
        row.SetPadding(Dp(6), Dp(10), Dp(6), Dp(10));
        row.Background = OneUiTheme.CreateRippleRoundedDrawable(this, Color.Transparent, 14f);

        (OneUiIconType iconType, Color badgeColor) = ResolveEntryIcon(entry);
        row.AddView(OneUiTheme.CreateVectorIconBadge(this, iconType, badgeColor, 32));

        LinearLayout texts = new(this) { Orientation = Orientation.Vertical };
        texts.SetPadding(Dp(12), 0, Dp(8), 0);

        TextView nameTv = new(this) {
            Text = entry.IsDir ? $"{entry.Name}/" : entry.Name,
            TextSize = 14f,
            Typeface = entry.IsDir ? FontBold : FontMedium,
        };
        nameTv.SetTextColor(OneUiTheme.TextPrimary);
        nameTv.SetSingleLine(true);
        nameTv.Ellipsize = TextUtils.TruncateAt.End;
        texts.AddView(nameTv);

        string meta = entry.IsDir
            ? (entry.IsSymlink ? "symlink · folder" : "folder")
            : $"{TermuxLocalFsClient.FormatSize(entry.SizeBytes)}{(string.IsNullOrEmpty(entry.Extension) ? "" : $" · {entry.Extension}")}";
        TextView metaTv = new(this) {
            Text = meta,
            TextSize = 11.5f,
            Typeface = FontRegular,
        };
        metaTv.SetTextColor(OneUiTheme.TextMuted);
        metaTv.SetSingleLine(true);
        metaTv.Ellipsize = TextUtils.TruncateAt.End;
        texts.AddView(metaTv);

        row.AddView(texts, new LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WrapContent, 1f));

        if (entry.IsDir) {
            OneUiIconView chevron = new(this, OneUiIconType.ChevronRight, OneUiTheme.TextMuted);
            row.AddView(chevron, new LinearLayout.LayoutParams(Dp(16), Dp(16)));
            row.Click += (_, _) => _ = LoadDirectoryAsync(entry.FullPath, showSpinner: true);
        } else {
            row.Click += (_, _) => _ = OpenFilePreviewModalAsync(entry);
        }

        row.LongClick += (_, e) => {
            e.Handled = true;
            ShowDeleteConfirmModal(entry);
        };
        OneUiTheme.AttachRecoil(row, 0.98f);
        return row;
    }

    static (OneUiIconType icon, Color color) ResolveEntryIcon(LocalFsEntry entry) {
        if (entry.IsDir) {
            return (OneUiIconType.Folder, OneUiTheme.IconStudioExport);
        }
        return (entry.Extension ?? "").ToLowerInvariant() switch {
            ".cs" or ".py" or ".c" or ".cpp" or ".h" or ".js" or ".mjs" or ".ts" or ".sh" => (OneUiIconType.Wrench, OneUiTheme.IconSpatial),
            ".md" or ".txt" or ".log" or ".json" or ".yml" or ".yaml" or ".xml" => (OneUiIconType.Logs, OneUiTheme.IconStemsCalib),
            ".flac" or ".wav" or ".mp3" or ".m4a" or ".mkv" or ".mp4" or ".ec3" => (OneUiIconType.MusicFile, OneUiTheme.IconAtmosUpmix),
            ".apk" or ".so" or ".onnx" => (OneUiIconType.Gear, OneUiTheme.FunctionalAmber),
            _ => (OneUiIconType.Info, OneUiTheme.IconSystemInfo),
        };
    }

    async Task OpenFilePreviewModalAsync(LocalFsEntry entry) {
        LocalFsReadResult res = await TermuxLocalFsClient.ReadFileAsync(store, entry.FullPath).ConfigureAwait(false);
        RunOnUiThread(() => {
            if (!res.Ok) {
                ShowToast(res.Error);
                return;
            }

            string body = res.IsBinary
                ? $"{S["FilesBinaryNotice"]}\n\n{res.Path}\nSize: {TermuxLocalFsClient.FormatSize(res.SizeBytes)}"
                : res.Content + (res.Truncated ? "\n\n... [truncated]" : "");

            LinearLayout custom = new(this) { Orientation = Orientation.Vertical };
            TextView metaTv = new(this) {
                Text = $"{res.Path}\n{TermuxLocalFsClient.FormatSize(res.SizeBytes)}" + (res.IsBinary ? "" : $" · {res.LineCount} lines"),
                TextSize = 11.5f,
                Typeface = Typeface.Monospace,
            };
            metaTv.SetTextColor(OneUiTheme.Primary);
            metaTv.SetPadding(0, 0, 0, Dp(8));
            custom.AddView(metaTv);

            LinearLayout actionRow = MakeHRow();
            if (!res.IsBinary) {
                Button copyBtn = MakeActionPill(S["FilesPreviewCopyBtn"], primary: false);
                copyBtn.Click += (_, _) => CopyToClipboard(res.Name, res.Content);
                actionRow.AddView(copyBtn, new LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WrapContent, 1f) { RightMargin = Dp(6) });
            }
            Button exportBtn = MakeActionPill(S["FilesPreviewExportBtn"], primary: true);
            exportBtn.Click += (_, _) => _ = ExportAndShareFileAsync(entry, res);
            actionRow.AddView(exportBtn, new LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WrapContent, 1f));
            custom.AddView(actionRow, new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MatchParent, ViewGroup.LayoutParams.WrapContent) { BottomMargin = Dp(10) });

            ScrollView codeScroll = new(this) { VerticalScrollBarEnabled = true };
            codeScroll.Background = OneUiTheme.CreateRoundedDrawable(
                this,
                OneUiTheme.SurfaceModalRow,
                16f,
                OneUiTheme.WithAlpha(Color.White, 28),
                1f);
            codeScroll.SetPadding(Dp(12), Dp(10), Dp(12), Dp(10));

            TextView codeTv = new(this) {
                Text = body,
                TextSize = 11.5f,
                Typeface = Typeface.Monospace,
            };
            codeTv.SetTextColor(OneUiTheme.TextSecondary);
            codeTv.SetTextIsSelectable(true);
            codeScroll.AddView(codeTv);
            custom.AddView(codeScroll, new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MatchParent, Dp(300)));

            OneUiModalDialog.Show(
                this,
                res.Name,
                customContent: custom,
                icon: OneUiIconType.Logs,
                iconBadgeColor: OneUiTheme.IconStudioExport,
                positiveText: S["CloseBtn"],
                negativeText: S["FilesDeleteBtn"],
                onNegative: () => ShowDeleteConfirmModal(entry));
        });
    }

    async Task ExportAndShareFileAsync(LocalFsEntry entry, LocalFsReadResult readResult) {
        (bool ok, string exportedPath, string err) = await TermuxLocalFsClient.ExportToDownloadsAsync(store, entry.FullPath).ConfigureAwait(false);
        if (ok) {
            ShowToast(string.Format(S["FilesExportedToastFmt"], exportedPath));
        } else if (!string.IsNullOrEmpty(err)) {
            ShowToast(err);
        }

        if (!readResult.IsBinary && !string.IsNullOrEmpty(readResult.Content)) {
            try {
                string safeName = IOPath.GetFileName(entry.Name);
                string shareFile = IOPath.Combine(OmniApkFileProvider.GetShareDirectory(this), safeName);
                await File.WriteAllTextAsync(shareFile, readResult.Content).ConfigureAwait(false);
                Android.Net.Uri uri = OmniApkFileProvider.GetShareUri(this, safeName);
                RunOnUiThread(() => {
                    Intent send = new(Intent.ActionSend);
                    send.SetType("text/plain");
                    send.PutExtra(Intent.ExtraStream, uri);
                    send.PutExtra(Intent.ExtraSubject, safeName);
                    send.AddFlags(ActivityFlags.GrantReadUriPermission);
                    StartActivity(Intent.CreateChooser(send, safeName));
                });
            } catch {
                // Export to Downloads already succeeded; share sheet is optional.
            }
        }
    }

    void ShowCreateFolderModal() {
        EditText nameEt = MakeInput(S["FilesCreateFolderHint"], "", isSecret: false);
        OneUiModalDialog.Show(
            this,
            S["FilesCreateFolderTitle"],
            customContent: nameEt,
            icon: OneUiIconType.Folder,
            iconBadgeColor: OneUiTheme.IconStudioExport,
            positiveText: S["FilesCreateBtn"],
            onPositive: () => {
                string name = (nameEt.Text ?? "").Trim().Replace("/", "").Replace("\\", "");
                if (string.IsNullOrEmpty(name)) {
                    return;
                }
                string full = currentExplorerPath.EndsWith('/') ? currentExplorerPath + name : $"{currentExplorerPath}/{name}";
                _ = CreateFolderAndReloadAsync(full);
            },
            negativeText: S["CancelBtn"]);
    }

    async Task CreateFolderAndReloadAsync(string fullPath) {
        (bool ok, string err) = await TermuxLocalFsClient.CreateDirectoryAsync(store, fullPath).ConfigureAwait(false);
        if (!ok) {
            ShowToast(err);
            return;
        }
        await LoadDirectoryAsync(currentExplorerPath, showSpinner: false).ConfigureAwait(false);
    }

    void ShowDeleteConfirmModal(LocalFsEntry entry) {
        OneUiModalDialog.Show(
            this,
            S["FilesDeleteConfirmTitle"],
            string.Format(S["FilesDeleteConfirmFmt"], entry.FullPath),
            icon: OneUiIconType.CleanFilter,
            iconBadgeColor: OneUiTheme.FunctionalRed,
            positiveText: S["FilesDeleteBtn"],
            onPositive: () => _ = DeleteAndReloadAsync(entry.FullPath),
            negativeText: S["CancelBtn"]);
    }

    async Task DeleteAndReloadAsync(string fullPath) {
        (bool ok, string err) = await TermuxLocalFsClient.DeletePathAsync(store, fullPath).ConfigureAwait(false);
        if (!ok) {
            ShowToast(err);
            return;
        }
        await LoadDirectoryAsync(currentExplorerPath, showSpinner: false).ConfigureAwait(false);
    }
}
