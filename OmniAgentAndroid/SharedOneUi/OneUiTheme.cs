using System.Text.Json;

using Android.Content;
using Android.Content.Res;
using Android.Graphics;
using Android.Graphics.Drawables;
using Android.OS;
using Android.Text;
using Android.Views;
using Android.Widget;

namespace CavernizeAndroid.Theme;

/// <summary>
/// True Samsung One UI 9 (Galaxy S25 Ultra / SESL9) design system:
/// dynamically adapts to the device's One UI Wallpaper Color Palette (Monet API 31+) with genuine
/// Samsung Blue fallback, blurred atmospheric backdrop, frosted glass specular rims,
/// Samsung Recoil press physics, vector icon badges, and 44dp fat capsule sliders with double-tap reset.
/// </summary>
public static class OneUiTheme {
    static Typeface appleFontBold;
    static Typeface appleFontSemibold;
    static Typeface appleFontMedium;
    static Typeface appleFontRegular;

    /// <summary>
    /// Returns Apple Music's native SF Pro Android UI variable typeface tuned to the requested weight.
    /// </summary>
    public static Typeface GetAppleFontBold(Context ctx) =>
        appleFontBold ??= LoadVariableAppleFont(ctx, 700, TypefaceStyle.Bold);

    public static Typeface GetAppleFontSemibold(Context ctx) =>
        appleFontSemibold ??= LoadVariableAppleFont(ctx, 600, TypefaceStyle.Bold);

    public static Typeface GetAppleFontMedium(Context ctx) =>
        appleFontMedium ??= LoadVariableAppleFont(ctx, 500, TypefaceStyle.Normal);

    public static Typeface GetAppleFontRegular(Context ctx) =>
        appleFontRegular ??= LoadVariableAppleFont(ctx, 400, TypefaceStyle.Normal);

    /// <summary>
    /// Applies the unified SF Pro headline/UI typeface instead of raw Roboto Bold.
    /// </summary>
    public static void ApplyHeadlineFont(Context ctx, TextView tv, bool bold = true) {
        if (tv == null) {
            return;
        }
        tv.Typeface = bold ? GetAppleFontBold(ctx) : GetAppleFontMedium(ctx);
    }

    static Typeface LoadVariableAppleFont(Context ctx, int weight, TypefaceStyle fallbackStyle) {
        try {
            if (ctx?.Assets != null && OperatingSystem.IsAndroidVersionAtLeast(26)) {
                Typeface.Builder builder = new(ctx.Assets, "fonts/sf_pro_android_ui.ttf");
                builder.SetWeight(weight);
                builder.SetFontVariationSettings($"'wght' {weight}");
                Typeface tf = builder.Build();
                if (tf != null) {
                    return tf;
                }
            }
        } catch {
            // Fallback to CreateFromAsset below
        }
        try {
            if (ctx?.Assets != null) {
                Typeface assetTf = Typeface.CreateFromAsset(ctx.Assets, "fonts/sf_pro_android_ui.ttf");
                if (assetTf != null) {
                    return Typeface.Create(assetTf, fallbackStyle);
                }
            }
        } catch {
            // Fallback to system default
        }
        return Typeface.Create(Typeface.Default, fallbackStyle);
    }

    // Dynamic Atmospheric Dark + Frosted Glass Palette (refreshed in Initialize)
    public static Color BgScreen { get; private set; } = Color.ParseColor("#080A10");
    public static Color SurfaceCard { get; private set; } = Color.ParseColor("#B816171F");
    public static Color SurfaceElevated { get; private set; } = Color.ParseColor("#CE20212B");
    public static Color SurfaceModal { get; private set; } = Color.ParseColor("#EC1D1E27");
    public static Color SurfaceModalRow { get; private set; } = Color.ParseColor("#E02A2C38");
    public static Color SurfaceFloating { get; private set; } = Color.ParseColor("#36FFFFFF");
    public static Color SurfaceSubTile { get; private set; } = Color.ParseColor("#24FFFFFF");
    public static Color SurfaceBlurTint { get; private set; } = Color.ParseColor("#3A3A44");
    public static Color ControlStroke { get; private set; } = Color.ParseColor("#5A5B66");
    public static Color BorderSubtle { get; private set; } = Color.ParseColor("#22FFFFFF");

    // Specular Glass Borders
    public static Color GlassRimTop { get; private set; } = Color.ParseColor("#48FFFFFF");
    public static Color GlassRimMid { get; private set; } = Color.ParseColor("#14FFFFFF");
    public static Color GlassRimBottom { get; private set; } = Color.ParseColor("#2BFFFFFF");
    public static Color FloatingBorder { get; private set; } = Color.ParseColor("#26FFFFFF");

    // Dynamic Brand & Wallpaper Accent Tokens (resolved verbatim from framework-res wallpapertheme_template.json on API 31+)
    public static Color Primary { get; private set; } = Color.ParseColor("#387AFF");
    public static Color PrimaryHigh { get; private set; } = Color.ParseColor("#598FFF");
    public static Color PrimaryPressed { get; private set; } = Color.ParseColor("#2E65D4");
    public static Color Secondary { get; private set; } = Color.ParseColor("#6E7CF6");
    public static Color SecondaryHigh { get; private set; } = Color.ParseColor("#8995FA");
    public static Color SecondaryPressed { get; private set; } = Color.ParseColor("#4F5CE0");
    public static Color HighlightAccent { get; private set; } = Color.ParseColor("#8E96FA");
    public static Color Tertiary { get; private set; } = Color.ParseColor("#6E9CFF");
    public static Color TertiaryHigh { get; private set; } = Color.ParseColor("#89B0FF");
    public static Color TertiaryPressed { get; private set; } = Color.ParseColor("#4B7BE5");
    public static Color SliderAccent { get; private set; } = Color.ParseColor("#6E9CFF");
    public static Color SliderAccentHigh => SliderAccent;
    public static Color SliderAccentEnd => SliderAccent;
    public static Color CoralStart { get; private set; } = Color.ParseColor("#8E96FA");
    public static Color CoralEnd { get; private set; } = Color.ParseColor("#6E9CFF");
    public static Color PeachText { get; private set; } = Color.ParseColor("#C2D8FF");

    // Samsung Gallery Bottom Bar & Floating Tab Capsule Tokens
    public static Color SurfaceFloatingBar { get; private set; } = Color.Argb(230, 58, 58, 61);
    public static Color SelectedTabPill { get; private set; } = Color.Argb(26, 255, 255, 255);

    // 6 Harmonic Semantic Icon Palette Tokens (wallpaper-adaptive so icons share tonal unity yet stay visually distinct)
    public static Color IconSpatial { get; private set; } = Color.ParseColor("#387AFF");
    public static Color IconSliderMedia { get; private set; } = Color.ParseColor("#6E9CFF");
    public static Color IconAtmosUpmix { get; private set; } = Color.ParseColor("#8E96FA");
    public static Color IconStudioExport { get; private set; } = Color.ParseColor("#4FB5E8");
    public static Color IconStemsCalib { get; private set; } = Color.ParseColor("#56B8A8");
    public static Color IconSystemInfo { get; private set; } = Color.ParseColor("#8A97B8");

    public static Color TextPrimary { get; private set; } = Color.ParseColor("#FCFCFF");
    public static Color TextSecondary { get; private set; } = Color.ParseColor("#E9E9EC");
    public static Color TextSub { get; private set; } = Color.ParseColor("#B7B7BB");
    public static Color TextMuted { get; private set; } = Color.ParseColor("#99999E");
    public static Color Divider { get; private set; } = Color.ParseColor("#26FFFFFF");

    // Functional Semantic Colors (harmonized to avoid clashing while staying clear)
    public static readonly Color FunctionalGreen = Color.ParseColor("#34D399");
    public static readonly Color FunctionalOrange = Color.ParseColor("#F97316");
    public static readonly Color FunctionalIndigo = Color.ParseColor("#7C6CF6");
    public static readonly Color FunctionalAmber = Color.ParseColor("#F59E0B");
    public static readonly Color FunctionalCyan = Color.ParseColor("#38BDF8");
    public static readonly Color FunctionalRed = Color.ParseColor("#F56962");
    public static readonly Color FunctionalRedFill = Color.ParseColor("#D93E36");

    // Room Canvas & Spatial Source Tokens
    public static Color RoomBg { get; private set; } = Color.ParseColor("#99101219");
    public static Color BedChannel { get; private set; } = Color.ParseColor("#598FFF");
    public static readonly Color LfeChannel = Color.ParseColor("#A855F7");
    public static readonly Color DynamicObject = Color.ParseColor("#F56962");
    public static readonly Color SoloGold = Color.ParseColor("#FACC15");

    public static bool IsDynamicWallpaperActive { get; private set; }
    public static string ActiveDesignTokensSource { get; private set; } = "local";
    public const int DesignKitSchemaVersion = 1;

    // SESL9 Dimensions (dp)
    public const float CardCornerDp = 26f;
    public const float ButtonCornerDp = 18f;
    public const float TileCornerDp = 22f;
    public const float PillCornerDp = 999f;

    public static float EffectiveCardCornerDp { get; private set; } = CardCornerDp;
    public static float EffectiveButtonCornerDp { get; private set; } = ButtonCornerDp;
    public static float EffectiveTileCornerDp { get; private set; } = TileCornerDp;

