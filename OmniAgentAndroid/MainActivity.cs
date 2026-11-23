using Android.App;
using Android.Content;
using Android.Content.PM;
using Android.Graphics;
using Android.Graphics.Drawables;
using Android.OS;
using Android.Provider;
using Android.Text;
using Android.Util;
using Android.Views;
using Android.Widget;

using OmniAgentAndroid.Services;

namespace OmniAgentAndroid;

/// <summary>
/// One UI 9 inspired AMOLED dashboard for OmniAgent Companion (Phase 2).
/// Zero stock AlertDialog/Spinner, strict BaselineAligned=false anti-clipping,
/// dynamic WindowInsets safe-area padding, runtime Termux RUN_COMMAND permission flow,
/// and system-following locale with live Auto / PT-BR / EN switcher.
/// </summary>
[Activity(
    Label = "OmniAgent",
    MainLauncher = true,
    Exported = true,
    ConfigurationChanges = ConfigChanges.Orientation | ConfigChanges.ScreenSize | ConfigChanges.UiMode | ConfigChanges.Locale | ConfigChanges.LayoutDirection)]
public sealed class MainActivity : Activity {
    const int ReqPostNotifications = 101;
    const int ReqTermuxRunCommand = 102;

    const string TermuxBootstrapCmd =
        "mkdir -p ~/.termux && (grep -qxF 'allow-external-apps=true' ~/.termux/termux.properties 2>/dev/null || echo 'allow-external-apps=true' >> ~/.termux/termux.properties) && (termux-reload-settings 2>/dev/null || true) && proot-distro login debian -- bash -lc 'cd /root/.gemini/antigravity/scratch/Cavern && (git stash push -u -m \"backup-pre-w41\" || true) && git pull --rebase origin master && bash .agents/agent/install_termux_agent.sh --bg'";

    static readonly Color BgDark = Color.ParseColor("#09090B");
    static readonly Color CardBg = Color.ParseColor("#16161B");
    static readonly Color CardElevated = Color.ParseColor("#202028");
    static readonly Color AccentBlue = Color.ParseColor("#2F7CF6");
    static readonly Color AccentGreen = Color.ParseColor("#22C55E");
    static readonly Color AccentRed = Color.ParseColor("#EF4444");
    static readonly Color TextPrimary = Color.ParseColor("#F4F4F6");
    static readonly Color TextSecondary = Color.ParseColor("#9CA3AF");
    static readonly Color StrokeColor = Color.ParseColor("#2A2A35");

    OmniSettingsStore store;
    bool isPt;
    bool pendingTermuxWake;
    int insetTopPx;
    int insetBottomPx;

    LinearLayout contentLayout;
    TextView statusBadge;
    TextView statusSubtitle;
    Button toggleServiceBtn;
    Button langBtn;
    Button modeSmartBtn;
    Button modeBioBtn;
    Button modeTrustedBtn;
    TextView relaySummaryTv;
    TextView githubSummaryTv;
    TextView cloudflareSummaryTv;
    TextView logViewTv;
    Button overlayPermBtn;
    Button batteryPermBtn;
    Button cavernizeLinkBtn;
    FrameLayout modalOverlay;

    protected override void OnCreate(Bundle savedInstanceState) {
        base.OnCreate(savedInstanceState);
        store = new OmniSettingsStore(this);
        isPt = ResolveIsPt();

        if (OperatingSystem.IsAndroidVersionAtLeast(33)) {
            if (CheckSelfPermission(Android.Manifest.Permission.PostNotifications) != Permission.Granted) {
                RequestPermissions([Android.Manifest.Permission.PostNotifications], ReqPostNotifications);
            }
        }

        BuildUi();
        OmniAgentForegroundService.StateChanged += OnServiceOrLogChanged;
        OmniSettingsStore.ActivityChanged += OnServiceOrLogChanged;
        RefreshUi();
    }

    protected override void OnResume() {
        base.OnResume();
        RefreshUi();
    }

    protected override void OnDestroy() {
        OmniAgentForegroundService.StateChanged -= OnServiceOrLogChanged;
        OmniSettingsStore.ActivityChanged -= OnServiceOrLogChanged;
        base.OnDestroy();
    }

    public override void OnRequestPermissionsResult(int requestCode, string[] permissions, Permission[] grantResults) {
        base.OnRequestPermissionsResult(requestCode, permissions, grantResults);
        if (requestCode == ReqTermuxRunCommand) {
            bool granted = grantResults is { Length: > 0 } && grantResults[0] == Permission.Granted;
            bool shouldRetry = pendingTermuxWake;
            pendingTermuxWake = false;
            if (granted) {
                OmniSettingsStore.AppendActivity("PERM", T("Termux RUN_COMMAND permission granted.", "Permissão Termux RUN_COMMAND concedida."));
                if (shouldRetry) {
                    DispatchSmartTermuxWake();
                }
            } else {
                OmniSettingsStore.AppendActivity("ERR", T("Termux RUN_COMMAND permission denied.", "Permissão Termux RUN_COMMAND negada."));
                ShowTermuxSetupSheet(permissionDenied: true);
            }
        }
    }

    void OnServiceOrLogChanged() => RunOnUiThread(RefreshUi);

    bool ResolveIsPt() {
        string pref = store?.UiLanguage ?? "auto";
        if (pref == "pt") {
            return true;
        }
        if (pref == "en") {
            return false;
        }
        try {
            if (OperatingSystem.IsAndroidVersionAtLeast(24)) {
                LocaleList locales = Resources?.Configuration?.Locales;
                if (locales != null && locales.Size() > 0) {
                    string primaryLang = locales.Get(0)?.Language;
                    if (!string.IsNullOrEmpty(primaryLang)) {
                        return string.Equals(primaryLang, "pt", StringComparison.OrdinalIgnoreCase);
                    }
                }
            }
        } catch {
            // Fall back to JVM default locale below
        }
        return string.Equals(Java.Util.Locale.Default?.Language, "pt", StringComparison.OrdinalIgnoreCase);
    }

    void CycleLanguage() {
        string next = store.UiLanguage switch {
            "auto" => "pt",
            "pt" => "en",
            _ => "auto"
        };
        store.UiLanguage = next;
        isPt = ResolveIsPt();
        BuildUi();
        RefreshUi();
    }

    string FormatLangBadge() => store.UiLanguage switch {
        "pt" => "PT-BR",
        "en" => "EN",
        _ => isPt ? "Auto · PT" : "Auto · EN"
    };

