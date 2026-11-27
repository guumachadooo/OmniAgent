using System.Runtime.CompilerServices;

using Android.App;
using Android.Content;
using Android.Graphics;
using Android.Graphics.Drawables;
using Android.Text;
using Android.Views;
using Android.Widget;

using CavernizeAndroid.Theme;

namespace CavernizeAndroid.Views;

/// <summary>
/// Reusable One UI 9 factory primitives for Settings Hub tiles, status chips, and prerequisite cards.
/// </summary>
public static class OneUiComponents {
    public static TextView CreateStatusChip(Context ctx, string text, Color accentColor) =>
        OneUiTheme.CreateStatusChip(ctx, text, accentColor);

    public static void UpdateStatusChip(Context ctx, TextView chip, string text, Color accentColor) =>
        OneUiTheme.UpdateStatusChip(ctx, chip, text, accentColor);

    public static LinearLayout CreateEmptyPrereqCard(
        Context ctx,
        string title,
        string subtitle,
        string ctaText = null,
        Action onCta = null) =>
        OneUiTheme.CreateEmptyPrereqCard(ctx, title, subtitle, ctaText, onCta);

    public static LinearLayout CreateCategoryHubTile(
        Context ctx,
        OneUiIconType icon,
        Color iconColor,
        string title,
        string liveSubtitle,
        Action onClick,
        out TextView subtitleView) =>
        OneUiTheme.CreateCategoryHubTile(ctx, icon, iconColor, title, liveSubtitle, onClick, out subtitleView);
}

/// <summary>
/// In-app One UI 9 frosted glass collapsible selector replacing stock Android Spinner dropdowns.
/// Tapping the pill expands an inline list of styled options inside the current card.
/// </summary>
public sealed class OneUiInlineSelector : LinearLayout {
    readonly TextView titleView;
    readonly TextView currentValueView;
    readonly OneUiIconView chevronView;
    readonly LinearLayout headerPill;
    readonly LinearLayout optionsPanel;

    IReadOnlyList<string> items = [];
    int selectedIndex = -1;
    bool expanded;

    public int SelectedIndex => selectedIndex;
    public int ItemCount => items.Count;
    public IReadOnlyList<string> Items => items;
    public event Action<int> SelectionChanged;

    public OneUiInlineSelector(Context ctx, string title = null) : base(ctx) {
        Orientation = Orientation.Vertical;
        int dp(float v) => OneUiTheme.Dp(ctx, v);

        if (!string.IsNullOrEmpty(title)) {
            titleView = new TextView(ctx) {
                Text = title,
                TextSize = 12f
            };
            titleView.SetTextColor(OneUiTheme.TextMuted);
            titleView.SetSingleLine(true);
            titleView.Ellipsize = TextUtils.TruncateAt.End;
            titleView.SetPadding(dp(2), 0, dp(2), dp(5));
            AddView(titleView);
        }

        headerPill = new LinearLayout(ctx) { Orientation = Orientation.Horizontal, BaselineAligned = false };
        headerPill.SetGravity(GravityFlags.CenterVertical);
        headerPill.Background = OneUiTheme.CreateGlassCardDrawable(ctx, OneUiTheme.SurfaceSubTile, OneUiTheme.ButtonCornerDp);
        headerPill.SetPadding(dp(14), dp(10), dp(12), dp(10));
        headerPill.SetMinimumHeight(dp(44));
        OneUiTheme.AttachRecoil(headerPill, 0.98f);

        currentValueView = new TextView(ctx) {
            Text = "-",
            TextSize = 13f
        };
        currentValueView.SetTextColor(OneUiTheme.TextPrimary);
        currentValueView.Typeface = OneUiTheme.GetAppleFontBold(ctx);
        currentValueView.SetIncludeFontPadding(false);
        currentValueView.SetSingleLine(true);
        currentValueView.Ellipsize = TextUtils.TruncateAt.End;

        chevronView = new OneUiIconView(ctx, OneUiIconType.ChevronDown, OneUiTheme.PeachText);

        headerPill.AddView(currentValueView, new LayoutParams(0, ViewGroup.LayoutParams.WrapContent, 1f) { RightMargin = dp(8) });
        headerPill.AddView(chevronView, new LayoutParams(dp(16), dp(16)));
        headerPill.Click += (_, _) => SetExpanded(!expanded);

        AddView(headerPill, new LayoutParams(ViewGroup.LayoutParams.MatchParent, ViewGroup.LayoutParams.WrapContent));

        optionsPanel = new LinearLayout(ctx) {
            Orientation = Orientation.Vertical,
            Visibility = ViewStates.Gone
        };
        optionsPanel.Background = OneUiTheme.CreateGlassCardDrawable(ctx, OneUiTheme.SurfaceElevated, 20f, OneUiTheme.WithAlpha(OneUiTheme.Primary, 110));
        optionsPanel.SetPadding(dp(8), dp(6), dp(8), dp(6));
        AddView(optionsPanel, new LayoutParams(ViewGroup.LayoutParams.MatchParent, ViewGroup.LayoutParams.WrapContent) {
            TopMargin = dp(6)
        });
    }

