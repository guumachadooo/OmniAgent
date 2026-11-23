using Android.App;
using Android.Content;
using Android.Content.PM;
using Android.Graphics;
using Android.OS;
using Android.Text;
using Android.Views;
using Android.Widget;
using Android.Window;

using CavernizeAndroid.Theme;
using CavernizeAndroid.Views;

using OmniAgentAndroid.Language;
using OmniAgentAndroid.Services;

namespace OmniAgentAndroid;

/// <summary>
/// Main One UI 9 shell for OmniAgent Companion v2.1+.
/// Inherits CavernizeAndroid's exact One UI 9 design kit (<see cref="OneUiTheme"/>,
/// <see cref="OneUiBackdropView"/>, <see cref="OneUiFrostedCapsuleView"/>, <see cref="OneUiModalDialog"/>,
/// <see cref="OneUiInlineSelector"/>, <see cref="OneUiIconView"/>) with zero-quota automatic design sync.
/// </summary>
[Activity(
    Label = "OmniAgent",
    MainLauncher = true,
    Theme = "@style/OmniAgentTheme",
    LaunchMode = LaunchMode.SingleTask,
    ConfigurationChanges = ConfigChanges.Orientation | ConfigChanges.ScreenSize | ConfigChanges.ScreenLayout | ConfigChanges.SmallestScreenSize | ConfigChanges.UiMode | ConfigChanges.KeyboardHidden)]
public sealed partial class MainActivity : Activity {
    const int ReqPostNotifications = 4101;
    const int ReqTermuxRunCommand = 4102;

    const int TabHubIndex = 0;
    const int TabFilesIndex = 1;
    const int TabBridgeIndex = 2;
    const int TabSettingsIndex = 3;

    OmniSettingsStore store;
    OneUiBackdropView backdropView;
    FrameLayout contentHost;
    OneUiFrostedCapsuleView bottomDock;
    readonly ScrollView[] tabScrolls = new ScrollView[4];
    readonly LinearLayout[] tabRoots = new LinearLayout[4];
    readonly LinearLayout[] dockTabItems = new LinearLayout[4];
    readonly OneUiIconView[] dockTabIcons = new OneUiIconView[4];
    readonly TextView[] dockTabLabels = new TextView[4];
    static int designSyncStartedThisProcess;
    static int autoRecreateCountThisProcess;

    int activeTabIndex = TabHubIndex;
    int topInsetPx;
    int bottomInsetPx;
    int lastAppliedTopPad = -1;
    int lastAppliedBottomPad = -1;
    int lastAppliedDockMargin = -1;
    Handler uiHandler;
    bool pendingUiRefresh;
    IOnBackInvokedCallback predictiveBackCallback;

    static OmniStrings S => OmniStrings.Active;
    Typeface FontBold => OneUiTheme.GetAppleFontBold(this);
    Typeface FontMedium => OneUiTheme.GetAppleFontMedium(this);
    Typeface FontRegular => OneUiTheme.GetAppleFontRegular(this);

    protected override void OnCreate(Bundle savedInstanceState) {
        base.OnCreate(savedInstanceState);

        uiHandler = new Handler(Looper.MainLooper);
        store = new OmniSettingsStore(this);
        OmniStrings.Refresh(store.UiLanguage);

        // Apply shared Cavernize One UI 9 theme + cached live Cavernize design tokens immediately.
        OneUiTheme.Initialize(this);
        CavernizeDesignSyncService.ApplyCachedTokensIfPresent(this);

        ConfigureEdgeToEdge();
        BuildShellUi();
        RegisterPredictiveBack();
        RequestInitialPermissions();

        // Zero-click Always-On automation off the UI thread so startup stays 120Hz silky smooth.
        Context appCtx = ApplicationContext ?? this;
        _ = Task.Run(() => OmniAgentForegroundService.EnsureAlwaysOn(appCtx));

        OmniAgentForegroundService.StateChanged += OnServiceOrActivityChanged;
        OmniSettingsStore.ActivityChanged += OnServiceOrActivityChanged;

        RefreshAllVisibleState(probeDebian: false);
        _ = LoadHubSnapshotAsync(silent: false);
        _ = SyncCavernizeDesignInBackgroundAsync();
    }