    string T(string en, string pt) => isPt ? pt : en;

    int Dp(int v) => (int)Math.Round(TypedValue.ApplyDimension(ComplexUnitType.Dip, v, Resources.DisplayMetrics));

    GradientDrawable Pill(Color fill, int radiusDp, Color? stroke = null) {
        GradientDrawable g = new();
        g.SetColor(fill);
        g.SetCornerRadius(Dp(radiusDp));
        if (stroke.HasValue) {
            g.SetStroke(Dp(1), stroke.Value);
        }
        return g;
    }

    void ApplySafeAreaPadding() {
        if (contentLayout != null) {
            int hPad = Dp(18);
            int topPad = Math.Max(Dp(20), insetTopPx + Dp(14));
            int bottomPad = Math.Max(Dp(32), insetBottomPx + Dp(24));
            contentLayout.SetPadding(hPad, topPad, hPad, bottomPad);
        }
        if (modalOverlay != null) {
            modalOverlay.SetPadding(0, insetTopPx, 0, insetBottomPx);
        }
    }

    void BuildUi() {
        FrameLayout rootFrame = new(this) {
            LayoutParameters = new ViewGroup.LayoutParams(ViewGroup.LayoutParams.MatchParent, ViewGroup.LayoutParams.MatchParent)
        };
        rootFrame.SetBackgroundColor(BgDark);

        ScrollView scroll = new(this) {
            LayoutParameters = new FrameLayout.LayoutParams(ViewGroup.LayoutParams.MatchParent, ViewGroup.LayoutParams.MatchParent),
            FillViewport = true
        };
        scroll.SetClipToPadding(false);

        contentLayout = new LinearLayout(this) {
            Orientation = Orientation.Vertical,
            LayoutParameters = new FrameLayout.LayoutParams(ViewGroup.LayoutParams.MatchParent, ViewGroup.LayoutParams.WrapContent)
        };

        // Header Row (Title + Live Auto/PT-BR/EN Language Switcher)
        LinearLayout headerRow = MakeRow();
        TextView title = new(this) { Text = "OmniAgent", TextSize = 28 };
        title.SetTextColor(TextPrimary);
        title.SetTypeface(Typeface.DefaultBold, TypefaceStyle.Bold);
        headerRow.AddView(title, MakeWeightedLp(1f, rightMarginDp: 8));

        langBtn = MakeButton(FormatLangBadge(), CardElevated, TextPrimary, false);
        langBtn.SetPadding(Dp(12), Dp(6), Dp(12), Dp(6));
        langBtn.SetMinHeight(Dp(34));
        langBtn.Background = Pill(CardElevated, 18, StrokeColor);
        langBtn.Click += (_, _) => CycleLanguage();
        headerRow.AddView(langBtn);
        contentLayout.AddView(headerRow);

        TextView subtitle = new(this) {
            Text = T("Unified Hub v6 • Zero-Trust E2E • Cavernize Bridge", "Hub Unificado v6 • E2E Zero-Trust • Ponte Cavernize"),
            TextSize = 13
        };
        subtitle.SetTextColor(TextSecondary);
        subtitle.SetPadding(0, Dp(2), 0, Dp(16));
        contentLayout.AddView(subtitle);

        // 1. Live Status Card
        LinearLayout statusCard = MakeCard();
        LinearLayout statusRow = MakeRow();
        statusBadge = new TextView(this) { TextSize = 13 };
        statusBadge.SetTypeface(Typeface.DefaultBold, TypefaceStyle.Bold);
        statusBadge.SetPadding(Dp(12), Dp(5), Dp(12), Dp(5));
        statusBadge.SetSingleLine(true);
        statusRow.AddView(statusBadge);
        statusCard.AddView(statusRow);

        statusSubtitle = new TextView(this) { TextSize = 13 };
        statusSubtitle.SetTextColor(TextSecondary);
        statusSubtitle.SetPadding(0, Dp(8), 0, Dp(14));
        statusCard.AddView(statusSubtitle);

        LinearLayout actionRow = MakeRow();
        toggleServiceBtn = MakeButton(T("Start Agent", "Ligar Agente"), AccentBlue, TextPrimary, true);
        toggleServiceBtn.Click += (_, _) => ToggleService();
        actionRow.AddView(toggleServiceBtn, MakeWeightedLp(1f, rightMarginDp: 8));

        Button termuxBtn = MakeButton(T("Wake Termux", "Acordar Termux"), CardElevated, TextPrimary, false);
        termuxBtn.Click += (_, _) => TriggerWakeTermux();
        actionRow.AddView(termuxBtn, MakeWeightedLp(1f, leftMarginDp: 0));
        statusCard.AddView(actionRow);
        contentLayout.AddView(statusCard);

        // 2. Instant Popup & Background Resilience Card
        LinearLayout sysCard = MakeCard();
        sysCard.AddView(MakeCardTitle(T("Instant Fingerprint Popup & Background", "Popup Instantâneo & 2º Plano")));
        TextView sysDesc = new(this) {
            Text = T(
                "Grant 'Appear on top' so biometric approval pops instantly over any app without tapping a notification, and set Battery to Unrestricted.",
                "Ative 'Sobrepor a outros apps' para a digital aparecer direto na tela sem tocar em notificação, e deixe Bateria sem restrições."),
            TextSize = 12
        };
        sysDesc.SetTextColor(TextSecondary);
        sysDesc.SetPadding(0, Dp(4), 0, Dp(12));
        sysCard.AddView(sysDesc);

        LinearLayout permRow = MakeRow();
        overlayPermBtn = MakeButton(T("Instant Popup", "Popup Tela"), CardElevated, TextPrimary, false);
        overlayPermBtn.Click += (_, _) => OpenOverlayPermissionSettings();
        permRow.AddView(overlayPermBtn, MakeWeightedLp(1f, rightMarginDp: 8));

        batteryPermBtn = MakeButton(T("Battery", "Bateria"), CardElevated, TextPrimary, false);
        batteryPermBtn.Click += (_, _) => RequestUnrestrictedBattery();
        permRow.AddView(batteryPermBtn, MakeWeightedLp(1f));
        sysCard.AddView(permRow);

        cavernizeLinkBtn = MakeButton(T("Cavernize Local IPC", "Ponte Local Cavernize"), CardElevated, TextPrimary, false);
        LinearLayout.LayoutParams cavLp = new(ViewGroup.LayoutParams.MatchParent, ViewGroup.LayoutParams.WrapContent);
        cavLp.SetMargins(0, Dp(8), 0, 0);
        cavernizeLinkBtn.Click += (_, _) => OpenCavernizeApp();
        sysCard.AddView(cavernizeLinkBtn, cavLp);
        contentLayout.AddView(sysCard);

        // 3. Security Mode Card (never nags with fingerprint in Smart mode!)
        LinearLayout secCard = MakeCard();
        secCard.AddView(MakeCardTitle(T("Approval Policy", "Política de Aprovação")));
        TextView secDesc = new(this) {
            Text = T(
                "Smart mode uses 256-bit E2E HMAC silently with zero fingerprint popups. Switch to Biometric only if you want fingerprint prompts on push/deploy/install.",
                "Modo Smart usa HMAC E2E de 256 bits sem popups chatos de digital. Troque para Digital apenas se quiser confirmar push/deploy/instalação."),
            TextSize = 12
        };
        secDesc.SetTextColor(TextSecondary);
        secDesc.SetPadding(0, Dp(4), 0, Dp(12));
        secCard.AddView(secDesc);

        LinearLayout modeRow = MakeRow();
        modeSmartBtn = MakeModeButton("Smart", "smart");
        modeBioBtn = MakeModeButton(T("Biometric", "Digital"), "biometric");
        modeTrustedBtn = MakeModeButton(T("Trusted", "Livre"), "trusted");
        modeRow.AddView(modeSmartBtn, MakeWeightedLp(1f, rightMarginDp: 6));
        modeRow.AddView(modeBioBtn, MakeWeightedLp(1f, rightMarginDp: 6));
        modeRow.AddView(modeTrustedBtn, MakeWeightedLp(1f));
        secCard.AddView(modeRow);
        contentLayout.AddView(secCard);

        // 4. Unified Connections Card (Cloudflare Relay + Cloudflare Auto-Deploy + Direct GitHub Push + Termux Setup)
        LinearLayout connCard = MakeCard();
        connCard.AddView(MakeCardTitle(T("Hub, Cloudflare & GitHub", "Hub, Cloudflare & GitHub")));

        relaySummaryTv = new TextView(this) { TextSize = 12 };
        relaySummaryTv.SetTextColor(TextSecondary);
        relaySummaryTv.SetPadding(0, Dp(6), 0, Dp(4));
        connCard.AddView(relaySummaryTv);

        cloudflareSummaryTv = new TextView(this) { TextSize = 12 };
        cloudflareSummaryTv.SetTextColor(TextSecondary);
        cloudflareSummaryTv.SetPadding(0, 0, 0, Dp(4));
        connCard.AddView(cloudflareSummaryTv);

        githubSummaryTv = new TextView(this) { TextSize = 12 };
        githubSummaryTv.SetTextColor(TextSecondary);
        githubSummaryTv.SetPadding(0, 0, 0, Dp(12));
        connCard.AddView(githubSummaryTv);

        LinearLayout cfgRow = MakeRow();
        Button cfgRelayBtn = MakeButton(T("Relay & Key", "Worker & Chave"), CardElevated, TextPrimary, false);
        cfgRelayBtn.Click += (_, _) => ShowRelayConfigSheet();
        cfgRow.AddView(cfgRelayBtn, MakeWeightedLp(1f, rightMarginDp: 8));

        Button cfgCfBtn = MakeButton(T("CF Auto-Deploy", "Deploy CF"), CardElevated, TextPrimary, false);
        cfgCfBtn.Click += (_, _) => ShowCloudflareDeploySheet();
        cfgRow.AddView(cfgCfBtn, MakeWeightedLp(1f));
        connCard.AddView(cfgRow);

        LinearLayout cfgRow2 = MakeRow();
        cfgRow2.SetPadding(0, Dp(8), 0, 0);
        Button cfgGhBtn = MakeButton(T("GitHub Push", "GitHub Push"), CardElevated, TextPrimary, false);
        cfgGhBtn.Click += (_, _) => ShowGitHubConfigSheet();
        cfgRow2.AddView(cfgGhBtn, MakeWeightedLp(1f, rightMarginDp: 8));

        Button termuxSetupBtn = MakeButton(T("Termux / Debian", "Termux / Debian"), CardElevated, TextSecondary, false);
        termuxSetupBtn.Click += (_, _) => ShowTermuxSetupSheet(permissionDenied: false);
        cfgRow2.AddView(termuxSetupBtn, MakeWeightedLp(1f));
        connCard.AddView(cfgRow2);
        contentLayout.AddView(connCard);

        // 5. Live Activity Journal
        LinearLayout logCard = MakeCard();
        LinearLayout logHeader = MakeRow();
        TextView logTitle = MakeCardTitle(T("Live Agent Activity", "Atividade do Agente"));
        logHeader.AddView(logTitle, MakeWeightedLp(1f));
        Button clearBtn = MakeButton(T("Clear", "Limpar"), CardElevated, TextSecondary, false);
        clearBtn.SetPadding(Dp(12), Dp(4), Dp(12), Dp(4));
        clearBtn.Click += (_, _) => OmniSettingsStore.ClearActivity();
        logHeader.AddView(clearBtn);
        logCard.AddView(logHeader);

        logViewTv = new TextView(this) { TextSize = 11 };
        logViewTv.SetTypeface(Typeface.Monospace, TypefaceStyle.Normal);
        logViewTv.SetTextColor(TextSecondary);
        logViewTv.SetPadding(0, Dp(10), 0, 0);
        logCard.AddView(logViewTv);
        contentLayout.AddView(logCard);

        scroll.AddView(contentLayout);
        rootFrame.AddView(scroll);

        // Custom One UI 9 Modal Overlay (zero stock AlertDialog!)
        modalOverlay = new FrameLayout(this) {
            Visibility = ViewStates.Gone,
            Clickable = true,
            Focusable = true,
            LayoutParameters = new FrameLayout.LayoutParams(ViewGroup.LayoutParams.MatchParent, ViewGroup.LayoutParams.MatchParent)
        };
        modalOverlay.SetBackgroundColor(Color.Argb(185, 0, 0, 0));
        modalOverlay.Click += (_, _) => HideModal();
        rootFrame.AddView(modalOverlay);

        ApplySafeAreaPadding();
        rootFrame.SetOnApplyWindowInsetsListener(new SafeAreaInsetsListener((top, bottom) => {
            insetTopPx = top;
            insetBottomPx = bottom;
            ApplySafeAreaPadding();
        }));

        SetContentView(rootFrame);
        rootFrame.RequestApplyInsets();
    }