    public void SetItems(IReadOnlyList<string> labels, int initialIndex = 0, bool notify = false) {
        items = labels ?? [];
        if (items.Count == 0) {
            selectedIndex = -1;
            currentValueView.Text = "-";
            expanded = false;
            optionsPanel.Visibility = ViewStates.Gone;
            optionsPanel.RemoveAllViews();
            return;
        }
        SetSelection(Math.Clamp(initialIndex, 0, items.Count - 1), notify);
    }

    public void SetSelection(int index, bool notify = false) {
        if (items.Count == 0) {
            return;
        }
        selectedIndex = Math.Clamp(index, 0, items.Count - 1);
        currentValueView.Text = items[selectedIndex];
        RebuildOptionRows();
        if (notify) {
            SelectionChanged?.Invoke(selectedIndex);
        }
    }

    public void SetExpanded(bool open) {
        if (items.Count <= 1 && open) {
            return;
        }
        expanded = open;
        optionsPanel.Visibility = expanded ? ViewStates.Visible : ViewStates.Gone;
        chevronView.IconType = expanded ? OneUiIconType.ChevronUp : OneUiIconType.ChevronDown;
    }

    void RebuildOptionRows() {
        optionsPanel.RemoveAllViews();
        int dp(float v) => OneUiTheme.Dp(Context, v);

        for (int i = 0; i < items.Count; i++) {
            int idx = i;
            bool isSel = i == selectedIndex;

            LinearLayout row = new(Context) { Orientation = Orientation.Horizontal };
            row.SetGravity(GravityFlags.CenterVertical);
            row.SetPadding(dp(10), dp(9), dp(10), dp(9));
            if (isSel) {
                row.Background = OneUiTheme.CreateRoundedDrawable(
                    Context, OneUiTheme.WithAlpha(OneUiTheme.Primary, 72), 14f);
            } else {
                row.Background = OneUiTheme.CreateRippleRoundedDrawable(Context, Color.Transparent, 14f);
            }
            OneUiTheme.AttachRecoil(row, 0.98f);

            View radio = new(Context) {
                Background = isSel
                    ? OneUiTheme.CreateRoundedDrawable(Context, OneUiTheme.Primary, 99f, OneUiTheme.TextPrimary, 2.5f)
                    : OneUiTheme.CreateRoundedDrawable(Context, Color.Transparent, 99f, OneUiTheme.TextMuted, 1.5f)
            };

            TextView tv = new(Context) {
                Text = items[i],
                TextSize = 13f
            };
            tv.SetIncludeFontPadding(false);
            tv.SetSingleLine(true);
            tv.Ellipsize = TextUtils.TruncateAt.End;
            tv.SetTextColor(isSel ? OneUiTheme.TextPrimary : OneUiTheme.TextSecondary);
            tv.Typeface = isSel ? OneUiTheme.GetAppleFontBold(Context) : OneUiTheme.GetAppleFontRegular(Context);

            row.AddView(radio, new LayoutParams(dp(16), dp(16)) { RightMargin = dp(10) });
            row.AddView(tv, new LayoutParams(0, ViewGroup.LayoutParams.WrapContent, 1f));
            if (isSel) {
                OneUiIconView check = new(Context, OneUiIconType.Check, OneUiTheme.PrimaryHigh);
                row.AddView(check, new LayoutParams(dp(16), dp(16)) { LeftMargin = dp(6) });
            }

            row.Click += (_, _) => {
                SetExpanded(false);
                if (selectedIndex != idx) {
                    SetSelection(idx, notify: true);
                }
            };

            optionsPanel.AddView(row, new LayoutParams(ViewGroup.LayoutParams.MatchParent, ViewGroup.LayoutParams.WrapContent) {
                BottomMargin = i < items.Count - 1 ? dp(2) : 0
            });
        }
    }
}

