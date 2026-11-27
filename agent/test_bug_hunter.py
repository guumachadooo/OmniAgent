#!/usr/bin/env python3
"""Unit tests for `.agents/agent/cavern_bug_hunter.py` pure parsers, classifiers, ledger & orchestrator."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

AGENT_DIR = Path(__file__).resolve().parent
if str(AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_DIR))

import cavern_bug_hunter as bh  # noqa: E402


class FakeBridge:
    """Deterministic fake Cavernize bridge for offline orchestrator testing."""

    def __init__(self) -> None:
        self.calls = 0
        self.history: list[tuple[str, dict]] = []
        self.settings = {
            "Iso226Enabled": True,
            "LgsrEnabled": False,
            "LoudnessNormModeIndex": 1,
            "BinauralProfileId": "sadie_d2",
        }
        self._log_lines = [
            "[2026-11-24 14:00:00.000] [WARN] [T#1] [AUDIO] Baseline warning",
        ]

    def cmd(self, name: str, args: dict | None = None, tier: int = 2, timeout_sec: int | None = None) -> dict:
        self.calls += 1
        a = dict(args or {})
        self.history.append((name, a))
        if name == "ping":
            return {"ok": True, "elapsedMs": 10, "body": "{}", "json": {"appVersion": "2.9.69", "appBuild": 69, "device": "S25U", "android": "17", "uiHostAttached": True, "counts": {"warn": 1, "error": 0, "fatal": 0}}, "error": ""}
        if name == "capabilities":
            return {"ok": True, "elapsedMs": 5, "body": "{}", "json": {"commands": [{"cmd": "ping"}, {"cmd": "chaos_hunt"}]}, "error": ""}
        if name == "app_state":
            return {"ok": True, "elapsedMs": 8, "body": "{}", "json": {"activeTab": 2, "loadedFile": "/sdcard/a.flac", "isPlaying": True, "isOperationRunning": False, "libraryTrackCount": 10}, "error": ""}
        if name == "settings_get":
            return {"ok": True, "elapsedMs": 5, "body": json.dumps(self.settings), "json": dict(self.settings), "error": ""}
        if name == "settings_set":
            if "key" in a:
                self.settings[a["key"]] = a["value"]
                if a["key"] == "LgsrEnabled" and a["value"] is True:
                    self._log_lines.append("[2026-11-24 14:01:00.000] [ERROR] [T#9] [DSP] Synthetic LGSR toggle error")
            elif "values" in a:
                self.settings.update(a["values"])
            return {"ok": True, "elapsedMs": 5, "body": "{}", "json": {"applied": ["ok"]}, "error": ""}
        if name == "memory":
            return {"ok": True, "elapsedMs": 5, "body": "{}", "json": {"availRamMb": 4000, "managedGcMb": 30, "lowMemory": False}, "error": ""}
        if name == "log_tail":
            return {"ok": True, "elapsedMs": 5, "body": "\n".join(self._log_lines), "json": None, "error": ""}
        if name == "file_read":
            return {"ok": False, "elapsedMs": 5, "body": "rejected", "json": None, "error": "rejected"}
        if name == "definitely_nonexistent_cmd_xyz":
            return {"ok": False, "elapsedMs": 5, "body": "Unknown command", "json": None, "error": ""}
        if name == "chaos_hunt":
            return {"ok": True, "elapsedMs": 40, "body": "{}", "json": {"anomalies": [{"severity": "HIGH", "category": "dsp_race", "scenario": "concurrent_binaural", "message": "Detected static state race"}]}, "error": ""}
        return {"ok": True, "elapsedMs": 5, "body": "{}", "json": {}, "error": ""}

    def pipeline(self, steps: list[dict], stop_on_error: bool = False, tier: int = 2, timeout_sec: int = 45) -> list[dict]:
        out = []
        for st in steps:
            r = self.cmd(str(st.get("cmd") or ""), st.get("args") or {}, tier=tier, timeout_sec=timeout_sec)
            out.append({"ok": r["ok"], "subCmd": str(st.get("cmd") or ""), "body": r["body"], "json": r["json"], "error": r["error"]})
            if stop_on_error and not r["ok"]:
                break
        return out

    def fetch_logcat(self, lines: int = 120, min_level: str = "W", buffer: str = "main") -> str:
        return "E/libsigchain(123): Setting SIGSEGV to SIG_DFL\nI/Choreographer(100): Skipped 48 frames! The application may be doing too much work on its main thread.\n"


class BugHunterUnitTests(unittest.TestCase):
    def test_parse_applog_and_classify(self) -> None:
        raw = (
            "[2026-11-24 14:33:10.733] [WARN] [T#19] [AUDIO] Pro-Audio native backend reported disconnect (WritePlayback=-1, streak=1)\n"
            "[2026-11-24 14:34:58.904] [ERROR] [T#3] [DSP] NaN detected in filter\n"
            "[2026-11-24 14:35:00.000] [FATAL] [T#1] [SYS] AppDomain.UnhandledException: boom\n"
        )
        entries = bh.parse_app_logger_lines(raw)
        self.assertEqual(len(entries), 3)
        self.assertEqual(bh.classify_applog_severity(entries[0])[0], "MEDIUM")
        self.assertEqual(bh.classify_applog_severity(entries[1])[0], "HIGH")
        self.assertEqual(bh.classify_applog_severity(entries[2])[0], "CRITICAL")

    def test_logcat_filters_benign_and_catches_real_anomalies(self) -> None:
        raw = "\n".join([
            "--------- beginning of main",
            "E/libsigchain(14176): Setting SIGSEGV to SIG_DFL",
            "E/libsigchain(14176):   #00 pc 00006790 /apex/com.android.art/lib64/libsigchain.so",
            "I/Choreographer(10778): Skipped 12 frames!",
            "I/Choreographer(10778): Skipped 72 frames! The application may be doing too much work on its main thread.",
            "E/AAudioStream(10778): AAudio stream disconnected error=-899",
            "E/AndroidRuntime(10778): FATAL EXCEPTION: main",
        ])
        anomalies = bh.parse_logcat_anomalies(raw)
        cats = [a["category"] for a in anomalies]
        self.assertNotIn("logcat_error", cats)  # libsigchain filtered out
        self.assertIn("ui_jank", cats)  # 72 frames kept, 12 ignored
        self.assertIn("audio_native", cats)
        self.assertIn("crash", cats)
        self.assertEqual(len(anomalies), 3)

    def test_dedup_ledger_collapses_volatile_numbers(self) -> None:
        ledger = bh.FindingLedger()
        is_new1, _ = ledger.record(
            severity="MEDIUM",
            vector="V1",
            source="applogger",
            category="perf",
            title="VocalChain slow: 20.8ms for 42.7ms block (49% RT budget)",
            repro_step="step1",
            evidence="ev1",
        )
        is_new2, f2 = ledger.record(
            severity="MEDIUM",
            vector="V1",
            source="applogger",
            category="perf",
            title="VocalChain slow: 24.1ms for 42.7ms block (56% RT budget)",
            repro_step="step2",
            evidence="ev2",
        )
        self.assertTrue(is_new1)
        self.assertFalse(is_new2)
        self.assertEqual(f2.count, 2)
        self.assertEqual(len(ledger.ranked()), 1)

    def test_ui_tree_anomaly_detection(self) -> None:
        tree = {
            "activeDialogs": 2,
            "activity": {
                "type": "LinearLayout",
                "w": 1080,
                "h": 2400,
                "children": [
                    {"type": "TextView", "text": "Loudness: NaN dB", "w": 200, "h": 40},
                    {"type": "TextView", "text": "Track {0} of {1}", "w": 200, "h": 40},
                    {"type": "Button", "text": "OK", "clickable": True, "w": 0, "h": 0},
                ],
            },
        }
        defects = bh.analyze_ui_tree_anomalies(tree, "Tab0")
        cats = {d["category"] for d in defects}
        self.assertIn("ui_stacked_dialogs", cats)
        self.assertIn("ui_nan_leak", cats)
        self.assertIn("ui_placeholder_leak", cats)
        self.assertIn("ui_zero_size_button", cats)

    def test_acoustic_probe_analyzer(self) -> None:
        issues = bh.analyze_acoustic_probe_result({"status": "PASS", "fingerprint": {"gainDb": -166.0, "peakDbfs": 2.4}}, "probe1")
        cats = {i["category"] for i in issues}
        self.assertIn("dsp_silent_output", cats)
        self.assertIn("dsp_clipping", cats)

    def test_pick_edge_case_tracks(self) -> None:
        tracks = [
            {"Title": "Zero", "DurationMs": 0, "ChannelCount": 2, "FormatBadge": "MP3", "FilePath": "/a/zero.mp3"},
            {"Title": "Mono", "DurationMs": 1000, "ChannelCount": 1, "FormatBadge": "MP3", "FilePath": "/a/mono.mp3"},
            {"Title": "Atmos", "DurationMs": 2000, "ChannelCount": 6, "IsSpatial": True, "FormatBadge": "FLAC", "FilePath": "/a/at.flac"},
            {"Title": "Spec", "DurationMs": 2000, "ChannelCount": 2, "FormatBadge": "WAV", "FilePath": "/a/Ready® (1).wav"},
        ]
        picks = bh.pick_edge_case_library_tracks(tracks, max_picks=4)
        reasons = [r for r, _ in picks]
        self.assertIn("zero_duration_metadata", reasons)
        self.assertIn("spatial_or_multichannel", reasons)
        self.assertIn("mono_channel", reasons)
        self.assertIn("unicode_or_special_chars", reasons)

    def test_live_orchestrator_captures_delta_and_restores_settings(self) -> None:
        fb = FakeBridge()
        hunter = bh.LiveBugHunter(fb, mode="standard", allow_mutate=True, verbose=False)
        report = hunter.run_hunt(selected_vectors=["V3", "V8"])
        self.assertTrue(report["ok"])
        self.assertTrue(report["meta"]["restoredState"])
        # Settings must be restored to original baseline after chaos!
        self.assertEqual(fb.settings["Iso226Enabled"], True)
        self.assertEqual(fb.settings["LgsrEnabled"], False)
        self.assertEqual(fb.settings["LoudnessNormModeIndex"], 1)
        # Must have caught BOTH the live synthetic LGSR error AND the in-app chaos_hunt anomaly!
        titles = [f["title"] for f in report["findings"]]
        self.assertTrue(any("LGSR toggle error" in t for t in titles))
        self.assertTrue(any("static state race" in t for t in titles))
        md = bh.generate_report_markdown(report)
        self.assertIn("Cavernize Live Adversarial Bug-Hunter Report", md)
        self.assertIn("Active Findings", md)
        self.assertEqual(report["meta"].get("restoreDiffKeys"), [])

    def test_split_applog_by_session_stratification(self) -> None:
        raw = "\n".join([
            "[2026-11-23 10:00:00.000] [INFO] [T#1] [SYS] === Session started [sid=old001] ===",
            "[2026-11-23 10:01:00.000] [ERROR] [T#2] [DSP] Pre-fix error from yesterday",
            "[2026-11-24 14:00:00.000] [INFO] [T#1] [SYS] === Session started [sid=cur999] ===",
            "[2026-11-24 14:02:00.000] [WARN] [T#3] [AUDIO] Current session warning",
        ])
        cur, hist = bh.split_applog_by_session(raw, "cur999")
        self.assertEqual(len(cur), 1)
        self.assertEqual(len(hist), 1)
        self.assertIn("Current session warning", cur[0]["msg"])
        self.assertIn("Pre-fix error", hist[0]["msg"])

        # Missing session id treats all entries as historical rather than falsely active
        cur_empty, hist_all = bh.split_applog_by_session(raw, "")
        self.assertEqual(len(cur_empty), 0)
        self.assertEqual(len(hist_all), 4)

    def test_classify_ui_latency_ms(self) -> None:
        self.assertIsNone(bh.classify_ui_latency_ms(120.0, "tap_upmix"))
        stall = bh.classify_ui_latency_ms(450.0, "tap_upmix")
        self.assertIsNotNone(stall)
        assert stall is not None
        self.assertEqual(stall["severity"], "MEDIUM")
        self.assertEqual(stall["category"], "ui_stall")
        freeze = bh.classify_ui_latency_ms(2100.0, "tap_cavernize")
        self.assertIsNotNone(freeze)
        assert freeze is not None
        self.assertEqual(freeze["severity"], "HIGH")
        self.assertEqual(freeze["category"], "ui_freeze")

    def test_analyze_spatial_trajectory_clean_vs_collapse_vs_pingpong(self) -> None:
        # Clean smooth arc at constant radius r=1.0
        clean = [
            {"x": 0.0, "y": 0.0, "z": 1.0},
            {"x": 0.2588, "y": 0.0, "z": 0.9659},
            {"x": 0.5, "y": 0.0, "z": 0.866},
            {"x": 0.7071, "y": 0.0, "z": 0.7071},
            {"x": 0.866, "y": 0.0, "z": 0.5},
        ]
        self.assertEqual(bh.analyze_spatial_trajectory(clean, "Musical", "Guitar"), [])

        # Radial collapse toward center + pumping
        collapse = [
            {"x": 0.8, "y": 0.0, "z": 0.6},
            {"x": 0.2, "y": 0.0, "z": 0.2},
            {"x": 0.8, "y": 0.0, "z": 0.6},
            {"x": 0.15, "y": 0.0, "z": 0.15},
            {"x": 0.8, "y": 0.0, "z": 0.6},
            {"x": 0.2, "y": 0.0, "z": 0.2},
        ]
        collapse_cats = {i["category"] for i in bh.analyze_spatial_trajectory(collapse, "Cinematic", "Drums")}
        self.assertIn("spatial_radial_collapse", collapse_cats)
        self.assertIn("spatial_radial_pingpong", collapse_cats)

        # Azimuth ping-pong across center (alternating left/right sign flips)
        pingpong = [
            {"x": 0.7, "y": 0.0, "z": 0.714},
            {"x": -0.7, "y": 0.0, "z": 0.714},
            {"x": 0.7, "y": 0.0, "z": 0.714},
            {"x": -0.7, "y": 0.0, "z": 0.714},
            {"x": 0.7, "y": 0.0, "z": 0.714},
        ]
        pp_cats = {i["category"] for i in bh.analyze_spatial_trajectory(pingpong, "Musical", "Lead")}
        self.assertIn("spatial_pingpong_oscillation", pp_cats)

    def test_orbit_unwrapped_angular_velocity_avoids_wrap_false_positives(self) -> None:
        import math as _math
        # Smooth continuous 2-revolution 360deg orbit crossing 0deg and +/-180deg multiple times
        smooth_orbit = []
        for k in range(49):
            ang = _math.radians(-170.0 + k * 15.0)
            smooth_orbit.append({"x": _math.sin(ang), "y": 0.1, "z": _math.cos(ang)})
        self.assertEqual(bh.analyze_spatial_trajectory(smooth_orbit, "Orbit", "drums"), [])

        # Erratic back-and-forth angular velocity reversals in Orbit mode must still be caught
        erratic_angles = [0.0, 25.0, 5.0, 35.0, 10.0, 40.0, 15.0]
        erratic_orbit = [
            {"x": _math.sin(_math.radians(a)), "y": 0.0, "z": _math.cos(_math.radians(a))}
            for a in erratic_angles
        ]
        cats = {i["category"] for i in bh.analyze_spatial_trajectory(erratic_orbit, "Orbit", "other")}
        self.assertIn("spatial_pingpong_oscillation", cats)

    def test_v5_corrupt_file_expected_rejections_suppressed_while_crashes_caught(self) -> None:
        fb = FakeBridge()
        hunter = bh.LiveBugHunter(fb, mode="standard", allow_mutate=False, verbose=False)
        sample_log = "\n".join([
            "[2026-11-25 08:32:37.713] [WARN] [T#7] [APP] OpenLocalPathWithFallbackAsync rejected 'corrupt.mp3': Could not decode this audio file. It may be corrupted or use an unsupported codec.",
            "[2026-11-25 08:32:37.716] [ERROR] [UI] [UI] UI Error shown: Could not decode this audio file. It may be corrupted or use an unsupported codec.",
            "[2026-11-25 08:32:37.800] [ERROR] [T#9] [DSP] NullReferenceException in corrupt header parser",
        ])
        added = hunter._ingest_applog_delta(sample_log, "V5_corrupt_file_fuzzer", "corrupt fuzz burst")
        self.assertEqual(added, 1)
        findings = hunter.ledger.ranked()
        self.assertEqual(len(findings), 1)
        self.assertIn("NullReferenceException", findings[0].title)

    def test_finding_ledger_origin_stratification_and_live_promotion(self) -> None:
        ledger = bh.FindingLedger()
        ledger.record(
            severity="HIGH",
            vector="V1",
            source="applogger",
            category="audio_error",
            title="Native backend disconnect",
            repro_step="historical_log",
            evidence="old",
            origin="historical_archive",
        )
        ledger.record(
            severity="MEDIUM",
            vector="V1",
            source="applogger",
            category="perf",
            title="Current session jank",
            repro_step="current_log",
            evidence="cur",
            origin="current_session",
        )
        self.assertEqual(len(ledger.active_findings()), 1)
        self.assertEqual(len(ledger.historical_findings()), 1)
        self.assertEqual(ledger.counts_by_severity(active_only=True)["HIGH"], 0)
        self.assertEqual(ledger.counts_by_severity(active_only=False)["HIGH"], 1)

        # Reproducing the historical defect live must promote it to live_triggered!
        is_new, promoted = ledger.record(
            severity="HIGH",
            vector="V5",
            source="applogger",
            category="audio_error",
            title="Native backend disconnect",
            repro_step="live_repro_step",
            evidence="new",
            origin="live_triggered",
        )
        self.assertFalse(is_new)
        self.assertEqual(promoted.origin, "live_triggered")
        self.assertEqual(promoted.vector, "V5")
        self.assertEqual(len(ledger.active_findings()), 2)
        self.assertEqual(ledger.counts_by_origin()["historical_archive"], 0)

    def test_v9_music_only_album_oracle_and_runtime_telemetry(self) -> None:
        class V9MockBridge(FakeBridge):
            def __init__(self) -> None:
                super().__init__()
                self.last_lq_args: list[dict] = []

            def cmd(self, name: str, args: dict | None = None, *, tier: int = 0, timeout_sec: int = 30) -> dict:
                args = args or {}
                if name == "capabilities":
                    return {
                        "ok": True,
                        "body": "{}",
                        "json": {
                            "commands": [
                                {"cmd": "ping"},
                                {"cmd": "library_query"},
                                {"cmd": "runtime_telemetry"},
                                {"cmd": "tab_switch"},
                                {"cmd": "log_tail"},
                                {"cmd": "logcat_tail"},
                                {"cmd": "anomalies"},
                            ]
                        },
                        "elapsedMs": 5,
                        "error": "",
                    }
                if name == "library_query":
                    self.last_lq_args.append(dict(args))
                    q = str(args.get("query") or "").lower()
                    all_music = [
                        {"Title": "willow", "Album": "evermore", "Artist": "Taylor Swift", "DurationMs": 214000, "FilePath": "/Music/evermore/01.flac"},
                        {"Title": "evermore", "Album": "evermore", "Artist": "Taylor Swift", "DurationMs": 304000, "FilePath": "/Music/evermore/15.flac"},
                        {"Title": "gold rush", "Album": "evermore", "Artist": "Taylor Swift", "DurationMs": 185000, "FilePath": "/Music/evermore/03.flac"},
                    ]
                    tracks = [t for t in all_music if not q or q in t["Album"].lower() or q in t["Title"].lower()]
                    return {"ok": True, "body": "{}", "json": {"tracks": tracks, "musicOnly": bool(args.get("musicOnly"))}, "elapsedMs": 8, "error": ""}
                if name == "runtime_telemetry":
                    return {
                        "ok": True,
                        "body": "{}",
                        "json": {
                            "frameMetrics": {"sampledFrames": 120, "p95TotalMs": 9.4},
                            "audioXRuns": {"audioTrackUnderrunCount": 0},
                        },
                        "elapsedMs": 4,
                        "error": "",
                    }
                return super().cmd(name, args, tier=tier, timeout_sec=timeout_sec)

        fb = V9MockBridge()
        hunter = bh.LiveBugHunter(fb, mode="standard", allow_mutate=False, verbose=False)
        report = hunter.run_hunt(selected_vectors=["V9"])
        self.assertTrue(report["ok"])
        self.assertTrue(any(a.get("musicOnly") is True for a in fb.last_lq_args))
        v9_notes = report["vectors"][0]["notes"]
        self.assertIn("albumsAudited=1", v9_notes)
        self.assertIn("fm=120/p95=9.4ms,xruns=0", v9_notes)
        self.assertEqual([f for f in report["findings"] if f["severity"] == "HIGH"], [])

    def test_q1_wcag_contrast_and_alpha_compositing(self) -> None:
        # Pure white (#FFFFFFFF = -1) on One UI 9 BgScreen (#FF080A10 = -16250868)
        white_on_dark = bh.compute_wcag_contrast(-1, -16250868)
        self.assertGreater(white_on_dark, 15.0)
        # Near-black (#FF141822) on BgScreen (#FF080A10) fails WCAG (<3.0)
        dark_on_dark = bh.compute_wcag_contrast(0xFF141822, -16250868)
        self.assertLess(dark_on_dark, 3.0)
        # Semi-transparent white (50% alpha) over dark still composited deterministically
        semi_white = bh.compute_wcag_contrast(0x80FFFFFF, -16250868)
        self.assertGreater(semi_white, 4.5)
        self.assertLess(semi_white, white_on_dark)

    def test_q1_rect_iou_geometry(self) -> None:
        # Disjoint rectangles
        iou0, ow0, oh0 = bh.compute_rect_iou((0, 0, 100, 100), (120, 0, 100, 100))
        self.assertEqual((iou0, ow0, oh0), (0.0, 0, 0))
        # Half overlap horizontally (50x100 intersection over 150x100 union = 1/3)
        iou1, ow1, oh1 = bh.compute_rect_iou((0, 0, 100, 100), (50, 0, 100, 100))
        self.assertAlmostEqual(iou1, 1.0 / 3.0, places=4)
        self.assertEqual((ow1, oh1), (50, 100))
        # Identical rectangles -> IoU 1.0
        iou2, ow2, oh2 = bh.compute_rect_iou((10, 20, 80, 40), (10, 20, 80, 40))
        self.assertAlmostEqual(iou2, 1.0, places=6)
        self.assertEqual((ow2, oh2), (80, 40))

    def test_q1_golden_ssim_seed_pass_fail_and_dim_mismatch(self) -> None:
        import base64 as _b64
        import tempfile as _tf

        w, h = 48, 64
        ref = bytes(((x * 5 + y * 3) % 256) for y in range(h) for x in range(w))
        mut = bytearray(ref)
        for y in range(8, 56):
            for x in range(4, 44):
                mut[y * w + x] = 255 if (x + y) % 2 == 0 else 0

        same_res = bh.compute_ssim_luma_grid(w, h, list(ref), list(ref))
        self.assertTrue(same_res["ok"])
        self.assertAlmostEqual(same_res["ssim"], 1.0, places=5)
        self.assertIsNone(same_res["diffBBox"])

        diff_res = bh.compute_ssim_luma_grid(w, h, list(ref), list(mut))
        self.assertTrue(diff_res["ok"])
        self.assertLess(diff_res["ssim"], 0.6)
        self.assertIsNotNone(diff_res["diffBBox"])

        with _tf.TemporaryDirectory() as td:
            ref_payload = {"lumaW": w, "lumaH": h, "lumaB64": _b64.b64encode(ref).decode("ascii")}
            mut_payload = {"lumaW": w, "lumaH": h, "lumaB64": _b64.b64encode(bytes(mut)).decode("ascii")}
            bad_dim_payload = {"lumaW": 32, "lumaH": 32, "lumaB64": _b64.b64encode(bytes(32 * 32)).decode("ascii")}

            seeded = bh.compare_screenshot_against_golden("Tab0", ref_payload, td)
            self.assertEqual(seeded["status"], "SEEDED")
            passed = bh.compare_screenshot_against_golden("Tab0", ref_payload, td)
            self.assertEqual(passed["status"], "PASS")
            self.assertIsNone(passed["finding"])
            failed = bh.compare_screenshot_against_golden("Tab0", mut_payload, td)
            self.assertEqual(failed["status"], "FAIL")
            self.assertEqual(failed["finding"]["category"], "ui_visual_regression_ssim")
            dim_drift = bh.compare_screenshot_against_golden("Tab0", bad_dim_payload, td)
            self.assertEqual(dim_drift["status"], "MISMATCH_DIM")
            self.assertEqual(dim_drift["finding"]["category"], "ui_visual_dimension_drift")

    def test_q1_m1_mutation_canary_offline_100pct_catch_zero_fp(self) -> None:
        import tempfile as _tf

        with _tf.TemporaryDirectory() as td:
            res = bh.run_m1_mutation_canary_offline(td)
            self.assertTrue(res["ok"], f"M1 canary failed: {res}")
            self.assertEqual(res["caught"], res["total"])
            self.assertEqual(res["total"], 10)
            self.assertAlmostEqual(res["detectRate"], 1.0, places=6)
            self.assertEqual(res["cleanFalsePositives"], 0, f"Unexpected FP: {res['cleanDefects']}")

    def test_v6_q1_geometry_and_golden_ssim_integration(self) -> None:
        import base64 as _b64
        import tempfile as _tf

        w, h = 48, 48
        ref_grid = bytes(((x * 4 + y * 4) % 256) for y in range(h) for x in range(w))
        bad_grid = bytes((255 if (x + y) % 2 == 0 else 0) for y in range(h) for x in range(w))

        class V6Q1MockBridge(FakeBridge):
            def __init__(self, fail_tab2_ssim: bool) -> None:
                super().__init__()
                self.active_tab = 0
                self.fail_tab2_ssim = fail_tab2_ssim

            def cmd(self, name: str, args: dict | None = None, tier: int = 2, timeout_sec: int | None = None) -> dict:
                a = dict(args or {})
                self.history.append((name, a))
                if name == "tab_switch":
                    self.active_tab = int(a.get("index") or 0)
                    return {"ok": True, "elapsedMs": 5, "body": "{}", "json": {"tab": self.active_tab}, "error": ""}
                if name == "ui_audit":
                    return {"ok": True, "elapsedMs": 10, "body": "{}", "json": {"checks": []}, "error": ""}
                if name == "ui_tree":
                    # Inject a real Q1 button ellipsis defect on Tab 4
                    children = []
                    if self.active_tab == 4:
                        children.append({
                            "type": "Button", "text": "A/B Compare: ACTIVE", "clickable": True,
                            "x": 40, "y": 100, "w": 220, "h": 128, "ell": 7, "ellMode": "end", "fg": -1, "sp": 13.0,
                        })
                    payload = {
                        "schemaVersion": 2, "activeTab": self.active_tab, "dpDensity": 3.0,
                        "displayRect": {"x": 0, "y": 0, "w": 1080, "h": 2400},
                        "activity": {"type": "LinearLayout", "orient": "v", "x": 0, "y": 0, "w": 1080, "h": 2400, "children": children},
                    }
                    return {"ok": True, "elapsedMs": 8, "body": json.dumps(payload), "json": payload, "error": ""}
                if name == "screenshot_ui":
                    grid = bad_grid if (self.fail_tab2_ssim and self.active_tab == 2) else ref_grid
                    payload = {"lumaW": w, "lumaH": h, "lumaB64": _b64.b64encode(grid).decode("ascii")}
                    return {"ok": True, "elapsedMs": 12, "body": "{}", "json": payload, "error": ""}
                return super().cmd(name, args, tier=tier, timeout_sec=timeout_sec)

        with _tf.TemporaryDirectory() as td:
            # First run seeds goldens cleanly
            fb_seed = V6Q1MockBridge(fail_tab2_ssim=False)
            h_seed = bh.LiveBugHunter(fb_seed, mode="quick", allow_mutate=False, verbose=False, golden_dir=td, update_goldens=True)
            rep_seed = h_seed.run_hunt(selected_vectors=["V6"])
            self.assertTrue(rep_seed["ok"])
            ui_tree_calls = [a for n, a in fb_seed.history if n == "ui_tree"]
            self.assertEqual(len(ui_tree_calls), 5)
            self.assertTrue(all(a.get("geometry") is True and a.get("maxDepth") == 12 for a in ui_tree_calls))

            # Second run detects BOTH Q1 Tab4 button ellipsis AND Tab2 Golden SSIM regression
            fb_chk = V6Q1MockBridge(fail_tab2_ssim=True)
            h_chk = bh.LiveBugHunter(fb_chk, mode="quick", allow_mutate=False, verbose=False, golden_dir=td, update_goldens=False)
            rep_chk = h_chk.run_hunt(selected_vectors=["V6"])
            cats = {(f["source"], f["category"]) for f in rep_chk["findings"]}
            self.assertIn(("ui_tree_q1", "ui_button_text_ellipsized"), cats)
            self.assertIn(("golden_ssim_q1", "ui_visual_regression_ssim"), cats)

    def test_q2_exit_info_gate_stratifies_new_current_historical_and_cached_lmkd(self) -> None:
        # 1. Pre-PACKAGEUPDATED historical LOW_MEMORY + low-PSS CACHED LMKD must NOT fail the gate
        baseline = [
            {
                "pid": 100, "reasonCode": 16, "reason": "PACKAGEUPDATED",
                "timestampUtc": "2026-11-26T10:00:00Z", "pssMb": 410.0, "importance": 100, "importanceName": "FOREGROUND",
            },
            {
                "pid": 99, "reasonCode": 3, "reason": "LOW_MEMORY",
                "timestampUtc": "2026-11-26T09:00:00Z", "pssMb": 3892.5, "importance": 100, "importanceName": "FOREGROUND",
            },
            {
                "pid": 101, "reasonCode": 3, "reason": "LOW_MEMORY",
                "timestampUtc": "2026-11-26T10:05:00Z", "pssMb": 119.9, "importance": 400, "importanceName": "CACHED",
            },
        ]
        ev_clean = bh.evaluate_exit_info_gate(baseline)
        self.assertTrue(ev_clean["passed"], ev_clean)
        self.assertEqual(ev_clean["newFatalCount"], 0)
        self.assertEqual(ev_clean["currentBuildFatalCount"], 0)
        # Historical pre-update LOW_MEMORY is recorded as historical_archive, not live blocking
        self.assertEqual(ev_clean["historicalFatalCount"], 1)
        self.assertEqual(ev_clean["findings"][0]["origin"], "historical_archive")

        # 2. New ANR / CRASH_NATIVE / foreground LOW_MEMORY after baseline MUST fail with CRITICAL/HIGH
        keys = {bh.make_exit_reason_key(r) for r in baseline}
        for reason, code, expected_cat in (
            ("ANR", 6, "exit_info_new_anr"),
            ("CRASH_NATIVE", 5, "exit_info_new_crash_native"),
            ("LOW_MEMORY", 3, "exit_info_new_low_memory"),
        ):
            mut = [{
                "pid": 200 + code, "reasonCode": code, "reason": reason,
                "timestampUtc": "2026-11-26T10:20:00Z", "pssMb": 2400.0, "importance": 100, "importanceName": "FOREGROUND",
                "description": f"simulated {reason}",
            }] + baseline
            ev_bad = bh.evaluate_exit_info_gate(mut, baseline_keys=keys)
            self.assertFalse(ev_bad["passed"])
            self.assertEqual(ev_bad["newFatalCount"], 1)
            self.assertIn(expected_cat, [f["category"] for f in ev_bad["findings"]])

    def test_q2_frame_metrics_and_gesture_latency_gates_120hz(self) -> None:
        clean_fm = {
            "totalFrames": 90, "ringCount": 90, "p50Ms": 4.2, "p95Ms": 10.1,
            "cpuP95Ms": 5.8, "frozenFrames": 0, "deadlineMissedFrames": 1, "refreshRateHz": 120.0,
        }
        ev_ok = bh.evaluate_frame_metrics_gate(clean_fm, p95_budget_ms=12.0, max_frozen_frames=0)
        self.assertTrue(ev_ok["passed"], ev_ok)
        self.assertEqual(ev_ok["cpuP95Ms"], 5.8)

        # CPU P95 > 12ms fails 120 Hz gate
        slow_fm = dict(clean_fm, cpuP95Ms=18.5, p95Ms=24.0)
        ev_slow = bh.evaluate_frame_metrics_gate(slow_fm, p95_budget_ms=12.0)
        self.assertFalse(ev_slow["passed"])
        self.assertIn("frame_metrics_120hz_p95_exceeded", [f["category"] for f in ev_slow["findings"]])

        # Frozen frame > 0 fails CRITICAL even with good P95
        frozen_fm = dict(clean_fm, frozenFrames=1, sessionMaxMs=912.0)
        ev_frozen = bh.evaluate_frame_metrics_gate(frozen_fm, p95_budget_ms=12.0)
        self.assertFalse(ev_frozen["passed"])
        self.assertIn("frame_metrics_frozen_frames", [f["category"] for f in ev_frozen["findings"]])

        # Gesture touch-to-first-frame (<50ms fast, <160ms heavy modal)
        gest_ok = bh.evaluate_gesture_latency_gate([
            {"Target": "Tab:Play", "Complexity": "fast", "FirstFrameMs": 22.0, "UiThreadBlockMs": 8.0},
            {"Target": "Modal:ProAudioSheet", "Complexity": "heavy", "FirstFrameMs": 110.0, "UiThreadBlockMs": 30.0},
        ])
        self.assertTrue(gest_ok["passed"])
        gest_bad = bh.evaluate_gesture_latency_gate([
            {"Target": "Tab:Settings", "Complexity": "fast", "FirstFrameMs": 88.0, "UiThreadBlockMs": 72.0},
        ])
        self.assertFalse(gest_bad["passed"])
        self.assertEqual(gest_bad["findings"][0]["category"], "gesture_touch_to_frame_exceeded")

    def test_q2_strict_mode_external_watchdog_and_crash_forensics(self) -> None:
        sm_bad = bh.evaluate_strict_mode_gate(
            {"enabled": True, "violationCount": 1, "violations": [{"kind": "thread", "violation": "DiskReadViolation", "stackTop": "MainActivity.cs:42"}]},
            logcat_text="D/StrictMode: StrictMode policy violation; ~duration=12 ms: DiskWriteViolation",
        )
        self.assertFalse(sm_bad["passed"])
        self.assertEqual(sm_bad["violationCount"], 2)

        w_anr = bh.evaluate_external_watchdog_probe({"state": "UNRESPONSIVE_ANR", "alive": False, "elapsedMs": 6012, "detail": "timeout"})
        self.assertFalse(w_anr["passed"])
        self.assertEqual(w_anr["findings"][0]["category"], "external_watchdog_anr_timeout")

        w_pid = bh.evaluate_external_watchdog_probe({"state": "ALIVE", "alive": True, "pid": 9001, "sessionId": "new-sid"}, expected_pid=8000, expected_session_id="old-sid")
        self.assertFalse(w_pid["passed"])
        self.assertEqual(w_pid["findings"][0]["category"], "external_watchdog_crash_restart")

        forensics = bh.parse_external_crash_forensics(
            "E/AndroidRuntime: FATAL EXCEPTION: main\n"
            "E/DEBUG: signal 11 (SIGSEGV), code 1 (SEGV_MAPERR)\n"
        )
        self.assertGreaterEqual(len(forensics), 2)
        self.assertTrue(any(f["severity"] == "CRITICAL" for f in forensics))

    def test_q2_m2_mutation_canary_offline_100pct_catch_zero_fp(self) -> None:
        res = bh.run_m2_mutation_canary_offline()
        self.assertTrue(res["ok"], f"M2 canary failed: {res}")
        self.assertEqual(res["caught"], res["total"])
        self.assertEqual(res["total"], 10)
        self.assertAlmostEqual(res["detectRate"], 1.0, places=6)
        self.assertEqual(res["cleanFalsePositives"], 0)

    def test_q2_v9_strict_mode_watchdog_and_finally_restore_integration(self) -> None:
        class Q2MockBridge(FakeBridge):
            def __init__(self) -> None:
                super().__init__()
                self.strict_calls: list[dict] = []
                self.watchdog_calls = 0
                self.forensics_called = False

            def cmd(self, name: str, args: dict | None = None, *, tier: int = 0, timeout_sec: int = 30) -> dict:
                a = dict(args or {})
                self.history.append((name, a))
                if name == "ping":
                    return {"ok": True, "body": "{}", "json": {"appVersion": "2.9.84", "appBuild": 84, "device": "S25U", "android": "17", "sessionId": "sid-q2", "pid": 7001, "uiHostAttached": True}, "elapsedMs": 4, "error": ""}
                if name == "capabilities":
                    return {"ok": True, "body": "{}", "json": {"commands": [
                        {"cmd": "ping"}, {"cmd": "runtime_telemetry"}, {"cmd": "strict_mode"},
                        {"cmd": "library_query"}, {"cmd": "tab_switch"}, {"cmd": "log_tail"}, {"cmd": "logcat_tail"},
                    ]}, "elapsedMs": 4, "error": ""}
                if name == "strict_mode":
                    self.strict_calls.append(a)
                    return {"ok": True, "body": "{}", "json": {"enabled": bool(a.get("enable"))}, "elapsedMs": 5, "error": ""}
                if name == "runtime_telemetry":
                    return {"ok": True, "body": "{}", "json": {
                        "pid": 7001,
                        "frameMetrics": {"totalFrames": 48, "p95Ms": 14.5, "cpuP95Ms": 14.5, "frozenFrames": 0, "refreshRateHz": 120.0},
                        "audioXRuns": {"totalOutputUnderruns": 0},
                        "exitReasons": [],
                        "strictMode": {"enabled": True, "violationCount": 1, "violations": [{"kind": "thread", "violation": "DiskReadViolation", "stackTop": "Test.cs:1"}]},
                    }, "elapsedMs": 4, "error": ""}
                return super().cmd(name, args, tier=tier, timeout_sec=timeout_sec)

            def watchdog_probe(self, timeout_sec: int = 6, reset_baseline: bool = False) -> dict:
                self.watchdog_calls += 1
                if reset_baseline:
                    return {"ok": True, "alive": True, "state": "ALIVE", "pid": 7001, "sessionId": "sid-q2", "elapsedMs": 6}
                return {"ok": False, "alive": False, "state": "UNRESPONSIVE_ANR", "pid": 7001, "elapsedMs": 6010, "detail": "injected ANR"}

            def fetch_external_crash_forensics(self, timeout_sec: int = 15) -> str:
                self.forensics_called = True
                return "E/AndroidRuntime: FATAL EXCEPTION: main\nANR in hu.sbence.cavern.cavernize\n"

        fb = Q2MockBridge()
        hunter = bh.LiveBugHunter(fb, mode="quick", allow_mutate=False, verbose=False, enable_strict_mode=True)
        rep = hunter.run_hunt(selected_vectors=["V9"])
        self.assertTrue(rep["ok"])
        self.assertFalse(rep["q2GateSummary"]["passed"])
        self.assertEqual(rep["q2GateSummary"]["watchdogState"], "UNRESPONSIVE_ANR")
        self.assertTrue(fb.forensics_called)
        # StrictMode enabled at prime and MUST be restored OFF in finally even with allow_mutate=False!
        self.assertEqual([c.get("enable") for c in fb.strict_calls], [True, False])
        cats = {(f["source"], f["category"]) for f in rep["findings"]}
        self.assertIn(("frame_metrics_q2", "frame_metrics_120hz_p95_exceeded"), cats)
        self.assertIn(("strict_mode_q2", "strict_mode_main_thread_violation"), cats)
        self.assertIn(("external_watchdog_q2", "external_watchdog_anr_timeout"), cats)
        self.assertIn(("external_crash_forensics_q2", "external_crash_logcat"), cats)

    def test_q3_ols_slope_and_soak_leak_gate_ignores_warmup_catches_leaks(self) -> None:
        slope, r2 = bh.compute_ols_slope([10.0, 12.0, 14.0, 16.0, 18.0])
        self.assertAlmostEqual(slope, 2.0, places=5)
        self.assertAlmostEqual(r2, 1.0, places=5)

        # Realistic cycle-0 cold warmup jump that plateaus MUST pass with 0 findings
        clean_samples = []
        for i in range(16):
            clean_samples.append({
                "nativePssMb": 180.0 if i == 0 else 218.0 + (i % 2) * 1.5,
                "managedGcMb": 20.0 if i == 0 else 32.0 + (i % 3) * 0.5,
                "openFdCount": 120 if i == 0 else 135 + (i % 2),
                "viewCount": 250 + (i % 2) * 4,
                "threadCount": 38 + (i % 2),
                "activeDialogs": 0,
                "isOperationRunning": False,
                "sessionFatalCount": 0,
                "lowMemory": False,
            })
        ev_clean = bh.evaluate_soak_leak_gate(clean_samples)
        self.assertTrue(ev_clean["passed"], f"Clean warmup+plateau should pass: {ev_clean}")
        self.assertEqual(ev_clean["findings"], [])

        # Multi-fault leak + dirty-state + fatal crash + lowMemory
        leaking = [dict(s) for s in clean_samples]
        for idx in range(1, len(leaking)):
            leaking[idx]["nativePssMb"] = round(218.0 + idx * 2.5, 2)
            leaking[idx]["openFdCount"] = 135 + idx * 1
            leaking[idx]["managedGcMb"] = round(32.0 + idx * 2.2, 2)
            leaking[idx]["viewCount"] = 250 + idx * 8
            leaking[idx]["threadCount"] = 38 + idx * 1
        leaking[-1]["isOperationRunning"] = True
        leaking[-1]["activeDialogs"] = 2
        leaking[-1]["sessionFatalCount"] = 1
        leaking[-2]["lowMemory"] = True
        ev_bad = bh.evaluate_soak_leak_gate(leaking)
        self.assertFalse(ev_bad["passed"])
        cats = {f["category"] for f in ev_bad["findings"]}
        for expected in (
            "soak_pss_leak",
            "soak_fd_leak",
            "soak_managed_heap_leak",
            "soak_view_count_leak",
            "soak_thread_leak",
            "dirty_state_operation_stuck",
            "dirty_state_dialog_leak",
            "soak_fatal_crash",
            "soak_low_memory",
        ):
            self.assertIn(expected, cats)

    def test_q3_rage_click_drag_sweep_and_reflow_evaluators(self) -> None:
        rage_ok = bh.evaluate_rage_click_result(
            {"ok": True, "succeeded": 6, "maxBlockMs": 14.2, "activeDialogsAfter": 0, "isOperationRunningAfter": False, "exceptions": []},
            target_label="Tab0:Play",
        )
        self.assertTrue(rage_ok["passed"])

        rage_bad = bh.evaluate_rage_click_result(
            {"ok": False, "succeeded": 4, "maxBlockMs": 1850.0, "activeDialogsAfter": 3, "isOperationRunningAfter": True, "exceptions": ["InvalidOperationException: race"]},
            target_label="Tab0:Export",
        )
        self.assertFalse(rage_bad["passed"])
        rcats = {f["category"] for f in rage_bad["findings"]}
        self.assertEqual(rcats, {
            "hostile_rage_click_exception",
            "hostile_rage_click_freeze",
            "dirty_state_dialog_leak",
            "dirty_state_operation_stuck",
        })

        # Benign "No visible SeekBar" skip should not trigger drag_sweep_error
        drag_skip = bh.evaluate_drag_sweep_result(
            {"ok": False, "stepsDispatched": 0, "maxStepUiBlockMs": 0.0, "error": "No visible SeekBar found"},
            None,
            target_label="Tab1:None",
        )
        self.assertTrue(drag_skip["passed"])

        drag_bad = bh.evaluate_drag_sweep_result(
            {"ok": True, "stepsDispatched": 20, "maxStepUiBlockMs": 68.0, "error": ""},
            {"totalFrames": 24, "ringCount": 24, "p50Ms": 9.0, "p95Ms": 22.0, "cpuP95Ms": 18.2, "frozenFrames": 1, "refreshRateHz": 120.0},
            target_label="Tab0:SeekBar",
        )
        self.assertFalse(drag_bad["passed"])
        dcats = {f["category"] for f in drag_bad["findings"]}
        self.assertIn("drag_sweep_ui_stall", dcats)
        self.assertIn("drag_sweep_jank_120hz", dcats)

        overflow_tree = {
            "schemaVersion": 2,
            "dpDensity": 3.0,
            "displayRect": {"x": 0, "y": 0, "w": 1080, "h": 2400},
            "activity": {
                "type": "LinearLayout", "orient": "v", "x": 0, "y": 0, "w": 1080, "h": 2400,
                "children": [
                    {"type": "Button", "text": "OverflowBtn", "clickable": True, "x": 920, "y": 100, "w": 320, "h": 144, "fg": -1, "sp": 18.0},
                ],
            },
        }
        reflow_bad = bh.evaluate_reflow_probe_result({"font_130": overflow_tree}, "Tab0")
        self.assertFalse(reflow_bad["passed"])
        self.assertEqual(reflow_bad["variantsChecked"], 1)
        self.assertGreaterEqual(reflow_bad["highDefectCount"], 1)
        self.assertEqual(reflow_bad["findings"][0]["category"], "reflow_layout_defect")

    def test_q3_m3_mutation_canary_offline_100pct_catch_zero_fp(self) -> None:
        res = bh.run_m3_mutation_canary_offline()
        self.assertTrue(res["ok"], f"M3 canary failed: {res}")
        self.assertEqual(res["caught"], res["total"])
        self.assertEqual(res["total"], 10)
        self.assertAlmostEqual(res["detectRate"], 1.0, places=6)
        self.assertEqual(res["cleanFalsePositives"], 0)

    def test_q3_v6_hostile_and_v11_soak_orchestrator_integration(self) -> None:
        class Q3MockBridge(FakeBridge):
            def __init__(self) -> None:
                super().__init__()
                self.soak_idx = 0
                self.hostile_actions: list[str] = []

            def cmd(self, name: str, args: dict | None = None, *, tier: int = 0, timeout_sec: int = 30) -> dict:
                a = dict(args or {})
                self.history.append((name, a))
                if name == "capabilities":
                    return {"ok": True, "body": "{}", "json": {"commands": [
                        {"cmd": "ping"}, {"cmd": "app_state"}, {"cmd": "settings_get"}, {"cmd": "settings_set"},
                        {"cmd": "tab_switch"}, {"cmd": "ui_tree"}, {"cmd": "ui_interact"}, {"cmd": "soak_snapshot"},
                        {"cmd": "runtime_telemetry"}, {"cmd": "memory"}, {"cmd": "log_tail"}, {"cmd": "logcat_tail"},
                    ]}, "elapsedMs": 3, "error": ""}
                if name == "soak_snapshot":
                    idx = self.soak_idx
                    self.soak_idx += 1
                    return {"ok": True, "body": "{}", "json": {
                        "nativePssMb": round(200.0 + idx * 4.5, 2),
                        "managedGcMb": round(26.0 + idx * 0.4, 2),
                        "openFdCount": 140 + idx * 2,
                        "viewCount": 300,
                        "threadCount": 42,
                        "activeDialogs": 0,
                        "isOperationRunning": (idx >= 9),
                        "sessionFatalCount": 0,
                        "lowMemory": False,
                    }, "elapsedMs": 4, "error": ""}
                if name == "ui_interact":
                    act = str(a.get("action") or "")
                    self.hostile_actions.append(act)
                    if act == "rage_click":
                        return {"ok": True, "body": "{}", "json": {
                            "ok": False, "action": "rage_click", "succeeded": 3, "maxBlockMs": 22.0,
                            "activeDialogsAfter": 2, "isOperationRunningAfter": False,
                            "exceptions": ["ObjectDisposedException: rage race"],
                        }, "elapsedMs": 12, "error": ""}
                    if act == "drag_sweep":
                        return {"ok": True, "body": "{}", "json": {
                            "ok": True, "action": "drag_sweep", "stepsDispatched": 20, "maxStepUiBlockMs": 64.0, "error": "",
                        }, "elapsedMs": 15, "error": ""}
                    if act == "reflow_probe":
                        bad_tree = {
                            "schemaVersion": 2, "dpDensity": 3.0,
                            "displayRect": {"x": 0, "y": 0, "w": 1080, "h": 2400},
                            "activity": {
                                "type": "LinearLayout", "orient": "v", "x": 0, "y": 0, "w": 1080, "h": 2400,
                                "children": [
                                    {"type": "Button", "text": "WideLabel", "clickable": True, "x": 900, "y": 120, "w": 360, "h": 144, "fg": -1, "sp": 18.0},
                                ],
                            },
                        }
                        return {"ok": True, "body": "{}", "json": {
                            "ok": True, "action": "reflow_probe", "font_130": bad_tree, "landscape": bad_tree,
                        }, "elapsedMs": 20, "error": ""}
                    return {"ok": True, "body": "{}", "json": {"ok": True}, "elapsedMs": 5, "error": ""}
                if name == "runtime_telemetry":
                    return {"ok": True, "body": "{}", "json": {
                        "pid": 7001,
                        "frameMetrics": {"totalFrames": 24, "ringCount": 24, "p50Ms": 6.0, "p95Ms": 18.5, "cpuP95Ms": 16.0, "frozenFrames": 0, "refreshRateHz": 120.0},
                        "audioXRuns": {"totalOutputUnderruns": 0},
                        "exitReasons": [],
                    }, "elapsedMs": 4, "error": ""}
                return super().cmd(name, args, tier=tier, timeout_sec=timeout_sec)

        fb = Q3MockBridge()
        hunter = bh.LiveBugHunter(fb, mode="quick", allow_mutate=True, verbose=False, soak_cycles=8)
        rep = hunter.run_hunt(selected_vectors=["V6", "V11"])
        self.assertTrue(rep["ok"])
        self.assertIn("q3SoakSummary", rep)
        self.assertFalse(rep["q3SoakSummary"]["passed"])
        self.assertGreater(rep["q3SoakSummary"]["fdDelta"], 8)
        self.assertGreater(rep["q3SoakSummary"]["pssDeltaMb"], 25.0)
        self.assertTrue(rep["q3SoakSummary"]["stuckOp"])
        for act in ("rage_click", "drag_sweep", "reflow_probe"):
            self.assertIn(act, fb.hostile_actions)
        cats = {f["category"] for f in rep["findings"]}
        self.assertIn("soak_pss_leak", cats)
        self.assertIn("soak_fd_leak", cats)
        self.assertIn("dirty_state_operation_stuck", cats)
        self.assertIn("hostile_rage_click_exception", cats)
        self.assertIn("drag_sweep_ui_stall", cats)
        self.assertIn("reflow_layout_defect", cats)
        md = bh.generate_report_markdown(rep)
        self.assertIn("Q3 Soak & Stress Gate (`V11`)", md)

    def test_q1_frosted_capsule_ancestor_custom_bg_no_wcag_fp(self) -> None:
        dark_bg = -15658728  # #FF111118
        dark_fg = -14998748  # #FF1B1B24
        # Child TextView inside OneUiFrostedCapsuleView has no bg on itself, inheriting custom canvas backdrop -> 0 WCAG FP
        frosted_tree = {
            "schemaVersion": 2,
            "dpDensity": 3.0,
            "displayRect": {"x": 0, "y": 0, "w": 1080, "h": 2400},
            "activity": {
                "type": "FrameLayout", "x": 0, "y": 0, "w": 1080, "h": 2400, "bg": dark_bg,
                "children": [
                    {
                        "type": "OneUiFrostedCapsuleView", "x": 40, "y": 2100, "w": 1000, "h": 180,
                        "children": [
                            {"type": "TextView", "text": "Player", "x": 80, "y": 2140, "w": 180, "h": 80, "fg": dark_fg, "sp": 13.0},
                            {"type": "TextView", "text": "3D Room", "x": 280, "y": 2140, "w": 180, "h": 80, "fg": dark_fg, "sp": 13.0},
                        ],
                    },
                ],
            },
        }
        res_clean = bh.analyze_ui_geometry_oracle(frosted_tree, "Tab0")
        wcag_clean = [f for f in res_clean if f["category"] == "ui_wcag_contrast_fail"]
        self.assertEqual(wcag_clean, [], f"Frosted capsule children should not trigger WCAG FP: {wcag_clean}")

        # Ordinary LinearLayout with dark text on dark solid bg MUST still fail WCAG
        bad_tree = {
            "schemaVersion": 2,
            "dpDensity": 3.0,
            "displayRect": {"x": 0, "y": 0, "w": 1080, "h": 2400},
            "activity": {
                "type": "LinearLayout", "orient": "v", "x": 0, "y": 0, "w": 1080, "h": 2400, "bg": dark_bg,
                "children": [
                    {"type": "TextView", "text": "Unreadable", "x": 40, "y": 200, "w": 300, "h": 80, "fg": dark_fg, "bg": dark_bg, "sp": 13.0},
                ],
            },
        }
        res_bad = bh.analyze_ui_geometry_oracle(bad_tree, "Tab0")
        wcag_bad = [f for f in res_bad if f["category"] == "ui_wcag_contrast_fail"]
        self.assertEqual(len(wcag_bad), 1)

    def test_q4_m4_mutation_canary_offline_100pct_catch_zero_fp(self) -> None:
        res = bh.run_m4_mutation_canary_offline()
        self.assertTrue(res["ok"], f"M4 canary failed: {res}")
        self.assertEqual(res["caught"], res["total"])
        self.assertEqual(res["total"], 10)
        self.assertAlmostEqual(res["detectRate"], 1.0, places=6)
        self.assertEqual(res["cleanFalsePositives"], 0)

    def test_q4_v12_contracts_and_gate_orchestrator(self) -> None:
        import tempfile as _tf

        class Q4MockBridge(FakeBridge):
            def __init__(self, *, blur_enabled: bool = False, break_settings: bool = False, hot_exec_fail: bool = False) -> None:
                super().__init__()
                self.blur_enabled = blur_enabled
                self.break_settings = break_settings
                self.hot_exec_fail = hot_exec_fail
                self.hot_exec_called = False

            def cmd(self, name: str, args: dict | None = None, *, tier: int = 0, timeout_sec: int = 30) -> dict:
                a = dict(args or {})
                self.history.append((name, a))
                if name == "capabilities":
                    return {"ok": True, "body": "{}", "json": {"commands": [
                        {"cmd": "ping"}, {"cmd": "app_state"}, {"cmd": "settings_get"}, {"cmd": "settings_set"},
                        {"cmd": "reflect_get"}, {"cmd": "hot_exec"}, {"cmd": "log_tail"}, {"cmd": "logcat_tail"},
                    ]}, "elapsedMs": 3, "error": ""}
                if name == "reflect_get":
                    if a.get("path") == "EnableFullScreenRenderEffectBlur":
                        return {"ok": True, "body": "{}", "json": {"value": self.blur_enabled, "type": "Boolean"}, "elapsedMs": 3, "error": ""}
                    return {"ok": True, "body": "{}", "json": {"value": "ok"}, "elapsedMs": 3, "error": ""}
                if name == "settings_set" and self.break_settings:
                    # Simulate broken persistence that drops the write silently
                    return {"ok": True, "body": "{}", "json": {"ok": True}, "elapsedMs": 3, "error": ""}
                return super().cmd(name, args, tier=tier, timeout_sec=timeout_sec)

            def run_hot_exec_probe(self, probe_cs: str) -> dict:
                self.hot_exec_called = True
                assert "ProcessMultichannelInPlace" in probe_cs
                if self.hot_exec_fail:
                    return {
                        "ok": False,
                        "blur_disabled": False,
                        "onnx_pss_cap_lte_2600mb": True,
                        "spatial_karaoke_reset_zero_nan": False,
                        "no_stuck_operation": True,
                        "error": "injected_fail",
                    }
                return {
                    "ok": True,
                    "blur_disabled": True,
                    "onnx_pss_cap_lte_2600mb": True,
                    "onnxCapMb": 2400,
                    "spatial_karaoke_reset_zero_nan": True,
                    "no_stuck_operation": True,
                    "error": "",
                }

        contracts_doc = {
            "version": 1,
            "policy": "1-bug=1-permanent-automated-test-first",
            "contracts": [
                {
                    "id": "REG-TEST-BLUR",
                    "bug_ref": "H1",
                    "title": "Blur stays disabled.",
                    "domain": "ui",
                    "severity": "HIGH",
                    "status": "frozen",
                    "immutable": True,
                    "mode": "hybrid",
                    "test_ref": ".agents/agent/test_bug_hunter.py::test_q4_v12_contracts_and_gate_orchestrator",
                    "offline_checks": [{"kind": "file_exists", "path": ".agents/agent/cavern_bug_hunter.py"}],
                    "live_check": {
                        "kind": "reflect_equals",
                        "type": "CavernizeAndroid.Theme.OneUiMotionBlur",
                        "path": "EnableFullScreenRenderEffectBlur",
                        "expected": False,
                    },
                },
                {
                    "id": "REG-TEST-ROUNDTRIP",
                    "bug_ref": "STATE1",
                    "title": "Settings roundtrip persists and restores.",
                    "domain": "state",
                    "severity": "HIGH",
                    "status": "frozen",
                    "immutable": True,
                    "mode": "hybrid",
                    "test_ref": ".agents/agent/test_bug_hunter.py::test_q4_v12_contracts_and_gate_orchestrator",
                    "offline_checks": [{"kind": "file_exists", "path": ".agents/agent/cavern_bug_hunter.py"}],
                    "live_check": {"kind": "settings_roundtrip", "key": "Iso226Enabled"},
                },
                {
                    "id": "REG-TEST-HOTEXEC",
                    "bug_ref": "STATE2",
                    "title": "In-RAM hot_exec state probe passes.",
                    "domain": "runtime",
                    "severity": "HIGH",
                    "status": "frozen",
                    "immutable": True,
                    "mode": "hybrid",
                    "test_ref": ".agents/agent/test_bug_hunter.py::test_q4_v12_contracts_and_gate_orchestrator",
                    "offline_checks": [{"kind": "file_exists", "path": ".agents/agent/cavern_bug_hunter.py"}],
                    "live_check": {
                        "kind": "hot_exec_bundle",
                        "invariants": ["blur_disabled", "onnx_pss_cap_lte_2600mb", "spatial_karaoke_reset_zero_nan"],
                    },
                },
            ],
        }

        with _tf.TemporaryDirectory() as td:
            cpath = str(Path(td) / "contracts.json")
            with open(cpath, "w", encoding="utf-8") as fh:
                json.dump(contracts_doc, fh)

            # 1. Clean run -> PASS
            fb_ok = Q4MockBridge()
            h_ok = bh.LiveBugHunter(fb_ok, mode="quick", allow_mutate=True, verbose=False, contracts_file=cpath)
            rep_ok = h_ok.run_hunt(selected_vectors=["V12"])
            self.assertTrue(rep_ok["ok"])
            self.assertTrue(fb_ok.hot_exec_called)
            self.assertEqual(rep_ok["q4GateSummary"]["failed"], 0, rep_ok["q4GateSummary"])
            self.assertEqual(rep_ok["q4GateSummary"]["passed"], 3, rep_ok["q4GateSummary"])
            self.assertTrue(rep_ok["q4GateSummary"]["hotExecVerified"])
            self.assertEqual(rep_ok["qaGate"]["verdict"], "PASS", rep_ok["qaGate"])
            md_ok = bh.generate_report_markdown(rep_ok)
            self.assertIn("Q4 Frozen Regression Contracts (`V12`)", md_ok)
            self.assertIn("QA_GATE|VERDICT=PASS", md_ok)

            # 2. Regression in reflect_get + broken settings roundtrip + hot_exec failure -> FAIL
            fb_bad = Q4MockBridge(blur_enabled=True, break_settings=True, hot_exec_fail=True)
            h_bad = bh.LiveBugHunter(fb_bad, mode="quick", allow_mutate=True, verbose=False, contracts_file=cpath)
            rep_bad = h_bad.run_hunt(selected_vectors=["V12"])
            self.assertGreaterEqual(rep_bad["q4GateSummary"]["failed"], 3)
            self.assertFalse(rep_bad["q4GateSummary"]["hotExecVerified"])
            self.assertEqual(rep_bad["qaGate"]["verdict"], "FAIL")
            cats_bad = {f["category"] for f in rep_bad["findings"]}
            self.assertIn("regression_contract_failed", cats_bad)
            md_bad = bh.generate_report_markdown(rep_bad)
            self.assertIn("QA_GATE|VERDICT=FAIL", md_bad)

    def test_w53_1b_codec2_probe_noise_filtered_while_real_mediacodec_error_caught(self) -> None:
        raw = "\n".join([
            "E/MediaCodec(10778): Failed to query component interface for required system resources: 6",
            "E/CCodec(10778): Failed to query component interface for required system resources: 6",
            "E/MediaCodec(10778): Codec reported fatal error 0xffffffea during configure",
        ])
        anomalies = bh.parse_logcat_anomalies(raw)
        self.assertEqual(len(anomalies), 1)
        self.assertEqual(anomalies[0]["category"], "codec")
        self.assertIn("Codec reported fatal error", anomalies[0]["message"])

    def test_w53_1b_v6_play_button_rage_click_targets_framelayout(self) -> None:
        src = (AGENT_DIR / "cavern_bug_hunter.py").read_text(encoding="utf-8")
        self.assertIn('"Tab0:PlayButton"', src)
        self.assertIn('"type": "FrameLayout", "text": "Play"', src)


if __name__ == "__main__":
    unittest.main()