    void OpenOverlayPermissionSettings() {
        try {
            if (OperatingSystem.IsAndroidVersionAtLeast(23)) {
                Intent intent = new(Settings.ActionManageOverlayPermission, Android.Net.Uri.Parse($"package:{PackageName}"));
                StartActivity(intent);
            }
        } catch {
            Toast.MakeText(this, T("Open Settings -> Apps -> OmniAgent -> Appear on top", "Abra Configurações -> Apps -> OmniAgent -> Aparecer sobre outros"), ToastLength.Long)?.Show();
        }
    }

    void RequestUnrestrictedBattery() {
        try {
            if (OperatingSystem.IsAndroidVersionAtLeast(23)) {
                Intent intent = new(Settings.ActionRequestIgnoreBatteryOptimizations, Android.Net.Uri.Parse($"package:{PackageName}"));
                StartActivity(intent);
                return;
            }
        } catch {
            // Fallback to battery optimization list
        }
        try {
            if (OperatingSystem.IsAndroidVersionAtLeast(23)) {
                StartActivity(new Intent(Settings.ActionIgnoreBatteryOptimizationSettings));
            }
        } catch {
            Intent details = new(Settings.ActionApplicationDetailsSettings, Android.Net.Uri.FromParts("package", PackageName, null));
            StartActivity(details);
        }
    }