/// <summary>
/// Full One UI 9 frosted-glass modal dialog replacing all unstyled Android AlertDialogs.
/// Softly blurs the live background window on API 31+ and uses smoked frosted surface opacity
/// so foreground controls stay crisp and never visually clash with content behind the dialog.
/// </summary>
public static class OneUiModalDialog {
    sealed class HelpActionState {
        public FrameLayout Button;
        public Action Handler;
    }

    static readonly HashSet<Dialog> activeBlurredDialogs = [];
    static readonly List<WeakReference<Dialog>> activeDialogs = [];
    static readonly object activeDialogsLock = new();
    static readonly ConditionalWeakTable<Dialog, HelpActionState> helpStates = new();

    public static IReadOnlyList<Dialog> GetActiveDialogs() {
        List<Dialog> live = [];
        lock (activeDialogsLock) {
            for (int i = activeDialogs.Count - 1; i >= 0; i--) {
                if (activeDialogs[i].TryGetTarget(out Dialog d) && d != null && d.IsShowing) {
                    live.Add(d);
                } else {
                    activeDialogs.RemoveAt(i);
                }
            }
        }
        return live;
    }

    public static int DismissAllActiveDialogs() {
        IReadOnlyList<Dialog> live = GetActiveDialogs();
        int dismissed = 0;
        for (int i = 0; i < live.Count; i++) {
            try {
                live[i].Dismiss();
                dismissed++;
            } catch {
                // Ignore already-detached dialog
            }
        }
        return dismissed;
    }

    static void RegisterActiveDialog(Dialog dialog) {
        lock (activeDialogsLock) {
            for (int i = activeDialogs.Count - 1; i >= 0; i--) {
                if (!activeDialogs[i].TryGetTarget(out Dialog existing) || existing == null || ReferenceEquals(existing, dialog) || !existing.IsShowing) {
                    activeDialogs.RemoveAt(i);
                }
            }
            activeDialogs.Add(new WeakReference<Dialog>(dialog));
        }
    }

    static void UnregisterActiveDialog(Dialog dialog) {
        lock (activeDialogsLock) {
            for (int i = activeDialogs.Count - 1; i >= 0; i--) {
                if (!activeDialogs[i].TryGetTarget(out Dialog existing) || existing == null || ReferenceEquals(existing, dialog)) {
                    activeDialogs.RemoveAt(i);
                }
            }
        }
    }

    public static void SetHelpAction(this Dialog dialog, Action onHelp, string contentDesc = null) {
        if (dialog == null || !helpStates.TryGetValue(dialog, out HelpActionState state) || state.Button == null) {
            return;
        }
        state.Handler = onHelp;
        state.Button.Visibility = onHelp != null ? ViewStates.Visible : ViewStates.Gone;
        if (!string.IsNullOrEmpty(contentDesc)) {
            state.Button.ContentDescription = contentDesc;
        }
    }

    public static Dialog Show(
        Context ctx,
        string title,
        string message = null,
        View customContent = null,
        OneUiIconType icon = OneUiIconType.Info,
        Color? iconBadgeColor = null,
        string positiveText = "OK",
        Action onPositive = null,
        string negativeText = null,
        Action onNegative = null,
        bool cancelable = true,
        bool monospaceBody = false,
        Action onHelp = null,
        string helpContentDesc = null) {
        Dialog dialog = new(ctx);
        dialog.RequestWindowFeature((int)WindowFeatures.NoTitle);
        dialog.SetCancelable(cancelable);

        int dp(float v) => OneUiTheme.Dp(ctx, v);

        LinearLayout card = new(ctx) { Orientation = Orientation.Vertical };
        card.Background = OneUiTheme.CreateGlassCardDrawable(ctx, OneUiTheme.SurfaceModal, 28f);
        card.Elevation = dp(20);
        card.SetPadding(dp(20), dp(18), dp(20), dp(16));

        // Header with crisp One UI 9 vector badge + optional '?' help trigger
        LinearLayout header = new(ctx) { Orientation = Orientation.Horizontal, BaselineAligned = false };
        header.SetGravity(GravityFlags.CenterVertical);
        header.SetPadding(0, 0, 0, dp(12));
        header.AddView(OneUiTheme.CreateVectorIconBadge(ctx, icon, iconBadgeColor ?? OneUiTheme.IconSpatial, 32));

        TextView titleTv = new(ctx) {
            Text = title ?? "",
            TextSize = 16.5f
        };
        titleTv.SetTextColor(OneUiTheme.TextPrimary);
        titleTv.Typeface = OneUiTheme.GetAppleFontBold(ctx);
        titleTv.SetPadding(dp(12), 0, dp(8), 0);
        header.AddView(titleTv, new LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WrapContent, 1f));