    /// <summary>
    /// Reads Samsung One UI / Android Material You dynamic wallpaper colors from <paramref name="ctx"/>
    /// (API 31+) with clean Samsung One UI 9 Blue as fallback.
    /// </summary>
    public static void Initialize(Context ctx) {
        Color defaultBlue = Color.ParseColor("#387AFF");
        Color defaultBlueHigh = Color.ParseColor("#598FFF");
        Color defaultBluePressed = Color.ParseColor("#2E65D4");
        Color defaultSec = Color.ParseColor("#6E7CF6");
        Color defaultSecHigh = Color.ParseColor("#8995FA");
        Color defaultSecPressed = Color.ParseColor("#4F5CE0");
        Color defaultTert = Color.ParseColor("#6E9CFF");
        Color defaultTertHigh = Color.ParseColor("#89B0FF");
        Color defaultTertPressed = Color.ParseColor("#4B7BE5");
        Color defaultHighlight = Color.ParseColor("#8E96FA");
        Color defaultHeaderTint = Color.ParseColor("#D6E4FF");
        // Native One UI 9 Dark Mode neutral frosted surfaces (harmonized with ambient cover glow)
        Color defaultBaseBg = Color.Rgb(8, 8, 12);
        Color defaultCard = Color.Argb(186, 21, 22, 28);
        Color defaultElevated = Color.Argb(208, 30, 31, 39);
        Color defaultRoomBg = Color.Argb(172, 12, 14, 20);

        Primary = defaultBlue;
        PrimaryHigh = defaultBlueHigh;
        PrimaryPressed = defaultBluePressed;
        Secondary = defaultSec;
        SecondaryHigh = defaultSecHigh;
        SecondaryPressed = defaultSecPressed;
        HighlightAccent = defaultHighlight;
        SliderAccent = defaultTert;
        Tertiary = defaultTert;
        TertiaryHigh = defaultTertHigh;
        TertiaryPressed = defaultTertPressed;
        CoralStart = defaultHighlight;
        CoralEnd = defaultTert;
        PeachText = defaultHeaderTint;
        BedChannel = defaultBlueHigh;
        BgScreen = defaultBaseBg;
        SurfaceCard = defaultCard;
        SurfaceElevated = defaultElevated;
        SurfaceModal = Color.Argb(236, 28, 29, 38);
        SurfaceModalRow = Color.Argb(224, 40, 42, 54);
        RoomBg = defaultRoomBg;
        SurfaceFloatingBar = Color.Argb(230, 58, 58, 61);
        SelectedTabPill = Color.Argb(26, 255, 255, 255);
        IconSpatial = defaultBlue;
        IconSliderMedia = defaultTert;
        IconAtmosUpmix = defaultHighlight;
        IconStudioExport = Color.ParseColor("#4FB5E8");
        IconStemsCalib = Color.ParseColor("#56B8A8");
        IconSystemInfo = Color.ParseColor("#8A97B8");
        IsDynamicWallpaperActive = false;

        if (ctx == null || !OperatingSystem.IsAndroidVersionAtLeast(31)) {
            return;
        }

        try {
            // Exact Samsung One UI 9 ColorPalette template mappings (wallpapertheme_template.json + SystemUI metadata_colorpalette.xml):
            // - quickpanel_btn_on (4-2-3) & primary -> system_accent1_300
            // - quickpanel_progress (4-8-5 / tw_progress_color_control_activated) -> system_accent2_300
            // - secondary / single_track -> system_accent2_500
            // - highlight -> system_accent3_300
            Color? sysA1_200 = TryGetFrameworkColor(ctx, "system_accent1_200");
            Color? sysA1_300 = TryGetFrameworkColor(ctx, "system_accent1_300");
            Color? sysA1_400 = TryGetFrameworkColor(ctx, "system_accent1_400") ?? TryGetFrameworkColor(ctx, "system_primary_dark");
            Color? sysA1_500 = TryGetFrameworkColor(ctx, "system_accent1_500");

            Color? sysA2_200 = TryGetFrameworkColor(ctx, "system_accent2_200");
            Color? sysA2_300 = TryGetFrameworkColor(ctx, "system_accent2_300") ?? TryGetFrameworkColor(ctx, "system_secondary_dark");
            Color? sysA2_400 = TryGetFrameworkColor(ctx, "system_accent2_400");
            Color? sysA2_500 = TryGetFrameworkColor(ctx, "system_accent2_500");

            Color? sysA3_200 = TryGetFrameworkColor(ctx, "system_accent3_200");
            Color? sysA3_300 = TryGetFrameworkColor(ctx, "system_accent3_300") ?? TryGetFrameworkColor(ctx, "system_tertiary_dark");
            Color? sysA3_400 = TryGetFrameworkColor(ctx, "system_accent3_400");
            Color? sysA3_500 = TryGetFrameworkColor(ctx, "system_accent3_500");

            Color? rawPrimary = sysA1_300 ?? sysA1_400;
            if (rawPrimary.HasValue && IsChromatic(rawPrimary.Value)) {
                IsDynamicWallpaperActive = true;

                // Preserve exact system_accent1_300 & system_accent2_300 without mutating hue or saturation
                Primary = rawPrimary.Value;
                PrimaryHigh = sysA1_200 ?? AdjustBrightness(Primary, 1.14f);
                PrimaryPressed = sysA1_400 ?? sysA1_500 ?? AdjustBrightness(Primary, 0.82f);

                Color exactQuickPanelSlider = sysA2_300 ?? sysA2_400 ?? sysA3_300 ?? Primary;
                SliderAccent = exactQuickPanelSlider;
                Tertiary = exactQuickPanelSlider;
                TertiaryHigh = sysA2_200 ?? AdjustBrightness(exactQuickPanelSlider, 1.10f);
                TertiaryPressed = sysA2_400 ?? AdjustBrightness(exactQuickPanelSlider, 0.84f);

                HighlightAccent = sysA3_300 ?? sysA3_400 ?? exactQuickPanelSlider;
                Secondary = sysA2_500 ?? sysA2_400 ?? exactQuickPanelSlider;
                SecondaryHigh = sysA2_400 ?? exactQuickPanelSlider;
                SecondaryPressed = sysA2_500 ?? AdjustBrightness(Secondary, 0.80f);

                CoralStart = HighlightAccent;
                CoralEnd = exactQuickPanelSlider;

                Color tintSrc = sysA2_200 ?? sysA1_200 ?? PrimaryHigh;
                PeachText = Blend(Color.Rgb(242, 243, 249), tintSrc, 0.36f);
                BedChannel = Blend(PrimaryHigh, Color.Rgb(108, 178, 255), 0.28f);

                // Harmonized 6-role semantic icon colors (share the wallpaper palette's saturation/luminance envelope)
                IconSpatial = Primary;
                IconSliderMedia = exactQuickPanelSlider;
                IconAtmosUpmix = IsChromatic(HighlightAccent) && HueDistance(Primary, HighlightAccent) >= 10f
                    ? HighlightAccent
                    : HarmonizeIconVariant(Primary, +28f);
                IconStudioExport = HarmonizeIconVariant(Primary, +44f);
                IconStemsCalib = HarmonizeIconVariant(exactQuickPanelSlider, -56f);
                IconSystemInfo = sysA2_400 ?? Blend(exactQuickPanelSlider, Color.Rgb(185, 192, 208), 0.35f);

                // Subtle 3% accent whisper on elevated frosted cards; ambient cover art flows cleanly behind
                SurfaceCard = WithAlpha(Blend(Color.Rgb(21, 22, 28), Primary, 0.03f), 188);
                SurfaceElevated = WithAlpha(Blend(Color.Rgb(30, 31, 39), exactQuickPanelSlider, 0.04f), 210);
                SurfaceModal = WithAlpha(Blend(Color.Rgb(26, 27, 36), exactQuickPanelSlider, 0.04f), 236);
                SurfaceModalRow = WithAlpha(Blend(Color.Rgb(38, 40, 52), exactQuickPanelSlider, 0.04f), 226);
            }
        } catch {
            // Fall back cleanly to Samsung One UI 9 Blue palette
        }
    }

    static Color HarmonizeIconVariant(Color referenceAccent, float hueShiftDeg) {
        float[] hsv = new float[3];
        Color.ColorToHSV(referenceAccent, hsv);
        hsv[0] = (hsv[0] + hueShiftDeg + 360f) % 360f;
        hsv[1] = Math.Clamp(hsv[1] * 0.92f, 0.32f, 0.72f);
        hsv[2] = Math.Clamp(hsv[2] * 0.98f, 0.68f, 0.92f);
        return new Color(Color.HSVToColor(255, hsv));
    }

    static Color? TryGetFrameworkColor(Context ctx, string name) {
        int id = ctx.Resources?.GetIdentifier(name, "color", "android") ?? 0;
        if (id == 0) {
            return null;
        }
        int argb = ctx.GetColor(id);
        return new Color(argb);
    }

    static bool IsChromatic(Color c) {
        float[] hsv = new float[3];
        Color.ColorToHSV(c, hsv);
        return hsv[1] >= 0.08f && hsv[2] >= 0.15f;
    }

    static float HueDistance(Color a, Color b) {
        float[] ha = new float[3];
        float[] hb = new float[3];
        Color.ColorToHSV(a, ha);
        Color.ColorToHSV(b, hb);
        float diff = Math.Abs(ha[0] - hb[0]);
        return Math.Min(diff, 360f - diff);
    }

    static Color AdjustBrightness(Color c, float factor) {
        float[] hsv = new float[3];
        Color.ColorToHSV(c, hsv);
        hsv[2] = Math.Clamp(hsv[2] * factor, 0f, 1f);
        if (factor > 1.05f) {
            hsv[1] = Math.Clamp(hsv[1] * 0.82f, 0f, 1f);
        }
        int rgb = Color.HSVToColor(c.A, hsv);
        return new Color(rgb);
    }

    static Color Blend(Color a, Color b, float t) {
        t = Math.Clamp(t, 0f, 1f);
        return Color.Argb(
            (int)Math.Round(a.A + (b.A - a.A) * t),
            (int)Math.Round(a.R + (b.R - a.R) * t),
            (int)Math.Round(a.G + (b.G - a.G) * t),
            (int)Math.Round(a.B + (b.B - a.B) * t));
    }

    public static Color WithAlpha(Color c, byte alpha) => Color.Argb(alpha, c.R, c.G, c.B);

    public static int Dp(Context ctx, float dp) =>
        (int)Math.Round(dp * ctx.Resources.DisplayMetrics.Density);

    public static Drawable CreateGlassCardDrawable(
        Context ctx,
        Color? tintOverride = null,
        float cornerDp = CardCornerDp,
        Color? borderAccent = null) {
        return new OneUiGlassDrawable(
            ctx,
            tintOverride ?? SurfaceCard,
            Dp(ctx, cornerDp),
            borderAccent);
    }