    void OpenCavernizeApp() {
        Intent launch = PackageManager?.GetLaunchIntentForPackage(CavernizeIpcBridgeClient.CavernizePackage);
        if (launch != null) {
            launch.AddFlags(ActivityFlags.NewTask);
            StartActivity(launch);
        } else {
            OpenUrl("https://github.com/guumachadooo/Cavern/releases/tag/android-latest");
        }
    }

    void OpenUrl(string url) {
        try {
            StartActivity(new Intent(Intent.ActionView, Android.Net.Uri.Parse(url)));
        } catch {
            Toast.MakeText(this, url, ToastLength.Long)?.Show();
        }
    }

    void TriggerWakeTermux() {
        if (!OmniAgentForegroundService.IsPackageInstalled(this, "com.termux")) {
            string msg = T("Termux is not installed.", "O app Termux não está instalado.");
            OmniSettingsStore.AppendActivity("ERR", msg);
            Toast.MakeText(this, msg, ToastLength.Short)?.Show();
            return;
        }
        if (!OmniAgentForegroundService.HasTermuxPermission(this)) {
            pendingTermuxWake = true;
            OmniSettingsStore.AppendActivity("PERM", T("Requesting Termux RUN_COMMAND permission...", "Solicitando permissão Termux RUN_COMMAND..."));
            RequestPermissions([OmniAgentForegroundService.TermuxRunCommandPermission], ReqTermuxRunCommand);
            return;
        }
        DispatchSmartTermuxWake();
    }

    void DispatchSmartTermuxWake() {
        (bool ok, bool needsPermission, bool needsExternalApps, string msg) =
            OmniAgentForegroundService.LaunchTermuxSmartDaemon(this, store.TermuxStartScript);
        OmniSettingsStore.AppendActivity(ok ? "TERMUX" : "ERR", msg);
        Toast.MakeText(this, ok ? T("Wake sent to Termux / Debian!", "Comando enviado ao Termux / Debian!") : msg, ToastLength.Short)?.Show();
        if (!ok && (needsPermission || needsExternalApps)) {
            ShowTermuxSetupSheet(permissionDenied: needsPermission);
        }
    }

    void ShowTermuxSetupSheet(bool permissionDenied) {
        LinearLayout sheet = CreateSheet(T("Termux & Debian One-Time Setup", "Configuração Única do Termux & Debian"));

        TextView desc = new(this) {
            Text = permissionDenied
                ? T(
                    "Android blocked the Termux RUN_COMMAND permission. Tap App Permissions below to enable 'Run commands in Termux environment', then run the setup command once in Termux.",
                    "O Android bloqueou a permissão RUN_COMMAND do Termux. Toque em Permissões abaixo para ativar 'Executar comandos no Termux' e depois cole o comando abaixo uma única vez no Termux.")
                : T(
                    "Copy the command below, open Termux, paste it once and press Enter. It enables allow-external-apps=true, updates Cavern inside Debian, and starts the background agent.",
                    "Copie o comando abaixo, abra o Termux, cole uma única vez e aperte Enter. Ele ativa allow-external-apps=true, atualiza o Cavern no Debian e liga o agente em segundo plano."),
            TextSize = 12
        };
        desc.SetTextColor(TextSecondary);
        desc.SetPadding(0, 0, 0, Dp(10));
        sheet.AddView(desc);

        TextView cmdBox = new(this) {
            Text = TermuxBootstrapCmd,
            TextSize = 11,
            Background = Pill(CardElevated, 14, StrokeColor)
        };
        cmdBox.SetTypeface(Typeface.Monospace, TypefaceStyle.Normal);
        cmdBox.SetTextColor(TextPrimary);
        cmdBox.SetPadding(Dp(12), Dp(10), Dp(12), Dp(10));
        sheet.AddView(cmdBox);

        LinearLayout row1 = MakeRow();
        row1.SetPadding(0, Dp(12), 0, 0);
        Button copyBtn = MakeButton(T("Copy Command", "Copiar Comando"), AccentBlue, TextPrimary, true);
        copyBtn.Click += (_, _) => {
            Android.Content.ClipboardManager cm = (Android.Content.ClipboardManager)GetSystemService(ClipboardService);
            if (cm != null) {
                cm.PrimaryClip = ClipData.NewPlainText("OmniAgent Termux Setup", TermuxBootstrapCmd);
            }
            Toast.MakeText(this, T("Command copied!", "Comando copiado!"), ToastLength.Short)?.Show();
        };
        row1.AddView(copyBtn, MakeWeightedLp(1f, rightMarginDp: 8));

        Button openTermuxBtn = MakeButton(T("Open Termux", "Abrir Termux"), CardElevated, TextPrimary, false);
        openTermuxBtn.Click += (_, _) => {
            Intent launch = PackageManager?.GetLaunchIntentForPackage("com.termux");
            if (launch != null) {
                launch.AddFlags(ActivityFlags.NewTask);
                StartActivity(launch);
            } else {
                Toast.MakeText(this, T("Termux not found.", "Termux não encontrado."), ToastLength.Short)?.Show();
            }
        };
        row1.AddView(openTermuxBtn, MakeWeightedLp(1f));
        sheet.AddView(row1);

        LinearLayout row2 = MakeRow();
        row2.SetPadding(0, Dp(8), 0, 0);
        Button permsBtn = MakeButton(T("App Permissions", "Permissões do App"), CardElevated, TextPrimary, false);
        permsBtn.Click += (_, _) => {
            Intent intent = new(Settings.ActionApplicationDetailsSettings);
            intent.SetData(Android.Net.Uri.FromParts("package", PackageName, null));
            StartActivity(intent);
        };
        row2.AddView(permsBtn, MakeWeightedLp(1f, rightMarginDp: 8));

        Button closeBtn = MakeButton(T("Close", "Fechar"), CardElevated, TextSecondary, false);
        closeBtn.Click += (_, _) => HideModal();
        row2.AddView(closeBtn, MakeWeightedLp(1f));
        sheet.AddView(row2);

        PresentSheet(sheet);
    }