        FrameLayout helpBtn = new(ctx) {
            Visibility = onHelp != null ? ViewStates.Visible : ViewStates.Gone,
            ContentDescription = helpContentDesc ?? title ?? "Help"
        };
        helpBtn.Background = OneUiTheme.CreateGlassCardDrawable(ctx, OneUiTheme.SurfaceSubTile, OneUiTheme.PillCornerDp);
        OneUiTheme.AttachRecoil(helpBtn, 0.92f);
        OneUiIconView helpIco = new(ctx, OneUiIconType.HelpQuestion, OneUiTheme.TextSecondary);
        helpBtn.AddView(helpIco, new FrameLayout.LayoutParams(dp(16), dp(16)) { Gravity = GravityFlags.Center });
        HelpActionState helpState = new() { Button = helpBtn, Handler = onHelp };
        helpStates.AddOrUpdate(dialog, helpState);
        helpBtn.Click += (_, _) => helpState.Handler?.Invoke();
        header.AddView(helpBtn, new LinearLayout.LayoutParams(dp(34), dp(34)));

        card.AddView(header);

        ScrollView scroll = new(ctx) { VerticalScrollBarEnabled = true };
        LinearLayout bodyWrap = new(ctx) { Orientation = Orientation.Vertical };

        if (!string.IsNullOrEmpty(message)) {
            TextView msgTv = new(ctx) {
                Text = message,
                TextSize = monospaceBody ? 11.5f : 13.5f
            };
            msgTv.SetTextColor(OneUiTheme.TextSecondary);
            msgTv.SetLineSpacing(dp(2), 1.08f);
            if (monospaceBody) {
                msgTv.SetTypeface(Typeface.Monospace, TypefaceStyle.Normal);
                msgTv.SetTextIsSelectable(true);
            }
            bodyWrap.AddView(msgTv);
        }

        if (customContent != null) {
            bodyWrap.AddView(customContent);
        }