    public static Color BlendColor(Color a, Color b, float t) => Blend(a, b, t);

    public static GradientDrawable CreateRoundedDrawable(
        Context ctx,
        Color fillColor,
        float cornerDp = CardCornerDp,
        Color? strokeColor = null,
        float strokeDp = 0f) {
        GradientDrawable d = new();
        d.SetShape(ShapeType.Rectangle);
        d.SetColor(fillColor.ToArgb());
        d.SetCornerRadius(Dp(ctx, cornerDp));
        if (strokeColor.HasValue && strokeDp > 0f) {
            d.SetStroke(Math.Max(1, Dp(ctx, strokeDp)), ColorStateList.ValueOf(strokeColor.Value));
        }
        return d;
    }

    public static Drawable CreateRippleRoundedDrawable(
        Context ctx,
        Color fillColor,
        float cornerDp = ButtonCornerDp,
        Color? strokeColor = null,
        float strokeDp = 0f) {
        GradientDrawable content = CreateRoundedDrawable(ctx, fillColor, cornerDp, strokeColor, strokeDp);
        GradientDrawable mask = CreateRoundedDrawable(ctx, Color.White, cornerDp);
        Color ripple = Color.ParseColor("#2EFFFFFF");
        return new RippleDrawable(ColorStateList.ValueOf(ripple), content, mask);
    }

    /// <summary>
    /// Creates a floating frosted glass capsule with diagonal specular edge sheen (One UI 9 Media/Nav/Search bars).
    /// </summary>
    public static Drawable CreateFloatingGlassPill(Context ctx) {
        Color tint = WithAlpha(SurfaceElevated, 240);
        return new OneUiGlassDrawable(
            ctx,
            tint,
            Dp(ctx, PillCornerDp),
            null,
            isFloatingCapsule: true);
    }

    /// <summary>
    /// Applies Samsung SeslRecoil tactile press scale physics (100ms down, 350ms spring release).
    /// </summary>
    public static void AttachRecoil(View v, float pressedScale = 0.96f) {
        v.Touch += (_, e) => {
            e.Handled = false;
            switch (e.Event.ActionMasked) {
                case MotionEventActions.Down:
                    v.Animate().ScaleX(pressedScale).ScaleY(pressedScale).SetDuration(100).Start();
                    break;
                case MotionEventActions.Up:
                case MotionEventActions.Cancel:
                    v.Animate().ScaleX(1f).ScaleY(1f).SetDuration(350).Start();
                    break;
            }
        };
    }

    /// <summary>
    /// Creates a colored circular/squircle Samsung Settings-style crisp vector icon badge.
    /// </summary>
    public static Views.OneUiIconBadge CreateVectorIconBadge(
        Context ctx,
        Views.OneUiIconType iconType,
        Color badgeColor,
        int sizeDp = 32) {
        int px = Dp(ctx, sizeDp);
        Views.OneUiIconBadge badge = new(ctx, iconType, badgeColor) {
            LayoutParameters = new LinearLayout.LayoutParams(px, px)
        };
        return badge;
    }

    /// <summary>
    /// Applies One UI 9 pill progress bar styling (thick capsule track with dynamic gradient fill).
    /// </summary>
    public static void StyleProgressBar(Context ctx, ProgressBar bar) {
        int trackH = Dp(ctx, 14f);
        int radius = Dp(ctx, 99f);

        GradientDrawable bgTrack = new();
        bgTrack.SetShape(ShapeType.Rectangle);
        bgTrack.SetColor(Color.Argb(75, 255, 255, 255).ToArgb());
        bgTrack.SetCornerRadius(radius);

        GradientDrawable progressFill = new(
            GradientDrawable.Orientation.LeftRight,
            [Primary.ToArgb(), PrimaryHigh.ToArgb()]);
        progressFill.SetShape(ShapeType.Rectangle);
        progressFill.SetCornerRadius(radius);

        ClipDrawable progressClip = new(progressFill, GravityFlags.Left, ClipDrawableOrientation.Horizontal);
        LayerDrawable layers = new([bgTrack, progressClip]);
        layers.SetId(0, Android.Resource.Id.Background);
        layers.SetId(1, Android.Resource.Id.Progress);
        if (OperatingSystem.IsAndroidVersionAtLeast(23)) {
            layers.SetLayerHeight(0, trackH);
            layers.SetLayerHeight(1, trackH);
        }
        bar.ProgressDrawable = layers;
    }

    /// <summary>
    /// Applies SESL9 switch thumb/track colors using the active dynamic accent.
    /// </summary>
    public static void StyleSwitch(Switch sw) {
        int[][] states = [
            [Android.Resource.Attribute.StateChecked],
            [-Android.Resource.Attribute.StateChecked]
        ];
        sw.ThumbTintList = new ColorStateList(states, [
            TextPrimary.ToArgb(),
            TextPrimary.ToArgb()
        ]);
        sw.TrackTintList = new ColorStateList(states, [
            Primary.ToArgb(),
            Color.ParseColor("#52525E").ToArgb()
        ]);
        sw.SetTextColor(TextPrimary);
    }

    /// <summary>
    /// Adds a subtle translucent One UI grouped list divider.
    /// </summary>
    public static View CreateDivider(Context ctx) {
        View line = new(ctx);
        line.SetBackgroundColor(Divider);
        LinearLayout.LayoutParams lp = new(ViewGroup.LayoutParams.MatchParent, Math.Max(1, Dp(ctx, 0.75f))) {
            LeftMargin = Dp(ctx, 4f),
            RightMargin = Dp(ctx, 4f),
            TopMargin = Dp(ctx, 6f),
            BottomMargin = Dp(ctx, 6f)
        };
        line.LayoutParameters = lp;
        return line;
    }

    /// <summary>
    /// Creates an ellipsized single-line One UI 9 status badge chip.
    /// </summary>
    public static TextView CreateStatusChip(Context ctx, string text, Color accentColor) {
        TextView chip = new(ctx) {
            Text = text ?? "",
            TextSize = 11f,
            Gravity = GravityFlags.Center
        };
        chip.SetSingleLine(true);
        chip.Ellipsize = TextUtils.TruncateAt.End;
        chip.SetIncludeFontPadding(false);
        chip.Typeface = GetAppleFontBold(ctx);
        UpdateStatusChip(ctx, chip, text, accentColor);
        return chip;
    }

    /// <summary>
    /// Updates an existing One UI 9 status chip's text and accent tint.
    /// </summary>
    public static void UpdateStatusChip(Context ctx, TextView chip, string text, Color accentColor) {
        if (chip == null) {
            return;
        }
        chip.Text = text ?? "";
        chip.SetTextColor(BlendColor(accentColor, Color.White, 0.35f));
        chip.SetPadding(Dp(ctx, 10f), Dp(ctx, 4f), Dp(ctx, 10f), Dp(ctx, 4f));
        chip.Background = CreateRoundedDrawable(
            ctx,
            WithAlpha(accentColor, 48),
            PillCornerDp,
            WithAlpha(accentColor, 145),
            1f);
    }

    /// <summary>
    /// Creates a clean One UI 9 prerequisite / empty state banner card with an optional CTA pill button.
    /// </summary>
    public static LinearLayout CreateEmptyPrereqCard(
        Context ctx,
        string title,
        string subtitle,
        string ctaText = null,
        Action onCta = null) {
        LinearLayout card = new(ctx) {
            Orientation = Android.Widget.Orientation.Vertical
        };
        card.Background = CreateGlassCardDrawable(
            ctx,
            SurfaceSubTile,
            18f,
            WithAlpha(FunctionalAmber, 110));
        card.SetPadding(Dp(ctx, 14f), Dp(ctx, 12f), Dp(ctx, 14f), Dp(ctx, 12f));

        if (!string.IsNullOrEmpty(title)) {
            TextView titleTv = new(ctx) {
                Text = title,
                TextSize = 13f
            };
            titleTv.SetTextColor(TextPrimary);
            titleTv.Typeface = GetAppleFontBold(ctx);
            titleTv.SetIncludeFontPadding(false);
            card.AddView(titleTv);
        }

        if (!string.IsNullOrEmpty(subtitle)) {
            TextView subTv = new(ctx) {
                Text = subtitle,
                TextSize = 11.8f
            };
            subTv.SetTextColor(TextSub);
            subTv.Typeface = GetAppleFontRegular(ctx);
            subTv.SetLineSpacing(Dp(ctx, 1.5f), 1.06f);
            subTv.SetPadding(0, Dp(ctx, string.IsNullOrEmpty(title) ? 0f : 4f), 0, 0);
            card.AddView(subTv);
        }

        if (!string.IsNullOrEmpty(ctaText)) {
            Button ctaBtn = new(ctx) {
                Text = ctaText,
                TextSize = 12.2f
            };
            ctaBtn.SetAllCaps(false);
            ctaBtn.SetSingleLine(true);
            ctaBtn.Ellipsize = TextUtils.TruncateAt.End;
            ctaBtn.SetIncludeFontPadding(false);
            ctaBtn.SetTextColor(TextPrimary);
            ctaBtn.Typeface = GetAppleFontBold(ctx);
            ctaBtn.SetMinimumHeight(Dp(ctx, 38f));
            ctaBtn.SetPadding(Dp(ctx, 16f), Dp(ctx, 7f), Dp(ctx, 16f), Dp(ctx, 7f));
            ctaBtn.Background = CreateRippleRoundedDrawable(ctx, Primary, PillCornerDp);
            AttachRecoil(ctaBtn, 0.96f);
            ctaBtn.Click += (_, _) => onCta?.Invoke();
            card.AddView(ctaBtn, new LinearLayout.LayoutParams(ViewGroup.LayoutParams.WrapContent, ViewGroup.LayoutParams.WrapContent) {
                TopMargin = Dp(ctx, 10f)
            });
        }

        return card;
    }