    Button MakeModeButton(string label, string modeValue) {
        Button b = MakeButton(label, CardElevated, TextPrimary, false);
        b.Click += (_, _) => {
            store.SecurityMode = modeValue;
            OmniSettingsStore.AppendActivity("SEC", $"Security mode -> {modeValue}");
            RefreshUi();
        };
        return b;
    }

    void ToggleService() {
        if (OmniAgentForegroundService.IsRunning) {
            store.ServiceEnabled = false;
            OmniAgentForegroundService.Stop(this);
        } else {
            if (!store.HasPairSecret || !OmniSettingsStore.TryNormalizeRelayUrl(store.RelayUrl, out _)) {
                ShowRelayConfigSheet();
                return;
            }
            store.ServiceEnabled = true;
            OmniAgentForegroundService.Start(this);
        }
        RefreshUi();
    }

    void RefreshUi() {
        if (statusBadge == null) {
            return;
        }
        bool running = OmniAgentForegroundService.IsRunning;
        statusBadge.Text = running
            ? T("● ONLINE (channel: android)", "● ONLINE (canal: android)")
            : T("○ STANDBY OFF", "○ DESLIGADO");
        statusBadge.SetTextColor(running ? AccentGreen : TextSecondary);
        statusBadge.Background = Pill(running ? Color.Argb(36, 34, 197, 94) : CardElevated, 20);

        statusSubtitle.Text = running
            ? T(
                $"Unified Hub v6 bridge active • {OmniAgentForegroundService.CommandsHandled} cmd(s) handled",
                $"Ponte Hub Unificado v6 ativa • {OmniAgentForegroundService.CommandsHandled} comando(s)")
            : T(
                "Tap Start Agent for remote Cavernize bridge, Cloudflare auto-deploy, GitHub push & Termux wake.",
                "Toque em Ligar Agente para ponte Cavernize, auto-deploy Cloudflare, GitHub push e Termux.");

        toggleServiceBtn.Text = running ? T("Stop Agent", "Parar Agente") : T("Start Agent", "Ligar Agente");
        toggleServiceBtn.Background = Pill(running ? AccentRed : AccentBlue, 18);
        if (langBtn != null) {
            langBtn.Text = FormatLangBadge();
        }

        if (overlayPermBtn != null) {
            bool okOv = OmniKnockApprovalActivity.CanDrawInstantOverlay(this);
            overlayPermBtn.Text = okOv ? T("✓ Instant Popup", "✓ Popup Tela") : T("! Instant Popup", "! Ativar Popup");
            overlayPermBtn.SetTextColor(okOv ? AccentGreen : AccentRed);
            overlayPermBtn.Background = Pill(okOv ? Color.Argb(32, 34, 197, 94) : CardElevated, 14, okOv ? null : AccentRed);
        }
        if (batteryPermBtn != null) {
            bool okBat = OmniAgentForegroundService.IsBatteryUnrestricted(this);
            batteryPermBtn.Text = okBat ? T("✓ Battery Free", "✓ Bateria Livre") : T("! Battery Free", "! Bateria Livre");
            batteryPermBtn.SetTextColor(okBat ? AccentGreen : AccentRed);
            batteryPermBtn.Background = Pill(okBat ? Color.Argb(32, 34, 197, 94) : CardElevated, 14, okBat ? null : AccentRed);
        }
        if (cavernizeLinkBtn != null) {
            bool cavInst = OmniAgentForegroundService.IsPackageInstalled(this, CavernizeIpcBridgeClient.CavernizePackage);
            bool cavSig = cavInst && CavernizeIpcBridgeClient.IsSignatureVerified(this);
            cavernizeLinkBtn.Text = cavSig
                ? T("✓ Cavernize Local IPC Connected — Open App", "✓ Ponte Local Cavernize Ativa — Abrir App")
                : (cavInst
                    ? T("! Cavernize Installed (Update APK for IPC)", "! Cavernize Instalado (Atualize para IPC)")
                    : T("Install Cavernize Android", "Baixar Cavernize Android"));
            cavernizeLinkBtn.SetTextColor(cavSig ? AccentGreen : TextPrimary);
        }

        string mode = store.SecurityMode;
        StyleModeBtn(modeSmartBtn, mode == "smart");
        StyleModeBtn(modeBioBtn, mode == "biometric");
        StyleModeBtn(modeTrustedBtn, mode == "trusted");

        string pairDisp = store.HasPairSecret ? OmniCrypto.FormatPairKeyDisplay(store.LoadPairSecretHex()) : T("Not paired", "Sem chave");
        relaySummaryTv.Text = $"Worker: {(string.IsNullOrEmpty(store.RelayUrl) ? T("Not set", "Não configurado") : store.RelayUrl)}  •  {T("Key", "Chave")}: {pairDisp}";
        if (cloudflareSummaryTv != null) {
            cloudflareSummaryTv.Text = $"Cloudflare Auto-Deploy: {store.ResolveEffectiveWorkerName()}  •  Token: {(store.HasCloudflareToken ? T("Encrypted in Keystore", "Salvo no Keystore") : T("Not set", "Não configurado"))}";
        }
        githubSummaryTv.Text = $"GitHub: {store.GitHubRepo} ({store.GitHubBranch})  •  PAT: {(store.HasGitHubPat ? T("Encrypted in Keystore", "Salvo no Keystore") : T("Optional", "Opcional"))}";

        List<string> lines = OmniSettingsStore.GetActivitySnapshot();
        logViewTv.Text = lines.Count == 0
            ? T("No commands yet. Start the agent or wake Termux daemon.", "Nenhum comando ainda. Ligue o agente ou acorde o daemon no Termux.")
            : string.Join("\n", lines.Take(18));
    }