    protected override void OnResume() {
        base.OnResume();
        Context appCtx = ApplicationContext ?? this;
        _ = Task.Run(() => OmniAgentForegroundService.EnsureAlwaysOn(appCtx));
        RefreshAllVisibleState(probeDebian: activeTabIndex == TabBridgeIndex);
        bottomDock?.Post(() => {
            if (!IsFinishing && !IsDestroyed) {
                bottomDock?.RefreshBlur(force: true);
            }
        });
    }

    protected override void OnDestroy() {
        uiHandler?.RemoveCallbacksAndMessages(null);
        pendingUiRefresh = false;
        OmniAgentForegroundService.StateChanged -= OnServiceOrActivityChanged;
        OmniSettingsStore.ActivityChanged -= OnServiceOrActivityChanged;
        if (OperatingSystem.IsAndroidVersionAtLeast(33) && predictiveBackCallback != null) {
            try {
                OnBackInvokedDispatcher?.UnregisterOnBackInvokedCallback(predictiveBackCallback);
            } catch {
                // Ignore if already unregistered during teardown.
            }
        }
        base.OnDestroy();
    }

    public override void OnRequestPermissionsResult(int requestCode, string[] permissions, Permission[] grantResults) {
        base.OnRequestPermissionsResult(requestCode, permissions, grantResults);
        if (requestCode == ReqTermuxRunCommand) {
            bool granted = grantResults is { Length: > 0 } && grantResults[0] == Permission.Granted;
            if (granted) {
                (bool ok, _, _, string msg) = OmniAgentForegroundService.LaunchTermuxSmartDaemon(this, store.TermuxStartScript);
                OmniSettingsStore.AppendActivity("TERMUX", msg);
                ShowToast(msg);
                if (ok) {
                    _ = DelayedRefreshAfterTermuxWakeAsync();
                }
            } else {
                ShowToast(S["TermuxOpenPermsBtn"]);
            }
        }
        RefreshAllVisibleState();
    }

    void ConfigureEdgeToEdge() {
        if (Window == null) {
            return;
        }
        if (OperatingSystem.IsAndroidVersionAtLeast(30)) {
            Window.SetDecorFitsSystemWindows(false);
        } else {
#pragma warning disable CA1422
            Window.DecorView.SystemUiVisibility = (StatusBarVisibility)(
                SystemUiFlags.LayoutStable |
                SystemUiFlags.LayoutFullscreen |
                SystemUiFlags.LayoutHideNavigation);
#pragma warning restore CA1422
        }
        if (!OperatingSystem.IsAndroidVersionAtLeast(35)) {
#pragma warning disable CA1422
            Window.SetStatusBarColor(Color.Transparent);
            Window.SetNavigationBarColor(Color.Transparent);
#pragma warning restore CA1422
        }
        if (OperatingSystem.IsAndroidVersionAtLeast(29)) {
            Window.NavigationBarContrastEnforced = false;
        }
    }

    void RegisterPredictiveBack() {
        if (!OperatingSystem.IsAndroidVersionAtLeast(33)) {
            return;
        }
        predictiveBackCallback = new OmniBackCallback(HandleBackNavigation);
        OnBackInvokedDispatcher?.RegisterOnBackInvokedCallback(0, predictiveBackCallback);
    }

    void HandleBackNavigation() {
        if (OneUiModalDialog.DismissAllActiveDialogs() > 0) {
            return;
        }
        if (activeTabIndex == TabFilesIndex && TryNavigateFilesUp()) {
            return;
        }
        if (activeTabIndex != TabHubIndex) {
            SelectTab(TabHubIndex);
            return;
        }
        MoveTaskToBack(true);
    }

    void RequestInitialPermissions() {
        if (OperatingSystem.IsAndroidVersionAtLeast(33) &&
            CheckSelfPermission(Android.Manifest.Permission.PostNotifications) != Permission.Granted) {
            RequestPermissions([Android.Manifest.Permission.PostNotifications], ReqPostNotifications);
        }
    }