    /// <summary>
    /// Creates a One UI 9 6-Category Settings Hub tile with squircle vector icon badge, bold title,
    /// live ellipsized status subtitle, and drill-down chevron.
    /// </summary>
    public static LinearLayout CreateCategoryHubTile(
        Context ctx,
        Views.OneUiIconType icon,
        Color iconColor,
        string title,
        string liveSubtitle,
        Action onClick,
        out TextView subtitleView) {
        LinearLayout tile = new(ctx) {
            Orientation = Android.Widget.Orientation.Horizontal,
            BaselineAligned = false
        };
        tile.SetGravity(GravityFlags.CenterVertical);
        tile.SetMinimumHeight(Dp(ctx, 68f));
        tile.SetPadding(Dp(ctx, 14f), Dp(ctx, 12f), Dp(ctx, 14f), Dp(ctx, 12f));
        tile.Background = CreateGlassCardDrawable(ctx, SurfaceCard, TileCornerDp);
        AttachRecoil(tile, 0.97f);

        Views.OneUiIconBadge badge = CreateVectorIconBadge(ctx, icon, iconColor, 38);
        tile.AddView(badge, new LinearLayout.LayoutParams(Dp(ctx, 38f), Dp(ctx, 38f)) {
            RightMargin = Dp(ctx, 12f)
        });

        LinearLayout textCol = new(ctx) {
            Orientation = Android.Widget.Orientation.Vertical
        };
        TextView titleTv = new(ctx) {
            Text = title ?? "",
            TextSize = 14.5f
        };
        titleTv.SetTextColor(TextPrimary);
        titleTv.Typeface = GetAppleFontBold(ctx);
        titleTv.SetIncludeFontPadding(false);
        titleTv.SetSingleLine(true);
        titleTv.Ellipsize = TextUtils.TruncateAt.End;

        subtitleView = new TextView(ctx) {
            Text = liveSubtitle ?? "",
            TextSize = 11.8f
        };
        subtitleView.SetTextColor(TextSub);
        subtitleView.Typeface = GetAppleFontMedium(ctx);
        subtitleView.SetIncludeFontPadding(false);
        subtitleView.SetSingleLine(true);
        subtitleView.Ellipsize = TextUtils.TruncateAt.End;
        subtitleView.SetPadding(0, Dp(ctx, 2.5f), 0, 0);

        textCol.AddView(titleTv);
        textCol.AddView(subtitleView);
        tile.AddView(textCol, new LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WrapContent, 1f) {
            RightMargin = Dp(ctx, 8f)
        });

        Views.OneUiIconView chevron = new(ctx, Views.OneUiIconType.ChevronRight, TextMuted);
        tile.AddView(chevron, new LinearLayout.LayoutParams(Dp(ctx, 16f), Dp(ctx, 16f)));

        tile.Click += (_, _) => onClick?.Invoke();
        return tile;
    }

    /// <summary>
    /// Serializes the active One UI 9 design tokens (palette, glass surfaces, semantic icons, corner radii)
    /// to compact JSON so companion apps (OmniAgent) can mirror Cavernize's live visual choices with zero quota.
    /// </summary>
    public static string ExportDesignTokensJson(string sourceTag = "cavernize-ipc") {
        return JsonSerializer.Serialize(new {
            schema = DesignKitSchemaVersion,
            source = sourceTag,
            dynamicWallpaper = IsDynamicWallpaperActive,
            cardCornerDp = EffectiveCardCornerDp,
            buttonCornerDp = EffectiveButtonCornerDp,
            tileCornerDp = EffectiveTileCornerDp,
            bgScreen = BgScreen.ToArgb(),
            surfaceCard = SurfaceCard.ToArgb(),
            surfaceElevated = SurfaceElevated.ToArgb(),
            surfaceModal = SurfaceModal.ToArgb(),
            surfaceModalRow = SurfaceModalRow.ToArgb(),
            surfaceFloatingBar = SurfaceFloatingBar.ToArgb(),
            glassRimTop = GlassRimTop.ToArgb(),
            glassRimMid = GlassRimMid.ToArgb(),
            glassRimBottom = GlassRimBottom.ToArgb(),
            primary = Primary.ToArgb(),
            primaryHigh = PrimaryHigh.ToArgb(),
            primaryPressed = PrimaryPressed.ToArgb(),
            secondary = Secondary.ToArgb(),
            secondaryHigh = SecondaryHigh.ToArgb(),
            tertiary = Tertiary.ToArgb(),
            highlightAccent = HighlightAccent.ToArgb(),
            peachText = PeachText.ToArgb(),
            iconSpatial = IconSpatial.ToArgb(),
            iconSliderMedia = IconSliderMedia.ToArgb(),
            iconAtmosUpmix = IconAtmosUpmix.ToArgb(),
            iconStudioExport = IconStudioExport.ToArgb(),
            iconStemsCalib = IconStemsCalib.ToArgb(),
            iconSystemInfo = IconSystemInfo.ToArgb(),
            utc = DateTime.UtcNow.ToString("O")
        });
    }

    /// <summary>
    /// Applies a JSON design-token snapshot exported by Cavernize so companion apps reflect
    /// Cavernize design adjustments immediately at runtime without rebuilding or spending quota.
    /// </summary>
    public static bool ApplyRuntimeDesignTokensJson(string json, string sourceLabel = "cavernize-ipc") {
        if (string.IsNullOrWhiteSpace(json)) {
            return false;
        }
        try {
            using JsonDocument doc = JsonDocument.Parse(json);
            JsonElement root = doc.RootElement;
            if (root.ValueKind != JsonValueKind.Object) {
                return false;
            }
            if (TryGetFloat(root, "cardCornerDp", out float cDp)) EffectiveCardCornerDp = Math.Clamp(cDp, 12f, 36f);
            if (TryGetFloat(root, "buttonCornerDp", out float bDp)) EffectiveButtonCornerDp = Math.Clamp(bDp, 10f, 32f);
            if (TryGetFloat(root, "tileCornerDp", out float tDp)) EffectiveTileCornerDp = Math.Clamp(tDp, 12f, 32f);
            if (TryGetArgb(root, "bgScreen", out Color c)) BgScreen = c;
            if (TryGetArgb(root, "surfaceCard", out c)) SurfaceCard = c;
            if (TryGetArgb(root, "surfaceElevated", out c)) SurfaceElevated = c;
            if (TryGetArgb(root, "surfaceModal", out c)) SurfaceModal = c;
            if (TryGetArgb(root, "surfaceModalRow", out c)) SurfaceModalRow = c;
            if (TryGetArgb(root, "surfaceFloatingBar", out c)) SurfaceFloatingBar = c;
            if (TryGetArgb(root, "glassRimTop", out c)) GlassRimTop = c;
            if (TryGetArgb(root, "glassRimMid", out c)) GlassRimMid = c;
            if (TryGetArgb(root, "glassRimBottom", out c)) GlassRimBottom = c;
            if (TryGetArgb(root, "primary", out c)) Primary = c;
            if (TryGetArgb(root, "primaryHigh", out c)) PrimaryHigh = c;
            if (TryGetArgb(root, "primaryPressed", out c)) PrimaryPressed = c;
            if (TryGetArgb(root, "secondary", out c)) Secondary = c;
            if (TryGetArgb(root, "secondaryHigh", out c)) SecondaryHigh = c;
            if (TryGetArgb(root, "tertiary", out c)) { Tertiary = c; SliderAccent = c; }
            if (TryGetArgb(root, "highlightAccent", out c)) HighlightAccent = c;
            if (TryGetArgb(root, "peachText", out c)) PeachText = c;
            if (TryGetArgb(root, "iconSpatial", out c)) IconSpatial = c;
            if (TryGetArgb(root, "iconSliderMedia", out c)) IconSliderMedia = c;
            if (TryGetArgb(root, "iconAtmosUpmix", out c)) IconAtmosUpmix = c;
            if (TryGetArgb(root, "iconStudioExport", out c)) IconStudioExport = c;
            if (TryGetArgb(root, "iconStemsCalib", out c)) IconStemsCalib = c;
            if (TryGetArgb(root, "iconSystemInfo", out c)) IconSystemInfo = c;
            ActiveDesignTokensSource = string.IsNullOrWhiteSpace(sourceLabel) ? "cavernize-ipc" : sourceLabel;
            return true;
        } catch {
            return false;
        }
    }

    static bool TryGetArgb(JsonElement obj, string name, out Color color) {
        color = default;
        if (obj.TryGetProperty(name, out JsonElement el) && el.ValueKind == JsonValueKind.Number && el.TryGetInt32(out int argb)) {
            color = new Color(argb);
            return true;
        }
        return false;
    }

    static bool TryGetFloat(JsonElement obj, string name, out float value) {
        value = 0f;
        if (obj.TryGetProperty(name, out JsonElement el) && el.ValueKind == JsonValueKind.Number && el.TryGetSingle(out float v)) {
            value = v;
            return true;
        }
        return false;
    }
}

/// <summary>
/// Multi-layered atmospheric depth &amp; RenderEffect-blurred radial backdrop inspired by One UI 9's Quick Panel and Settings,
/// reacting dynamically to the active wallpaper palette colors.
/// </summary>
public sealed class OneUiBackdropView : View {
    readonly Paint basePaint = new(PaintFlags.AntiAlias);
    readonly Paint orbPaint = new(PaintFlags.AntiAlias);
    Color? mediaPrimary;
    Color? mediaSecondary;

    public OneUiBackdropView(Context context) : base(context) {
        basePaint.Color = OneUiTheme.BgScreen;
        basePaint.SetStyle(Paint.Style.Fill);
        if (OperatingSystem.IsAndroidVersionAtLeast(31)) {
            SetRenderEffect(RenderEffect.CreateBlurEffect(96f, 96f, Shader.TileMode.Clamp));
        }
    }

    public void SetMediaAccent(Color? primary, Color? secondary) {
        mediaPrimary = primary;
        mediaSecondary = secondary;
        PostInvalidateOnAnimation();
    }