        scroll.AddView(bodyWrap);
        int maxBodyH = (int)((ctx.Resources?.DisplayMetrics?.HeightPixels ?? 1600) * 0.62f);
        card.AddView(scroll, new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MatchParent, ViewGroup.LayoutParams.WrapContent) {
            BottomMargin = dp(14)
        });
        // Cap max height cleanly after layout
        scroll.Post(() => {
            if (scroll.Height > maxBodyH) {
                scroll.LayoutParameters = new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MatchParent, maxBodyH) {
                    BottomMargin = dp(14)
                };
            }
        });

        bool hasNeg = !string.IsNullOrEmpty(negativeText);
        bool hasPos = !string.IsNullOrEmpty(positiveText);
        bool singleFooterBtn = hasNeg ^ hasPos;

        LinearLayout btnRow = new(ctx) {
            Orientation = Orientation.Horizontal,
            BaselineAligned = false
        };
        btnRow.SetGravity(singleFooterBtn
            ? GravityFlags.Center
            : (GravityFlags.CenterVertical | GravityFlags.End));

        if (hasNeg) {
            Button negBtn = MakeDialogButton(ctx, negativeText, primary: false, () => {
                dialog.Dismiss();
                onNegative?.Invoke();
            });
            if (singleFooterBtn) {
                negBtn.SetMinimumWidth(dp(148));
                negBtn.SetMaxWidth(dp(240));
                negBtn.SetPadding(dp(24), dp(10), dp(24), dp(10));
                btnRow.AddView(negBtn, new LinearLayout.LayoutParams(ViewGroup.LayoutParams.WrapContent, ViewGroup.LayoutParams.WrapContent));
            } else {
                btnRow.AddView(negBtn, new LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WrapContent, 1f) {
                    RightMargin = dp(8)
                });
            }
        }

        if (hasPos) {
            Button posBtn = MakeDialogButton(ctx, positiveText, primary: true, () => {
                dialog.Dismiss();
                onPositive?.Invoke();
            });
            if (singleFooterBtn) {
                posBtn.SetMinimumWidth(dp(148));
                posBtn.SetMaxWidth(dp(240));
                posBtn.SetPadding(dp(24), dp(10), dp(24), dp(10));
                btnRow.AddView(posBtn, new LinearLayout.LayoutParams(ViewGroup.LayoutParams.WrapContent, ViewGroup.LayoutParams.WrapContent));
            } else {
                btnRow.AddView(posBtn, new LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WrapContent, 1f));
            }
        }

        card.AddView(btnRow);

        FrameLayout frame = new(ctx);
        frame.SetPadding(dp(16), dp(24), dp(16), dp(24));
        frame.AddView(card, new FrameLayout.LayoutParams(ViewGroup.LayoutParams.MatchParent, ViewGroup.LayoutParams.WrapContent));

        dialog.SetContentView(frame);
        if (dialog.Window != null) {
            dialog.Window.SetBackgroundDrawable(new ColorDrawable(Color.Transparent));
            dialog.Window.SetDimAmount(0.68f);
            dialog.Window.SetLayout(ViewGroup.LayoutParams.MatchParent, ViewGroup.LayoutParams.WrapContent);
            dialog.Window.SetGravity(GravityFlags.Center);
        }

        View bgRoot = (ctx as Activity)?.Window?.DecorView;
        if (bgRoot != null && OperatingSystem.IsAndroidVersionAtLeast(31)) {
            activeBlurredDialogs.RemoveWhere(d => d == null || !d.IsShowing);
            bool wasEmpty = activeBlurredDialogs.Count == 0;
            activeBlurredDialogs.Add(dialog);
            if (wasEmpty) {
                try {
                    bgRoot.SetRenderEffect(RenderEffect.CreateBlurEffect(28f, 28f, Shader.TileMode.Clamp));
                } catch {
                    // Ignore on unsupported window surfaces
                }
            }

            bool released = false;
            void ReleaseDialogBlur() {
                if (released) {
                    return;
                }
                released = true;
                activeBlurredDialogs.Remove(dialog);
                activeBlurredDialogs.RemoveWhere(d => d == null || !d.IsShowing);
                if (activeBlurredDialogs.Count == 0 && OperatingSystem.IsAndroidVersionAtLeast(31)) {
                    try {
                        bgRoot.SetRenderEffect(null);
                    } catch {
                        // Ignore teardown race
                    }
                }
            }

            dialog.DismissEvent += (_, _) => ReleaseDialogBlur();
            dialog.CancelEvent += (_, _) => ReleaseDialogBlur();
            frame.ViewDetachedFromWindow += (_, _) => ReleaseDialogBlur();
        }

        RegisterActiveDialog(dialog);
        dialog.DismissEvent += (_, _) => UnregisterActiveDialog(dialog);
        dialog.CancelEvent += (_, _) => UnregisterActiveDialog(dialog);
        frame.ViewDetachedFromWindow += (_, _) => UnregisterActiveDialog(dialog);

        dialog.Show();
        card.Alpha = 0f;
        card.ScaleX = 0.94f;
        card.ScaleY = 0.94f;
        card.Animate()
            .Alpha(1f)
            .ScaleX(1f)
            .ScaleY(1f)
            .SetDuration(210)
            .SetInterpolator(new Android.Views.Animations.DecelerateInterpolator(1.6f))
            .Start();
        return dialog;
    }

    static Button MakeDialogButton(Context ctx, string text, bool primary, Action onClick) {
        int dp(float v) => OneUiTheme.Dp(ctx, v);
        Button b = new(ctx) { Text = text, TextSize = 13f };
        b.SetAllCaps(false);
        b.SetSingleLine(true);
        b.Ellipsize = Android.Text.TextUtils.TruncateAt.End;
        b.SetIncludeFontPadding(false);
        b.SetMinimumHeight(dp(42));
        b.SetPadding(dp(14), dp(8), dp(14), dp(8));
        b.Background = primary
            ? OneUiTheme.CreateRippleRoundedDrawable(ctx, OneUiTheme.Primary, OneUiTheme.PillCornerDp)
            : OneUiTheme.CreateGlassCardDrawable(ctx, OneUiTheme.SurfaceModalRow, OneUiTheme.PillCornerDp);
        b.SetTextColor(primary ? OneUiTheme.OnPrimary : OneUiTheme.TextPrimary);
        b.Typeface = primary ? OneUiTheme.GetAppleFontBold(ctx) : OneUiTheme.GetAppleFontMedium(ctx);
        OneUiTheme.AttachRecoil(b);
        b.Click += (_, _) => onClick?.Invoke();
        return b;
    }
}