    void StyleModeBtn(Button btn, bool selected) {
        btn.Background = Pill(selected ? AccentBlue : CardElevated, 14, selected ? null : StrokeColor);
        btn.SetTextColor(selected ? TextPrimary : TextSecondary);
    }

    void ShowRelayConfigSheet() {
        LinearLayout sheet = CreateSheet(T("Cloudflare Relay & Pairing Key", "Cloudflare Worker & Chave de Pareamento"));
        EditText urlInput = MakeInput(T("https://cavernize-diag-relay.xxx.workers.dev", "https://cavernize-diag-relay.xxx.workers.dev"), store.RelayUrl, false);
        EditText keyInput = MakeInput(T("Paste 64-hex CAVPAIR key", "Cole a chave CAVPAIR (64 hex)"), store.LoadPairSecretHex(), false);

        sheet.AddView(MakeFieldLabel("Cloudflare Worker URL"));
        sheet.AddView(urlInput);
        sheet.AddView(MakeFieldLabel(T("256-bit Pairing Key (AES-256-GCM Keystore)", "Chave 256-bit (Keystore AES-256-GCM)")));
        sheet.AddView(keyInput);

        TextView healthResultTv = new(this) { Text = "", TextSize = 12, Visibility = ViewStates.Gone };
        healthResultTv.SetPadding(0, Dp(8), 0, 0);
        sheet.AddView(healthResultTv);

        LinearLayout btnRow = MakeRow();
        btnRow.SetPadding(0, Dp(14), 0, 0);
        Button testBtn = MakeButton(T("Test", "Testar"), CardElevated, TextPrimary, false);
        testBtn.Click += async (_, _) => {
            healthResultTv.Visibility = ViewStates.Visible;
            healthResultTv.SetTextColor(TextSecondary);
            healthResultTv.Text = T("Testing Worker /health...", "Testando Worker /health...");
            (bool ok, _, _, string detail) = await OmniAgentForegroundService.TestRelayHealthAsync(urlInput.Text);
            RunOnUiThread(() => {
                healthResultTv.SetTextColor(ok ? AccentGreen : AccentRed);
                healthResultTv.Text = ok ? $"✓ {detail}" : $"✗ {detail}";
            });
        };
        btnRow.AddView(testBtn, MakeWeightedLp(1f, rightMarginDp: 6));

        Button genBtn = MakeButton(T("Generate", "Gerar"), CardElevated, TextPrimary, false);
        genBtn.Click += (_, _) => keyInput.Text = OmniCrypto.GeneratePairSecretHex();
        btnRow.AddView(genBtn, MakeWeightedLp(1f, rightMarginDp: 6));

        Button saveBtn = MakeButton(T("Save", "Salvar"), AccentBlue, TextPrimary, true);
        saveBtn.Click += (_, _) => {
            if (!OmniSettingsStore.TryNormalizeRelayUrl(urlInput.Text, out string normUrl)) {
                Toast.MakeText(this, T("Invalid https:// Worker URL", "URL https:// inválida"), ToastLength.Short)?.Show();
                return;
            }
            string normKey = OmniCrypto.NormalizePairSecretHex(keyInput.Text);
            if (string.IsNullOrEmpty(normKey)) {
                Toast.MakeText(this, T("Key must be 64 hex chars", "Chave precisa ter 64 caracteres hex"), ToastLength.Short)?.Show();
                return;
            }
            store.RelayUrl = normUrl;
            store.SavePairSecretHex(normKey);
            OmniSettingsStore.AppendActivity("CFG", "Saved Cloudflare Worker URL & Keystore pairing key.");
            HideModal();
            RefreshUi();
        };
        btnRow.AddView(saveBtn, MakeWeightedLp(1f));
        sheet.AddView(btnRow);

        LinearLayout knockRow = MakeRow();
        knockRow.SetPadding(0, Dp(8), 0, 0);
        Button knockToggleBtn = MakeButton(
            store.SmartKnockEnabled
                ? T("Smart Knock: ON", "Smart Knock: ATIVO")
                : T("Smart Knock: OFF", "Smart Knock: DESLIGADO"),
            store.SmartKnockEnabled ? Color.Argb(45, 34, 197, 94) : CardElevated,
            store.SmartKnockEnabled ? AccentGreen : TextSecondary,
            false);
        knockToggleBtn.Click += (_, _) => {
            store.SmartKnockEnabled = !store.SmartKnockEnabled;
            knockToggleBtn.Text = store.SmartKnockEnabled
                ? T("Smart Knock: ON", "Smart Knock: ATIVO")
                : T("Smart Knock: OFF", "Smart Knock: DESLIGADO");
            knockToggleBtn.Background = Pill(store.SmartKnockEnabled ? Color.Argb(45, 34, 197, 94) : CardElevated, 14);
            knockToggleBtn.SetTextColor(store.SmartKnockEnabled ? AccentGreen : TextSecondary);
        };
        knockRow.AddView(knockToggleBtn, MakeWeightedLp(1f, rightMarginDp: 6));

        Button copyKnockBtn = MakeButton(T("Copy Public Knock ID", "Copiar Knock ID Público"), CardElevated, TextPrimary, false);
        copyKnockBtn.Click += (_, _) => {
            string normK = OmniCrypto.NormalizePairSecretHex(keyInput.Text);
            if (string.IsNullOrEmpty(normK)) {
                normK = store.LoadPairSecretHex();
            }
            string kid = OmniCrypto.ComputeKnockChannelId(normK);
            if (string.IsNullOrEmpty(kid)) {
                Toast.MakeText(this, T("Generate or save a 64-hex key first", "Gere ou salve uma chave de 64 hex primeiro"), ToastLength.Short)?.Show();
                return;
            }
            if (GetSystemService(ClipboardService) is Android.Content.ClipboardManager cm) {
                cm.PrimaryClip = ClipData.NewPlainText("OmniAgent Public Knock ID", kid);
                Toast.MakeText(this, T("Public Knock ID copied (safe, zero secrets)!", "Knock ID Público copiado (seguro, sem segredos)!"), ToastLength.Long)?.Show();
            }
        };
        knockRow.AddView(copyKnockBtn, MakeWeightedLp(1.2f));
        sheet.AddView(knockRow);

        PresentSheet(sheet);
    }