    protected override void OnDraw(Canvas canvas) {
        base.OnDraw(canvas);
        float w = Width;
        float h = Height;
        if (w <= 0 || h <= 0) {
            return;
        }

        basePaint.Color = OneUiTheme.BgScreen;
        canvas.DrawRect(0, 0, w, h, basePaint);

        Color p1 = mediaPrimary ?? OneUiTheme.Primary;
        Color p2 = mediaSecondary ?? OneUiTheme.Tertiary;
        Color p3 = mediaPrimary ?? OneUiTheme.Secondary;
        int alpha1 = mediaPrimary.HasValue ? 64 : 24;
        int alpha2 = mediaSecondary.HasValue ? 46 : 20;
        int alpha3 = mediaPrimary.HasValue ? 36 : 18;

        // Top ambient wallpaper / cover art depth glow
        DrawGlow(canvas, w * 0.30f, h * 0.12f, Math.Max(w, h) * 0.58f,
            Color.Argb(alpha1, p1.R, p1.G, p1.B));

        // Mid-right secondary harmonic bokeh sphere
        DrawGlow(canvas, w * 0.86f, h * 0.44f, Math.Max(w, h) * 0.50f,
            Color.Argb(alpha2, p2.R, p2.G, p2.B));

        // Bottom-left deep ambient sphere behind floating nav capsule
        DrawGlow(canvas, w * 0.22f, h * 0.86f, Math.Max(w, h) * 0.48f,
            Color.Argb(alpha3, p3.R, p3.G, p3.B));
    }

    void DrawGlow(Canvas canvas, float cx, float cy, float radius, Color centerColor) {
        using RadialGradient grad = new(
            cx, cy, Math.Max(1f, radius),
            [centerColor.ToArgb(), Color.Transparent.ToArgb()],
            [0f, 1f],
            Shader.TileMode.Clamp);
        orbPaint.SetShader(grad);
        canvas.DrawCircle(cx, cy, radius, orbPaint);
        orbPaint.SetShader(null);
    }
}

/// <summary>
/// Frosted glass capsule container that samples and blurs the content behind itself in real time
/// (matching One UI 9 bottom navigation &amp; media control pills) while keeping foreground children razor-sharp.
/// </summary>
public sealed class OneUiFrostedCapsuleView : FrameLayout {
    readonly Paint frostOverlayPaint = new(PaintFlags.AntiAlias);
    readonly Paint rimPaint = new(PaintFlags.AntiAlias);
    readonly Paint bitmapPaint = new(PaintFlags.AntiAlias | PaintFlags.FilterBitmap);
    readonly Android.Graphics.Path clipPath = new();
    readonly int[] selfLoc = new int[2];
    readonly int[] targetLoc = new int[2];

    Bitmap blurBitmap;
    int[] pixelScratch;
    Canvas captureCanvas;
    bool isCapturing;
    long lastCaptureMs;

    public Func<View[]> BehindViewsProvider { get; set; }
    public float CornerRadiusDp { get; set; } = 30f;
    public Color? GlazeTopColor { get; set; }
    public Color? GlazeBottomColor { get; set; }

    public OneUiFrostedCapsuleView(Context context) : base(context) {
        SetWillNotDraw(false);
        frostOverlayPaint.SetStyle(Paint.Style.Fill);
        rimPaint.SetStyle(Paint.Style.Stroke);
        rimPaint.StrokeWidth = Math.Max(1.5f, OneUiTheme.Dp(context, 1.0f));
    }

    public void RefreshBlur(bool force = false) {
        if (isCapturing || Width <= 0 || Height <= 0 || Visibility != ViewStates.Visible) {
            return;
        }
        long now = SystemClock.UptimeMillis();
        if (!force && now - lastCaptureMs < 24) {
            return;
        }
        CaptureBehindRegion();
        PostInvalidateOnAnimation();
    }

    void CaptureBehindRegion() {
        View[] behind = BehindViewsProvider?.Invoke();
        if (behind == null || behind.Length == 0) {
            return;
        }

        const int downscale = 10;
        int bw = Math.Clamp(Width / downscale, 16, 160);
        int bh = Math.Clamp(Height / downscale, 8, 48);

        if (blurBitmap == null || blurBitmap.IsRecycled || blurBitmap.Width != bw || blurBitmap.Height != bh) {
            captureCanvas?.Dispose();
            blurBitmap?.Recycle();
            blurBitmap?.Dispose();
            blurBitmap = Bitmap.CreateBitmap(bw, bh, Bitmap.Config.Argb8888!);
            captureCanvas = new Canvas(blurBitmap);
            pixelScratch = new int[bw * bh];
        }

        isCapturing = true;
        try {
            lastCaptureMs = SystemClock.UptimeMillis();
            blurBitmap.EraseColor(OneUiTheme.BgScreen.ToArgb());
            GetLocationInWindow(selfLoc);

            float sx = bw / (float)Width;
            float sy = bh / (float)Height;

            foreach (View v in behind) {
                if (v == null || v.Visibility != ViewStates.Visible || v.Width <= 0 || v.Height <= 0) {
                    continue;
                }
                v.GetLocationInWindow(targetLoc);
                int save = captureCanvas.Save();
                captureCanvas.Scale(sx, sy);
                captureCanvas.Translate(targetLoc[0] - selfLoc[0], targetLoc[1] - selfLoc[1]);
                v.Draw(captureCanvas);
                captureCanvas.RestoreToCount(save);
            }

            FastBoxBlur(blurBitmap, pixelScratch, 3);
            FastBoxBlur(blurBitmap, pixelScratch, 2);
        } catch {
            // Keep smooth frosted surface fallback if any child cannot draw offscreen
        } finally {
            isCapturing = false;
        }
    }

    protected override void OnDraw(Canvas canvas) {
        float w = Width;
        float h = Height;
        if (w <= 0 || h <= 0) {
            base.OnDraw(canvas);
            return;
        }

        if (blurBitmap == null) {
            CaptureBehindRegion();
        }

        float rad = Math.Min(Math.Min(w, h) * 0.5f, OneUiTheme.Dp(Context, CornerRadiusDp));
        float halfStroke = rimPaint.StrokeWidth * 0.5f;
        RectF bounds = new(halfStroke, halfStroke, w - halfStroke, h - halfStroke);

        clipPath.Reset();
        clipPath.AddRoundRect(bounds, rad, rad, Android.Graphics.Path.Direction.Cw);

        int save = canvas.Save();
        canvas.ClipPath(clipPath);

        if (blurBitmap != null && !blurBitmap.IsRecycled) {
            canvas.DrawBitmap(blurBitmap, null, new RectF(0f, 0f, w, h), bitmapPaint);
        }

        // Translucent smoked One UI 9 frosted glaze + top subtle reflection highlight (supports Samsung Gallery #E63A3A3D)
        Color gTop = GlazeTopColor ?? Color.Argb(204, 34, 36, 44);
        Color gBot = GlazeBottomColor ?? Color.Argb(228, 16, 17, 22);
        using LinearGradient glaze = new(
            0f, 0f, 0f, h,
            [gTop.ToArgb(), gBot.ToArgb()],
            [0f, 1f],
            Shader.TileMode.Clamp!);
        frostOverlayPaint.SetShader(glaze);
        canvas.DrawRect(0f, 0f, w, h, frostOverlayPaint);
        frostOverlayPaint.SetShader(null);
        canvas.RestoreToCount(save);

        using LinearGradient rimGrad = new(
            0f, 0f, 0f, h,
            [Color.Argb(105, 255, 255, 255).ToArgb(), Color.Argb(22, 255, 255, 255).ToArgb(), Color.Argb(58, 255, 255, 255).ToArgb()],
            [0f, 0.45f, 1f],
            Shader.TileMode.Clamp!);
        rimPaint.SetShader(rimGrad);
        canvas.DrawRoundRect(bounds, rad, rad, rimPaint);
        rimPaint.SetShader(null);

        base.OnDraw(canvas);
    }

    static void FastBoxBlur(Bitmap bmp, int[] pixels, int radius) {
        int w = bmp.Width;
        int h = bmp.Height;
        if (w < 3 || h < 3 || radius <= 0 || pixels == null || pixels.Length < w * h) {
            return;
        }
        bmp.GetPixels(pixels, 0, w, 0, 0, w, h);
        int window = radius * 2 + 1;

        for (int y = 0; y < h; y++) {
            int row = y * w;
            for (int x = 0; x < w; x++) {
                int rSum = 0, gSum = 0, bSum = 0;
                for (int k = -radius; k <= radius; k++) {
                    int sx = Math.Clamp(x + k, 0, w - 1);
                    int c = pixels[row + sx];
                    rSum += (c >> 16) & 0xFF;
                    gSum += (c >> 8) & 0xFF;
                    bSum += c & 0xFF;
                }
                pixels[row + x] = unchecked((int)0xFF000000) | ((rSum / window) << 16) | ((gSum / window) << 8) | (bSum / window);
            }
        }

        for (int x = 0; x < w; x++) {
            for (int y = 0; y < h; y++) {
                int rSum = 0, gSum = 0, bSum = 0;
                for (int k = -radius; k <= radius; k++) {
                    int sy = Math.Clamp(y + k, 0, h - 1);
                    int c = pixels[sy * w + x];
                    rSum += (c >> 16) & 0xFF;
                    gSum += (c >> 8) & 0xFF;
                    bSum += c & 0xFF;
                }
                pixels[y * w + x] = unchecked((int)0xFF000000) | ((rSum / window) << 16) | ((gSum / window) << 8) | (bSum / window);
            }
        }
        bmp.SetPixels(pixels, 0, w, 0, 0, w, h);
    }

    protected override void Dispose(bool disposing) {
        if (disposing) {
            captureCanvas?.Dispose();
            captureCanvas = null;
            blurBitmap?.Recycle();
            blurBitmap?.Dispose();
            blurBitmap = null;
        }
        base.Dispose(disposing);
    }
}