    void BuildShellUi() {
        FrameLayout root = new(this);
        root.SetBackgroundColor(OneUiTheme.BgScreen);

        backdropView = new OneUiBackdropView(this);
        root.AddView(backdropView, new FrameLayout.LayoutParams(
            ViewGroup.LayoutParams.MatchParent, ViewGroup.LayoutParams.MatchParent));

        contentHost = new FrameLayout(this);
        root.AddView(contentHost, new FrameLayout.LayoutParams(
            ViewGroup.LayoutParams.MatchParent, ViewGroup.LayoutParams.MatchParent));

        for (int i = 0; i < 4; i++) {
            ScrollView sv = new(this) {
                FillViewport = true,
                VerticalScrollBarEnabled = false,
                OverScrollMode = OverScrollMode.Always,
                Visibility = i == activeTabIndex ? ViewStates.Visible : ViewStates.Gone,
            };
            sv.SetClipToPadding(false);

            LinearLayout container = new(this) { Orientation = Orientation.Vertical };
            container.SetPadding(Dp(16), Dp(14), Dp(16), Dp(116));
            sv.AddView(container, new ViewGroup.LayoutParams(
                ViewGroup.LayoutParams.MatchParent, ViewGroup.LayoutParams.WrapContent));

            int tabIdx = i;
            sv.ScrollChange += (_, _) => {
                if (activeTabIndex == tabIdx && bottomDock != null && bottomDock.IsAttachedToWindow) {
                    bottomDock.RefreshBlur(force: false);
                }
            };

            tabScrolls[i] = sv;
            tabRoots[i] = container;
            contentHost.AddView(sv, new FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MatchParent, ViewGroup.LayoutParams.MatchParent));
        }

        BuildHubTab(tabRoots[TabHubIndex]);
        BuildFilesTab(tabRoots[TabFilesIndex]);
        BuildBridgeTab(tabRoots[TabBridgeIndex]);
        BuildSettingsTab(tabRoots[TabSettingsIndex]);

        bottomDock = BuildFloatingBottomDock();
        FrameLayout.LayoutParams dockLp = new(ViewGroup.LayoutParams.MatchParent, ViewGroup.LayoutParams.WrapContent) {
            Gravity = GravityFlags.Bottom | GravityFlags.CenterHorizontal,
            LeftMargin = Dp(18),
            RightMargin = Dp(18),
            BottomMargin = Dp(16),
        };
        root.AddView(bottomDock, dockLp);

        root.SetOnApplyWindowInsetsListener(new OmniInsetsListener((top, bottom) => {
            topInsetPx = top;
            bottomInsetPx = bottom;
            ApplySafeAreaInsets();
        }));