    void ShowCloudflareDeploySheet() {
        LinearLayout sheet = CreateSheet(T("Cloudflare Auto-Deploy (Keystore)", "Auto-Deploy Cloudflare (Keystore)"));
        TextView desc = new(this) {
            Text = T(
                "Save a Cloudflare API Token (Edit Cloudflare Workers template) once in Android Keystore so AI Studio can update your Worker & Durable Objects automatically without manual uploads.",
                "Salve um Token de API Cloudflare (modelo 'Edit Cloudflare Workers') uma única vez no Keystore para a IA atualizar seu Worker e Durable Objects automaticamente sem upload manual."),
            TextSize = 12
        };
        desc.SetTextColor(TextSecondary);
        desc.SetPadding(0, 0, 0, Dp(6));
        sheet.AddView(desc);

        EditText tokenInput = MakeInput(T("Cloudflare API Token (leave empty to keep)", "Token API Cloudflare (vazio mantém atual)"), "", true);
        EditText workerInput = MakeInput(store.ResolveEffectiveWorkerName(), store.CloudflareWorkerName, false);
        EditText accInput = MakeInput(T("Optional (auto-detected)", "Opcional (detectado automático)"), store.CloudflareAccountId, false);

        sheet.AddView(MakeFieldLabel(T("Cloudflare API Token (AES-256-GCM Keystore)", "Token API Cloudflare (Keystore AES-256-GCM)")));
        sheet.AddView(tokenInput);
        sheet.AddView(MakeFieldLabel(T("Worker Name (optional)", "Nome do Worker (opcional)")));
        sheet.AddView(workerInput);
        sheet.AddView(MakeFieldLabel(T("Account ID (optional)", "Account ID (opcional)")));
        sheet.AddView(accInput);

        TextView testTv = new(this) { Text = "", TextSize = 12, Visibility = ViewStates.Gone };
        testTv.SetPadding(0, Dp(8), 0, 0);
        sheet.AddView(testTv);

        LinearLayout linkRow = MakeRow();
        linkRow.SetPadding(0, Dp(10), 0, 0);
        Button openCfBtn = MakeButton(T("Create CF Token", "Criar Token CF"), CardElevated, TextPrimary, false);
        openCfBtn.Click += (_, _) => OpenUrl("https://dash.cloudflare.com/profile/api-tokens");
        linkRow.AddView(openCfBtn, MakeWeightedLp(1f, rightMarginDp: 8));

        Button testBtn = MakeButton(T("Test Token", "Testar Token"), CardElevated, TextPrimary, false);
        testBtn.Click += async (_, _) => {
            string tok = string.IsNullOrWhiteSpace(tokenInput.Text) ? store.LoadCloudflareToken() : tokenInput.Text.Trim();
            testTv.Visibility = ViewStates.Visible;
            testTv.SetTextColor(TextSecondary);
            testTv.Text = T("Verifying Cloudflare API Token...", "Verificando Token Cloudflare...");
            (bool ok, string accId, _, string msg) = await CloudflareDeployService.TestTokenAsync(tok);
            RunOnUiThread(() => {
                testTv.SetTextColor(ok ? AccentGreen : AccentRed);
                testTv.Text = ok ? $"✓ {msg}" : $"✗ {msg}";
                if (ok && string.IsNullOrWhiteSpace(accInput.Text) && !string.IsNullOrEmpty(accId)) {
                    accInput.Text = accId;
                }
            });
        };
        linkRow.AddView(testBtn, MakeWeightedLp(1f));
        sheet.AddView(linkRow);

        LinearLayout btnRow = MakeRow();
        btnRow.SetPadding(0, Dp(10), 0, 0);
        Button cancelBtn = MakeButton(T("Cancel", "Cancelar"), CardElevated, TextSecondary, false);
        cancelBtn.Click += (_, _) => HideModal();
        btnRow.AddView(cancelBtn, MakeWeightedLp(1f, rightMarginDp: 8));

        Button saveBtn = MakeButton(T("Save", "Salvar"), AccentBlue, TextPrimary, true);
        saveBtn.Click += (_, _) => {
            if (!string.IsNullOrWhiteSpace(tokenInput.Text)) {
                store.SaveCloudflareToken(tokenInput.Text);
            }
            store.CloudflareWorkerName = workerInput.Text;
            store.CloudflareAccountId = accInput.Text;
            OmniSettingsStore.AppendActivity("CFG", $"Saved Cloudflare Auto-Deploy config (worker={store.ResolveEffectiveWorkerName()}).");
            HideModal();
            RefreshUi();
        };
        btnRow.AddView(saveBtn, MakeWeightedLp(1f));
        sheet.AddView(btnRow);
        PresentSheet(sheet);
    }

    void ShowGitHubConfigSheet() {
        LinearLayout sheet = CreateSheet(T("Direct GitHub Push (Optional)", "GitHub Push Direto (Opcional)"));
        EditText repoInput = MakeInput("guumachadooo/Cavern", store.GitHubRepo, false);
        EditText branchInput = MakeInput("master", store.GitHubBranch, false);
        EditText patInput = MakeInput(T("Fine-Grained PAT (Contents: Read & Write)", "Fine-Grained PAT (Contents: Read & Write)"), "", true);

        sheet.AddView(MakeFieldLabel(T("Repository (owner/repo)", "Repositório (dono/repo)")));
        sheet.AddView(repoInput);
        sheet.AddView(MakeFieldLabel("Branch"));
        sheet.AddView(branchInput);
        sheet.AddView(MakeFieldLabel(T("GitHub PAT (leave empty to keep current)", "GitHub PAT (deixe vazio para manter atual)")));
        sheet.AddView(patInput);

        Button openGhBtn = MakeButton(T("Create GitHub Fine-Grained Token", "Criar Token GitHub (Fine-Grained)"), CardElevated, TextPrimary, false);
        LinearLayout.LayoutParams ghLp = new(ViewGroup.LayoutParams.MatchParent, ViewGroup.LayoutParams.WrapContent);
        ghLp.SetMargins(0, Dp(10), 0, 0);
        openGhBtn.Click += (_, _) => OpenUrl("https://github.com/settings/personal-access-tokens/new");
        sheet.AddView(openGhBtn, ghLp);

        LinearLayout btnRow = MakeRow();
        btnRow.SetPadding(0, Dp(10), 0, 0);
        Button cancelBtn = MakeButton(T("Cancel", "Cancelar"), CardElevated, TextSecondary, false);
        cancelBtn.Click += (_, _) => HideModal();
        btnRow.AddView(cancelBtn, MakeWeightedLp(1f, rightMarginDp: 8));

        Button saveBtn = MakeButton(T("Save", "Salvar"), AccentBlue, TextPrimary, true);
        saveBtn.Click += (_, _) => {
            store.GitHubRepo = repoInput.Text;
            store.GitHubBranch = branchInput.Text;
            if (!string.IsNullOrWhiteSpace(patInput.Text)) {
                store.SaveGitHubPat(patInput.Text);
            }
            OmniSettingsStore.AppendActivity("CFG", $"Updated GitHub config ({store.GitHubRepo}@{store.GitHubBranch}).");
            HideModal();
            RefreshUi();
        };
        btnRow.AddView(saveBtn, MakeWeightedLp(1f));
        sheet.AddView(btnRow);
        PresentSheet(sheet);
    }