/// <summary>
/// Frosted glass container drawable with subtle inner elevation gradient and diagonal specular glass rim stroke.
/// </summary>
public sealed class OneUiGlassDrawable : Drawable {
    readonly float cornerRadiusPx;
    readonly float strokeWidthPx;
    readonly Color fillColor;
    readonly Color? accentBorder;
    readonly bool isFloatingCapsule;
    readonly Paint fillPaint = new(PaintFlags.AntiAlias);
    readonly Paint borderPaint = new(PaintFlags.AntiAlias);

    int cachedW = -1;
    int cachedH = -1;

    public OneUiGlassDrawable(
        Context ctx,
        Color fillColor,
        float cornerRadiusPx,
        Color? accentBorder = null,
        bool isFloatingCapsule = false) {
        this.fillColor = fillColor;
        this.cornerRadiusPx = cornerRadiusPx;
        this.accentBorder = accentBorder;
        this.isFloatingCapsule = isFloatingCapsule;
        strokeWidthPx = Math.Max(1.5f, OneUiTheme.Dp(ctx, 1.1f));

        fillPaint.SetStyle(Paint.Style.Fill);
        borderPaint.SetStyle(Paint.Style.Stroke);
        borderPaint.StrokeWidth = strokeWidthPx;
    }

    public override void Draw(Canvas canvas) {
        Android.Graphics.Rect b = Bounds;
        int w = b.Width();
        int h = b.Height();
        if (w <= 0 || h <= 0) {
            return;
        }

        if (w != cachedW || h != cachedH) {
            cachedW = w;
            cachedH = h;
            UpdateShaders(b.Left, b.Top, b.Right, b.Bottom);
        }

        float halfStroke = strokeWidthPx * 0.5f;
        RectF r = new(b.Left + halfStroke, b.Top + halfStroke, b.Right - halfStroke, b.Bottom - halfStroke);
        float maxRad = Math.Min(w, h) * 0.5f;
        float rad = Math.Min(cornerRadiusPx, maxRad);

        canvas.DrawRoundRect(r, rad, rad, fillPaint);
        canvas.DrawRoundRect(r, rad, rad, borderPaint);
    }

    void UpdateShaders(float left, float top, float right, float bottom) {
        Color topFill = Color.Argb(
            fillColor.A,
            Math.Min(255, fillColor.R + 14),
            Math.Min(255, fillColor.G + 14),
            Math.Min(255, fillColor.B + 18));
        Color botFill = fillColor;

        fillPaint.SetShader(new LinearGradient(
            left, top, right, bottom,
            [topFill.ToArgb(), botFill.ToArgb()],
            [0f, 1f],
            Shader.TileMode.Clamp));

        if (accentBorder.HasValue) {
            Color c = accentBorder.Value;
            borderPaint.SetShader(new LinearGradient(
                left, top, right, bottom,
                [Color.Argb(200, c.R, c.G, c.B).ToArgb(), Color.Argb(90, c.R, c.G, c.B).ToArgb()],
                [0f, 1f],
                Shader.TileMode.Clamp));
        } else {
            Color topRim = isFloatingCapsule ? Color.Argb(95, 255, 255, 255) : OneUiTheme.GlassRimTop;
            Color midRim = OneUiTheme.GlassRimMid;
            Color botRim = isFloatingCapsule ? Color.Argb(60, 255, 255, 255) : OneUiTheme.GlassRimBottom;
            borderPaint.SetShader(new LinearGradient(
                left, top, right, bottom,
                [topRim.ToArgb(), midRim.ToArgb(), botRim.ToArgb()],
                [0f, 0.5f, 1f],
                Shader.TileMode.Clamp));
        }
    }

    public override void SetAlpha(int alpha) {
        fillPaint.Alpha = alpha;
        borderPaint.Alpha = alpha;
    }

    public override void SetColorFilter(ColorFilter colorFilter) {
        fillPaint.SetColorFilter(colorFilter);
        borderPaint.SetColorFilter(colorFilter);
    }

#pragma warning disable CS0672, CS0618, CA1422
    public override int Opacity => (int)Android.Graphics.Format.Translucent;
#pragma warning restore CS0672, CS0618, CA1422
}

/// <summary>
/// Full-capsule 44dp One UI 9 slider (matching SystemUI Brightness &amp; SoundCraft Volume seekbars):
/// - Exact Samsung ToggleSeekBar.onTouchEvent relative-drag mechanism (touching anywhere keeps the
///   bar &amp; ball in place and adjusts proportionally from the initial touch point)
/// - Blooming circular touch "ball" thumb with spring animation on press/drag, optional SlimVolumeMode
///   for Apple Music's volume bar, embedded label &amp; value readout, double-tap reset, and scroll safety.
/// </summary>
public sealed class OneUiSeekBar : SeekBar {
    readonly int touchSlop;
    readonly Paint trackPaint = new(PaintFlags.AntiAlias);
    readonly Paint trackRimPaint = new(PaintFlags.AntiAlias);
    readonly Paint fillPaint = new(PaintFlags.AntiAlias);
    readonly Paint thumbHaloPaint = new(PaintFlags.AntiAlias);
    readonly Paint thumbBallPaint = new(PaintFlags.AntiAlias);
    readonly Paint thumbBallRimPaint = new(PaintFlags.AntiAlias);
    readonly TextPaint labelPaint = new(PaintFlags.AntiAlias);
    readonly Paint valuePaint = new(PaintFlags.AntiAlias);
    readonly Android.Graphics.Path clipPath = new();

    float downX;
    float downY;
    float dragAnchorX;
    float startDragRatio;
    float maxMoveDist;
    int initialProgress;
    bool isHorizontalDrag;
    bool isTouchDown;
    bool isCancelledToScroll;
    DateTime lastTapTime = DateTime.MinValue;

    float renderedRatio;
    float touchRatio;
    float thumbExpansion;
    bool hasInitializedRatio;
    long lastFrameMs;

    public string LabelText { get; set; } = "";
    public string ValueText { get; set; } = "";
    public bool UseBlueAccent { get; set; }
    public bool SlimVolumeMode { get; set; }
    public Color? CustomFillColor { get; set; }
    public int DefaultProgress { get; set; }
    public bool HasDefaultReset { get; set; } = true;

    public event Action<int> ResetRequested;
    public event Action<int> UserProgressChanged;

    public OneUiSeekBar(Context context) : base(context) {
        touchSlop = ViewConfiguration.Get(context)?.ScaledTouchSlop ?? 24;
        float dp = context.Resources.DisplayMetrics.Density;

        SetBackgroundColor(Color.Transparent);
#pragma warning disable CS0618
        SetThumb(new ColorDrawable(Color.Transparent));
#pragma warning restore CS0618
        ProgressDrawable = new ColorDrawable(Color.Transparent);
        SplitTrack = false;
        SetPadding(0, 0, 0, 0);

        trackPaint.Color = Color.Argb(58, 255, 255, 255);
        trackPaint.SetStyle(Paint.Style.Fill);

        trackRimPaint.Color = Color.Argb(36, 255, 255, 255);
        trackRimPaint.StrokeWidth = Math.Max(1f, dp);
        trackRimPaint.SetStyle(Paint.Style.Stroke);

        fillPaint.SetStyle(Paint.Style.Fill);

        thumbHaloPaint.SetStyle(Paint.Style.Fill);
        thumbBallPaint.SetStyle(Paint.Style.Fill);
        thumbBallRimPaint.SetStyle(Paint.Style.Stroke);
        thumbBallRimPaint.StrokeWidth = Math.Max(1.2f, 1.2f * dp);

        labelPaint.Color = OneUiTheme.TextPrimary;
        labelPaint.TextSize = 11.8f * dp;
        labelPaint.SetTypeface(OneUiTheme.GetAppleFontMedium(context));
        labelPaint.SetShadowLayer(2.5f * dp, 0f, 1f * dp, Color.Argb(140, 0, 0, 0));

        valuePaint.Color = OneUiTheme.TextPrimary;
        valuePaint.TextSize = 11.8f * dp;
        valuePaint.SetTypeface(OneUiTheme.GetAppleFontBold(context));
        valuePaint.TextAlign = Paint.Align.Right;
        valuePaint.SetShadowLayer(2.5f * dp, 0f, 1f * dp, Color.Argb(140, 0, 0, 0));
    }

    protected override void OnMeasure(int widthMeasureSpec, int heightMeasureSpec) {
        int desiredH = OneUiTheme.Dp(Context, SlimVolumeMode ? 26f : 44f);
        int w = MeasureSpec.GetSize(widthMeasureSpec);
        SetMeasuredDimension(ResolveSize(w, widthMeasureSpec), ResolveSize(desiredH, heightMeasureSpec));
    }