        SetContentView(root);
        SelectTab(TabHubIndex);
    }

    OneUiFrostedCapsuleView BuildFloatingBottomDock() {
        OneUiFrostedCapsuleView dock = new(this) {
            CornerRadiusDp = 30f,
            BehindViewsProvider = () => [backdropView, contentHost],
        };
        dock.SetPadding(Dp(6), Dp(6), Dp(6), Dp(6));

        LinearLayout row = new(this) {
            Orientation = Orientation.Horizontal,
            BaselineAligned = false,
        };
        row.SetGravity(GravityFlags.CenterVertical);

        (string label, OneUiIconType icon)[] tabs = [
            (S["TabHub"], OneUiIconType.Sparkles),
            (S["TabFiles"], OneUiIconType.Folder),
            (S["TabBridge"], OneUiIconType.Bolt),
            (S["TabSettings"], OneUiIconType.Gear),
        ];

        for (int i = 0; i < tabs.Length; i++) {
            int idx = i;
            LinearLayout item = new(this) {
                Orientation = Orientation.Vertical,
                Clickable = true,
                Focusable = true,
            };
            item.SetGravity(GravityFlags.Center);
            item.SetPadding(Dp(4), Dp(8), Dp(4), Dp(8));

            OneUiIconView iconView = new(this, tabs[i].icon, OneUiTheme.TextMuted);
            item.AddView(iconView, new LinearLayout.LayoutParams(Dp(20), Dp(20)));

            TextView labelTv = new(this) {
                Text = tabs[i].label,
                TextSize = 11f,
                Typeface = FontMedium,
                Gravity = GravityFlags.Center,
            };
            labelTv.SetSingleLine(true);
            labelTv.Ellipsize = TextUtils.TruncateAt.End;
            labelTv.SetPadding(0, Dp(3), 0, 0);
            item.AddView(labelTv, new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MatchParent, ViewGroup.LayoutParams.WrapContent));

            OneUiTheme.AttachRecoil(item, 0.94f);
            item.Click += (_, _) => SelectTab(idx);

            dockTabItems[i] = item;
            dockTabIcons[i] = iconView;
            dockTabLabels[i] = labelTv;

            row.AddView(item, new LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WrapContent, 1f));
        }

        dock.AddView(row, new FrameLayout.LayoutParams(
            ViewGroup.LayoutParams.MatchParent, ViewGroup.LayoutParams.WrapContent));
        return dock;
    }

    void ApplySafeAreaInsets() {
        int topPad = Math.Max(Dp(14), topInsetPx + Dp(8));
        int bottomPad = Math.Max(Dp(116), bottomInsetPx + Dp(106));
        if (topPad != lastAppliedTopPad || bottomPad != lastAppliedBottomPad) {
            lastAppliedTopPad = topPad;
            lastAppliedBottomPad = bottomPad;
            for (int i = 0; i < tabRoots.Length; i++) {
                tabRoots[i]?.SetPadding(Dp(16), topPad, Dp(16), bottomPad);
            }
        }
        int dockMargin = Math.Max(Dp(16), bottomInsetPx + Dp(10));
        if (dockMargin != lastAppliedDockMargin && bottomDock?.LayoutParameters is FrameLayout.LayoutParams lp) {
            lastAppliedDockMargin = dockMargin;
            lp.BottomMargin = dockMargin;
            bottomDock.LayoutParameters = lp;
        }
    }

    void SelectTab(int index) {
        activeTabIndex = Math.Clamp(index, 0, 3);
        for (int i = 0; i < 4; i++) {
            bool selected = i == activeTabIndex;
            if (tabScrolls[i] != null) {
                tabScrolls[i].Visibility = selected ? ViewStates.Visible : ViewStates.Gone;
            }
            if (dockTabItems[i] != null) {
                dockTabItems[i].Background = selected
                    ? OneUiTheme.CreateRoundedDrawable(
                        this,
                        OneUiTheme.WithAlpha(OneUiTheme.Primary, 56),
                        24f,
                        OneUiTheme.WithAlpha(OneUiTheme.Primary, 110),
                        1f)
                    : null;
            }
            if (dockTabIcons[i] != null) {
                dockTabIcons[i].IconColor = selected ? OneUiTheme.Primary : OneUiTheme.TextMuted;
            }
            if (dockTabLabels[i] != null) {
                dockTabLabels[i].SetTextColor(selected ? OneUiTheme.TextPrimary : OneUiTheme.TextMuted);
                dockTabLabels[i].Typeface = selected ? FontBold : FontMedium;
            }
        }

        if (activeTabIndex == TabFilesIndex && !filesInitialized) {
            _ = LoadDirectoryAsync(store.LastExplorerPath, showSpinner: true);
        }
        if (activeTabIndex == TabBridgeIndex) {
            RefreshBridgeTabState(probeDebian: true);
        }
        if (activeTabIndex == TabSettingsIndex) {
            RefreshSettingsTabState();
        }
        bottomDock?.Post(() => {
            if (!IsFinishing && !IsDestroyed) {
                bottomDock?.RefreshBlur(force: true);
            }
        });
    }

    void OnServiceOrActivityChanged() {
        if (IsFinishing || IsDestroyed || uiHandler == null) {
            return;
        }
        uiHandler.Post(() => {
            if (IsFinishing || IsDestroyed || pendingUiRefresh) {
                return;
            }
            pendingUiRefresh = true;
            uiHandler.PostDelayed(() => {
                pendingUiRefresh = false;
                if (IsFinishing || IsDestroyed) {
                    return;
                }
                if (activeTabIndex == TabBridgeIndex) {
                    RefreshBridgeTabState(probeDebian: false);
                } else if (activeTabIndex == TabSettingsIndex) {
                    RefreshSettingsTabState();
                }
            }, 250L);
        });
    }

    void RefreshAllVisibleState(bool probeDebian = false) {
        if (IsFinishing || IsDestroyed) {
            return;
        }
        RefreshBridgeTabState(probeDebian: probeDebian);
        RefreshSettingsTabState();
    }

    async Task SyncCavernizeDesignInBackgroundAsync() {
        if (System.Threading.Interlocked.Exchange(ref designSyncStartedThisProcess, 1) != 0) {
            return;
        }
        (bool ok, bool visualChanged, _) = await CavernizeDesignSyncService.SyncFromCavernizeAsync(
            this, logIfUnchanged: false).ConfigureAwait(false);
        if (!ok) {
            return;
        }
        RunOnUiThread(() => {
            if (IsFinishing || IsDestroyed) {
                return;
            }
            RefreshSettingsTabState();
            if (visualChanged && System.Threading.Interlocked.Increment(ref autoRecreateCountThisProcess) == 1) {
                Recreate();
            }
        });
    }

    async Task DelayedRefreshAfterTermuxWakeAsync() {
        await Task.Delay(2400).ConfigureAwait(false);
        await LoadHubSnapshotAsync(silent: true).ConfigureAwait(false);
        if (activeTabIndex == TabFilesIndex) {
            await LoadDirectoryAsync(currentExplorerPath, showSpinner: false).ConfigureAwait(false);
        }
        RunOnUiThread(() => {
            if (!IsFinishing && !IsDestroyed) {
                RefreshBridgeTabState(probeDebian: true);
            }
        });
    }

    // ── Shared One UI 9 Layout & Component Helpers ───────────────────────────────────────

    int Dp(float v) => OneUiTheme.Dp(this, v);

    LinearLayout MakeTabHeader(string title, string subtitle, OneUiIconType icon, Color badgeColor, View trailingAction = null) {
        LinearLayout header = MakeHRow();
        header.SetPadding(Dp(2), Dp(4), Dp(2), Dp(14));

        header.AddView(OneUiTheme.CreateVectorIconBadge(this, icon, badgeColor, 40));

        LinearLayout texts = new(this) { Orientation = Orientation.Vertical };
        texts.SetPadding(Dp(12), 0, Dp(8), 0);

        TextView titleTv = new(this) {
            Text = title,
            TextSize = 23f,
            Typeface = FontBold,
        };
        titleTv.SetTextColor(OneUiTheme.TextPrimary);
        titleTv.SetSingleLine(true);
        titleTv.Ellipsize = TextUtils.TruncateAt.End;
        texts.AddView(titleTv);

        if (!string.IsNullOrWhiteSpace(subtitle)) {
            TextView subTv = new(this) {
                Text = subtitle,
                TextSize = 12.5f,
                Typeface = FontRegular,
            };
            subTv.SetTextColor(OneUiTheme.TextSub);
            subTv.SetSingleLine(true);
            subTv.Ellipsize = TextUtils.TruncateAt.End;
            texts.AddView(subTv);
        }

        header.AddView(texts, new LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WrapContent, 1f));
        if (trailingAction != null) {
            header.AddView(trailingAction);
        }
        return header;
    }

    LinearLayout MakeGlassCard(float cornerDp = 0f) {
        float radius = cornerDp > 0f ? cornerDp : OneUiTheme.EffectiveCardCornerDp;
        LinearLayout card = new(this) { Orientation = Orientation.Vertical };
        card.Background = OneUiTheme.CreateGlassCardDrawable(this, OneUiTheme.SurfaceCard, radius);
        card.SetPadding(Dp(18), Dp(16), Dp(18), Dp(16));
        LinearLayout.LayoutParams lp = new(ViewGroup.LayoutParams.MatchParent, ViewGroup.LayoutParams.WrapContent) {
            BottomMargin = Dp(14),
        };
        card.LayoutParameters = lp;
        return card;
    }

    LinearLayout MakeHRow() {
        LinearLayout row = new(this) {
            Orientation = Orientation.Horizontal,
            BaselineAligned = false,
        };
        row.SetGravity(GravityFlags.CenterVertical);
        return row;
    }

    LinearLayout MakeCardHeader(string title, OneUiIconType icon, Color badgeColor, Action onHelp = null, View trailingView = null) {
        LinearLayout row = MakeHRow();
        row.SetPadding(0, 0, 0, Dp(10));

        row.AddView(OneUiTheme.CreateVectorIconBadge(this, icon, badgeColor, 30));

        TextView tv = new(this) {
            Text = title,
            TextSize = 15.5f,
            Typeface = FontBold,
        };
        tv.SetTextColor(OneUiTheme.TextPrimary);
        tv.SetSingleLine(true);
        tv.Ellipsize = TextUtils.TruncateAt.End;
        tv.SetPadding(Dp(10), 0, Dp(8), 0);
        row.AddView(tv, new LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WrapContent, 1f));

        if (trailingView != null) {
            row.AddView(trailingView);
        }

        if (onHelp != null) {
            FrameLayout helpBtn = new(this) {
                Clickable = true,
                Focusable = true,
                ContentDescription = title,
            };
            helpBtn.Background = OneUiTheme.CreateRippleRoundedDrawable(
                this,
                OneUiTheme.WithAlpha(OneUiTheme.Primary, 28),
                14f,
                OneUiTheme.WithAlpha(OneUiTheme.Primary, 80),
                1f);
            OneUiIconView qIcon = new(this, OneUiIconType.HelpQuestion, OneUiTheme.Primary);
            FrameLayout.LayoutParams iconLp = new(Dp(16), Dp(16)) { Gravity = GravityFlags.Center };
            helpBtn.AddView(qIcon, iconLp);
            OneUiTheme.AttachRecoil(helpBtn, 0.92f);
            helpBtn.Click += (_, _) => onHelp();

            LinearLayout.LayoutParams hlp = new(Dp(30), Dp(30)) { LeftMargin = Dp(6) };
            row.AddView(helpBtn, hlp);
        }
        return row;
    }

    Button MakeActionPill(string text, bool primary = false, Color? customFill = null, Color? customText = null) {
        Color fill = customFill ?? (primary ? OneUiTheme.Primary : OneUiTheme.SurfaceElevated);
        Color fg = customText ?? (primary ? Color.White : OneUiTheme.TextPrimary);
        Color stroke = primary
            ? OneUiTheme.WithAlpha(Color.White, 48)
            : OneUiTheme.WithAlpha(Color.White, 34);

        Button btn = new(this) {
            Text = text,
            TextSize = 13f,
            Typeface = primary ? FontBold : FontMedium,
        };
        btn.SetAllCaps(false);
        btn.SetTextColor(fg);
        btn.SetSingleLine(true);
        btn.Ellipsize = TextUtils.TruncateAt.End;
        btn.SetPadding(Dp(14), Dp(10), Dp(14), Dp(10));
        btn.SetMinimumHeight(Dp(42));
        btn.SetMinHeight(Dp(42));
        btn.Background = OneUiTheme.CreateRippleRoundedDrawable(this, fill, OneUiTheme.EffectiveButtonCornerDp, stroke, 1f);
        OneUiTheme.AttachRecoil(btn, 0.96f);
        return btn;
    }

    TextView MakeSectionLabel(string text) {
        TextView tv = new(this) {
            Text = text,
            TextSize = 12f,
            Typeface = FontBold,
        };
        tv.SetTextColor(OneUiTheme.TextSub);
        tv.SetPadding(Dp(4), Dp(4), Dp(4), Dp(8));
        return tv;
    }

    TextView MakeFieldLabel(string text) {
        TextView tv = new(this) {
            Text = text,
            TextSize = 12f,
            Typeface = FontMedium,
        };
        tv.SetTextColor(OneUiTheme.TextSub);
        tv.SetPadding(0, Dp(8), 0, Dp(4));
        return tv;
    }

    EditText MakeInput(string hint, string initialValue, bool isSecret = false) {
        EditText et = new(this) {
            Hint = hint,
            Text = initialValue ?? "",
            TextSize = 13.5f,
            Typeface = FontRegular,
            InputType = isSecret
                ? InputTypes.ClassText | InputTypes.TextVariationPassword
                : InputTypes.ClassText | InputTypes.TextFlagNoSuggestions,
        };
        et.SetTextColor(OneUiTheme.TextPrimary);
        et.SetHintTextColor(OneUiTheme.TextMuted);
        et.SetPadding(Dp(14), Dp(11), Dp(14), Dp(11));
        et.Background = OneUiTheme.CreateRoundedDrawable(
            this,
            OneUiTheme.SurfaceModalRow,
            14f,
            OneUiTheme.WithAlpha(Color.White, 38),
            1f);
        return et;
    }

    (LinearLayout row, OneUiSwitch sw) MakeSwitchRow(string label, string subtitle, bool initialChecked, Action<bool> onChanged) {
        LinearLayout row = MakeHRow();
        row.SetPadding(0, Dp(6), 0, Dp(6));

        LinearLayout texts = new(this) { Orientation = Orientation.Vertical };
        TextView lbl = new(this) {
            Text = label,
            TextSize = 14f,
            Typeface = FontMedium,
        };
        lbl.SetTextColor(OneUiTheme.TextPrimary);
        texts.AddView(lbl);

        if (!string.IsNullOrWhiteSpace(subtitle)) {
            TextView sub = new(this) {
                Text = subtitle,
                TextSize = 12f,
                Typeface = FontRegular,
            };
            sub.SetTextColor(OneUiTheme.TextMuted);
            texts.AddView(sub);
        }

        OneUiSwitch sw = new(this) { Checked = initialChecked };
        sw.CheckedChange += (_, e) => onChanged?.Invoke(e.IsChecked);

        row.AddView(texts, new LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WrapContent, 1f));
        LinearLayout.LayoutParams swLp = new(ViewGroup.LayoutParams.WrapContent, ViewGroup.LayoutParams.WrapContent) {
            LeftMargin = Dp(10),
        };
        row.AddView(sw, swLp);
        return (row, sw);
    }

    void CopyToClipboard(string label, string text) {
        if (GetSystemService(ClipboardService) is Android.Content.ClipboardManager cm) {
            cm.PrimaryClip = ClipData.NewPlainText(label, text ?? "");
            ShowToast(S["FilesCopiedToast"]);
        }
    }

    void OpenUrl(string url) {
        try {
            StartActivity(new Intent(Intent.ActionView, Android.Net.Uri.Parse(url)));
        } catch (Exception ex) {
            ShowToast(ex.Message);
        }
    }

    void ShowToast(string message) {
        if (string.IsNullOrWhiteSpace(message)) {
            return;
        }
        RunOnUiThread(() => Android.Widget.Toast.MakeText(this, message, ToastLength.Short)?.Show());
    }

    sealed class OmniInsetsListener(Action<int, int> onInsets) : Java.Lang.Object, View.IOnApplyWindowInsetsListener {
        public WindowInsets OnApplyWindowInsets(View v, WindowInsets insets) {
            int top = 0;
            int bottom = 0;
            if (OperatingSystem.IsAndroidVersionAtLeast(30)) {
                Insets bars = insets.GetInsets(WindowInsets.Type.SystemBars() | WindowInsets.Type.DisplayCutout());
                top = bars.Top;
                bottom = bars.Bottom;
            } else {
#pragma warning disable CA1422
                top = insets.SystemWindowInsetTop;
                bottom = insets.SystemWindowInsetBottom;
#pragma warning restore CA1422
            }
            onInsets(top, bottom);
            return insets;
        }
    }

    sealed class OmniBackCallback(Action onBack) : Java.Lang.Object, IOnBackInvokedCallback {
        public void OnBackInvoked() => onBack();
    }
}