    LinearLayout CreateSheet(string titleText) {
        LinearLayout sheet = new(this) {
            Orientation = Orientation.Vertical,
            Clickable = true,
            Background = Pill(CardBg, 26, StrokeColor)
        };
        sheet.SetPadding(Dp(20), Dp(20), Dp(20), Dp(22));
        TextView t = new(this) { Text = titleText, TextSize = 17 };
        t.SetTextColor(TextPrimary);
        t.SetTypeface(Typeface.DefaultBold, TypefaceStyle.Bold);
        t.SetPadding(0, 0, 0, Dp(12));
        sheet.AddView(t);
        return sheet;
    }

    void PresentSheet(LinearLayout sheet) {
        modalOverlay.RemoveAllViews();
        FrameLayout.LayoutParams lp = new(ViewGroup.LayoutParams.MatchParent, ViewGroup.LayoutParams.WrapContent) {
            Gravity = GravityFlags.Bottom
        };
        lp.SetMargins(Dp(12), Dp(12), Dp(12), Dp(20));
        modalOverlay.AddView(sheet, lp);
        modalOverlay.Visibility = ViewStates.Visible;
    }

    void HideModal() {
        modalOverlay.Visibility = ViewStates.Gone;
        modalOverlay.RemoveAllViews();
    }

    LinearLayout MakeCard() {
        LinearLayout card = new(this) {
            Orientation = Orientation.Vertical,
            Background = Pill(CardBg, 24, StrokeColor)
        };
        card.SetPadding(Dp(16), Dp(16), Dp(16), Dp(16));
        LinearLayout.LayoutParams lp = new(ViewGroup.LayoutParams.MatchParent, ViewGroup.LayoutParams.WrapContent);
        lp.SetMargins(0, 0, 0, Dp(14));
        card.LayoutParameters = lp;
        return card;
    }

    LinearLayout MakeRow() {
        LinearLayout row = new(this) {
            Orientation = Orientation.Horizontal,
            BaselineAligned = false
        };
        row.SetGravity(GravityFlags.CenterVertical);
        return row;
    }

    LinearLayout.LayoutParams MakeWeightedLp(float weight, int leftMarginDp = 0, int rightMarginDp = 0) {
        LinearLayout.LayoutParams lp = new(0, ViewGroup.LayoutParams.WrapContent, weight);
        lp.SetMargins(Dp(leftMarginDp), 0, Dp(rightMarginDp), 0);
        return lp;
    }

    TextView MakeCardTitle(string text) {
        TextView tv = new(this) { Text = text, TextSize = 15 };
        tv.SetTextColor(TextPrimary);
        tv.SetTypeface(Typeface.DefaultBold, TypefaceStyle.Bold);
        return tv;
    }

    TextView MakeFieldLabel(string text) {
        TextView tv = new(this) { Text = text, TextSize = 12 };
        tv.SetTextColor(TextSecondary);
        tv.SetPadding(0, Dp(8), 0, Dp(4));
        return tv;
    }

    EditText MakeInput(string hint, string initial, bool password) {
        EditText et = new(this) {
            Hint = hint,
            Text = initial ?? "",
            TextSize = 13,
            Background = Pill(CardElevated, 14, StrokeColor),
            InputType = password
                ? (InputTypes.ClassText | InputTypes.TextVariationPassword)
                : (InputTypes.ClassText | InputTypes.TextFlagNoSuggestions)
        };
        et.SetTextColor(TextPrimary);
        et.SetHintTextColor(TextSecondary);
        et.SetSingleLine(true);
        et.SetPadding(Dp(12), Dp(10), Dp(12), Dp(10));
        return et;
    }

    Button MakeButton(string text, Color bg, Color fg, bool bold) {
        Button b = new(this) {
            Text = text,
            TextSize = 13,
            Background = Pill(bg, 16)
        };
        b.SetAllCaps(false);
        b.SetSingleLine(true);
        b.Ellipsize = TextUtils.TruncateAt.End;
        b.SetTextColor(fg);
        b.SetTypeface(bold ? Typeface.DefaultBold : Typeface.Default, bold ? TypefaceStyle.Bold : TypefaceStyle.Normal);
        b.SetPadding(Dp(14), Dp(11), Dp(14), Dp(11));
        b.SetMinHeight(Dp(42));
        return b;
    }

    sealed class SafeAreaInsetsListener : Java.Lang.Object, View.IOnApplyWindowInsetsListener {
        readonly Action<int, int> onInsets;

        public SafeAreaInsetsListener(Action<int, int> onInsets) => this.onInsets = onInsets;

        public WindowInsets OnApplyWindowInsets(View v, WindowInsets insets) {
            if (insets == null) {
                return insets;
            }
            if (OperatingSystem.IsAndroidVersionAtLeast(30)) {
                Insets bars = insets.GetInsets(WindowInsets.Type.SystemBars() | WindowInsets.Type.DisplayCutout());
                onInsets(bars.Top, bars.Bottom);
            } else {
#pragma warning disable CS0618, CA1422
                onInsets(insets.SystemWindowInsetTop, insets.SystemWindowInsetBottom);
#pragma warning restore CS0618, CA1422
            }
            return insets;
        }
    }
}