    protected override void OnDraw(Canvas canvas) {
        float w = Width;
        float h = Height;
        if (w <= 0 || h <= 0) {
            return;
        }

        long nowMs = SystemClock.UptimeMillis();
        if (lastFrameMs == 0) {
            lastFrameMs = nowMs;
        }
        float dt = Math.Clamp((nowMs - lastFrameMs) / 1000f, 0.001f, 0.05f);
        lastFrameMs = nowMs;

        float discreteRatio = Max > 0 ? Math.Clamp(Progress / (float)Max, 0f, 1f) : 0f;
        float targetRatio = isHorizontalDrag ? touchRatio : discreteRatio;
        if (!hasInitializedRatio) {
            renderedRatio = targetRatio;
            hasInitializedRatio = true;
        } else {
            float ratioSpeed = isHorizontalDrag ? 30f : 16f;
            renderedRatio += (targetRatio - renderedRatio) * Math.Min(1f, dt * ratioSpeed);
            if (MathF.Abs(targetRatio - renderedRatio) < 0.0006f) {
                renderedRatio = targetRatio;
            }
        }

        float targetThumb = isTouchDown ? 1f : 0f;
        float thumbSpeed = isTouchDown ? 24f : 18f;
        thumbExpansion += (targetThumb - thumbExpansion) * Math.Min(1f, dt * thumbSpeed);
        if (MathF.Abs(targetThumb - thumbExpansion) < 0.004f) {
            thumbExpansion = targetThumb;
        }

        float dp = Resources.DisplayMetrics.Density;

        if (SlimVolumeMode) {
            DrawSlimVolumeTrack(canvas, w, h, dp);
            bool slimAnim =
                MathF.Abs(targetRatio - renderedRatio) > 0.0006f ||
                MathF.Abs(targetThumb - thumbExpansion) > 0.004f;
            if (slimAnim) {
                PostInvalidateOnAnimation();
            }
            return;
        }

        float radius = h * 0.5f;
        RectF bounds = new(0.5f, 0.5f, w - 0.5f, h - 0.5f);

        // 1. Background translucent neutral glass track
        canvas.DrawRoundRect(bounds, radius, radius, trackPaint);

        // 2. Faithful Samsung SystemUI Control Slider Fill:
        //    - Resting (!isTouchDown / thumbExpansion == 0) OR at Max: 100% solid uniform fill, ZERO visible ball
        //    - Active dragging/pressing below Max: left fade 0.50 alpha -> full alpha ball (r = h/2) of exact same RGB
        if (renderedRatio > 0.0005f || (isTouchDown && Progress > 0)) {
            clipPath.Reset();
            clipPath.AddRoundRect(bounds, radius, radius, Android.Graphics.Path.Direction.Cw);
            canvas.Save();
            canvas.ClipPath(clipPath);

            float fillRight = Math.Clamp(renderedRatio * w, 0f, w);
            Color accent = CustomFillColor ?? (UseBlueAccent ? OneUiTheme.Primary : OneUiTheme.SliderAccent);

            if (thumbExpansion <= 0.004f) {
                fillPaint.SetShader(null);
                fillPaint.Color = Color.Argb(255, accent.R, accent.G, accent.B);
                float cornerR = Math.Min(radius, Math.Max(0.5f, fillRight * 0.5f));
                canvas.DrawRoundRect(0f, 0f, fillRight, h, cornerR, cornerR, fillPaint);
            } else {
                float maxSolidity = SmoothStep(0.96f, 0.999f, renderedRatio);
                float reveal = thumbExpansion * (1f - maxSolidity);
                byte bodyLeftAlpha = (byte)Math.Round(255f - 128f * reveal);   // 255 -> 127 (0.50f like tw_progress_color_control_activated_start)
                byte bodyRightAlpha = (byte)Math.Round(255f - 70f * reveal);   // 255 -> 185 (0.72f near thumb)

                if (fillRight > 0.5f) {
                    if (reveal > 0.005f) {
                        Color bodyLeft = Color.Argb(bodyLeftAlpha, accent.R, accent.G, accent.B);
                        Color bodyRight = Color.Argb(bodyRightAlpha, accent.R, accent.G, accent.B);
                        using LinearGradient fillShader = new(0f, 0f, Math.Max(2f, fillRight), 0f, [bodyLeft.ToArgb(), bodyRight.ToArgb()], [0f, 1f], Shader.TileMode.Clamp);
                        fillPaint.SetShader(fillShader);
                    } else {
                        fillPaint.SetShader(null);
                        fillPaint.Color = Color.Argb(255, accent.R, accent.G, accent.B);
                    }
                    float cornerR = Math.Min(radius, Math.Max(0.5f, fillRight * 0.5f));
                    canvas.DrawRoundRect(0f, 0f, fillRight, h, cornerR, cornerR, fillPaint);
                    fillPaint.SetShader(null);

                    if (reveal > 0.005f) {
                        float edgeFade = Math.Clamp(fillRight / Math.Max(1f, h), 0f, 1f);
                        float ballX = Math.Clamp(fillRight - radius, 0f, w - radius);
                        thumbBallPaint.Color = Color.Argb((int)Math.Round(255f * reveal * edgeFade), accent.R, accent.G, accent.B);
                        canvas.DrawCircle(ballX, radius, radius, thumbBallPaint);
                    }
                }
            }

            canvas.Restore();
        }

        // Subtle tick notch indicating default reset point
        if (HasDefaultReset && Max > 0 && DefaultProgress > 0 && DefaultProgress < Max) {
            float defX = DefaultProgress / (float)Max * w;
            canvas.DrawLine(defX, h - 5.5f * dp, defX, h - 1.5f * dp, trackRimPaint);
        }

        canvas.DrawRoundRect(bounds, radius, radius, trackRimPaint);

        // 3. Embedded label & live value centered vertically inside the capsule (always crisp above fill & ball)
        float textY = radius - (labelPaint.Ascent() + labelPaint.Descent()) * 0.5f;
        string valText = string.IsNullOrEmpty(ValueText) ? Progress.ToString() : ValueText;
        float valW = valuePaint.MeasureText(valText);

        if (!string.IsNullOrEmpty(LabelText)) {
            float maxLabelW = Math.Max(28f * dp, w - 36f * dp - valW);
            string ellipsized = TextUtils.Ellipsize(LabelText, labelPaint, maxLabelW, TextUtils.TruncateAt.End) ?? LabelText;
            canvas.Save();
            canvas.ClipRect(12f * dp, 0f, 14f * dp + maxLabelW, h);
            canvas.DrawText(ellipsized, 14f * dp, textY, labelPaint);
            canvas.Restore();
        }
        canvas.DrawText(valText, w - 14f * dp, textY, valuePaint);

        bool needsNextFrame =
            MathF.Abs(targetRatio - renderedRatio) > 0.0006f ||
            MathF.Abs(targetThumb - thumbExpansion) > 0.004f;
        if (needsNextFrame) {
            PostInvalidateOnAnimation();
        }
    }

    void DrawSlimVolumeTrack(Canvas canvas, float w, float h, float dp) {
        float barH = (7f + 5f * thumbExpansion) * dp;
        float cy = h * 0.5f;
        float top = cy - barH * 0.5f;
        float bot = cy + barH * 0.5f;
        float rad = barH * 0.5f;

        RectF trackRect = new(0f, top, w, bot);
        fillPaint.SetShader(null);
        fillPaint.Color = Color.Argb(54, 255, 255, 255);
        canvas.DrawRoundRect(trackRect, rad, rad, fillPaint);

        if (renderedRatio > 0.002f) {
            clipPath.Reset();
            clipPath.AddRoundRect(trackRect, rad, rad, Android.Graphics.Path.Direction.Cw);
            canvas.Save();
            canvas.ClipPath(clipPath);

            float fillRight = Math.Clamp(renderedRatio * w, 0f, w);
            Color c = CustomFillColor ?? Color.Argb(228, 255, 255, 255);
            fillPaint.Color = Color.Argb((int)Math.Round(185 + 70 * thumbExpansion), c.R, c.G, c.B);
            canvas.DrawRoundRect(0f, top, Math.Max(rad * 1.4f, fillRight), bot, rad, rad, fillPaint);
            canvas.Restore();
        }

        if (HasDefaultReset && Max > 0 && DefaultProgress > 0 && DefaultProgress < Max) {
            float defX = DefaultProgress / (float)Max * w;
            canvas.DrawLine(defX, top + 1f * dp, defX, bot - 1f * dp, trackRimPaint);
        }
    }

    public override bool OnTouchEvent(MotionEvent e) {
        if (!Enabled) {
            return false;
        }

        float w = Math.Max(1f, Width);
        float dp = Resources.DisplayMetrics.Density;

        switch (e.ActionMasked) {
            case MotionEventActions.Down:
                // Exact Samsung ToggleSeekBar.onTouchEvent mechanism:
                // Do NOT jump progress to downX; record downX & current ratio and anchor relative drag.
                downX = e.GetX();
                downY = e.GetY();
                dragAnchorX = downX;
                maxMoveDist = 0f;
                initialProgress = Progress;
                startDragRatio = Max > 0 ? Math.Clamp(Progress / (float)Max, 0f, 1f) : 0f;
                touchRatio = startDragRatio;
                isHorizontalDrag = false;
                isTouchDown = true;
                isCancelledToScroll = false;
                lastFrameMs = SystemClock.UptimeMillis();
                PostInvalidateOnAnimation();
                return true;

            case MotionEventActions.Move:
                if (isCancelledToScroll) {
                    return false;
                }
                float curX = e.GetX();
                float curY = e.GetY();
                float dxAbs = Math.Abs(curX - downX);
                float dyAbs = Math.Abs(curY - downY);
                float moveDist = MathF.Sqrt(dxAbs * dxAbs + dyAbs * dyAbs);
                if (moveDist > maxMoveDist) {
                    maxMoveDist = moveDist;
                }

                if (!isHorizontalDrag) {
                    if (dyAbs > touchSlop && dyAbs > dxAbs) {
                        isCancelledToScroll = true;
                        isTouchDown = false;
                        Parent?.RequestDisallowInterceptTouchEvent(false);
                        Progress = initialProgress;
                        PostInvalidateOnAnimation();
                        return false;
                    }
                    if (dxAbs > touchSlop * 0.5f && dxAbs >= dyAbs * 1.05f) {
                        isHorizontalDrag = true;
                        dragAnchorX = curX;
                        startDragRatio = Max > 0 ? Math.Clamp(Progress / (float)Max, 0f, 1f) : renderedRatio;
                        touchRatio = startDragRatio;
                        Parent?.RequestDisallowInterceptTouchEvent(true);
                        if (!SlimVolumeMode) {
                            Animate().ScaleX(1.008f).ScaleY(1.032f).SetDuration(120).Start();
                        }
                    } else {
                        PostInvalidateOnAnimation();
                        return true;
                    }
                }

                Parent?.RequestDisallowInterceptTouchEvent(true);
                float usableW = Math.Max(1f, w - (SlimVolumeMode ? 12f * dp : Height * 0.45f));
                float rawRatio = startDragRatio + (curX - dragAnchorX) / usableW;
                if (rawRatio > 1f) {
                    rawRatio = 1f;
                    startDragRatio = 1f;
                    dragAnchorX = curX;
                } else if (rawRatio < 0f) {
                    rawRatio = 0f;
                    startDragRatio = 0f;
                    dragAnchorX = curX;
                }
                touchRatio = rawRatio;

                int newProg = Max > 0 ? Math.Clamp((int)Math.Round(touchRatio * Max), 0, Max) : 0;
                if (newProg != Progress) {
                    Progress = newProg;
                    UserProgressChanged?.Invoke(newProg);
                }
                PostInvalidateOnAnimation();
                return true;

            case MotionEventActions.Up:
                Parent?.RequestDisallowInterceptTouchEvent(false);
                Animate().ScaleX(1f).ScaleY(1f).SetDuration(280).Start();
                isTouchDown = false;
                if (isCancelledToScroll) {
                    isCancelledToScroll = false;
                    PostInvalidateOnAnimation();
                    return false;
                }

                bool wasTap = !isHorizontalDrag && maxMoveDist < touchSlop;
                if (wasTap && HasDefaultReset) {
                    DateTime now = DateTime.UtcNow;
                    if ((now - lastTapTime).TotalMilliseconds <= 320) {
                        lastTapTime = DateTime.MinValue;
                        isHorizontalDrag = false;
                        int target = Math.Clamp(DefaultProgress, 0, Max);
                        Progress = target;
                        touchRatio = Max > 0 ? Math.Clamp(target / (float)Max, 0f, 1f) : 0f;
                        ResetRequested?.Invoke(target);
                        Animate().ScaleX(0.99f).ScaleY(0.95f).SetDuration(80).WithEndAction(
                            new Java.Lang.Runnable(() => Animate().ScaleX(1f).ScaleY(1f).SetDuration(240).Start())).Start();
                        PostInvalidateOnAnimation();
                        return true;
                    }
                    lastTapTime = now;
                }

                // On single tap without horizontal drag, keep current Progress intact (ToggleSeekBar behavior)
                isHorizontalDrag = false;
                touchRatio = Max > 0 ? Math.Clamp(Progress / (float)Max, 0f, 1f) : 0f;
                PostInvalidateOnAnimation();
                return true;

            case MotionEventActions.Cancel:
                Parent?.RequestDisallowInterceptTouchEvent(false);
                Animate().ScaleX(1f).ScaleY(1f).SetDuration(280).Start();
                isTouchDown = false;
                bool wasDragging = isHorizontalDrag;
                isHorizontalDrag = false;
                isCancelledToScroll = false;
                touchRatio = Max > 0 ? Math.Clamp(Progress / (float)Max, 0f, 1f) : 0f;
                PostInvalidateOnAnimation();
                return wasDragging;
        }

        return base.OnTouchEvent(e);
    }

    static float SmoothStep(float edge0, float edge1, float x) {
        if (edge1 <= edge0) {
            return x >= edge1 ? 1f : 0f;
        }
        float t = Math.Clamp((x - edge0) / (edge1 - edge0), 0f, 1f);
        return t * t * (3f - 2f * t);
    }
}

/// <summary>
/// Faithful Samsung One UI 9 / Gallery SESL9 Switch (extracted from SamsungGallery2018.apk sesl_switch_*):
/// - Track capsule: 32dp x 20dp (corner radius 10dp), track off #636368, track on Dynamic Primary
/// - Thumb: 20dp outer ring (2dp stroke in #8F8F8F off / Primary on) + 16dp #FCFCFF inner circle
/// - Tactile press animation: thumb core scales cleanly from 1.0 to 0.80 while pressed, and glides 180ms on toggle.
/// </summary>
public sealed class OneUiSwitch : Switch {
    readonly Paint trackPaint = new(PaintFlags.AntiAlias);
    readonly Paint thumbCorePaint = new(PaintFlags.AntiAlias);
    readonly Paint thumbRingPaint = new(PaintFlags.AntiAlias);
    readonly int touchSlop;

    float thumbPos;
    float pressFrac;
    bool hasDrawnOnce;
    bool isPressedDown;
    bool didSwipe;
    float downX;
    float downY;
    long lastFrameMs;

    public OneUiSwitch(Context context) : base(context) {
        touchSlop = ViewConfiguration.Get(context)?.ScaledTouchSlop ?? 20;
        SetBackgroundColor(Color.Transparent);
#pragma warning disable CS0618
        ThumbDrawable = new ColorDrawable(Color.Transparent);
        TrackDrawable = new ColorDrawable(Color.Transparent);
#pragma warning restore CS0618
        SplitTrack = false;
        ShowText = false;
        SetPadding(0, 0, 0, 0);
        SetMinWidth(0);
        SetMinHeight(0);
        Clickable = true;
        Focusable = true;

        trackPaint.SetStyle(Android.Graphics.Paint.Style.Fill);
        thumbCorePaint.SetStyle(Android.Graphics.Paint.Style.Fill);
        thumbRingPaint.SetStyle(Android.Graphics.Paint.Style.Stroke);

        CheckedChange += (_, _) => {
            if (hasDrawnOnce) {
                lastFrameMs = SystemClock.UptimeMillis();
                PostInvalidateOnAnimation();
            } else {
                thumbPos = Checked ? 1f : 0f;
            }
        };
    }

    public override void OnMeasure(int widthMeasureSpec, int heightMeasureSpec) {
        int w = OneUiTheme.Dp(Context, 40f);
        int h = OneUiTheme.Dp(Context, 26f);
        SetMeasuredDimension(ResolveSize(w, widthMeasureSpec), ResolveSize(h, heightMeasureSpec));
    }

    protected override void OnDraw(Canvas canvas) {
        float w = Width;
        float h = Height;
        if (w <= 0 || h <= 0) {
            return;
        }

        if (!hasDrawnOnce) {
            hasDrawnOnce = true;
            thumbPos = Checked ? 1f : 0f;
            pressFrac = 0f;
            lastFrameMs = SystemClock.UptimeMillis();
        }

        long nowMs = SystemClock.UptimeMillis();
        float dt = Math.Clamp((nowMs - lastFrameMs) / 1000f, 0.001f, 0.05f);
        lastFrameMs = nowMs;

        float targetPos = Checked ? 1f : 0f;
        thumbPos += (targetPos - thumbPos) * Math.Min(1f, dt * 16f);
        if (MathF.Abs(targetPos - thumbPos) < 0.004f) {
            thumbPos = targetPos;
        }

        float targetPress = isPressedDown ? 1f : 0f;
        pressFrac += (targetPress - pressFrac) * Math.Min(1f, dt * 22f);
        if (MathF.Abs(targetPress - pressFrac) < 0.004f) {
            pressFrac = targetPress;
        }

        float dp = Resources.DisplayMetrics.Density;
        float trackW = 32f * dp;
        float trackH = 20f * dp;
        float trackLeft = (w - trackW) * 0.5f;
        float trackTop = (h - trackH) * 0.5f;
        float corner = trackH * 0.5f;

        Color trackOff = Color.Rgb(99, 99, 104); // #636368 Samsung Gallery dark track off
        Color trackOn = OneUiTheme.Primary;
        trackPaint.Color = OneUiTheme.BlendColor(trackOff, trackOn, thumbPos);
        canvas.DrawRoundRect(
            new RectF(trackLeft, trackTop, trackLeft + trackW, trackTop + trackH),
            corner, corner, trackPaint);

        // Thumb travels cleanly inside the 32x20 capsule (outer diameter 20dp, inner white circle 16dp)
        float outerR = 10f * dp;
        float cy = h * 0.5f;
        float startCx = trackLeft + outerR;
        float endCx = trackLeft + trackW - outerR;
        float cx = startCx + (endCx - startCx) * thumbPos;

        float strokeW = 2f * dp;
        float scale = 1f - 0.16f * pressFrac;
        float coreR = (8.2f * dp) * scale;
        float ringR = (9.0f * dp) * (1f - 0.10f * pressFrac);

        Color ringOff = Color.Rgb(143, 143, 148); // #8F8F8F sesl_switch_thumb_off stroke
        thumbRingPaint.StrokeWidth = strokeW;
        thumbRingPaint.Color = OneUiTheme.BlendColor(ringOff, trackOn, thumbPos);
        thumbCorePaint.Color = Color.Rgb(252, 252, 255); // #FCFCFF sesl_switch_thumb fill

        canvas.DrawCircle(cx, cy, coreR, thumbCorePaint);
        canvas.DrawCircle(cx, cy, ringR, thumbRingPaint);

        if (MathF.Abs(targetPos - thumbPos) > 0.004f || MathF.Abs(targetPress - pressFrac) > 0.004f) {
            PostInvalidateOnAnimation();
        }
    }

    public override bool OnTouchEvent(MotionEvent e) {
        if (!Enabled) {
            return false;
        }
        switch (e.ActionMasked) {
            case MotionEventActions.Down:
                downX = e.GetX();
                downY = e.GetY();
                didSwipe = false;
                isPressedDown = true;
                lastFrameMs = SystemClock.UptimeMillis();
                PostInvalidateOnAnimation();
                return true;

            case MotionEventActions.Move:
                float dx = e.GetX() - downX;
                float dy = e.GetY() - downY;
                if (!didSwipe && MathF.Abs(dx) > touchSlop * 0.45f && MathF.Abs(dx) >= MathF.Abs(dy)) {
                    didSwipe = true;
                    Parent?.RequestDisallowInterceptTouchEvent(true);
                }
                if (didSwipe) {
                    bool desired = dx > 0f;
                    if (Checked != desired) {
                        Checked = desired;
                    }
                    PostInvalidateOnAnimation();
                    return true;
                }
                return true;

            case MotionEventActions.Up:
                Parent?.RequestDisallowInterceptTouchEvent(false);
                isPressedDown = false;
                if (!didSwipe && MathF.Abs(e.GetY() - downY) <= touchSlop) {
                    PerformClick();
                }
                lastFrameMs = SystemClock.UptimeMillis();
                PostInvalidateOnAnimation();
                return true;

            case MotionEventActions.Cancel:
                Parent?.RequestDisallowInterceptTouchEvent(false);
                isPressedDown = false;
                didSwipe = false;
                lastFrameMs = SystemClock.UptimeMillis();
                PostInvalidateOnAnimation();
                return true;
        }
        return base.OnTouchEvent(e);
    }
}
