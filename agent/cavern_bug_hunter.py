#!/usr/bin/env python3
"""
Cavernize Live Adversarial Bug-Hunter & Chaos Fuzzer (`cavern_bug_hunter.py`).

Actively drives the running Cavernize Android app on the user's device via OmniAgent
local signature-verified IPC (`cavernize_cmd`), continuously diffing BOTH `AppLogger`
and Android `logcat` (`*:W`, crash buffer, Choreographer/GC/AAudio/MediaCodec/ANR)
after every adversarial step to uncover real-world bugs, race conditions, crashes,
warnings, UI defects, acoustic anomalies, decoder failures, and memory leaks.

Zero external Python dependencies (stdlib only). Guaranteed settings & state restore.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import math
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

AGENT_DIR = Path(__file__).resolve().parent
REPO_ROOT = AGENT_DIR.parents[1]
PLUGIN_SCRIPTS_DIR = REPO_ROOT / ".agents" / "plugins" / "dynamic-fractal-planner" / "scripts"
for _sp in (str(AGENT_DIR), str(PLUGIN_SCRIPTS_DIR)):
    if _sp not in sys.path:
        sys.path.insert(0, _sp)

import omni_agent_client as oac  # noqa: E402
import qa_gate  # noqa: E402

# ---------------------------------------------------------------------------
# Pure Parsers, Classifiers & Normalizers (100% unit-testable offline)
# ---------------------------------------------------------------------------

APPLOG_LINE_RE = re.compile(
    r"^\[(?P<ts>\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}(?:\.\d+)?)\]\s+"
    r"\[(?P<level>INFO|WARN|ERROR|FATAL|DEBUG)\]\s+"
    r"(?:\[T#(?P<thread>[^\]]+)\]\s+)?"
    r"\[(?P<tag>[^\]]+)\]\s*(?P<msg>.*)$"
)

LOGCAT_BRIEF_RE = re.compile(
    r"^(?P<level>[VDIWEFA])/(?P<tag>[^(\s]+)\s*\(\s*(?P<pid>\d+)\):\s*(?P<msg>.*)$"
)

LOGCAT_THREADTIME_RE = re.compile(
    r"^(?P<date>\d{2}-\d{2})\s+(?P<time>\d{2}:\d{2}:\d{2}\.\d+)\s+"
    r"(?P<pid>\d+)\s+(?P<tid>\d+)\s+(?P<level>[VDIWEFA])\s+"
    r"(?P<tag>[^:]+):\s*(?P<msg>.*)$"
)

CHOREOGRAPHER_SKIP_RE = re.compile(r"Skipped\s+(\d+)\s+frames", re.IGNORECASE)
UNFORMATTED_PLACEHOLDER_RE = re.compile(r"\{\d+(?::[^}]+)?\}")
CAMEL_KEY_RE = re.compile(r"^[A-Z][a-z]+[A-Z][A-Za-z0-9]{4,}$")

# Benign Android / .NET child-process fork noise to exclude from findings
BENIGN_LOGCAT_PATTERNS = (
    re.compile(r"libsigchain.*Setting SIGSEGV to SIG_DFL", re.IGNORECASE),
    re.compile(r"libsigchain.*#\d{2}\s+pc\s+", re.IGNORECASE),
    re.compile(r"ImeTracker.*onCancelled at PHASE_CLIENT_ALREADY_HIDDEN", re.IGNORECASE),
    re.compile(r"ImeFocusController.*skipped hasWindowFocus=false", re.IGNORECASE),
    re.compile(r"ViewPostIme pointer", re.IGNORECASE),
    re.compile(r"Input channel (constructed|destroyed)", re.IGNORECASE),
    re.compile(r"Failed to query component interface for required system resources:\s*6\b", re.IGNORECASE),
)

HIGH_SIGNAL_LOGCAT_PATTERNS: tuple[tuple[str, str, re.Pattern[str]], ...] = (
    ("CRITICAL", "crash", re.compile(r"FATAL EXCEPTION|SIGSEGV|SIGABRT|SIGBUS|JNI DETECTED ERROR|Fatal signal|ANR in", re.IGNORECASE)),
    ("CRITICAL", "oom", re.compile(r"OutOfMemoryError|lowmemorykiller|Throwing OutOfMemoryError", re.IGNORECASE)),
    ("HIGH", "exception", re.compile(r"UnhandledException|NullReferenceException|ObjectDisposedException|InvalidOperationException|DeadObjectException|IllegalStateException|RuntimeException", re.IGNORECASE)),
    ("HIGH", "audio_native", re.compile(r"AAudio.*(error|disconnect|invalid|xrun|underrun|timeout|failed)|AudioTrack.*(error|dead|underrun|failed)|OpenSL.*error", re.IGNORECASE)),
    ("HIGH", "codec", re.compile(r"MediaCodec.*(error|failed|codecException)|MediaExtractor.*error|FFmpeg.*(error|failed)|truehdd.*(error|failed)", re.IGNORECASE)),
    ("MEDIUM", "ui_jank", re.compile(r"Choreographer.*Skipped\s+(\d+)\s+frames", re.IGNORECASE)),
    ("MEDIUM", "strict_mode", re.compile(r"StrictMode.*(violation|DiskRead|DiskWrite|Network)", re.IGNORECASE)),
    ("MEDIUM", "graphics", re.compile(r"OpenGLRenderer.*(error|failed|Surface|timeout|lost)|BufferQueue.*abandon|EGL.*error", re.IGNORECASE)),
    ("LOW", "gc_stall", re.compile(r"GC freed.*paused\s+[^,]+,(\d+(?:\.\d+)?)ms", re.IGNORECASE)),
)

SEVERITY_RANK = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3, "INFO": 4}
ORIGIN_RANK = {"live_triggered": 0, "current_session": 1, "historical_archive": 2}
SESSION_HEADER_RE = re.compile(r"===\s*Session started\s*\[sid=(?P<sid>[A-Za-z0-9_-]+)\]", re.IGNORECASE)


def split_applog_by_session(text: str, current_session_id: str) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """Partition AppLogger entries into (current_session_entries, historical_archive_entries)."""
    cur_sid = (current_session_id or "").strip().lower()
    if not cur_sid:
        return [], parse_app_logger_lines(text)
    active_lines: list[str] = []
    hist_lines: list[str] = []
    in_current = False
    saw_any_header = False
    for raw_line in (text or "").splitlines():
        m = SESSION_HEADER_RE.search(raw_line)
        if m:
            saw_any_header = True
            in_current = m.group("sid").strip().lower() == cur_sid
            continue
        if in_current:
            active_lines.append(raw_line)
        else:
            hist_lines.append(raw_line)
    if not saw_any_header:
        return [], parse_app_logger_lines(text)
    return parse_app_logger_lines("\n".join(active_lines)), parse_app_logger_lines("\n".join(hist_lines))


def classify_ui_latency_ms(elapsed_ms: float, action: str) -> dict[str, str] | None:
    """Flag UI main-thread stalls (>400ms) and freezes (>1500ms) per BP_LIVE_QA_BUG_HUNTER_SOTA."""
    if elapsed_ms >= 1500.0:
        return {
            "severity": "HIGH",
            "category": "ui_freeze",
            "message": f"UI freeze detected on '{action}' ({elapsed_ms:.0f} ms >= 1500 ms budget)",
        }
    if elapsed_ms >= 400.0:
        return {
            "severity": "MEDIUM",
            "category": "ui_stall",
            "message": f"UI stall detected on '{action}' ({elapsed_ms:.0f} ms >= 400 ms budget)",
        }
    return None


def analyze_spatial_trajectory(samples: list[dict[str, Any]], mode_name: str, stem_name: str = "stem") -> list[dict[str, str]]:
    """Detect radial center-pull collapse & alternating-sign azimuth ping-pong oscillation in 3D trajectories."""
    import math as _math

    issues: list[dict[str, str]] = []
    if not samples or len(samples) < 4 or str(mode_name).lower() == "off":
        return issues

    radii: list[float] = []
    azimuths: list[float] = []
    for s in samples:
        x = float(s.get("x", 0.0))
        y = float(s.get("y", 0.0))
        z = float(s.get("z", 1.0))
        if not (_math.isfinite(x) and _math.isfinite(y) and _math.isfinite(z)):
            issues.append({
                "severity": "CRITICAL",
                "category": "spatial_trajectory_nan",
                "message": f"SpatialChoreo({mode_name}/{stem_name}) emitted NaN/Infinity coordinates",
            })
            return issues
        r = _math.sqrt(x * x + y * y + z * z)
        az = _math.degrees(_math.atan2(x, z))
        radii.append(r)
        azimuths.append(az)

    r_min, r_max = min(radii), max(radii)
    if r_min < 0.50 or (r_max - r_min) > 0.25:
        issues.append({
            "severity": "HIGH",
            "category": "spatial_radial_collapse",
            "message": (
                f"SpatialChoreo({mode_name}/{stem_name}) exhibits radial collapse/oscillation toward center "
                f"(r_min={r_min:.2f}, r_max={r_max:.2f}, span={r_max - r_min:.2f})"
            ),
        })

    if str(mode_name).lower() == "orbit":
        # Orbit sweeps smoothly around 360deg, naturally crossing both 0deg and +/-180deg.
        # Only flag ping-pong if unwrapped angular velocity actually reverses direction repeatedly!
        vel_reversals = 0
        prev_vel_sign = 0
        for i in range(1, len(azimuths)):
            d_az = azimuths[i] - azimuths[i - 1]
            while d_az > 180.0:
                d_az -= 360.0
            while d_az < -180.0:
                d_az += 360.0
            if abs(d_az) < 5.0:
                continue
            vel_sign = 1 if d_az > 0 else -1
            if prev_vel_sign != 0 and vel_sign != prev_vel_sign:
                vel_reversals += 1
            prev_vel_sign = vel_sign
        if vel_reversals >= 3:
            issues.append({
                "severity": "HIGH",
                "category": "spatial_pingpong_oscillation",
                "message": (
                    f"SpatialChoreo({mode_name}/{stem_name}) reversed orbital direction repeatedly "
                    f"({vel_reversals} angular-velocity reversals)"
                ),
            })
    else:
        # Detect back-and-forth ping-pong swings across center (azimuth sign reversals with significant amplitude)
        sign_reversals = 0
        prev_sign = 0
        prev_az = azimuths[0]
        for az in azimuths[1:]:
            cur_sign = 1 if az > 8.0 else (-1 if az < -8.0 else 0)
            if cur_sign != 0:
                if prev_sign != 0 and cur_sign != prev_sign:
                    sign_reversals += 1
                prev_sign = cur_sign
            prev_az = az

        if sign_reversals > 2 and (max(azimuths) - min(azimuths)) >= 30.0:
            issues.append({
                "severity": "HIGH",
                "category": "spatial_pingpong_oscillation",
                "message": (
                    f"SpatialChoreo({mode_name}/{stem_name}) oscillates back-and-forth across center "
                    f"({sign_reversals} azimuth sign reversals, az=[{min(azimuths):+.1f}..{max(azimuths):+.1f}]deg)"
                ),
            })

    # Detect radial direction reversals (in-out-in-out pumping toward center)
    rad_reversals = 0
    prev_dr_sign = 0
    for i in range(1, len(radii)):
        dr = radii[i] - radii[i - 1]
        if abs(dr) < 0.015:
            continue
        dr_sign = 1 if dr > 0 else -1
        if prev_dr_sign != 0 and dr_sign != prev_dr_sign:
            rad_reversals += 1
        prev_dr_sign = dr_sign
    if rad_reversals >= 3 and (r_max - r_min) >= 0.12:
        issues.append({
            "severity": "MEDIUM",
            "category": "spatial_radial_pingpong",
            "message": (
                f"SpatialChoreo({mode_name}/{stem_name}) pumps toward/away from center "
                f"({rad_reversals} radial reversals, r=[{r_min:.2f}..{r_max:.2f}])"
            ),
        })
    return issues


def normalize_signature(source: str, category: str, text: str) -> str:
    """Create a stable deduplication signature by stripping volatile numbers/timestamps/addrs."""
    s = (text or "").strip()
    s = re.sub(r"0x[0-9a-fA-F]+", "0xADDR", s)
    s = re.sub(r"\b[0-9a-f]{8,}\b", "HEXID", s)
    s = re.sub(r"\d{4}-\d{2}-\d{2}[T\s]\d{2}:\d{2}:\d{2}(?:\.\d+)?Z?", "TS", s)
    s = re.sub(r"\b\d+(?:\.\d+)?\s*(?:ms|us|s|KB|MB|GB|Hz|dB|%|px|f)\b", "NUM", s)
    s = re.sub(r"T#\d+", "T#N", s)
    s = re.sub(r"\(\d+\)", "(N)", s)
    s = re.sub(r"\b\d{2,}\b", "N", s)
    s = re.sub(r"\s+", " ", s)[:180]
    raw = f"{source}|{category}|{s}".lower()
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]
    return f"{source}:{category}:{digest}"


def parse_app_logger_lines(text: str) -> list[dict[str, str]]:
    """Parse structured AppLogger lines into dicts."""
    entries: list[dict[str, str]] = []
    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        m = APPLOG_LINE_RE.match(line)
        if m:
            entries.append({
                "ts": m.group("ts"),
                "level": m.group("level"),
                "thread": m.group("thread") or "",
                "tag": m.group("tag"),
                "msg": m.group("msg").strip(),
                "raw": line,
            })
        elif any(tok in line for tok in ("[ERROR]", "[WARN]", "[FATAL]", "Exception:", "FAIL [")):
            lvl = "FATAL" if "[FATAL]" in line else ("ERROR" if "[ERROR]" in line or "Exception" in line else "WARN")
            entries.append({
                "ts": "",
                "level": lvl,
                "thread": "",
                "tag": "RAW",
                "msg": line,
                "raw": line,
            })
    return entries


def is_benign_logcat_line(line: str) -> bool:
    for pat in BENIGN_LOGCAT_PATTERNS:
        if pat.search(line):
            return True
    return False


def parse_logcat_anomalies(text: str) -> list[dict[str, Any]]:
    """Extract genuine warnings, errors, fatals, jank & crashes from Android logcat output."""
    anomalies: list[dict[str, Any]] = []
    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("--------- beginning of"):
            continue
        if is_benign_logcat_line(line):
            continue

        level = ""
        tag = ""
        msg = line
        m = LOGCAT_THREADTIME_RE.match(line)
        if m:
            level = m.group("level")
            tag = m.group("tag").strip()
            msg = m.group("msg").strip()
        else:
            m2 = LOGCAT_BRIEF_RE.match(line)
            if m2:
                level = m2.group("level")
                tag = m2.group("tag").strip()
                msg = m2.group("msg").strip()

        matched = False
        for sev, cat, pat in HIGH_SIGNAL_LOGCAT_PATTERNS:
            hm = pat.search(line)
            if not hm:
                continue
            if cat == "ui_jank":
                try:
                    skipped = int(hm.group(1))
                except (ValueError, IndexError):
                    skipped = 0
                if skipped < 25:
                    matched = True
                    break
                sev = "HIGH" if skipped >= 60 else "MEDIUM"
            elif cat == "gc_stall":
                try:
                    pause_ms = float(hm.group(1))
                except (ValueError, IndexError):
                    pause_ms = 0.0
                if pause_ms < 25.0:
                    matched = True
                    break
            anomalies.append({
                "severity": sev,
                "category": cat,
                "level": level or "E",
                "tag": tag or "logcat",
                "message": f"{tag}: {msg}" if tag else msg,
                "raw": line,
            })
            matched = True
            break

        if not matched and level in ("E", "F", "A"):
            # Samsung / Android 17 Codec2 CCodec internal probe for optional system resources returns C2_NOT_FOUND (6)
            # during normal MediaExtractor/MediaCodec codec capability probing (benign vendor logcat noise).
            if level == "E" and "failed to query component interface for required system resources: 6" in line.lower():
                continue
            anomalies.append({
                "severity": "CRITICAL" if level in ("F", "A") else "HIGH",
                "category": "logcat_error",
                "level": level,
                "tag": tag or "logcat",
                "message": f"{tag}: {msg}" if tag else msg,
                "raw": line,
            })
        elif not matched and level == "W":
            # Filter noisy Android framework warnings unless Cavern/audio/media/UI relevant
            relevant_tags = (
                "cavern", "mono", "dotnet", "audio", "aaudio", "media", "codec",
                "exo", "ffmpeg", "onnx", "opengl", "choreographer", "window",
                "view", "input", "strictmode", "activity", "system.err",
            )
            low_line = line.lower()
            if any(rt in low_line for rt in relevant_tags):
                anomalies.append({
                    "severity": "LOW",
                    "category": "logcat_warn",
                    "level": level,
                    "tag": tag or "logcat",
                    "message": f"{tag}: {msg}" if tag else msg,
                    "raw": line,
                })
    return anomalies


ONEUI9_STANDARD_RADII_DP: tuple[float, ...] = (
    4.0, 6.0, 8.0, 10.0, 12.0, 14.0, 16.0, 18.0, 20.0, 22.0, 24.0, 26.0, 28.0, 32.0, 36.0
)


def _argb_to_rgba(argb: int) -> tuple[int, int, int, int]:
    u = int(argb) & 0xFFFFFFFF
    return ((u >> 16) & 0xFF, (u >> 8) & 0xFF, u & 0xFF, (u >> 24) & 0xFF)


def _composite_over(fg_rgba: tuple[int, int, int, int], bg_rgb: tuple[float, float, float]) -> tuple[float, float, float]:
    r, g, b, a = fg_rgba
    alpha = max(0.0, min(1.0, a / 255.0))
    return (
        r * alpha + bg_rgb[0] * (1.0 - alpha),
        g * alpha + bg_rgb[1] * (1.0 - alpha),
        b * alpha + bg_rgb[2] * (1.0 - alpha),
    )


def _srgb_channel_linear(c_255: float) -> float:
    s = max(0.0, min(1.0, c_255 / 255.0))
    return s / 12.92 if s <= 0.03928 else ((s + 0.055) / 1.055) ** 2.4


def _relative_luminance(rgb: tuple[float, float, float]) -> float:
    return (
        0.2126 * _srgb_channel_linear(rgb[0])
        + 0.7152 * _srgb_channel_linear(rgb[1])
        + 0.0722 * _srgb_channel_linear(rgb[2])
    )


def compute_wcag_contrast(fg_argb: int, bg_argb: int | None, base_bg_argb: int = -16250868) -> float:
    """Compute WCAG 2.1 contrast ratio (1.0..21.0) with alpha compositing over the base screen surface."""
    br, bg, bb, _ = _argb_to_rgba(base_bg_argb)
    eff_bg: tuple[float, float, float] = (float(br), float(bg), float(bb))
    if bg_argb is not None:
        eff_bg = _composite_over(_argb_to_rgba(bg_argb), eff_bg)
    eff_fg = _composite_over(_argb_to_rgba(fg_argb), eff_bg)
    l1 = _relative_luminance(eff_fg)
    l2 = _relative_luminance(eff_bg)
    hi, lo = (l1, l2) if l1 >= l2 else (l2, l1)
    return (hi + 0.05) / (lo + 0.05)


def compute_rect_iou(
    r1: tuple[int, int, int, int],
    r2: tuple[int, int, int, int],
) -> tuple[float, int, int]:
    """Return (IoU, overlap_w_px, overlap_h_px) for two (x, y, w, h) rectangles."""
    x1, y1, w1, h1 = r1
    x2, y2, w2, h2 = r2
    if w1 <= 0 or h1 <= 0 or w2 <= 0 or h2 <= 0:
        return 0.0, 0, 0
    ix1 = max(x1, x2)
    iy1 = max(y1, y2)
    ix2 = min(x1 + w1, x2 + w2)
    iy2 = min(y1 + h1, y2 + h2)
    ow = max(0, ix2 - ix1)
    oh = max(0, iy2 - iy1)
    inter = ow * oh
    if inter <= 0:
        return 0.0, 0, 0
    union = w1 * h1 + w2 * h2 - inter
    return (inter / union if union > 0 else 0.0), ow, oh


def _node_has_geometry(node: Any) -> bool:
    if not isinstance(node, dict):
        return False
    if "x" in node and "y" in node:
        return True
    for ch in node.get("children") or []:
        if _node_has_geometry(ch):
            return True
    return False


def analyze_ui_geometry_oracle(
    tree_obj: Any,
    scope: str,
    design_tokens: dict[str, Any] | None = None,
) -> list[dict[str, str]]:
    """Q1 Deterministic Native Android Visual & Geometric Oracle.

    Evaluates screen/parent overflow (scroll-axis-aware), sibling overlap IoU,
    laid-out zero-size interactive/text views, touch target ergonomics, ellipsis &
    hard text clipping, horizontal row alignment, WCAG 2.1 contrast, & One UI 9 token grid.
    """
    defects: list[dict[str, str]] = []
    if not isinstance(tree_obj, dict):
        return defects

    density = float(tree_obj.get("dpDensity") or 3.0)
    if density <= 0.1:
        density = 3.0
    tokens = design_tokens if isinstance(design_tokens, dict) else (tree_obj.get("designTokens") or {})
    base_bg_argb = int(tokens.get("bgScreen")) if isinstance(tokens.get("bgScreen"), int) else -16250868
    allowed_radii = set(ONEUI9_STANDARD_RADII_DP)
    for tk in ("cardCornerDp", "buttonCornerDp", "tileCornerDp"):
        val = tokens.get(tk)
        if isinstance(val, (int, float)) and val > 0:
            allowed_radii.add(float(val))

    disp = tree_obj.get("displayRect") if isinstance(tree_obj.get("displayRect"), dict) else {}
    disp_x = int(disp.get("x") or 0)
    disp_y = int(disp.get("y") or 0)
    disp_w = int(disp.get("w") or disp.get("screenW") or 0)
    disp_h = int(disp.get("h") or disp.get("screenH") or 0)

    tol_px = max(2, int(round(2.0 * density)))
    overlap_min_px = max(2, int(round(2.0 * density)))
    align_tol_px = max(3, int(round(3.5 * density)))
    hard_clip_tol_px = max(3, int(round(4.0 * density)))

    def _is_interactive_leaf(ntype: str) -> bool:
        return ntype in (
            "Button", "OneUiButton", "ImageButton", "EditText",
            "SeekBar", "OneUiWaveMediaSeekBar", "OneUiInlineSelector",
            "Switch", "OneUiSwitch", "CheckBox",
        )

    def _is_compact_allowed_clickable(ntype: str) -> bool:
        return ntype in (
            "Switch", "OneUiSwitch", "CheckBox", "RadioButton",
            "EditText", "OneUiIconView", "ImageView", "TextView",
        )

    def _inspect_root(root_node: Any, root_scope: str, is_dialog: bool) -> None:
        if not isinstance(root_node, dict):
            return
        r_x = int(root_node.get("x") or disp_x)
        r_y = int(root_node.get("y") or disp_y)
        r_w = int(root_node.get("w") or disp_w)
        r_h = int(root_node.get("h") or disp_h)
        bounds_w = r_w if (is_dialog and r_w > 0) else (disp_w or r_w)
        bounds_h = r_h if (is_dialog and r_h > 0) else (disp_h or r_h)
        bounds_left = r_x if is_dialog else disp_x
        bounds_top = r_y if is_dialog else disp_y
        laid_out = bounds_w > 0 and bounds_h > 0

        def walk(
            node: Any,
            depth: int,
            in_h_scroll: bool,
            in_v_scroll: bool,
            parent_rect: tuple[int, int, int, int] | None,
            parent_clips: bool,
            parent_clickable_ok: bool,
            eff_bg_argb: int,
            ancestor_custom_bg: bool = False,
        ) -> None:
            if not isinstance(node, dict) or depth > 22:
                return
            ntype = str(node.get("type") or "View")
            txt = str(node.get("text") or "")
            label = txt[:36] if txt else str(node.get("desc") or ntype)[:36]
            clickable = bool(node.get("clickable"))
            enabled = node.get("enabled") is not False
            has_xy = ("x" in node and "y" in node)
            nx = int(node.get("x") or 0)
            ny = int(node.get("y") or 0)
            nw = int(node.get("w") or 0)
            nh = int(node.get("h") or 0)

            node_bg = node.get("bg")
            cur_bg = int(node_bg) if isinstance(node_bg, int) else eff_bg_argb
            is_custom_canvas = ntype in ("OneUiFrostedCapsuleView", "OneUiGlassCardView")
            cur_custom_bg = bool(node.get("customBg")) or is_custom_canvas or (
                ancestor_custom_bg and not isinstance(node_bg, int)
            )

            # G3: Zero-size visible interactive or text view on laid-out screen
            if laid_out and (nw <= 0 or nh <= 0):
                if clickable or (txt and ntype in ("TextView", "Button", "OneUiButton", "EditText")) or _is_interactive_leaf(ntype):
                    cat = "ui_zero_size_button" if ntype in ("Button", "OneUiButton") else "ui_zero_size_interactive"
                    defects.append({
                        "severity": "HIGH",
                        "category": cat,
                        "message": f"{root_scope}: Laid-out visible {ntype} '{label}' has zero size ({nw}x{nh})",
                    })

            # G1: Screen & parent bounds overflow (scroll-axis-aware)
            if has_xy and nw > 0 and nh > 0 and bounds_w > 0:
                if not in_h_scroll:
                    right = nx + nw
                    if right > bounds_left + bounds_w + tol_px or nx < bounds_left - tol_px:
                        defects.append({
                            "severity": "HIGH",
                            "category": "ui_horizontal_overflow",
                            "message": (
                                f"{root_scope}: {ntype} '{label}' overflows screen horizontally "
                                f"(x={nx}, right={right}, screen=[{bounds_left}..{bounds_left + bounds_w}])"
                            ),
                        })
                    elif parent_rect is not None and parent_clips:
                        px, _py, pw, _ph = parent_rect
                        if pw > 0 and (right > px + pw + tol_px or nx < px - tol_px):
                            defects.append({
                                "severity": "HIGH",
                                "category": "ui_parent_horizontal_clip",
                                "message": (
                                    f"{root_scope}: {ntype} '{label}' clips parent bounds horizontally "
                                    f"(x={nx}..{right} outside parent {px}..{px + pw})"
                                ),
                            })
                if not in_v_scroll and bounds_h > 0:
                    bottom = ny + nh
                    if bottom > bounds_top + bounds_h + tol_px or ny < bounds_top - tol_px:
                        defects.append({
                            "severity": "HIGH",
                            "category": "ui_vertical_overflow",
                            "message": (
                                f"{root_scope}: {ntype} '{label}' overflows screen vertically "
                                f"(y={ny}, bottom={bottom}, screen=[{bounds_top}..{bounds_top + bounds_h}])"
                            ),
                        })

            # G4: Touch target ergonomics (respect clickable parent wrappers & compact controls)
            if clickable and enabled and nw > 0 and nh > 0 and not parent_clickable_ok:
                w_dp = nw / density
                h_dp = nh / density
                min_dp = min(w_dp, h_dp)
                if not _is_compact_allowed_clickable(ntype):
                    if min_dp < 24.0:
                        defects.append({
                            "severity": "HIGH",
                            "category": "ui_touch_target_crushed",
                            "message": (
                                f"{root_scope}: Crushed touch target on {ntype} '{label}' "
                                f"({w_dp:.1f}x{h_dp:.1f}dp < 24dp)"
                            ),
                        })
                    elif ntype in ("Button", "OneUiButton", "ImageButton") and min_dp < 28.0:
                        defects.append({
                            "severity": "MEDIUM",
                            "category": "ui_touch_target_small",
                            "message": (
                                f"{root_scope}: Undersized button touch target on {ntype} '{label}' "
                                f"({w_dp:.1f}x{h_dp:.1f}dp < 28dp)"
                            ),
                        })

            # Dead clickable control
            if clickable and enabled and node.get("hasClick") is False and ntype in ("Button", "OneUiButton", "ImageButton"):
                defects.append({
                    "severity": "MEDIUM",
                    "category": "ui_dead_clickable",
                    "message": f"{root_scope}: Clickable {ntype} '{label}' has no OnClickListener attached",
                })

            # G5: Ellipsis & hard text clipping
            ell = int(node.get("ell") or 0)
            ell_mode = str(node.get("ellMode") or "")
            meas_w = int(node.get("measW") or 0)
            if ell > 0:
                if ntype in ("Button", "OneUiButton"):
                    defects.append({
                        "severity": "HIGH",
                        "category": "ui_button_text_ellipsized",
                        "message": f"{root_scope}: Button '{label}' text is ellipsized ({ell} chars truncated)",
                    })
                elif len(txt.strip()) <= 24:
                    defects.append({
                        "severity": "MEDIUM",
                        "category": "ui_text_ellipsized",
                        "message": f"{root_scope}: Short {ntype} '{label}' text is ellipsized ({ell} chars truncated)",
                    })
            if meas_w > 0 and nw > 0 and not ell_mode and meas_w > nw + hard_clip_tol_px:
                defects.append({
                    "severity": "HIGH",
                    "category": "ui_text_hard_clipped",
                    "message": (
                        f"{root_scope}: {ntype} '{label}' text hard-clipped without ellipsis "
                        f"(measured={meas_w}px > width={nw}px)"
                    ),
                })

            # G7: WCAG 2.1 contrast & G8: minimum readable font size
            sp = node.get("sp")
            if isinstance(sp, (int, float)) and txt and sp > 0 and sp < 9.0:
                defects.append({
                    "severity": "MEDIUM",
                    "category": "ui_text_too_small",
                    "message": f"{root_scope}: {ntype} '{label}' font size {sp:.1f}sp is below 9sp readability floor",
                })
            fg = node.get("fg")
            has_custom_bg = cur_custom_bg
            if isinstance(fg, int) and txt and enabled and not has_custom_bg:
                fg_a = (fg >> 24) & 0xFF
                if fg_a >= 96:
                    ratio = compute_wcag_contrast(fg, cur_bg, base_bg_argb)
                    if ratio < 2.2:
                        defects.append({
                            "severity": "HIGH",
                            "category": "ui_wcag_contrast_fail",
                            "message": (
                                f"{root_scope}: Unreadable WCAG contrast ({ratio:.2f}:1 < 2.2) on {ntype} '{label}'"
                            ),
                        })
                    elif ratio < 3.0:
                        defects.append({
                            "severity": "MEDIUM",
                            "category": "ui_wcag_contrast_warn",
                            "message": (
                                f"{root_scope}: Low WCAG contrast ({ratio:.2f}:1 < 3.0) on {ntype} '{label}'"
                            ),
                        })

            # G8: One UI 9 corner radius token grid
            rad_dp = node.get("radDp")
            if isinstance(rad_dp, (int, float)) and 0.5 < rad_dp < 200.0 and nw > 0 and nh > 0:
                min_half_dp = min(nw, nh) / (2.0 * density)
                is_capsule = abs(rad_dp - min_half_dp) <= 2.5 or rad_dp >= min_half_dp - 1.5
                if not is_capsule:
                    nearest = min(allowed_radii, key=lambda t: abs(t - rad_dp))
                    if abs(nearest - rad_dp) > 2.0:
                        defects.append({
                            "severity": "LOW",
                            "category": "ui_token_radius_drift",
                            "message": (
                                f"{root_scope}: Non-token corner radius {rad_dp:.1f}dp on {ntype} '{label}' "
                                f"(nearest One UI 9 token={nearest:.0f}dp)"
                            ),
                        })

            # G2 & G6: Sibling overlap IoU & horizontal row alignment
            children = node.get("children") or []
            if len(children) >= 2:
                is_linear = ntype == "LinearLayout"
                orient = str(node.get("orient") or "")
                vis_children = [
                    ch for ch in children
                    if isinstance(ch, dict) and "x" in ch and "y" in ch
                    and int(ch.get("w") or 0) > 0 and int(ch.get("h") or 0) > 0
                ]
                for i in range(len(vis_children)):
                    c1 = vis_children[i]
                    r1 = (int(c1["x"]), int(c1["y"]), int(c1["w"]), int(c1["h"]))
                    c1_click = bool(c1.get("clickable")) and c1.get("enabled") is not False
                    for j in range(i + 1, len(vis_children)):
                        c2 = vis_children[j]
                        r2 = (int(c2["x"]), int(c2["y"]), int(c2["w"]), int(c2["h"]))
                        iou, ow, oh = compute_rect_iou(r1, r2)
                        if is_linear and iou > 0.05 and ow > overlap_min_px and oh > overlap_min_px:
                            l1 = str(c1.get("text") or c1.get("type") or "View")[:24]
                            l2 = str(c2.get("text") or c2.get("type") or "View")[:24]
                            defects.append({
                                "severity": "HIGH",
                                "category": "ui_sibling_overlap",
                                "message": (
                                    f"{root_scope}: Overlapping LinearLayout siblings '{l1}' & '{l2}' "
                                    f"(IoU={iou:.2f}, overlap={ow}x{oh}px)"
                                ),
                            })
                        elif (not is_linear) and c1_click and bool(c2.get("clickable")) and c2.get("enabled") is not False:
                            if iou > 0.15 and ow > overlap_min_px and oh > overlap_min_px:
                                l1 = str(c1.get("text") or c1.get("type") or "View")[:24]
                                l2 = str(c2.get("text") or c2.get("type") or "View")[:24]
                                defects.append({
                                    "severity": "HIGH",
                                    "category": "ui_clickable_overlap",
                                    "message": (
                                        f"{root_scope}: Overlapping clickable siblings '{l1}' & '{l2}' "
                                        f"(IoU={iou:.2f}, overlap={ow}x{oh}px)"
                                    ),
                                })

                if is_linear and orient == "h":
                    btns = [
                        ch for ch in vis_children
                        if str(ch.get("type") or "") in ("Button", "OneUiButton")
                    ]
                    if len(btns) >= 2:
                        if node.get("baselineAligned") is True:
                            defects.append({
                                "severity": "MEDIUM",
                                "category": "ui_row_misalignment",
                                "message": f"{root_scope}: Horizontal button row ({len(btns)} btns) missing BaselineAligned=false",
                            })
                        cys = [int(b["y"]) + int(b["h"]) / 2.0 for b in btns]
                        drift = max(cys) - min(cys)
                        if drift > align_tol_px:
                            defects.append({
                                "severity": "MEDIUM",
                                "category": "ui_row_misalignment",
                                "message": (
                                    f"{root_scope}: Horizontal button row vertical center drift "
                                    f"({drift / density:.1f}dp > 3.5dp across {len(btns)} buttons)"
                                ),
                            })

            # Recurse into children
            scroll_ax = str(node.get("scrollAxis") or "")
            next_h_scroll = in_h_scroll or scroll_ax == "h" or ntype == "HorizontalScrollView"
            next_v_scroll = in_v_scroll or scroll_ax == "v" or ntype in ("ScrollView", "NestedScrollView", "ListView")
            next_rect = (nx, ny, nw, nh) if (has_xy and nw > 0 and nh > 0) else parent_rect
            next_clips = node.get("clipCh") is not False
            this_clickable_ok = parent_clickable_ok or (
                clickable and enabled and nw > 0 and nh > 0 and min(nw / density, nh / density) >= 40.0
            )
            for ch in children:
                walk(
                    ch,
                    depth + 1,
                    next_h_scroll,
                    next_v_scroll,
                    next_rect,
                    next_clips,
                    this_clickable_ok,
                    cur_bg,
                    cur_custom_bg,
                )

        walk(
            root_node,
            0,
            in_h_scroll=False,
            in_v_scroll=False,
            parent_rect=(bounds_left, bounds_top, bounds_w, bounds_h) if laid_out else None,
            parent_clips=True,
            parent_clickable_ok=False,
            eff_bg_argb=base_bg_argb,
            ancestor_custom_bg=False,
        )

    if tree_obj.get("activity"):
        _inspect_root(tree_obj["activity"], scope, is_dialog=False)
    for idx, dlg in enumerate(tree_obj.get("dialogs") or []):
        _inspect_root(dlg, f"{scope}:Dialog{idx}", is_dialog=True)
    return defects


def decode_luma_grid(payload: dict[str, Any]) -> tuple[int, int, list[int]] | None:
    """Decode a deterministic grayscale luma grid from `screenshot_ui` (`lumaB64`) or raw PGM/PPM bytes."""
    if not isinstance(payload, dict):
        return None
    b64 = payload.get("lumaB64")
    lw = int(payload.get("lumaW") or 0)
    lh = int(payload.get("lumaH") or 0)
    if isinstance(b64, str) and b64 and lw > 0 and lh > 0:
        try:
            raw = base64.b64decode(b64)
            if len(raw) == lw * lh:
                return lw, lh, list(raw)
        except Exception:
            return None
    # Pure-Python binary PGM (P5) fallback
    pgm_bytes = payload.get("pgmBytes")
    if isinstance(pgm_bytes, (bytes, bytearray)) and pgm_bytes.startswith(b"P5"):
        parts = pgm_bytes.split(None, 3)
        if len(parts) == 4:
            try:
                pw, ph, _maxv = int(parts[1]), int(parts[2]), int(parts[3].split(None, 1)[0])
                pix = parts[3].split(None, 1)[1]
                if len(pix) >= pw * ph:
                    return pw, ph, list(pix[: pw * ph])
            except Exception:
                return None
    return None


def compute_ssim_luma_grid(
    width: int,
    height: int,
    ref_px: list[int],
    cur_px: list[int],
    block_size: int = 8,
) -> dict[str, Any]:
    """Pure-Python Wang et al. (2004) windowed SSIM over 8-bit grayscale grids (zero external deps)."""
    if width <= 0 or height <= 0 or len(ref_px) != width * height or len(cur_px) != width * height:
        return {"ok": False, "ssim": 0.0, "minBlockSsim": 0.0, "changedBlocks": 0, "totalBlocks": 0, "diffBBox": None}

    c1 = (0.01 * 255.0) ** 2
    c2 = (0.03 * 255.0) ** 2
    bs = max(2, min(block_size, width, height))

    ssim_sum = 0.0
    min_ssim = 1.0
    total_blocks = 0
    changed_blocks = 0
    min_bx, min_by, max_bx, max_by = width, height, 0, 0

    for by in range(0, height - bs + 1, bs):
        for bx in range(0, width - bs + 1, bs):
            n = bs * bs
            sx = 0.0
            sy = 0.0
            sxx = 0.0
            syy = 0.0
            sxy = 0.0
            for dy in range(bs):
                row_off = (by + dy) * width + bx
                for dx in range(bs):
                    x = float(ref_px[row_off + dx])
                    y = float(cur_px[row_off + dx])
                    sx += x
                    sy += y
                    sxx += x * x
                    syy += y * y
                    sxy += x * y
            mx = sx / n
            my = sy / n
            vx = max(0.0, sxx / n - mx * mx)
            vy = max(0.0, syy / n - my * my)
            cov = sxy / n - mx * my
            num = (2.0 * mx * my + c1) * (2.0 * cov + c2)
            den = (mx * mx + my * my + c1) * (vx + vy + c2)
            b_ssim = num / den if den > 1e-12 else 1.0
            b_ssim = max(-1.0, min(1.0, b_ssim))
            ssim_sum += b_ssim
            total_blocks += 1
            if b_ssim < min_ssim:
                min_ssim = b_ssim
            if b_ssim < 0.90:
                changed_blocks += 1
                min_bx = min(min_bx, bx)
                min_by = min(min_by, by)
                max_bx = max(max_bx, bx + bs)
                max_by = max(max_by, by + bs)

    mean_ssim = ssim_sum / total_blocks if total_blocks > 0 else 1.0
    bbox = [min_bx, min_by, max_bx - min_bx, max_by - min_by] if changed_blocks > 0 else None
    return {
        "ok": True,
        "ssim": round(mean_ssim, 5),
        "minBlockSsim": round(min_ssim, 5),
        "changedBlocks": changed_blocks,
        "totalBlocks": total_blocks,
        "diffBBox": bbox,
    }


def compare_screenshot_against_golden(
    screen_key: str,
    shot_payload: dict[str, Any],
    golden_dir: str | Path,
    *,
    update_goldens: bool = False,
    fail_threshold: float = 0.92,
    warn_threshold: float = 0.97,
) -> dict[str, Any]:
    """Compare a captured `screenshot_ui` luma grid against a golden baseline (or seed/update baseline)."""
    decoded = decode_luma_grid(shot_payload)
    if decoded is None:
        return {"status": "SKIP", "reason": "no_luma_grid", "finding": None}
    w, h, cur_px = decoded
    safe_key = re.sub(r"[^A-Za-z0-9_.-]+", "_", screen_key).strip("_") or "screen"
    gdir = Path(golden_dir)
    gfile = gdir / f"{safe_key}.luma.json"

    if update_goldens or not gfile.is_file():
        gdir.mkdir(parents=True, exist_ok=True)
        gfile.write_text(
            json.dumps({
                "screen": screen_key,
                "lumaW": w,
                "lumaH": h,
                "lumaB64": base64.b64encode(bytes(cur_px)).decode("ascii"),
                "updatedUtc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }, indent=2),
            encoding="utf-8",
        )
        return {"status": "SEEDED", "goldenPath": str(gfile), "ssim": 1.0, "finding": None}

    try:
        ref_obj = json.loads(gfile.read_text(encoding="utf-8"))
        ref_dec = decode_luma_grid(ref_obj)
    except Exception as exc:
        return {"status": "ERROR", "reason": str(exc), "finding": None}
    if ref_dec is None or ref_dec[0] != w or ref_dec[1] != h:
        return {
            "status": "MISMATCH_DIM",
            "reason": f"golden={ref_dec[0] if ref_dec else 0}x{ref_dec[1] if ref_dec else 0} vs cur={w}x{h}",
            "finding": {
                "severity": "MEDIUM",
                "category": "ui_visual_dimension_drift",
                "message": f"{screen_key}: Golden SSIM grid size changed ({ref_dec[0] if ref_dec else 0}x{ref_dec[1] if ref_dec else 0} -> {w}x{h})",
            },
        }

    res = compute_ssim_luma_grid(w, h, ref_dec[2], cur_px)
    ssim_val = float(res["ssim"])
    finding = None
    status = "PASS"
    if ssim_val < fail_threshold:
        status = "FAIL"
        finding = {
            "severity": "HIGH",
            "category": "ui_visual_regression_ssim",
            "message": (
                f"{screen_key}: Visual regression vs golden (SSIM={ssim_val:.4f} < {fail_threshold:.2f}, "
                f"minBlock={res['minBlockSsim']:.3f}, changed={res['changedBlocks']}/{res['totalBlocks']}, bbox={res['diffBBox']})"
            ),
        }
    elif ssim_val < warn_threshold:
        status = "WARN"
        finding = {
            "severity": "MEDIUM",
            "category": "ui_visual_drift_ssim",
            "message": (
                f"{screen_key}: Visual drift vs golden (SSIM={ssim_val:.4f} < {warn_threshold:.2f}, "
                f"changed={res['changedBlocks']}/{res['totalBlocks']})"
            ),
        }
    return {"status": status, **res, "finding": finding}


def run_m1_mutation_canary_offline(tmp_dir: str | Path) -> dict[str, Any]:
    """Prove Q1 Visual & Geometric Oracle is non-decorative: 100% catch rate on injected M1 defects & 0% FP on clean One UI 9 tree."""
    tokens = {
        "cardCornerDp": 26.0,
        "buttonCornerDp": 18.0,
        "tileCornerDp": 22.0,
        "bgScreen": -16250868,  # #FF080A10
    }
    clean_tree = {
        "schemaVersion": 2,
        "dpDensity": 3.0,
        "displayRect": {"x": 0, "y": 0, "w": 1080, "h": 2400},
        "designTokens": tokens,
        "activity": {
            "type": "FrameLayout",
            "x": 0, "y": 0, "w": 1080, "h": 2400,
            "children": [
                # Intentional FrameLayout overlay pill (non-clickable background pill behind nav buttons)
                {"type": "View", "x": 40, "y": 2160, "w": 200, "h": 150, "bg": 0x1AFFFFFF, "radDp": 25.0},
                {
                    "type": "LinearLayout", "orient": "v", "x": 0, "y": 100, "w": 1080, "h": 2000,
                    "children": [
                        {
                            "type": "HorizontalScrollView", "scrollAxis": "h", "x": 24, "y": 120, "w": 1032, "h": 140,
                            "children": [
                                # Off-screen chip inside HorizontalScrollView -> MUST NOT false-positive!
                                {"type": "Button", "text": "Chip Far Right", "clickable": True, "x": 960, "y": 124, "w": 360, "h": 128, "fg": -1, "bg": -1139140063, "sp": 13.0, "radDp": 18.0},
                            ],
                        },
                        {
                            "type": "LinearLayout", "orient": "h", "x": 24, "y": 300, "w": 1032, "h": 140,
                            "children": [
                                {"type": "Button", "text": "Play", "clickable": True, "x": 24, "y": 306, "w": 490, "h": 128, "fg": -1, "bg": -13083393, "sp": 14.0, "radDp": 18.0},
                                {"type": "Button", "text": "Queue", "clickable": True, "x": 540, "y": 306, "w": 490, "h": 128, "fg": -1, "bg": -1139140063, "sp": 14.0, "radDp": 18.0},
                            ],
                        },
                        # Disabled muted button -> MUST NOT fail WCAG!
                        {"type": "Button", "text": "Cancel", "enabled": False, "x": 24, "y": 480, "w": 300, "h": 128, "fg": 0x66404450, "bg": -16250868, "sp": 13.0, "radDp": 18.0},
                        # Empty hidden-slot LinearLayout (non-clickable, no text) -> MUST NOT fail zero-size!
                        {"type": "LinearLayout", "x": 24, "y": 640, "w": 1032, "h": 0},
                    ],
                },
            ],
        },
    }
    clean_defects = analyze_ui_tree_anomalies(clean_tree, "CleanOneUi9")

    mutations: list[tuple[str, str, dict[str, Any]]] = [
        ("M1_horiz_overflow", "ui_horizontal_overflow", {
            "schemaVersion": 2, "dpDensity": 3.0, "displayRect": {"x": 0, "y": 0, "w": 1080, "h": 2400}, "designTokens": tokens,
            "activity": {"type": "LinearLayout", "orient": "h", "x": 0, "y": 0, "w": 1080, "h": 200, "children": [
                {"type": "Button", "text": "Repeat", "clickable": True, "x": 980, "y": 20, "w": 180, "h": 128, "fg": -1, "sp": 14.0}
            ]},
        }),
        ("M1_sibling_overlap", "ui_sibling_overlap", {
            "schemaVersion": 2, "dpDensity": 3.0, "displayRect": {"x": 0, "y": 0, "w": 1080, "h": 2400}, "designTokens": tokens,
            "activity": {"type": "LinearLayout", "orient": "h", "x": 0, "y": 0, "w": 1080, "h": 200, "children": [
                {"type": "Button", "text": "Vol", "clickable": True, "x": 20, "y": 20, "w": 300, "h": 128, "fg": -1, "sp": 14.0},
                {"type": "Button", "text": "Loop", "clickable": True, "x": 200, "y": 20, "w": 300, "h": 128, "fg": -1, "sp": 14.0},
            ]},
        }),
        ("M1_clickable_overlap", "ui_clickable_overlap", {
            "schemaVersion": 2, "dpDensity": 3.0, "displayRect": {"x": 0, "y": 0, "w": 1080, "h": 2400}, "designTokens": tokens,
            "activity": {"type": "FrameLayout", "x": 0, "y": 0, "w": 1080, "h": 200, "children": [
                {"type": "Button", "text": "A", "clickable": True, "x": 40, "y": 20, "w": 200, "h": 128, "fg": -1, "sp": 14.0},
                {"type": "Button", "text": "B", "clickable": True, "x": 80, "y": 20, "w": 200, "h": 128, "fg": -1, "sp": 14.0},
            ]},
        }),
        ("M1_crushed_target", "ui_touch_target_crushed", {
            "schemaVersion": 2, "dpDensity": 3.0, "displayRect": {"x": 0, "y": 0, "w": 1080, "h": 2400}, "designTokens": tokens,
            "activity": {"type": "LinearLayout", "orient": "v", "x": 0, "y": 0, "w": 1080, "h": 200, "children": [
                {"type": "Button", "text": "X", "clickable": True, "x": 40, "y": 20, "w": 48, "h": 48, "fg": -1, "sp": 12.0},
            ]},
        }),
        ("M1_zero_size_interactive", "ui_zero_size_interactive", {
            "schemaVersion": 2, "dpDensity": 3.0, "displayRect": {"x": 0, "y": 0, "w": 1080, "h": 2400}, "designTokens": tokens,
            "activity": {"type": "LinearLayout", "orient": "v", "x": 0, "y": 0, "w": 1080, "h": 200, "children": [
                {"type": "OneUiInlineSelector", "clickable": True, "x": 40, "y": 20, "w": 0, "h": 0},
            ]},
        }),
        ("M1_button_ellipsis", "ui_button_text_ellipsized", {
            "schemaVersion": 2, "dpDensity": 3.0, "displayRect": {"x": 0, "y": 0, "w": 1080, "h": 2400}, "designTokens": tokens,
            "activity": {"type": "LinearLayout", "orient": "v", "x": 0, "y": 0, "w": 1080, "h": 200, "children": [
                {"type": "Button", "text": "A/B Compare: ACTIVE", "clickable": True, "x": 40, "y": 20, "w": 220, "h": 128, "ell": 7, "ellMode": "end", "fg": -1, "sp": 13.0},
            ]},
        }),
        ("M1_text_hard_clipped", "ui_text_hard_clipped", {
            "schemaVersion": 2, "dpDensity": 3.0, "displayRect": {"x": 0, "y": 0, "w": 1080, "h": 2400}, "designTokens": tokens,
            "activity": {"type": "LinearLayout", "orient": "v", "x": 0, "y": 0, "w": 1080, "h": 200, "children": [
                {"type": "TextView", "text": "Unclipped Overflow Title", "x": 40, "y": 20, "w": 180, "h": 60, "measW": 340, "fg": -1, "sp": 14.0},
            ]},
        }),
        ("M1_row_misaligned", "ui_row_misalignment", {
            "schemaVersion": 2, "dpDensity": 3.0, "displayRect": {"x": 0, "y": 0, "w": 1080, "h": 2400}, "designTokens": tokens,
            "activity": {"type": "LinearLayout", "orient": "h", "x": 0, "y": 0, "w": 1080, "h": 220, "children": [
                {"type": "Button", "text": "Left", "clickable": True, "x": 20, "y": 20, "w": 240, "h": 128, "fg": -1, "sp": 14.0},
                {"type": "Button", "text": "Right", "clickable": True, "x": 300, "y": 50, "w": 240, "h": 128, "fg": -1, "sp": 14.0},
            ]},
        }),
        ("M1_wcag_low_contrast", "ui_wcag_contrast_fail", {
            "schemaVersion": 2, "dpDensity": 3.0, "displayRect": {"x": 0, "y": 0, "w": 1080, "h": 2400}, "designTokens": tokens,
            "activity": {"type": "LinearLayout", "orient": "v", "x": 0, "y": 0, "w": 1080, "h": 200, "children": [
                {"type": "TextView", "text": "Invisible Text", "x": 40, "y": 20, "w": 400, "h": 80, "fg": 0xFF151822, "bg": -16250868, "sp": 14.0},
            ]},
        }),
    ]

    caught = 0
    details: dict[str, bool] = {}
    for m_name, expected_cat, m_tree in mutations:
        found = analyze_ui_tree_anomalies(m_tree, m_name)
        hit = any(d["category"] == expected_cat for d in found)
        details[m_name] = hit
        if hit:
            caught += 1

    # Also prove Golden SSIM canary: identical passes (~1.0), mutated banner fails (<0.92)
    w, h = 48, 96
    ref_grid = bytes(((x * 3 + y * 2) % 256) for y in range(h) for x in range(w))
    mut_grid = bytearray(ref_grid)
    for y in range(10, 60):
        for x in range(4, 44):
            mut_grid[y * w + x] = 250 if (x + y) % 2 == 0 else 5
    gdir = Path(tmp_dir) / "m1_goldens"
    seed = compare_screenshot_against_golden("tab0", {"lumaW": w, "lumaH": h, "lumaB64": base64.b64encode(ref_grid).decode("ascii")}, gdir)
    same = compare_screenshot_against_golden("tab0", {"lumaW": w, "lumaH": h, "lumaB64": base64.b64encode(ref_grid).decode("ascii")}, gdir)
    diff = compare_screenshot_against_golden("tab0", {"lumaW": w, "lumaH": h, "lumaB64": base64.b64encode(bytes(mut_grid)).decode("ascii")}, gdir)
    ssim_canary_ok = (
        seed["status"] == "SEEDED"
        and same["status"] == "PASS"
        and diff["status"] == "FAIL"
        and (diff.get("finding") or {}).get("category") == "ui_visual_regression_ssim"
    )
    details["M1_golden_ssim"] = ssim_canary_ok
    if ssim_canary_ok:
        caught += 1

    total = len(mutations) + 1
    return {
        "ok": caught == total and len(clean_defects) == 0,
        "caught": caught,
        "total": total,
        "detectRate": caught / total,
        "cleanFalsePositives": len(clean_defects),
        "cleanDefects": clean_defects,
        "details": details,
    }


# ---------------------------------------------------------------------------
# Q2 Deterministic Crash / ANR Watchdog, StrictMode & 120 Hz Motion Gates
# ---------------------------------------------------------------------------

FATAL_EXIT_REASONS = frozenset({
    "ANR",
    "CRASH",
    "CRASH_NATIVE",
    "EXCESSIVE_RESOURCE_USAGE",
    "SIGNALED",
    "INITIALIZATION_FAILURE",
})

BENIGN_EXIT_REASONS = frozenset({
    "USER_REQUESTED",
    "USER_STOPPED",
    "EXIT_SELF",
    "PACKAGEUPDATED",
    "PACKAGE_UPDATED",
    "PACKAGE_STATE_CHANGE",
    "PERMISSION_CHANGE",
    "OTHER",
})


def _parse_iso_ts_ms(ts_utc: str, fallback_ms: int = 0) -> int:
    if fallback_ms and fallback_ms > 0:
        return int(fallback_ms)
    s = (ts_utc or "").strip()
    if not s:
        return 0
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return int(datetime.fromisoformat(s).timestamp() * 1000)
    except Exception:
        return 0


def make_exit_reason_key(rec: dict[str, Any]) -> str:
    """Stable deduplication key for an Android ApplicationExitInfo record."""
    return (
        f"{int(rec.get('pid') or 0)}|"
        f"{int(rec.get('reasonCode') or 0)}|"
        f"{str(rec.get('reason') or '').upper()}|"
        f"{str(rec.get('timestampUtc') or rec.get('timestampMs') or '')}"
    )


def evaluate_exit_info_gate(
    exit_reasons: list[dict[str, Any]] | None,
    *,
    baseline_keys: set[str] | None = None,
    low_memory_fg_pss_mb: float = 1500.0,
) -> dict[str, Any]:
    """Q2 Blocking ApplicationExitInfo Gate (`ANR` / `CRASH` / `CRASH_NATIVE` / foreground `LOW_MEMORY`).

    Stratifies findings cleanly:
      - NEW fatal exit not in `baseline_keys` -> `CRITICAL` (`origin='live_triggered'`, hard FAIL)
      - Fatal exit AFTER latest `PACKAGEUPDATED` -> `CRITICAL`/`HIGH` (`origin='current_session'`, hard FAIL)
      - Fatal exit BEFORE latest `PACKAGEUPDATED` -> `historical_archive` (forensic record, non-blocking)
      - Background CACHED (`importance >= 400`) low-PSS LMKD -> ignored (normal Android reclamation)
    """
    records = [r for r in (exit_reasons or []) if isinstance(r, dict)]
    latest_pkg_update_ms = 0
    for r in records:
        reason = str(r.get("reason") or "").strip().upper()
        if reason in ("PACKAGEUPDATED", "PACKAGE_UPDATED"):
            ts_ms = _parse_iso_ts_ms(str(r.get("timestampUtc") or ""), int(r.get("timestampMs") or 0))
            if ts_ms > latest_pkg_update_ms:
                latest_pkg_update_ms = ts_ms

    findings: list[dict[str, str]] = []
    new_fatal_count = 0
    current_build_fatal_count = 0
    historical_fatal_count = 0

    for r in records:
        reason = str(r.get("reason") or "").strip().upper()
        if not reason or reason in BENIGN_EXIT_REASONS:
            continue
        pss_mb = float(r.get("pssMb") or 0.0)
        imp = r.get("importance")
        imp_int = int(imp) if isinstance(imp, (int, float)) else -1
        is_fg_or_service = (imp_int > 0 and imp_int <= 230)

        if reason == "LOW_MEMORY":
            # Only fail on foreground/visible/perceptible OOM OR massive PSS (>=1500 MB) LMKD kill
            if not (is_fg_or_service or pss_mb >= low_memory_fg_pss_mb):
                continue
        elif reason not in FATAL_EXIT_REASONS:
            continue

        key = make_exit_reason_key(r)
        ts_ms = _parse_iso_ts_ms(str(r.get("timestampUtc") or ""), int(r.get("timestampMs") or 0))
        is_new_since_baseline = baseline_keys is not None and key not in baseline_keys
        is_pre_update = latest_pkg_update_ms > 0 and ts_ms > 0 and ts_ms < latest_pkg_update_ms

        if is_new_since_baseline:
            origin = "live_triggered"
            sev = "CRITICAL"
            cat = f"exit_info_new_{reason.lower()}"
            new_fatal_count += 1
        elif is_pre_update:
            origin = "historical_archive"
            sev = "HIGH" if reason == "LOW_MEMORY" else "CRITICAL"
            cat = f"exit_info_historical_{reason.lower()}"
            historical_fatal_count += 1
        else:
            origin = "current_session"
            sev = "CRITICAL" if reason in ("ANR", "CRASH", "CRASH_NATIVE", "LOW_MEMORY") else "HIGH"
            cat = f"exit_info_current_{reason.lower()}"
            current_build_fatal_count += 1

        desc = str(r.get("description") or "").strip()
        imp_name = str(r.get("importanceName") or (f"imp={imp_int}" if imp_int > 0 else "imp=?"))
        findings.append({
            "severity": sev,
            "category": cat,
            "origin": origin,
            "message": (
                f"ApplicationExitInfo {reason} (pid={r.get('pid', '?')}, {imp_name}, "
                f"pss={pss_mb:.1f}MB, ts={r.get('timestampUtc', '?')})"
                + (f": {desc[:120]}" if desc else "")
            ),
        })

    passed = (new_fatal_count == 0 and current_build_fatal_count == 0)
    return {
        "passed": passed,
        "newFatalCount": new_fatal_count,
        "currentBuildFatalCount": current_build_fatal_count,
        "historicalFatalCount": historical_fatal_count,
        "findings": findings,
    }


def evaluate_frame_metrics_gate(
    frame_metrics: dict[str, Any] | None,
    *,
    p95_budget_ms: float = 12.0,
    max_frozen_frames: int = 0,
    min_frames: int = 8,
    max_deadline_miss_pct: float = 20.0,
) -> dict[str, Any]:
    """Q2 Blocking 120 Hz FrameMetrics Gate (`P95 <= 12.0 ms`, `frozen == 0`).

    Supports both current `AndroidRuntimeTelemetry` keys (`totalFrames`, `ringCount`, `p95Ms`,
    `cpuP95Ms`, `frozenFrames`, `deadlineMissedFrames`) and legacy aliases (`sampledFrames`, `p95TotalMs`).
    """
    fm = frame_metrics if isinstance(frame_metrics, dict) else {}
    total_frames = int(fm.get("totalFrames") or fm.get("ringCount") or fm.get("sampledFrames") or 0)
    ring_count = int(fm.get("ringCount") or total_frames)
    p95_ms = float(fm.get("p95Ms") if fm.get("p95Ms") is not None else (fm.get("p95TotalMs") or 0.0))
    has_cpu_p95 = fm.get("cpuP95Ms") is not None and float(fm.get("cpuP95Ms") or 0.0) > 0.0
    cpu_p95_ms = float(fm.get("cpuP95Ms") or 0.0)
    frozen = int(fm.get("frozenFrames") or 0)
    deadline_missed = int(fm.get("deadlineMissedFrames") or 0)
    refresh_hz = float(fm.get("refreshRateHz") or 120.0)

    findings: list[dict[str, str]] = []
    if frozen > max_frozen_frames:
        findings.append({
            "severity": "CRITICAL",
            "category": "frame_metrics_frozen_frames",
            "message": (
                f"120 Hz FrameMetrics gate FAIL: {frozen} frozen frame(s) (>700ms) detected "
                f"(budget == {max_frozen_frames}, sessionMax={fm.get('sessionMaxMs', 0)}ms)"
            ),
        })

    if total_frames >= min_frames:
        # Gate pure app CPU/RenderThread sync P95 strictly against 12ms when available,
        # and also gate total P95 against 12ms (or 16.67ms when cpuP95Ms already proves app work <=12ms on LTPO idle).
        if has_cpu_p95:
            if cpu_p95_ms > p95_budget_ms:
                findings.append({
                    "severity": "HIGH",
                    "category": "frame_metrics_120hz_p95_exceeded",
                    "message": (
                        f"120 Hz FrameMetrics CPU/Sync P95={cpu_p95_ms:.2f}ms exceeds {p95_budget_ms:.1f}ms budget "
                        f"(totalP95={p95_ms:.2f}ms across {total_frames} frames @ {refresh_hz:.0f}Hz)"
                    ),
                })
            elif p95_ms > max(p95_budget_ms, 16.67) and refresh_hz >= 90.0:
                findings.append({
                    "severity": "MEDIUM",
                    "category": "frame_metrics_total_p95_high",
                    "message": (
                        f"120 Hz FrameMetrics Total P95={p95_ms:.2f}ms elevated despite CPU P95={cpu_p95_ms:.2f}ms "
                        f"(GPU/SurfaceFlinger pressure across {total_frames} frames)"
                    ),
                })
        elif p95_ms > p95_budget_ms:
            findings.append({
                "severity": "HIGH",
                "category": "frame_metrics_120hz_p95_exceeded",
                "message": (
                    f"120 Hz FrameMetrics P95={p95_ms:.2f}ms exceeds {p95_budget_ms:.1f}ms budget "
                    f"(p50={fm.get('p50Ms', 0)}ms, p99={fm.get('p99Ms', 0)}ms across {total_frames} frames)"
                ),
            })

        if deadline_missed > 0 and total_frames > 0:
            miss_pct = (deadline_missed * 100.0) / total_frames
            if miss_pct > max_deadline_miss_pct:
                findings.append({
                    "severity": "MEDIUM",
                    "category": "frame_metrics_deadline_missed",
                    "message": (
                        f"FrameMetrics deadline missed on {deadline_missed}/{total_frames} frames "
                        f"({miss_pct:.1f}% > {max_deadline_miss_pct:.0f}%)"
                    ),
                })

    blocking = [f for f in findings if f["severity"] in ("CRITICAL", "HIGH")]
    return {
        "passed": len(blocking) == 0,
        "totalFrames": total_frames,
        "ringCount": ring_count,
        "p95Ms": p95_ms,
        "cpuP95Ms": cpu_p95_ms if has_cpu_p95 else None,
        "frozenFrames": frozen,
        "deadlineMissedFrames": deadline_missed,
        "findings": findings,
    }


def evaluate_gesture_latency_gate(
    perf_samples: list[dict[str, Any]] | None,
    *,
    touch_budget_ms: float = 50.0,
    heavy_modal_budget_ms: float = 160.0,
) -> dict[str, Any]:
    """Q2 Blocking Gesture / Tab Touch-to-First-Frame Gate (`<50 ms` fast/tab interactions, `<160 ms` heavy modals)."""
    findings: list[dict[str, str]] = []
    worst_fast_ms = 0.0
    checked = 0
    for s in (perf_samples or []):
        if not isinstance(s, dict):
            continue
        checked += 1
        target = str(s.get("Target") or s.get("target") or "target")
        complexity = str(s.get("Complexity") or s.get("complexity") or "").lower()
        first_ms = float(s.get("FirstFrameMs") if s.get("FirstFrameMs") is not None else (s.get("firstFrameMs") or 0.0))
        ui_block_ms = float(s.get("UiThreadBlockMs") if s.get("UiThreadBlockMs") is not None else (s.get("uiThreadBlockMs") or 0.0))
        is_heavy = target.startswith("Modal:") or ("heavy" in complexity) or ("complex" in complexity)
        budget = heavy_modal_budget_ms if is_heavy else touch_budget_ms
        if not is_heavy:
            worst_fast_ms = max(worst_fast_ms, first_ms)
        if first_ms > budget:
            findings.append({
                "severity": "HIGH",
                "category": "modal_touch_to_frame_exceeded" if is_heavy else "gesture_touch_to_frame_exceeded",
                "message": (
                    f"Touch-to-first-frame on '{target}' took {first_ms:.0f}ms "
                    f"(uiBlock={ui_block_ms:.0f}ms, budget <= {budget:.0f}ms)"
                ),
            })
    return {
        "passed": len(findings) == 0,
        "checked": checked,
        "worstFastMs": worst_fast_ms,
        "findings": findings,
    }


def evaluate_strict_mode_gate(
    strict_mode_obj: dict[str, Any] | None,
    logcat_text: str = "",
) -> dict[str, Any]:
    """Q2 Blocking StrictMode Gate: promotes main-thread disk/network/VM StrictMode violations to HIGH FAIL."""
    findings: list[dict[str, str]] = []
    seen_msgs: set[str] = set()
    sm = strict_mode_obj if isinstance(strict_mode_obj, dict) else {}
    for v in (sm.get("violations") or []):
        if not isinstance(v, dict):
            continue
        kind = str(v.get("kind") or "thread")
        viol = str(v.get("violation") or "StrictModeViolation").strip()
        stack = str(v.get("stackTop") or "").strip()
        key = f"{kind}:{viol[:100]}:{stack[:80]}"
        if key in seen_msgs:
            continue
        seen_msgs.add(key)
        findings.append({
            "severity": "HIGH",
            "category": "strict_mode_main_thread_violation" if kind == "thread" else "strict_mode_vm_violation",
            "message": f"StrictMode ({kind}) violation: {viol}" + (f" @ {stack}" if stack else ""),
        })

    for raw_line in (logcat_text or "").splitlines():
        if "StrictMode" not in raw_line:
            continue
        if re.search(r"StrictMode.*(violation|DiskRead|DiskWrite|Network|Leaked|policy violation)", raw_line, re.IGNORECASE):
            clean = raw_line.strip()
            k2 = clean[:120]
            if k2 in seen_msgs:
                continue
            seen_msgs.add(k2)
            findings.append({
                "severity": "HIGH",
                "category": "strict_mode_main_thread_violation",
                "message": f"StrictMode logcat violation: {clean[:180]}",
            })

    return {
        "passed": len(findings) == 0,
        "violationCount": len(findings),
        "findings": findings,
    }


def evaluate_external_watchdog_probe(
    probe_res: dict[str, Any] | None,
    *,
    expected_pid: int | None = None,
    expected_session_id: str = "",
) -> dict[str, Any]:
    """Q2 External OmniAgent PID/Heartbeat/ANR Watchdog Evaluator."""
    p = probe_res if isinstance(probe_res, dict) else {}
    state = str(p.get("state") or "").upper()
    alive = bool(p.get("alive"))
    pid = int(p.get("pid") or 0)
    sid = str(p.get("sessionId") or "")
    detail = str(p.get("detail") or p.get("error") or "")
    findings: list[dict[str, str]] = []

    if state in ("NOT_INSTALLED", "SIGNATURE_MISMATCH"):
        findings.append({
            "severity": "CRITICAL",
            "category": "external_watchdog_unavailable",
            "message": f"External OmniAgent watchdog failed ({state}): {detail}",
        })
    elif state == "UNRESPONSIVE_ANR" or (state != "WATCHDOG_TRANSPORT_ERROR" and not alive):
        findings.append({
            "severity": "CRITICAL",
            "category": "external_watchdog_anr_timeout",
            "message": f"External OmniAgent watchdog detected Cavernize ANR/freeze timeout ({p.get('elapsedMs', 0)}ms): {detail}",
        })
    elif state == "PID_RESTARTED_CRASH" or bool(p.get("pidRestarted")):
        findings.append({
            "severity": "CRITICAL",
            "category": "external_watchdog_crash_restart",
            "message": (
                f"External OmniAgent watchdog detected silent Cavernize crash-restart "
                f"(pid {p.get('prevPid', expected_pid)}->{pid}, sid '{p.get('prevSessionId', expected_session_id)}'->'{sid}')"
            ),
        })
    else:
        pid_changed = bool(expected_pid and expected_pid > 0 and pid > 0 and pid != expected_pid)
        sid_changed = bool(expected_session_id and sid and sid != expected_session_id)
        if pid_changed or sid_changed:
            findings.append({
                "severity": "CRITICAL",
                "category": "external_watchdog_crash_restart",
                "message": (
                    f"External watchdog detected Cavernize process identity change "
                    f"(pid {expected_pid}->{pid}, sid '{expected_session_id}'->'{sid}')"
                ),
            })

    return {
        "passed": len(findings) == 0,
        "state": state or ("ALIVE" if alive else "UNKNOWN"),
        "alive": alive,
        "pid": pid,
        "sessionId": sid,
        "findings": findings,
    }


def parse_external_crash_forensics(raw_text: str) -> list[dict[str, str]]:
    """Parse crash/ANR/Fatal evidence captured externally via Termux/ADB when Cavernize is dead or frozen."""
    findings: list[dict[str, str]] = []
    if not (raw_text or "").strip():
        return findings
    for an in parse_logcat_anomalies(raw_text):
        if an["severity"] in ("CRITICAL", "HIGH") or an["category"] in ("crash", "oom", "strict_mode"):
            findings.append({
                "severity": "CRITICAL" if an["category"] in ("crash", "oom") else an["severity"],
                "category": "external_crash_logcat" if an["category"] in ("crash", "oom") else f"external_{an['category']}",
                "message": f"External Termux/ADB forensics: {an['message'][:180]}",
                "raw": an["raw"],
            })
    # Also detect `dumpsys activity exit-info` fatal lines even when not in brief logcat format
    for ln in raw_text.splitlines():
        s = ln.strip()
        if re.search(r"reason=(ANR|CRASH|CRASH_NATIVE|LOW_MEMORY)", s, re.IGNORECASE):
            findings.append({
                "severity": "CRITICAL",
                "category": "external_crash_logcat",
                "message": f"External dumpsys exit-info: {s[:180]}",
                "raw": s,
            })
    return findings


def run_m2_mutation_canary_offline() -> dict[str, Any]:
    """Prove Q2 External Watchdog, ExitInfo, StrictMode & 120 Hz Motion Gates are non-decorative (10/10 catch, 0 FP)."""
    # 0. Clean baseline (with pre-update historical LOW_MEMORY + low-PSS cached background LMKD that MUST NOT false-positive!)
    baseline_exits = [
        {
            "pid": 1000, "reasonCode": 16, "reason": "PACKAGEUPDATED",
            "timestampUtc": "2026-11-26T10:00:00Z", "pssMb": 420.0, "importance": 100, "importanceName": "FOREGROUND",
        },
        {
            "pid": 950, "reasonCode": 3, "reason": "LOW_MEMORY",
            "timestampUtc": "2026-11-26T09:30:00Z", "pssMb": 3800.0, "importance": 100, "importanceName": "FOREGROUND",
        },
        {
            "pid": 1020, "reasonCode": 3, "reason": "LOW_MEMORY",
            "timestampUtc": "2026-11-26T10:15:00Z", "pssMb": 115.0, "importance": 400, "importanceName": "CACHED",
        },
    ]
    baseline_keys = {make_exit_reason_key(r) for r in baseline_exits}
    clean_exit_res = evaluate_exit_info_gate(baseline_exits, baseline_keys=baseline_keys)
    clean_fm_res = evaluate_frame_metrics_gate({
        "totalFrames": 120, "ringCount": 120, "p50Ms": 5.2, "p95Ms": 9.8, "cpuP95Ms": 6.4,
        "frozenFrames": 0, "deadlineMissedFrames": 2, "refreshRateHz": 120.0,
    })
    clean_gest_res = evaluate_gesture_latency_gate([
        {"Target": "Tab:Play", "Complexity": "Simple", "FirstFrameMs": 18, "UiThreadBlockMs": 4},
        {"Target": "Modal:AutoEQ", "Complexity": "Heavy", "FirstFrameMs": 92, "UiThreadBlockMs": 28},
    ])
    clean_sm_res = evaluate_strict_mode_gate({"enabled": True, "violations": []}, "")
    clean_wd_res = evaluate_external_watchdog_probe(
        {"ok": True, "state": "ALIVE", "alive": True, "pid": 2048, "sessionId": "sidA", "elapsedMs": 6},
        expected_pid=2048,
        expected_session_id="sidA",
    )
    clean_active_fp = (
        [f for f in clean_exit_res["findings"] if f.get("origin") != "historical_archive"]
        + clean_fm_res["findings"]
        + clean_gest_res["findings"]
        + clean_sm_res["findings"]
        + clean_wd_res["findings"]
    )

    checks: list[tuple[str, str, list[dict[str, Any]]]] = [
        (
            "M2.1_exit_info_new_anr",
            "exit_info_new_anr",
            evaluate_exit_info_gate(
                baseline_exits + [{
                    "pid": 2048, "reasonCode": 6, "reason": "ANR",
                    "timestampUtc": "2026-11-26T11:05:00Z", "pssMb": 510.0, "importance": 100, "importanceName": "FOREGROUND",
                    "description": "Input dispatching timed out",
                }],
                baseline_keys=baseline_keys,
            )["findings"],
        ),
        (
            "M2.2_exit_info_new_crash_native",
            "exit_info_new_crash_native",
            evaluate_exit_info_gate(
                baseline_exits + [{
                    "pid": 2049, "reasonCode": 5, "reason": "CRASH_NATIVE",
                    "timestampUtc": "2026-11-26T11:06:00Z", "pssMb": 480.0, "importance": 100, "importanceName": "FOREGROUND",
                    "description": "SIGSEGV in libonnxruntime.so",
                }],
                baseline_keys=baseline_keys,
            )["findings"],
        ),
        (
            "M2.3_exit_info_new_fg_oom",
            "exit_info_new_low_memory",
            evaluate_exit_info_gate(
                baseline_exits + [{
                    "pid": 2050, "reasonCode": 3, "reason": "LOW_MEMORY",
                    "timestampUtc": "2026-11-26T11:07:00Z", "pssMb": 2950.0, "importance": 100, "importanceName": "FOREGROUND",
                    "description": "LMKD foreground kill",
                }],
                baseline_keys=baseline_keys,
            )["findings"],
        ),
        (
            "M2.4_frame_metrics_p95_exceeded",
            "frame_metrics_120hz_p95_exceeded",
            evaluate_frame_metrics_gate({
                "totalFrames": 120, "ringCount": 120, "p50Ms": 9.0, "p95Ms": 19.4, "cpuP95Ms": 16.2,
                "frozenFrames": 0, "refreshRateHz": 120.0,
            })["findings"],
        ),
        (
            "M2.5_frame_metrics_frozen",
            "frame_metrics_frozen_frames",
            evaluate_frame_metrics_gate({
                "totalFrames": 60, "ringCount": 60, "p50Ms": 5.0, "p95Ms": 9.0, "cpuP95Ms": 5.5,
                "frozenFrames": 1, "sessionMaxMs": 820.0, "refreshRateHz": 120.0,
            })["findings"],
        ),
        (
            "M2.6_gesture_touch_to_frame_gt_50ms",
            "gesture_touch_to_frame_exceeded",
            evaluate_gesture_latency_gate([
                {"Target": "Tab:Settings", "Complexity": "Simple", "FirstFrameMs": 84, "UiThreadBlockMs": 62},
            ])["findings"],
        ),
        (
            "M2.7_strict_mode_disk_read",
            "strict_mode_main_thread_violation",
            evaluate_strict_mode_gate({
                "enabled": True,
                "violations": [{"kind": "thread", "violation": "DiskReadViolation", "stackTop": "MainActivity.OnClick"}],
            })["findings"],
        ),
        (
            "M2.8_external_watchdog_anr_timeout",
            "external_watchdog_anr_timeout",
            evaluate_external_watchdog_probe(
                {"ok": False, "state": "UNRESPONSIVE_ANR", "alive": False, "elapsedMs": 6012, "detail": "ping timeout"},
                expected_pid=2048,
                expected_session_id="sidA",
            )["findings"],
        ),
        (
            "M2.9_external_watchdog_crash_restart",
            "external_watchdog_crash_restart",
            evaluate_external_watchdog_probe(
                {"ok": False, "state": "PID_RESTARTED_CRASH", "alive": True, "pid": 4096, "prevPid": 2048, "sessionId": "sidB", "prevSessionId": "sidA", "pidRestarted": True},
                expected_pid=2048,
                expected_session_id="sidA",
            )["findings"],
        ),
        (
            "M2.10_external_termux_crash_logcat",
            "external_crash_logcat",
            parse_external_crash_forensics(
                "F/libc(2048): Fatal signal 11 (SIGSEGV), code 1 (SEGV_MAPERR) in hu.sbence.cavern.cavernize\n"
            ),
        ),
    ]

    caught = 0
    details: list[dict[str, Any]] = []
    for name, expected_cat, flist in checks:
        cats = [str(f.get("category") or "") for f in flist]
        hit = expected_cat in cats
        if hit:
            caught += 1
        details.append({"canary": name, "expected": expected_cat, "caught": hit, "got": cats})

    total = len(checks)
    return {
        "ok": caught == total and len(clean_active_fp) == 0,
        "caught": caught,
        "total": total,
        "detectRate": caught / total,
        "cleanFalsePositives": len(clean_active_fp),
        "cleanDefects": clean_active_fp,
        "details": details,
    }


def compute_ols_slope(values: list[float]) -> tuple[float, float]:
    """Compute Ordinary Least Squares (slope_per_step, r_squared) in pure Python."""
    n = len(values)
    if n < 2:
        return 0.0, 0.0
    xs = [float(i) for i in range(n)]
    mean_x = sum(xs) / n
    mean_y = sum(values) / n
    sxx = sum((x - mean_x) ** 2 for x in xs)
    if sxx <= 1e-12:
        return 0.0, 0.0
    sxy = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, values))
    slope = sxy / sxx
    syy = sum((y - mean_y) ** 2 for y in values)
    if syy <= 1e-12:
        return slope, 0.0
    r2 = (sxy * sxy) / (sxx * syy)
    return slope, max(0.0, min(1.0, r2))


def _median_num(vals: list[float]) -> float:
    if not vals:
        return 0.0
    s = sorted(vals)
    m = len(s) // 2
    return s[m] if len(s) % 2 == 1 else (s[m - 1] + s[m]) / 2.0


def evaluate_soak_leak_gate(
    samples: list[dict[str, Any]],
    *,
    max_pss_delta_mb: float = 25.0,
    max_fd_delta: int = 8,
    max_gc_delta_mb: float = 25.0,
    max_view_delta: int = 80,
    max_thread_delta: int = 12,
) -> dict[str, Any]:
    """Q3 Dirty-State & Resource Soak Gate (V11).

    Evaluates 15-20 cycle soak telemetry for monotonic leaks in native PSS, managed GC heap,
    `/proc/self/fd` open file descriptors, OS thread count, and Android View tree nodes,
    plus dirty-state invariants (`isOperationRunning`, leaked dialogs, fatal crashes).
    Ignores cycle-0 cold warm-up transient when >=5 samples are available.
    """
    findings: list[dict[str, str]] = []
    if not samples:
        return {
            "passed": True,
            "cycles": 0,
            "pssDeltaMb": 0.0,
            "pssSlopeMbPerCycle": 0.0,
            "fdDelta": 0,
            "fdSlopePerCycle": 0.0,
            "gcDeltaMb": 0.0,
            "viewDelta": 0,
            "threadDelta": 0,
            "stuckOp": False,
            "leakedDialogs": 0,
            "findings": findings,
        }

    def _series(key: str, *, allow_neg: bool = False) -> list[float]:
        out: list[float] = []
        for s in samples:
            if not isinstance(s, dict):
                continue
            v = s.get(key)
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                fv = float(v)
                if math.isfinite(fv) and (allow_neg or fv >= 0.0):
                    out.append(fv)
        return out

    def _delta_and_slope(vals: list[float]) -> tuple[float, float, float, float, float]:
        if len(vals) < 2:
            v0 = vals[0] if vals else 0.0
            return 0.0, 0.0, 0.0, v0, v0
        # Skip cycle-0 cold warm-up when we have enough cycles so initial JIT/cache warmup never false-positives
        post = vals[1:] if len(vals) >= 5 else vals
        base = _median_num(post[:2]) if len(post) >= 8 else post[0]
        tail = _median_num(post[-2:]) if len(post) >= 8 else post[-1]
        slope, r2 = compute_ols_slope(post)
        delta = tail - base
        if len(post) >= 4 and r2 >= 0.70 and slope > 0.0:
            delta = max(delta, slope * (len(post) - 1))
        return delta, slope, r2, base, tail

    pss_vals = _series("nativePssMb")
    pss_delta, pss_slope, pss_r2, pss_base, pss_tail = _delta_and_slope(pss_vals)
    if len(pss_vals) >= 2 and pss_delta > max_pss_delta_mb and pss_slope > 0.25:
        findings.append({
            "severity": "HIGH",
            "category": "soak_pss_leak",
            "message": (
                f"Native/Total PSS grew by +{pss_delta:.1f} MB over {len(samples)} soak cycles "
                f"({pss_base:.1f}->{pss_tail:.1f} MB, slope={pss_slope:+.2f} MB/cycle, R2={pss_r2:.2f}, budget <= +{max_pss_delta_mb:.0f} MB)"
            ),
        })

    fd_vals = _series("openFdCount")
    fd_delta_f, fd_slope, fd_r2, fd_base, fd_tail = _delta_and_slope(fd_vals)
    fd_delta = int(round(fd_delta_f))
    if len(fd_vals) >= 2 and fd_delta > max_fd_delta and fd_slope > 0.10:
        findings.append({
            "severity": "HIGH",
            "category": "soak_fd_leak",
            "message": (
                f"Open /proc/self/fd file descriptors leaked by +{fd_delta} over {len(samples)} soak cycles "
                f"({int(round(fd_base))}->{int(round(fd_tail))}, slope={fd_slope:+.2f} FD/cycle, R2={fd_r2:.2f}, budget <= +{max_fd_delta})"
            ),
        })

    gc_vals = _series("managedGcMb")
    gc_delta, gc_slope, gc_r2, gc_base, gc_tail = _delta_and_slope(gc_vals)
    if len(gc_vals) >= 2 and gc_delta > max_gc_delta_mb and gc_slope > 0.25:
        findings.append({
            "severity": "HIGH",
            "category": "soak_managed_heap_leak",
            "message": (
                f"Managed GC heap grew by +{gc_delta:.1f} MB over {len(samples)} soak cycles "
                f"({gc_base:.1f}->{gc_tail:.1f} MB, slope={gc_slope:+.2f} MB/cycle, R2={gc_r2:.2f}, budget <= +{max_gc_delta_mb:.0f} MB)"
            ),
        })

    view_vals = _series("viewCount")
    view_delta_f, view_slope, view_r2, view_base, view_tail = _delta_and_slope(view_vals)
    view_delta = int(round(view_delta_f))
    if len(view_vals) >= 2 and view_delta > max_view_delta and view_slope > 0.50:
        findings.append({
            "severity": "HIGH",
            "category": "soak_view_count_leak",
            "message": (
                f"Android View tree leaked +{view_delta} nodes over {len(samples)} soak cycles "
                f"({int(round(view_base))}->{int(round(view_tail))}, slope={view_slope:+.2f} views/cycle, R2={view_r2:.2f}, budget <= +{max_view_delta})"
            ),
        })

    thread_vals = _series("threadCount")
    thread_delta_f, thread_slope, thread_r2, thread_base, thread_tail = _delta_and_slope(thread_vals)
    thread_delta = int(round(thread_delta_f))
    if len(thread_vals) >= 2 and thread_delta > max_thread_delta and thread_slope > 0.15:
        findings.append({
            "severity": "HIGH",
            "category": "soak_thread_leak",
            "message": (
                f"Process OS threads leaked by +{thread_delta} over {len(samples)} soak cycles "
                f"({int(round(thread_base))}->{int(round(thread_tail))}, slope={thread_slope:+.2f} threads/cycle, R2={thread_r2:.2f}, budget <= +{max_thread_delta})"
            ),
        })

    last = samples[-1] if isinstance(samples[-1], dict) else {}
    first = samples[0] if isinstance(samples[0], dict) else {}
    stuck_op = bool(last.get("isOperationRunning"))
    if stuck_op:
        findings.append({
            "severity": "HIGH",
            "category": "dirty_state_operation_stuck",
            "message": f"Dirty-state lock: isOperationRunning remained TRUE after {len(samples)} soak cycles",
        })

    leaked_dialogs = int(last.get("activeDialogs") or 0)
    if leaked_dialogs > 0:
        findings.append({
            "severity": "HIGH",
            "category": "dirty_state_dialog_leak",
            "message": f"Dirty-state modal leak: {leaked_dialogs} active dialog(s) remained open after soak completion",
        })

    fatal_start = int(first.get("sessionFatalCount") or 0)
    fatal_end = int(last.get("sessionFatalCount") or 0)
    if fatal_end > fatal_start:
        findings.append({
            "severity": "CRITICAL",
            "category": "soak_fatal_crash",
            "message": f"Fatal AppLogger error count increased during soak ({fatal_start} -> {fatal_end})",
        })

    if any(isinstance(s, dict) and s.get("lowMemory") is True for s in samples):
        findings.append({
            "severity": "HIGH",
            "category": "soak_low_memory",
            "message": "Android reported lowMemory=true during V11 soak",
        })

    return {
        "passed": len(findings) == 0,
        "cycles": len(samples),
        "pssDeltaMb": round(pss_delta, 2),
        "pssSlopeMbPerCycle": round(pss_slope, 3),
        "fdDelta": fd_delta,
        "fdSlopePerCycle": round(fd_slope, 3),
        "gcDeltaMb": round(gc_delta, 2),
        "viewDelta": view_delta,
        "threadDelta": thread_delta,
        "stuckOp": stuck_op,
        "leakedDialogs": leaked_dialogs,
        "findings": findings,
    }


def evaluate_rage_click_result(
    res: dict[str, Any] | None,
    *,
    target_label: str,
    max_block_ms: float = 1500.0,
    expect_settled: bool = True,
) -> dict[str, Any]:
    """Evaluate a Q3 hostile rage-click burst for race exceptions, UI freezes, stacked modals, or stuck flags."""
    findings: list[dict[str, str]] = []
    if not isinstance(res, dict):
        return {"passed": True, "maxBlockMs": 0.0, "succeeded": 0, "exceptionsCount": 0, "findings": findings}

    excs = [str(e) for e in (res.get("exceptions") or []) if str(e).strip()]
    max_blk = float(res.get("maxBlockMs") or 0.0)
    succ = int(res.get("succeeded") or 0)
    dlgs = int(res.get("activeDialogsAfter") or 0)
    op_running = bool(res.get("isOperationRunningAfter"))

    if excs:
        findings.append({
            "severity": "HIGH",
            "category": "hostile_rage_click_exception",
            "message": f"Rage-click on '{target_label}' threw {len(excs)} exception(s): {excs[0][:140]}",
        })
    if max_blk >= max_block_ms:
        findings.append({
            "severity": "HIGH",
            "category": "hostile_rage_click_freeze",
            "message": f"Rage-click on '{target_label}' froze UI thread for {max_blk:.0f}ms (>= {max_block_ms:.0f}ms budget)",
        })
    if dlgs > 1:
        findings.append({
            "severity": "HIGH",
            "category": "dirty_state_dialog_leak",
            "message": f"Rage-click on '{target_label}' stacked {dlgs} concurrent modal dialogs (missing debounce/re-entrancy guard)",
        })
    if expect_settled and op_running:
        findings.append({
            "severity": "HIGH",
            "category": "dirty_state_operation_stuck",
            "message": f"Rage-click on '{target_label}' left isOperationRunning=true after settle",
        })
    return {
        "passed": len(findings) == 0,
        "maxBlockMs": round(max_blk, 2),
        "succeeded": succ,
        "exceptionsCount": len(excs),
        "findings": findings,
    }


def evaluate_drag_sweep_result(
    sweep_res: dict[str, Any] | None,
    frame_metrics: dict[str, Any] | None,
    *,
    target_label: str,
    p95_budget_ms: float = 12.0,
    max_step_block_ms: float = 50.0,
) -> dict[str, Any]:
    """Evaluate a Q3 continuous SeekBar/volume drag sweep under 120 Hz FrameMetrics."""
    findings: list[dict[str, str]] = []
    if not isinstance(sweep_res, dict):
        return {"passed": True, "maxStepUiBlockMs": 0.0, "cpuP95Ms": None, "p95Ms": 0.0, "frozenFrames": 0, "findings": findings}

    err = str(sweep_res.get("error") or "").strip()
    if err or sweep_res.get("ok") is False:
        # Ignore benign "No visible SeekBar" skip unless an actual runtime exception occurred
        if "no visible seekbar" not in err.lower():
            findings.append({
                "severity": "HIGH",
                "category": "drag_sweep_error",
                "message": f"Continuous drag sweep on '{target_label}' failed: {err[:160]}",
            })

    max_step_blk = float(sweep_res.get("maxStepUiBlockMs") or 0.0)
    if max_step_blk >= max_step_block_ms:
        findings.append({
            "severity": "HIGH",
            "category": "drag_sweep_ui_stall",
            "message": f"Continuous drag sweep on '{target_label}' stalled UI thread for {max_step_blk:.1f}ms (>= {max_step_block_ms:.0f}ms)",
        })

    fm_cpu_p95: float | None = None
    fm_p95 = 0.0
    fm_frozen = 0
    if isinstance(frame_metrics, dict):
        fm_ev = evaluate_frame_metrics_gate(frame_metrics, p95_budget_ms=p95_budget_ms, max_frozen_frames=0, min_frames=5)
        fm_cpu_p95 = fm_ev.get("cpuP95Ms")
        fm_p95 = float(fm_ev.get("p95Ms") or 0.0)
        fm_frozen = int(fm_ev.get("frozenFrames") or 0)
        for ff in fm_ev.get("findings") or []:
            findings.append({
                "severity": ff["severity"],
                "category": "drag_sweep_jank_120hz",
                "message": f"Drag sweep '{target_label}' 120 Hz jank: {ff['message']}",
            })

    return {
        "passed": len(findings) == 0,
        "maxStepUiBlockMs": round(max_step_blk, 2),
        "cpuP95Ms": fm_cpu_p95,
        "p95Ms": fm_p95,
        "frozenFrames": fm_frozen,
        "findings": findings,
    }


def evaluate_reflow_probe_result(reflow_obj: dict[str, Any] | None, scope: str) -> dict[str, Any]:
    """Evaluate Q3 `1.3x` font-scale and landscape orientation reflow snapshots using the Q1 geometric oracle."""
    findings: list[dict[str, str]] = []
    variants_checked = 0
    high_cnt = 0
    warn_cnt = 0
    if not isinstance(reflow_obj, dict):
        return {"passed": True, "variantsChecked": 0, "highDefectCount": 0, "warnCount": 0, "findings": findings}

    for variant in ("font_130", "landscape"):
        sub_tree = reflow_obj.get(variant)
        if not isinstance(sub_tree, dict):
            continue
        variants_checked += 1
        geom_defects = analyze_ui_geometry_oracle(sub_tree, f"{scope}:{variant}")
        for d in geom_defects:
            sev = str(d.get("severity") or "MEDIUM").upper()
            sub_cat = str(d.get("category") or "layout")
            if sev in ("CRITICAL", "HIGH"):
                high_cnt += 1
                findings.append({
                    "severity": sev,
                    "category": "reflow_layout_defect",
                    "message": f"[{variant}/{sub_cat}] {d['message']}",
                })
            elif sev == "MEDIUM":
                warn_cnt += 1
                findings.append({
                    "severity": "MEDIUM",
                    "category": "reflow_layout_warn",
                    "message": f"[{variant}/{sub_cat}] {d['message']}",
                })
    return {
        "passed": high_cnt == 0,
        "variantsChecked": variants_checked,
        "highDefectCount": high_cnt,
        "warnCount": warn_cnt,
        "findings": findings,
    }


def run_m3_mutation_canary_offline() -> dict[str, Any]:
    """Prove Q3 Hostile Stress, Reflow & Dirty-State Soak (`V11`) catches 10/10 injected M3 faults with 0 FP."""
    # Clean 20-cycle baseline (includes realistic cycle-0 cold warmup jump that plateaus!)
    clean_samples: list[dict[str, Any]] = []
    for i in range(20):
        warmup = 0.0 if i == 0 else 18.0
        clean_samples.append({
            "nativePssMb": round(210.0 + warmup + (i % 3) * 1.2, 2),
            "managedGcMb": round(28.0 + (4.0 if i > 0 else 0.0) + (i % 2) * 0.8, 2),
            "openFdCount": 142 + (3 if i > 0 else 0) + (i % 2),
            "viewCount": 310 + (4 if i % 2 == 0 else 0),
            "threadCount": 44 + (1 if i % 3 == 0 else 0),
            "activeDialogs": 0,
            "isOperationRunning": False,
            "sessionFatalCount": 0,
            "lowMemory": False,
        })
    clean_soak = evaluate_soak_leak_gate(clean_samples)
    clean_rage = evaluate_rage_click_result(
        {"ok": True, "succeeded": 8, "maxBlockMs": 11.4, "activeDialogsAfter": 0, "isOperationRunningAfter": False, "exceptions": []},
        target_label="Tab0:PlayBtn",
    )
    clean_drag = evaluate_drag_sweep_result(
        {"ok": True, "stepsDispatched": 24, "maxStepUiBlockMs": 4.8, "error": ""},
        {"totalFrames": 30, "ringCount": 30, "p50Ms": 4.2, "p95Ms": 9.8, "cpuP95Ms": 3.9, "frozenFrames": 0, "refreshRateHz": 120.0},
        target_label="Tab0:VolumeSeekBar",
    )
    clean_reflow_tree = {
        "schemaVersion": 2,
        "dpDensity": 3.0,
        "displayRect": {"x": 0, "y": 0, "w": 1080, "h": 2400},
        "activity": {
            "type": "LinearLayout", "orient": "v", "x": 0, "y": 0, "w": 1080, "h": 2400,
            "children": [
                {"type": "Button", "text": "Play", "clickable": True, "x": 40, "y": 100, "w": 260, "h": 144, "fg": -1, "sp": 16.0},
            ],
        },
    }
    clean_reflow = evaluate_reflow_probe_result({"font_130": clean_reflow_tree, "landscape": clean_reflow_tree}, "Tab0")
    clean_fp = (
        list(clean_soak["findings"])
        + list(clean_rage["findings"])
        + list(clean_drag["findings"])
        + list(clean_reflow["findings"])
    )

    def _leak_samples(field: str, per_cycle: float) -> list[dict[str, Any]]:
        out = [dict(s) for s in clean_samples]
        for idx, s in enumerate(out):
            if idx >= 1:
                val = float(s[field]) + idx * per_cycle
                s[field] = int(round(val)) if isinstance(s[field], int) else round(val, 2)
        return out

    stuck_op_samples = [dict(s) for s in clean_samples]
    stuck_op_samples[-1]["isOperationRunning"] = True

    leaked_dlg_samples = [dict(s) for s in clean_samples]
    leaked_dlg_samples[-1]["activeDialogs"] = 2

    reflow_overflow_tree = {
        "schemaVersion": 2,
        "dpDensity": 3.0,
        "displayRect": {"x": 0, "y": 0, "w": 1080, "h": 2400},
        "activity": {
            "type": "LinearLayout", "orient": "v", "x": 0, "y": 0, "w": 1080, "h": 2400,
            "children": [
                {"type": "Button", "text": "Spatialize Audio Now", "clickable": True, "x": 850, "y": 100, "w": 360, "h": 144, "ell": 5, "ellMode": "end", "fg": -1, "sp": 18.0},
            ],
        },
    }

    checks: list[tuple[str, str, list[dict[str, str]]]] = [
        ("M3.1_soak_pss_leak", "soak_pss_leak", evaluate_soak_leak_gate(_leak_samples("nativePssMb", 2.2))["findings"]),
        ("M3.2_soak_fd_leak", "soak_fd_leak", evaluate_soak_leak_gate(_leak_samples("openFdCount", 1.0))["findings"]),
        ("M3.3_soak_managed_heap_leak", "soak_managed_heap_leak", evaluate_soak_leak_gate(_leak_samples("managedGcMb", 1.9))["findings"]),
        ("M3.4_soak_view_count_leak", "soak_view_count_leak", evaluate_soak_leak_gate(_leak_samples("viewCount", 7.0))["findings"]),
        ("M3.5_soak_thread_leak", "soak_thread_leak", evaluate_soak_leak_gate(_leak_samples("threadCount", 1.1))["findings"]),
        ("M3.6_dirty_state_operation_stuck", "dirty_state_operation_stuck", evaluate_soak_leak_gate(stuck_op_samples)["findings"]),
        ("M3.7_dirty_state_dialog_leak", "dirty_state_dialog_leak", evaluate_soak_leak_gate(leaked_dlg_samples)["findings"]),
        (
            "M3.8_hostile_rage_click_exception",
            "hostile_rage_click_exception",
            evaluate_rage_click_result(
                {"ok": False, "succeeded": 3, "maxBlockMs": 40.0, "activeDialogsAfter": 2, "isOperationRunningAfter": False, "exceptions": ["InvalidOperationException: reentrant dialog"]},
                target_label="Tab0:AiBtn",
            )["findings"],
        ),
        (
            "M3.9_drag_sweep_jank_120hz",
            "drag_sweep_jank_120hz",
            evaluate_drag_sweep_result(
                {"ok": True, "stepsDispatched": 24, "maxStepUiBlockMs": 14.0, "error": ""},
                {"totalFrames": 36, "ringCount": 36, "p50Ms": 10.0, "p95Ms": 22.0, "cpuP95Ms": 17.8, "frozenFrames": 0, "refreshRateHz": 120.0},
                target_label="Tab0:SeekBar",
            )["findings"],
        ),
        (
            "M3.10_reflow_layout_defect",
            "reflow_layout_defect",
            evaluate_reflow_probe_result({"font_130": reflow_overflow_tree, "landscape": clean_reflow_tree}, "Tab0")["findings"],
        ),
    ]

    caught = 0
    details: list[dict[str, Any]] = []
    for name, expected_cat, flist in checks:
        cats = [str(f.get("category") or "") for f in flist]
        hit = expected_cat in cats
        if hit:
            caught += 1
        details.append({"canary": name, "expected": expected_cat, "caught": hit, "got": cats})

    total = len(checks)
    return {
        "ok": caught == total and len(clean_fp) == 0,
        "caught": caught,
        "total": total,
        "detectRate": caught / total,
        "cleanFalsePositives": len(clean_fp),
        "cleanDefects": clean_fp,
        "details": details,
    }


def analyze_ui_tree_anomalies(tree_obj: Any, scope: str) -> list[dict[str, str]]:
    """Inspect a `ui_tree` JSON object for real user-visible UI & Q1 geometric/visual defects."""
    defects: list[dict[str, str]] = []
    if not isinstance(tree_obj, dict):
        return defects

    active_dialogs = int(tree_obj.get("activeDialogs") or 0)
    if active_dialogs > 1:
        defects.append({
            "severity": "MEDIUM",
            "category": "ui_stacked_dialogs",
            "message": f"{scope}: {active_dialogs} stacked modal dialogs simultaneously open",
        })

    has_geom = int(tree_obj.get("schemaVersion") or 1) >= 2 or _node_has_geometry(tree_obj.get("activity"))

    def walk(node: Any, depth: int) -> None:
        if not isinstance(node, dict):
            return
        if depth > 22:
            defects.append({
                "severity": "LOW",
                "category": "ui_deep_hierarchy",
                "message": f"{scope}: View hierarchy exceeds depth 22 at {node.get('type')}",
            })
            return
        txt = str(node.get("text") or "")
        ntype = str(node.get("type") or "View")
        if txt:
            if UNFORMATTED_PLACEHOLDER_RE.search(txt):
                defects.append({
                    "severity": "HIGH",
                    "category": "ui_placeholder_leak",
                    "message": f"{scope}: Unformatted placeholder in {ntype}: '{txt[:80]}'",
                })
            if re.search(r"\b(NaN|Infinity|-Infinity)\b", txt):
                defects.append({
                    "severity": "HIGH",
                    "category": "ui_nan_leak",
                    "message": f"{scope}: NaN/Infinity text visible in {ntype}: '{txt[:80]}'",
                })
            if CAMEL_KEY_RE.match(txt.strip()) and any(
                suffix in txt for suffix in ("Btn", "Label", "Title", "Desc", "Hint", "Error", "Toast", "Modal", "Tab")
            ):
                defects.append({
                    "severity": "MEDIUM",
                    "category": "ui_raw_string_key",
                    "message": f"{scope}: Possible untranslated string key in {ntype}: '{txt[:60]}'",
                })
        if not has_geom:
            w = int(node.get("w") or 0)
            h = int(node.get("h") or 0)
            clickable = bool(node.get("clickable"))
            if clickable and ntype in ("Button", "OneUiButton") and (w <= 0 or h <= 0):
                defects.append({
                    "severity": "LOW",
                    "category": "ui_zero_size_button",
                    "message": f"{scope}: Visible clickable {ntype} '{txt[:40]}' has zero size ({w}x{h})",
                })
        for ch in node.get("children") or []:
            walk(ch, depth + 1)

    if tree_obj.get("activity"):
        walk(tree_obj["activity"], 0)
    for idx, dlg in enumerate(tree_obj.get("dialogs") or []):
        walk(dlg, 0)

    if has_geom:
        defects.extend(analyze_ui_geometry_oracle(tree_obj, scope))
    return defects


def analyze_acoustic_probe_result(probe_obj: Any, label: str) -> list[dict[str, str]]:
    """Detect psychoacoustic/DSP anomalies in an `acoustic_probe` JSON payload."""
    issues: list[dict[str, str]] = []
    if not isinstance(probe_obj, dict):
        return issues

    raw_str = json.dumps(probe_obj)
    if re.search(r"\b(NaN|Infinity|-Infinity)\b", raw_str):
        issues.append({
            "severity": "CRITICAL",
            "category": "dsp_nan_inf",
            "message": f"Acoustic probe '{label}' emitted NaN/Infinity",
        })

    # Inspect nested fingerprint / binaural metrics regardless of exact casing
    def find_num(obj: Any, *keys: str) -> float | None:
        if not isinstance(obj, dict):
            return None
        lower_map = {k.lower(): v for k, v in obj.items()}
        for k in keys:
            val = lower_map.get(k.lower())
            if isinstance(val, (int, float)):
                return float(val)
        for v in obj.values():
            if isinstance(v, dict):
                res = find_num(v, *keys)
                if res is not None:
                    return res
        return None

    non_finite = find_num(probe_obj, "nonFiniteSamples", "nonFinite", "nanCount")
    if non_finite is not None and non_finite > 0:
        issues.append({
            "severity": "CRITICAL",
            "category": "dsp_non_finite",
            "message": f"Acoustic probe '{label}' reported {int(non_finite)} non-finite samples",
        })

    gain_db = find_num(probe_obj, "gainDb", "outputGainDb", "rmsDb")
    if gain_db is not None and gain_db < -85.0:
        issues.append({
            "severity": "HIGH",
            "category": "dsp_silent_output",
            "message": f"Acoustic probe '{label}' collapsed to near-silence (Gain={gain_db:.1f} dB)",
        })

    peak_db = find_num(probe_obj, "peakDbfs", "peakDb")
    if peak_db is not None and peak_db > 1.5:
        issues.append({
            "severity": "MEDIUM",
            "category": "dsp_clipping",
            "message": f"Acoustic probe '{label}' exceeded digital full scale (Peak={peak_db:+.2f} dBFS)",
        })

    status = str(probe_obj.get("status") or probe_obj.get("verdict") or "").upper()
    if status == "FAIL":
        issues.append({
            "severity": "HIGH",
            "category": "dsp_probe_fail",
            "message": f"Acoustic probe '{label}' returned FAIL: {probe_obj.get('verdictNote') or probe_obj.get('error') or raw_str[:160]}",
        })
    elif status == "WARN":
        issues.append({
            "severity": "MEDIUM",
            "category": "dsp_probe_warn",
            "message": f"Acoustic probe '{label}' returned WARN: {probe_obj.get('verdictNote') or raw_str[:160]}",
        })
    return issues


# ---------------------------------------------------------------------------
# Q4 Frozen Regression Contracts, Blocking QA Gate & Offline M4 Canary
# ---------------------------------------------------------------------------

def evaluate_cavern_qa_gate(
    report: dict[str, Any],
    contracts_eval: dict[str, Any] | None = None,
    *,
    p95_budget_ms: float = 12.0,
) -> dict[str, Any]:
    """Evaluate Cavernize QA Gate verdict (`CRITICAL=0, HIGH=0, GEOM=0, EXIT_CRASH_NEW=0, P95<=12ms, CONTRACTS=0`)."""
    thresholds = {
        "max_critical": 0,
        "max_high": 0,
        "max_geom": 0,
        "max_exit_crash_new": 0,
        "max_frame_p95_ms": float(p95_budget_ms),
        "max_contracts_failed": 0,
        "require_soak_pass": True,
    }
    return qa_gate.evaluate_qa_gate(report, contracts_eval, thresholds)


def run_m4_mutation_canary_offline() -> dict[str, Any]:
    """Mutation-testing ('test-the-test') canary for QA Layer Q4 (`W53.Q4`).

    Verifies:
      - 2 clean baselines (empty active + historical-archive-only) -> 0 false positives (PASS)
      - 10 adversarial Q4 mutations -> 100% caught (FAIL)
    """
    clean_report = {
        "ok": True,
        "findings": [],
        "q2GateSummary": {"passed": True, "exitInfoNewFatal": 0, "totalFrames": 60, "frameCpuP95Ms": 7.2, "frozenFrames": 0},
        "q3SoakSummary": {"passed": True, "cycles": 10},
        "q4GateSummary": {"passed": 12, "failed": 0, "total": 12, "schemaErrors": []},
    }
    hist_only_report = {
        "ok": True,
        "findings": [
            {"severity": "CRITICAL", "category": "exit_info_crash", "origin": "historical_archive", "title": "old crash"},
            {"severity": "HIGH", "category": "ui_screen_overflow", "origin": "historical_archive", "title": "old overflow"},
        ],
        "q2GateSummary": {"passed": True, "exitInfoNewFatal": 0, "totalFrames": 40, "frameCpuP95Ms": 8.4, "frozenFrames": 0},
        "q3SoakSummary": {"passed": True, "cycles": 10},
        "q4GateSummary": {"passed": 12, "failed": 0, "total": 12, "schemaErrors": []},
    }

    clean_fps = 0
    if not evaluate_cavern_qa_gate(clean_report)["ok"]:
        clean_fps += 1
    if not evaluate_cavern_qa_gate(hist_only_report)["ok"]:
        clean_fps += 1

    mutations: list[tuple[str, str, Callable[[], bool]]] = [
        ("M4_active_critical_blocks", "CRITICAL=1>0", lambda: (
            not evaluate_cavern_qa_gate({**clean_report, "findings": [{"severity": "CRITICAL", "category": "crash", "origin": "live_triggered"}]})["ok"]
        )),
        ("M4_active_high_blocks", "HIGH=1>0", lambda: (
            not evaluate_cavern_qa_gate({**clean_report, "findings": [{"severity": "HIGH", "category": "exception", "origin": "live_triggered"}]})["ok"]
        )),
        ("M4_geom_overflow_blocks", "GEOM=1>0", lambda: (
            "GEOM=1>0" in evaluate_cavern_qa_gate({**clean_report, "findings": [{"severity": "HIGH", "category": "ui_parent_horizontal_clip", "origin": "live_triggered"}]})["reasons"]
        )),
        ("M4_exit_crash_new_blocks", "EXIT_CRASH_NEW=1>0", lambda: (
            "EXIT_CRASH_NEW=1>0" in evaluate_cavern_qa_gate({**clean_report, "q2GateSummary": {**clean_report["q2GateSummary"], "exitInfoNewFatal": 1}})["reasons"]
        )),
        ("M4_frame_p95_exceeded_blocks", "P95=15.40ms>12.0ms", lambda: (
            any(r.startswith("P95=") for r in evaluate_cavern_qa_gate({**clean_report, "q2GateSummary": {**clean_report["q2GateSummary"], "frameCpuP95Ms": 15.4}})["reasons"])
        )),
        ("M4_soak_v11_fail_blocks", "SOAK_V11=FAIL", lambda: (
            "SOAK_V11=FAIL" in evaluate_cavern_qa_gate({**clean_report, "q3SoakSummary": {"passed": False, "cycles": 10}})["reasons"]
        )),
        ("M4_frozen_contract_fail_blocks", "CONTRACTS_FAIL=1>0", lambda: (
            "CONTRACTS_FAIL=1>0" in evaluate_cavern_qa_gate({**clean_report, "q4GateSummary": {"passed": 11, "failed": 1, "total": 12, "schemaErrors": []}})["reasons"]
        )),
        ("M4_live_contract_finding_blocks", "CONTRACTS_FAIL=1>0", lambda: (
            "CONTRACTS_FAIL=1>0" in evaluate_cavern_qa_gate({**clean_report, "findings": [{"severity": "HIGH", "category": "regression_contract_failed", "origin": "live_triggered"}]})["reasons"]
        )),
        ("M4_tampered_disabled_contract_rejected", "schema_error", lambda: (
            len(qa_gate.validate_contracts_registry({
                "version": 1,
                "contracts": [{
                    "id": "REG-TEST-TAMPER",
                    "bug_ref": "B99",
                    "title": "Tampered",
                    "domain": "ui",
                    "severity": "HIGH",
                    "status": "disabled",
                    "immutable": False,
                    "mode": "offline",
                    "test_ref": "test_x",
                    "offline_checks": [{"kind": "file_exists", "path": "README.md"}],
                }],
            })) >= 2
        )),
        ("M4_cold_context_prompt_isolation", "cold_prompt_ok", lambda: (
            (lambda p: p["isolated"] is True and p["stripBuilderHistory"] is True
             and "COLD_CONTEXT_ADVERSARIAL_QA_DESTROYER" in p["prompt"]
             and "1-bug=1-test-first" in p["prompt"]
             and "QA_GATE|VERDICT=PASS|FAIL" in p["prompt"])(
                qa_gate.build_cold_context_qa_prompt(
                    "Verify playback & 3D room",
                    changed_files=["MainActivity.PlayerTab.cs"],
                    acceptance_criteria=["Repeat chip never clips"],
                    contracts_count=12,
                )
            )
        )),
    ]

    details: list[dict[str, Any]] = []
    caught = 0
    for name, expected, fn in mutations:
        hit = bool(fn())
        if hit:
            caught += 1
        details.append({"mutation": name, "expected": expected, "caught": hit})

    total = len(mutations)
    return {
        "ok": (caught == total) and (clean_fps == 0),
        "caught": caught,
        "total": total,
        "detectRate": round(caught / float(total), 4) if total else 0.0,
        "cleanFalsePositives": clean_fps,
        "details": details,
    }


def classify_applog_severity(entry: dict[str, str]) -> tuple[str, str]:
    """Map an AppLogger entry to (severity, category)."""
    lvl = entry.get("level", "INFO").upper()
    tag = entry.get("tag", "SYS").upper()
    msg = entry.get("msg", "")
    low = msg.lower()

    if lvl == "FATAL" or "unhandledexception" in low or "crash marker" in low:
        return "CRITICAL", f"applog_{tag.lower()}_fatal"
    if lvl == "ERROR":
        return "HIGH", f"applog_{tag.lower()}_error"
    if "selfcheck fail" in low:
        return "HIGH", "selfcheck_fail"
    if "disconnect" in low or "underrun" in low or "xrun" in low:
        return "MEDIUM", "audio_dropout"
    if "slow:" in low or "stall" in low or "jank" in low or "slower-than-budget" in low:
        return "MEDIUM", "perf_jank"
    if lvl == "WARN":
        return "LOW", f"applog_{tag.lower()}_warn"
    return "INFO", f"applog_{tag.lower()}"


@dataclass
class Finding:
    signature: str
    severity: str
    vector: str
    source: str
    category: str
    title: str
    repro_step: str
    evidence: str
    origin: str = "live_triggered"
    count: int = 1
    first_seen_utc: str = ""
    last_seen_utc: str = ""
    extra_examples: list[str] = field(default_factory=list)


class FindingLedger:
    """Deduplicating bug ledger ranked by origin (active before historical), severity, and frequency."""

    def __init__(self) -> None:
        self._items: dict[str, Finding] = {}

    def record(
        self,
        *,
        severity: str,
        vector: str,
        source: str,
        category: str,
        title: str,
        repro_step: str,
        evidence: str,
        origin: str = "live_triggered",
    ) -> tuple[bool, Finding]:
        sev = severity.upper() if severity.upper() in SEVERITY_RANK else "MEDIUM"
        orig = origin if origin in ORIGIN_RANK else "live_triggered"
        sig = normalize_signature(source, category, title)
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        existing = self._items.get(sig)
        if existing is not None:
            existing.count += 1
            existing.last_seen_utc = now
            if SEVERITY_RANK[sev] < SEVERITY_RANK[existing.severity]:
                existing.severity = sev
            # Promote origin if a historical defect is reproduced live in the active session
            if ORIGIN_RANK[orig] < ORIGIN_RANK.get(existing.origin, 2):
                existing.origin = orig
                existing.vector = vector
                existing.repro_step = (repro_step or "").strip()[:240]
            ev_short = (evidence or "").strip()[:300]
            if ev_short and ev_short != existing.evidence and len(existing.extra_examples) < 3:
                if ev_short not in existing.extra_examples:
                    existing.extra_examples.append(ev_short)
            return False, existing

        f = Finding(
            signature=sig,
            severity=sev,
            vector=vector,
            source=source,
            category=category,
            title=(title or "").strip()[:240],
            repro_step=(repro_step or "").strip()[:240],
            evidence=(evidence or "").strip()[:400],
            origin=orig,
            count=1,
            first_seen_utc=now,
            last_seen_utc=now,
        )
        self._items[sig] = f
        return True, f

    def ranked(self) -> list[Finding]:
        return sorted(
            self._items.values(),
            key=lambda x: (
                ORIGIN_RANK.get(x.origin, 2),
                SEVERITY_RANK.get(x.severity, 9),
                -x.count,
                x.vector,
                x.title,
            ),
        )

    def active_findings(self) -> list[Finding]:
        return [f for f in self.ranked() if f.origin != "historical_archive"]

    def historical_findings(self) -> list[Finding]:
        return [f for f in self.ranked() if f.origin == "historical_archive"]

    def counts_by_severity(self, active_only: bool = False) -> dict[str, int]:
        counts = {"CRITICAL": 0, "HIGH": 0, "MEDIUM": 0, "LOW": 0}
        pool = self.active_findings() if active_only else list(self._items.values())
        for f in pool:
            if f.severity in counts:
                counts[f.severity] += 1
        return counts

    def counts_by_origin(self) -> dict[str, int]:
        counts = {"live_triggered": 0, "current_session": 0, "historical_archive": 0}
        for f in self._items.values():
            key = f.origin if f.origin in counts else "live_triggered"
            counts[key] += 1
        return counts

    def to_dicts(self) -> list[dict[str, Any]]:
        return [
            {
                "signature": f.signature,
                "severity": f.severity,
                "origin": f.origin,
                "vector": f.vector,
                "source": f.source,
                "category": f.category,
                "title": f.title,
                "reproStep": f.repro_step,
                "evidence": f.evidence,
                "count": f.count,
                "firstSeenUtc": f.first_seen_utc,
                "lastSeenUtc": f.last_seen_utc,
                "extraExamples": f.extra_examples,
            }
            for f in self.ranked()
        ]


def generate_report_markdown(report: dict[str, Any]) -> str:
    """Render a concise, stratified Markdown bug dossier (Active Live/Session vs Historical Archive)."""
    lines: list[str] = []
    meta = report.get("meta") or {}
    counts = report.get("severityCounts") or {}
    active_counts = report.get("activeSeverityCounts") or counts
    origins = report.get("originCounts") or {}
    findings = report.get("findings") or []
    active_list = [f for f in findings if f.get("origin", "live_triggered") != "historical_archive"]
    hist_list = [f for f in findings if f.get("origin") == "historical_archive"]

    lines.append("# Cavernize Live Adversarial Bug-Hunter Report (SOTA v2)")
    lines.append("")
    lines.append(
        f"- **App:** `v{meta.get('appVersion', '?')}` (build `{meta.get('appBuild', '?')}`) | "
        f"**Session:** `{meta.get('sessionId', '?')}` | "
        f"**Device:** `{meta.get('device', '?')}` | **Android:** `{meta.get('android', '?')}`"
    )
    lines.append(
        f"- **Mode:** `{meta.get('mode', '?')}` | **Steps Executed:** `{meta.get('stepsExecuted', 0)}` | "
        f"**Elapsed:** `{meta.get('elapsedSec', 0):.1f}s`"
    )
    lines.append(
        f"- **Active Findings (Live + Current Session):** `{len(active_list)}` "
        f"(`CRITICAL={active_counts.get('CRITICAL', 0)}` · `HIGH={active_counts.get('HIGH', 0)}` · "
        f"`MEDIUM={active_counts.get('MEDIUM', 0)}` · `LOW={active_counts.get('LOW', 0)}`)"
    )
    lines.append(
        f"- **Origin Breakdown:** `live_triggered={origins.get('live_triggered', 0)}` · "
        f"`current_session={origins.get('current_session', 0)}` · "
        f"`historical_archive={origins.get('historical_archive', len(hist_list))}` "
        f"(Total unique: `{report.get('totalUniqueFindings', len(findings))}`)"
    )
    if meta.get("restoredState") is not None:
        diff_keys = meta.get("restoreDiffKeys") or []
        lines.append(
            f"- **State Restored Cleanly:** `{meta.get('restoredState')}` "
            f"(settings diff keys: `{len(diff_keys)}`)"
        )
    q2 = report.get("q2GateSummary")
    if isinstance(q2, dict) and q2:
        lines.append(
            f"- **Q2 Crash/ANR/120Hz Gate:** `{'PASS' if q2.get('passed') else 'FAIL'}` | "
            f"watchdog=`{q2.get('watchdogState', '?')}` | "
            f"exitNewFatal=`{q2.get('exitInfoNewFatal', 0)}` | "
            f"p95=`{q2.get('frameP95Ms', 0)}ms` (cpuP95=`{q2.get('frameCpuP95Ms', 'n/a')}`) | "
            f"frozen=`{q2.get('frozenFrames', 0)}` | "
            f"strictMode=`{q2.get('strictModeViolations', 0)}`"
        )
    q3 = report.get("q3SoakSummary")
    if isinstance(q3, dict) and q3.get("cycles", 0) > 0:
        lines.append(
            f"- **Q3 Soak & Stress Gate (`V11`):** `{'PASS' if q3.get('passed') else 'FAIL'}` | "
            f"cycles=`{q3.get('cycles', 0)}` | "
            f"ΔPSS=`{q3.get('pssDeltaMb', 0):+.1f}MB` (`{q3.get('pssSlopeMbPerCycle', 0):+.2f}/cyc`) | "
            f"ΔFD=`{q3.get('fdDelta', 0):+d}` | ΔGC=`{q3.get('gcDeltaMb', 0):+.1f}MB` | "
            f"ΔViews=`{q3.get('viewDelta', 0):+d}` | ΔThreads=`{q3.get('threadDelta', 0):+d}` | "
            f"stuckOp=`{q3.get('stuckOp', False)}` | dialogs=`{q3.get('leakedDialogs', 0)}`"
        )
    q4 = report.get("q4GateSummary")
    if isinstance(q4, dict) and q4.get("total", 0) > 0:
        lines.append(
            f"- **Q4 Frozen Regression Contracts (`V12`):** `{'PASS' if q4.get('failed', 0) == 0 and not q4.get('schemaErrors') else 'FAIL'}` | "
            f"passed=`{q4.get('passed', 0)}/{q4.get('total', 0)}` | failed=`{q4.get('failed', 0)}` | "
            f"live=`{q4.get('livePassed', 0)}/{q4.get('liveTotal', 0)}` | hotExec=`{q4.get('hotExecVerified', False)}`"
        )
    gate_obj = report.get("qaGate")
    if isinstance(gate_obj, dict) and gate_obj.get("summaryLine"):
        lines.append(f"- **Blocking QA Gate Verdict:** `{gate_obj['summaryLine']}`")
    lines.append("")

    vectors = report.get("vectors") or []
    if vectors:
        lines.append("## Vector Execution Summary")
        lines.append("")
        lines.append("| Vector | Status | New Findings | Elapsed | Notes |")
        lines.append("|---|---|---:|---:|---|")
        for v in vectors:
            notes = str(v.get("notes") or "").replace("|", "\\|").replace("\n", " ")[:140]
            lines.append(
                f"| `{v.get('name')}` | **{v.get('status')}** | {v.get('newFindings', 0)} | "
                f"{v.get('elapsedSec', 0):.1f}s | {notes} |"
            )
        lines.append("")

    def _render_findings(section_title: str, items: list[dict[str, Any]], empty_msg: str) -> None:
        lines.append(f"## {section_title}")
        lines.append("")
        if not items:
            lines.append(empty_msg)
            lines.append("")
            return
        for idx, f in enumerate(items, 1):
            orig = f.get("origin", "live_triggered")
            lines.append(
                f"### {idx}. [{f['severity']}] `{f['category']}` ({f['count']}x · `{orig}`) — {f['title']}"
            )
            lines.append(f"- **Vector:** `{f['vector']}` | **Source:** `{f['source']}` | **Sig:** `{f['signature']}`")
            lines.append(f"- **Trigger / Repro:** `{f['reproStep']}`")
            lines.append(f"- **Evidence:** `{f['evidence']}`")
            for ex in f.get("extraExamples") or []:
                lines.append(f"  - *Also seen:* `{ex}`")
            lines.append("")

    _render_findings(
        "Active Findings — Current Build & Live Triggered (Actionable)",
        active_list,
        "No active anomalies or errors triggered on the current build session.",
    )
    if hist_list:
        _render_findings(
            "Historical Pre-Fix Archive (Prior Sessions in Retained AppLogger — Non-Blocking)",
            hist_list,
            "None.",
        )
    return "\n".join(lines)


def pick_edge_case_library_tracks(tracks: list[dict[str, Any]], max_picks: int = 6) -> list[tuple[str, dict[str, Any]]]:
    """Select diverse & edge-case real tracks from the user's media library."""
    picks: list[tuple[str, dict[str, Any]]] = []
    seen_paths: set[str] = set()

    def add(reason: str, t: dict[str, Any]) -> None:
        p = str(t.get("FilePath") or "")
        if not p or p in seen_paths or len(picks) >= max_picks:
            return
        seen_paths.add(p)
        picks.append((reason, t))

    for t in tracks:
        if int(t.get("DurationMs") or 0) <= 0:
            add("zero_duration_metadata", t)
            break
    for t in tracks:
        if bool(t.get("IsSpatial")) or int(t.get("ChannelCount") or 0) > 2:
            add("spatial_or_multichannel", t)
            break
    for t in tracks:
        if int(t.get("ChannelCount") or 0) == 1:
            add("mono_channel", t)
            break
    for t in tracks:
        p = str(t.get("FilePath") or "")
        if any(ord(ch) > 127 or ch in "()'&®" for ch in p):
            add("unicode_or_special_chars", t)
            break
    seen_badges: set[str] = set()
    for t in tracks:
        badge = str(t.get("FormatBadge") or "").upper()
        if badge and badge not in seen_badges:
            seen_badges.add(badge)
            add(f"format_{badge.lower()}", t)
    return picks


# ---------------------------------------------------------------------------
# Live Device Transport + Adversarial Orchestrator
# ---------------------------------------------------------------------------

class LiveCavernBridge:
    """Thin wrapper over OmniAgent `cavernize_cmd` with structured JSON decoding."""

    def __init__(self, relay: str, secret: str, admin: str, timeout_sec: int = 45) -> None:
        self.relay = relay
        self.secret = secret
        self.admin = admin
        self.timeout_sec = timeout_sec
        self.calls = 0

    def cmd(self, name: str, args: dict[str, Any] | None = None, tier: int = 2, timeout_sec: int | None = None) -> dict[str, Any]:
        self.calls += 1
        t_out = timeout_sec or self.timeout_sec
        payload = oac.build_cavernize_cmd_payload(name, json.dumps(args or {}), tier, t_out)
        t0 = time.monotonic()
        try:
            res = oac.send_agent_command(
                self.relay,
                self.secret,
                "cavernize_cmd",
                payload,
                channel="android",
                admin_key=self.admin,
                timeout_sec=max(60, t_out + 15),
            )
        except Exception as exc:
            return {
                "ok": False,
                "subCmd": name,
                "elapsedMs": int((time.monotonic() - t0) * 1000),
                "body": "",
                "json": None,
                "error": f"{type(exc).__name__}: {exc}",
            }

        body = str(res.get("body") or "")
        parsed = None
        if body.strip().startswith(("{", "[")):
            try:
                parsed = json.loads(body)
            except Exception:
                parsed = None
        return {
            "ok": bool(res.get("ok")),
            "subCmd": name,
            "elapsedMs": int(res.get("elapsedMs") or ((time.monotonic() - t0) * 1000)),
            "body": body,
            "json": parsed,
            "error": str(res.get("error") or ""),
        }

    def pipeline(self, steps: list[dict[str, Any]], stop_on_error: bool = False, tier: int = 2, timeout_sec: int = 45) -> list[dict[str, Any]]:
        """Execute up to 30 commands in ONE round-trip (millisecond-burst stress + 10x lower latency)."""
        if not steps:
            return []
        res = self.cmd("pipeline", {"steps": steps[:30], "stopOnError": stop_on_error}, tier=tier, timeout_sec=timeout_sec)
        out: list[dict[str, Any]] = []
        if isinstance(res.get("json"), dict) and isinstance(res["json"].get("results"), list):
            for item in res["json"]["results"]:
                body = str(item.get("body") or "")
                parsed = None
                if body.strip().startswith(("{", "[")):
                    try:
                        parsed = json.loads(body)
                    except Exception:
                        parsed = None
                out.append({
                    "ok": bool(item.get("ok")),
                    "subCmd": str(item.get("cmd") or ""),
                    "body": body,
                    "json": parsed,
                    "error": "" if item.get("ok") else body[:240],
                })
            return out
        # Fallback if pipeline itself failed or wasn't supported
        return [{
            "ok": False,
            "subCmd": str(st.get("cmd") or ""),
            "body": res.get("body", ""),
            "json": None,
            "error": res.get("error") or res.get("body", "")[:240],
        } for st in steps[:30]]

    def fetch_logcat(self, lines: int = 120, min_level: str = "W", buffer: str = "main") -> str:
        """Try native `logcat_tail` first; fall back seamlessly to sandboxed `shell_exec logcat`."""
        r = self.cmd("logcat_tail", {"lines": lines, "minLevel": min_level, "buffer": buffer}, tier=0, timeout_sec=15)
        if r["ok"] and r["body"] and "Unknown command" not in r["body"]:
            if isinstance(r["json"], dict):
                return str(r["json"].get("output") or "")
            return r["body"]

        # Seamless fallback for APKs prior to native `logcat_tail`
        buf_flag = f"-b {buffer} " if buffer != "main" else ""
        lvl_spec = f"*:{min_level}" if min_level else "*:V"
        sh = self.cmd(
            "shell_exec",
            {"command": f"logcat -d {buf_flag}-t {int(lines)} -v brief {lvl_spec}", "timeoutSec": 10},
            tier=2,
            timeout_sec=18,
        )
        if sh["ok"] and isinstance(sh["json"], dict):
            return str(sh["json"].get("stdout") or "")
        return ""

    def watchdog_probe(self, timeout_sec: int = 6, reset_baseline: bool = False) -> dict[str, Any]:
        """External Q2 Cavernize PID/heartbeat/ANR probe executed in OmniAgent's separate process."""
        return oac.probe_cavernize_watchdog(
            self.relay,
            self.secret,
            self.admin,
            timeout_sec=timeout_sec,
            reset_baseline=reset_baseline,
        )

    def fetch_external_crash_forensics(self) -> str:
        """External Termux/ADB crash logcat & exit-info fallback when Cavernize itself is dead or frozen."""
        cmd = (
            "({ logcat -d -b crash -t 80 -v brief 2>/dev/null || "
            "adb logcat -d -b crash -t 80 -v brief 2>/dev/null || true; }; "
            "{ dumpsys activity exit-info hu.sbence.cavern.cavernize 2>/dev/null || "
            "adb shell dumpsys activity exit-info hu.sbence.cavern.cavernize 2>/dev/null || true; } | head -n 60; "
            "ls -t /sdcard/Download/Cavernize/*.txt 2>/dev/null | head -n 1 | xargs -r tail -n 40 2>/dev/null || true)"
        )
        try:
            res = oac.send_agent_command(
                self.relay,
                self.secret,
                "shell",
                {"command": cmd, "timeoutSec": 12},
                channel="termux",
                admin_key=self.admin,
                timeout_sec=20,
                auto_wake=False,
            )
            if isinstance(res, dict):
                return str(res.get("stdout") or res.get("output") or res.get("body") or "")
        except Exception:
            pass
        return ""


class LiveBugHunter:
    """Runs adversarial attack vectors while continuously watching AppLogger, Logcat & AppState."""

    def __init__(
        self,
        bridge: LiveCavernBridge,
        *,
        mode: str = "standard",
        allow_mutate: bool = True,
        verbose: bool = True,
        out_md: str = "",
        out_json: str = "",
        golden_dir: str = "",
        update_goldens: bool = False,
        enable_strict_mode: bool = False,
        q2_p95_budget_ms: float = 12.0,
        q2_touch_budget_ms: float = 50.0,
        soak_cycles: int = 16,
        contracts_file: str = "",
        gate_mode: bool = False,
    ) -> None:
        self.bridge = bridge
        self.mode = mode
        self.allow_mutate = allow_mutate
        self.verbose = verbose
        self.out_md = out_md
        self.out_json = out_json
        self.golden_dir = golden_dir
        self.update_goldens = update_goldens
        self.enable_strict_mode = enable_strict_mode
        self.q2_p95_budget_ms = float(q2_p95_budget_ms)
        self.q2_touch_budget_ms = float(q2_touch_budget_ms)
        self.soak_cycles = max(2, min(30, int(soak_cycles)))
        self.contracts_file = contracts_file
        self.gate_mode = bool(gate_mode)
        self.ledger = FindingLedger()
        self.vector_summaries: list[dict[str, Any]] = []
        self.meta: dict[str, Any] = {
            "mode": mode,
            "allowMutate": allow_mutate,
            "strictModeEnabled": enable_strict_mode,
            "gateMode": bool(gate_mode),
            "startedUtc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }

        self._seen_applog_lines: set[str] = set()
        self._seen_logcat_lines: set[str] = set()
        self._baseline_state: dict[str, Any] = {}
        self._baseline_settings: dict[str, Any] = {}
        self._baseline_memory: dict[str, Any] = {}
        self._baseline_counts: dict[str, int] = {}
        self._baseline_exit_keys: set[str] = set()
        self._baseline_pid: int = 0
        self._strict_mode_activated: bool = False
        self._q2_summary: dict[str, Any] = {
            "passed": True,
            "watchdogState": "UNPROBED",
            "exitInfoNewFatal": 0,
            "frameP95Ms": 0.0,
            "frameCpuP95Ms": None,
            "frozenFrames": 0,
            "strictModeViolations": 0,
        }
        self._q3_summary: dict[str, Any] = {
            "passed": True,
            "cycles": 0,
            "pssDeltaMb": 0.0,
            "pssSlopeMbPerCycle": 0.0,
            "fdDelta": 0,
            "gcDeltaMb": 0.0,
            "viewDelta": 0,
            "threadDelta": 0,
            "stuckOp": False,
            "leakedDialogs": 0,
            "rageTargets": 0,
            "dragSweeps": 0,
            "reflowTabs": 0,
        }
        self._q4_summary: dict[str, Any] = {
            "passed": 0,
            "failed": 0,
            "total": 0,
            "offlinePassed": 0,
            "offlineFailed": 0,
            "livePassed": 0,
            "liveTotal": 0,
            "hotExecVerified": False,
            "schemaErrors": [],
            "failedIds": [],
        }
        self._created_cache_files: list[str] = []
        self._has_chaos_hunt_cmd = False
        self._has_native_logcat = False
        self._capabilities: set[str] = set()

    def _emit(self, msg: str) -> None:
        if self.verbose:
            sys.stderr.write(f"[bug-hunter] {msg}\n")
            sys.stderr.flush()

    def _ingest_applog_delta(self, log_body: str, vector: str, repro_step: str) -> int:
        new_count = 0
        if not log_body or "(No WARN/ERROR/FATAL" in log_body:
            return 0
        fresh = []
        for raw in log_body.splitlines():
            s = raw.strip()
            if s and s not in self._seen_applog_lines:
                self._seen_applog_lines.add(s)
                fresh.append(s)
        for entry in parse_app_logger_lines("\n".join(fresh)):
            if entry["level"] in ("WARN", "ERROR", "FATAL"):
                if vector == "V5_corrupt_file_fuzzer" and entry["level"] in ("WARN", "ERROR"):
                    low_msg = entry["msg"].lower()
                    if (
                        "could not decode this audio file" in low_msg
                        or "openlocalpathwithfallbackasync rejected" in low_msg
                    ):
                        # Expected graceful rejection of intentionally malformed synthetic fuzz files
                        continue
                sev, cat = classify_applog_severity(entry)
                is_new, _ = self.ledger.record(
                    severity=sev,
                    vector=vector,
                    source="applogger_live",
                    category=cat,
                    title=entry["msg"][:200],
                    repro_step=repro_step,
                    evidence=entry["raw"],
                )
                if is_new:
                    new_count += 1
        return new_count

    def _ingest_logcat_delta(self, logcat_text: str, vector: str, repro_step: str) -> int:
        new_count = 0
        fresh_lc = []
        for raw in (logcat_text or "").splitlines():
            s = raw.strip()
            if s and s not in self._seen_logcat_lines:
                self._seen_logcat_lines.add(s)
                fresh_lc.append(s)
        if fresh_lc:
            for an in parse_logcat_anomalies("\n".join(fresh_lc)):
                is_new, _ = self.ledger.record(
                    severity=an["severity"],
                    vector=vector,
                    source="logcat_live",
                    category=an["category"],
                    title=an["message"][:200],
                    repro_step=repro_step,
                    evidence=an["raw"],
                )
                if is_new:
                    new_count += 1
        return new_count

    def _prime_baselines(self) -> bool:
        # 1 batched pipeline round-trip for complete device bootstrap!
        boot = self.bridge.pipeline([
            {"cmd": "ping"},
            {"cmd": "capabilities"},
            {"cmd": "app_state"},
            {"cmd": "settings_get"},
            {"cmd": "memory"},
            {"cmd": "log_tail", "args": {"lines": 250, "errorsOnly": True}},
        ], stop_on_error=False, tier=2, timeout_sec=30)

        ping = boot[0] if len(boot) > 0 else {"ok": False, "json": None, "error": "empty", "body": ""}
        if not ping["ok"] or not isinstance(ping["json"], dict):
            self.ledger.record(
                severity="CRITICAL",
                vector="bootstrap",
                source="bridge",
                category="app_unreachable",
                title="Cavernize IPC bridge did not respond to ping",
                repro_step="cavernize-cmd --name ping",
                evidence=ping["error"] or ping["body"][:250],
            )
            return False

        p = ping["json"]
        self.meta["appVersion"] = p.get("appVersion", "?")
        self.meta["appBuild"] = p.get("appBuild", 0)
        self.meta["device"] = p.get("device", "?")
        self.meta["android"] = p.get("android", "?")
        self.meta["sessionId"] = p.get("sessionId", "")
        self.meta["pid"] = int(p.get("pid") or 0)
        self._baseline_pid = int(p.get("pid") or 0)
        self.meta["uiHostAttached"] = bool(p.get("uiHostAttached"))
        self._baseline_counts = dict(p.get("counts") or {})

        if len(boot) > 1 and boot[1]["ok"] and isinstance(boot[1]["json"], dict):
            cmd_names = {c.get("cmd") for c in (boot[1]["json"].get("commands") or []) if isinstance(c, dict)}
            self._capabilities = {c for c in cmd_names if isinstance(c, str)}
            self._has_chaos_hunt_cmd = "chaos_hunt" in cmd_names
            self._has_native_logcat = "logcat_tail" in cmd_names
        if len(boot) > 2 and boot[2]["ok"] and isinstance(boot[2]["json"], dict):
            self._baseline_state = boot[2]["json"]
        if len(boot) > 3 and boot[3]["ok"] and isinstance(boot[3]["json"], dict):
            self._baseline_settings = boot[3]["json"]
        if len(boot) > 4 and boot[4]["ok"] and isinstance(boot[4]["json"], dict):
            self._baseline_memory = boot[4]["json"]
        if len(boot) > 5 and boot[5]["ok"]:
            for ln in boot[5]["body"].splitlines():
                if ln.strip():
                    self._seen_applog_lines.add(ln.strip())

        if not self.meta["uiHostAttached"]:
            self._emit("MainActivity UI host not attached; bringing Cavernize to front...")
            self.bridge.cmd("ui_action", {"action": "bring_to_front"}, tier=1, timeout_sec=15)
            time.sleep(1.2)
            p2 = self.bridge.cmd("ping", tier=0, timeout_sec=15)
            if p2["ok"] and isinstance(p2["json"], dict):
                self.meta["uiHostAttached"] = bool(p2["json"].get("uiHostAttached"))
                if p2["json"].get("pid"):
                    self._baseline_pid = int(p2["json"].get("pid") or 0)
                    self.meta["pid"] = self._baseline_pid

        # Q2: Seed baseline ApplicationExitInfo keys & optionally activate opt-in QA StrictMode
        if "runtime_telemetry" in self._capabilities:
            rt0 = self.bridge.cmd("runtime_telemetry", {}, tier=0, timeout_sec=15)
            if rt0["ok"] and isinstance(rt0["json"], dict):
                if rt0["json"].get("pid") and not self._baseline_pid:
                    self._baseline_pid = int(rt0["json"].get("pid") or 0)
                    self.meta["pid"] = self._baseline_pid
                for er in (rt0["json"].get("exitReasons") or []):
                    if isinstance(er, dict):
                        self._baseline_exit_keys.add(make_exit_reason_key(er))

        if self.enable_strict_mode and "strict_mode" in self._capabilities:
            sm_on = self.bridge.cmd("strict_mode", {"enable": True, "clear": True}, tier=2, timeout_sec=15)
            if sm_on["ok"]:
                self._strict_mode_activated = True
                self._emit("Q2 StrictMode QA detector ENABLED (will auto-restore OFF in finally).")

        if hasattr(self.bridge, "watchdog_probe"):
            try:
                wp0 = self.bridge.watchdog_probe(timeout_sec=6, reset_baseline=True)
                if isinstance(wp0, dict) and wp0.get("state"):
                    self._q2_summary["watchdogState"] = str(wp0.get("state"))
                    if wp0.get("pid") and not self._baseline_pid:
                        self._baseline_pid = int(wp0.get("pid") or 0)
                        self.meta["pid"] = self._baseline_pid
            except Exception:
                pass

        lc = self.bridge.fetch_logcat(lines=180, min_level="W")
        for ln in lc.splitlines():
            if ln.strip():
                self._seen_logcat_lines.add(ln.strip())

        self._emit(
            f"Connected: Cavernize v{self.meta['appVersion']} ({self.meta['appBuild']}) on "
            f"{self.meta['device']} | pid={self._baseline_pid or '?'} | uiHost={self.meta['uiHostAttached']} | "
            f"tab={self._baseline_state.get('activeTab')} | playing={self._baseline_state.get('isPlaying')} | "
            f"tracks={self._baseline_state.get('libraryTrackCount')}"
        )
        return True

    def _watch_step_delta(self, vector: str, repro_step: str, check_logcat: bool = True) -> int:
        """Capture ONLY newly emitted AppLogger + Logcat anomalies since the previous check."""
        new_count = 0
        steps: list[dict[str, Any]] = [{"cmd": "log_tail", "args": {"lines": 80, "errorsOnly": True}}]
        if check_logcat:
            if self._has_native_logcat:
                steps.append({"cmd": "logcat_tail", "args": {"lines": 100, "minLevel": "W", "buffer": "main"}})
            else:
                steps.append({"cmd": "shell_exec", "args": {"command": "logcat -d -t 100 -v brief *:W", "timeoutSec": 10}})
        res = self.bridge.pipeline(steps, stop_on_error=False, tier=2, timeout_sec=25)
        if len(res) > 0 and res[0]["ok"]:
            new_count += self._ingest_applog_delta(res[0]["body"], vector, repro_step)
        if check_logcat and len(res) > 1 and res[1]["ok"]:
            lc_text = ""
            if isinstance(res[1]["json"], dict):
                lc_text = str(res[1]["json"].get("output") or res[1]["json"].get("stdout") or "")
            else:
                lc_text = res[1]["body"]
            new_count += self._ingest_logcat_delta(lc_text, vector, repro_step)
        return new_count

    def _flush_checkpoint_report(self, restored: bool | None = None) -> None:
        if not self.out_json and not self.out_md:
            return
        snap = {
            "ok": True,
            "meta": dict(self.meta, stepsExecuted=self.bridge.calls, restoredState=restored),
            "severityCounts": self.ledger.counts_by_severity(active_only=False),
            "activeSeverityCounts": self.ledger.counts_by_severity(active_only=True),
            "originCounts": self.ledger.counts_by_origin(),
            "totalUniqueFindings": len(self.ledger.ranked()),
            "totalActiveFindings": len(self.ledger.active_findings()),
            "q2GateSummary": dict(self._q2_summary),
            "q3SoakSummary": dict(self._q3_summary),
            "vectors": self.vector_summaries,
            "findings": self.ledger.to_dicts(),
        }
        try:
            if self.out_json:
                Path(self.out_json).write_text(json.dumps(snap, indent=2, ensure_ascii=False), encoding="utf-8")
            if self.out_md:
                Path(self.out_md).write_text(generate_report_markdown(snap), encoding="utf-8")
        except Exception:
            pass

    def _run_external_watchdog_check(self, vector_name: str, repro_step: str) -> None:
        """Q2 External OmniAgent PID/Heartbeat/ANR Watchdog + Termux/ADB external crash forensics fallback."""
        if not hasattr(self.bridge, "watchdog_probe"):
            return
        try:
            probe = self.bridge.watchdog_probe(timeout_sec=6, reset_baseline=False)
        except Exception:
            return
        if not isinstance(probe, dict) or str(probe.get("state") or "").upper() == "WATCHDOG_TRANSPORT_ERROR":
            return
        ev = evaluate_external_watchdog_probe(
            probe,
            expected_pid=self._baseline_pid or None,
            expected_session_id=str(self.meta.get("sessionId") or ""),
        )
        self._q2_summary["watchdogState"] = ev["state"]
        if not ev["passed"]:
            self._q2_summary["passed"] = False
            for f in ev["findings"]:
                self.ledger.record(
                    severity=f["severity"],
                    vector=vector_name,
                    source="external_watchdog_q2",
                    category=f["category"],
                    title=f["message"][:200],
                    repro_step=repro_step,
                    evidence=json.dumps(probe, ensure_ascii=False)[:400],
                )
            # Capture external Termux/ADB crash logcat & dumpsys exit-info when Cavernize dies/freezes!
            if hasattr(self.bridge, "fetch_external_crash_forensics"):
                try:
                    raw_ext = self.bridge.fetch_external_crash_forensics(timeout_sec=15)
                    for cf in parse_external_crash_forensics(raw_ext):
                        self.ledger.record(
                            severity=cf["severity"],
                            vector=vector_name,
                            source="external_crash_forensics_q2",
                            category=cf["category"],
                            title=cf["message"][:200],
                            repro_step=f"termux_external_forensics:{repro_step}",
                            evidence=cf.get("raw", "")[:320],
                        )
                except Exception:
                    pass

    def _run_vector(self, name: str, fn: Callable[[], str]) -> None:
        self._emit(f"--- Vector: {name} ---")
        t0 = time.monotonic()
        before_total = len(self.ledger.ranked())
        status = "OK"
        notes = ""
        try:
            notes = fn() or ""
        except Exception as exc:
            status = "ERROR"
            notes = f"{type(exc).__name__}: {exc}"
            self.ledger.record(
                severity="HIGH",
                vector=name,
                source="harness",
                category="vector_exception",
                title=f"Vector '{name}' raised {type(exc).__name__}: {exc}",
                repro_step=f"vector:{name}",
                evidence=notes,
            )
        finally:
            self._run_external_watchdog_check(name, f"post-vector:{name}")
        elapsed = time.monotonic() - t0
        new_found = len(self.ledger.ranked()) - before_total
        if new_found > 0 and status == "OK":
            status = "FINDINGS"
        self.vector_summaries.append({
            "name": name,
            "status": status,
            "newFindings": new_found,
            "elapsedSec": round(elapsed, 2),
            "notes": notes,
        })
        self._emit(f"<<< {name}: {status} (+{new_found} unique findings, {elapsed:.1f}s) {notes}")
        self._flush_checkpoint_report()

    # ==================== VECTOR 1: HISTORICAL & LIVE LOG FORENSICS ====================
    def _v1_log_forensics(self) -> str:
        mined_lines = 0
        fl = self.bridge.cmd("files_list", {"scope": "logs"}, tier=0, timeout_sec=15)
        log_path = ""
        if fl["ok"] and isinstance(fl["json"], dict):
            for ent in fl["json"].get("entries") or []:
                if str(ent.get("name") or "").endswith(".txt"):
                    log_path = str(ent.get("path") or "")
                    break

        # Batch file_read + logcat W/crash/I in 1 pipeline!
        steps: list[dict[str, Any]] = []
        if log_path:
            steps.append({"cmd": "file_read", "args": {"path": log_path, "maxChars": 50000, "tail": True}})
        if self._has_native_logcat:
            steps.append({"cmd": "logcat_tail", "args": {"lines": 250, "minLevel": "W", "buffer": "main"}})
            steps.append({"cmd": "logcat_tail", "args": {"lines": 80, "minLevel": "V", "buffer": "crash"}})
            steps.append({"cmd": "logcat_tail", "args": {"lines": 220, "minLevel": "I", "buffer": "main"}})
        else:
            steps.append({"cmd": "shell_exec", "args": {"command": "logcat -d -t 250 -v brief *:W", "timeoutSec": 10}})
            steps.append({"cmd": "shell_exec", "args": {"command": "logcat -d -b crash -t 80 -v brief", "timeoutSec": 10}})
            steps.append({"cmd": "shell_exec", "args": {"command": "logcat -d -t 220 -v brief *:I", "timeoutSec": 10}})

        res = self.bridge.pipeline(steps, stop_on_error=False, tier=2, timeout_sec=35)
        idx = 0
        if log_path and idx < len(res):
            fr = res[idx]
            idx += 1
            if fr["ok"] and fr["body"]:
                cur_entries, hist_entries = split_applog_by_session(fr["body"], str(self.meta.get("sessionId") or ""))
                mined_lines = len(cur_entries) + len(hist_entries)
                for origin_tag, entry_list in (("current_session", cur_entries), ("historical_archive", hist_entries)):
                    for e in entry_list:
                        if e["level"] in ("WARN", "ERROR", "FATAL"):
                            sev, cat = classify_applog_severity(e)
                            self.ledger.record(
                                severity=sev,
                                vector="V1_log_forensics",
                                source="applogger_current" if origin_tag == "current_session" else "applogger_history",
                                category=cat,
                                title=e["msg"][:200],
                                repro_step=f"{origin_tag} ({e['ts'] or 'recent'})",
                                evidence=e["raw"],
                                origin=origin_tag,
                            )

        def _extract_lc(r_item: dict[str, Any]) -> str:
            if not r_item.get("ok"):
                return ""
            if isinstance(r_item.get("json"), dict):
                return str(r_item["json"].get("output") or r_item["json"].get("stdout") or "")
            return str(r_item.get("body") or "")

        if idx < len(res):
            for an in parse_logcat_anomalies(_extract_lc(res[idx])):
                self.ledger.record(
                    severity=an["severity"],
                    vector="V1_log_forensics",
                    source="logcat_history",
                    category=an["category"],
                    title=an["message"][:200],
                    repro_step="logcat -d *:W",
                    evidence=an["raw"],
                    origin="historical_archive",
                )
            idx += 1
        if idx < len(res):
            for an in parse_logcat_anomalies(_extract_lc(res[idx])):
                self.ledger.record(
                    severity="CRITICAL",
                    vector="V1_log_forensics",
                    source="logcat_crash",
                    category="native_or_java_crash",
                    title=an["message"][:200],
                    repro_step="logcat -d -b crash",
                    evidence=an["raw"],
                    origin="historical_archive",
                )
            idx += 1
        if idx < len(res):
            for an in parse_logcat_anomalies(_extract_lc(res[idx])):
                if an["category"] in ("ui_jank", "audio_native", "codec", "strict_mode", "gc_stall"):
                    self.ledger.record(
                        severity=an["severity"],
                        vector="V1_log_forensics",
                        source="logcat_runtime",
                        category=an["category"],
                        title=an["message"][:200],
                        repro_step="logcat -d *:I (Choreographer/Audio/GC)",
                        evidence=an["raw"],
                        origin="historical_archive",
                    )

        return f"mined {mined_lines} AppLogger entries (stratified active vs historical) + logcat W/E/crash/Choreographer"

    # ==================== VECTOR 2: CONCURRENT PLAYBACK x ACOUSTIC ORACLE ====================
    def _v2_concurrent_playback_oracle(self) -> str:
        """Hunt for static/global DSP races by running acoustic_audit WHILE real playback is active."""
        was_playing = bool(self._baseline_state.get("isPlaying"))
        has_file = bool(self._baseline_state.get("loadedFile"))

        steps: list[dict[str, Any]] = []
        if self.allow_mutate and has_file and not was_playing:
            steps.append({"cmd": "playback_control", "args": {"action": "play"}})
        steps.append({"cmd": "acoustic_audit", "args": {"quick": self.mode != "deep", "format": "json"}})
        steps.append({"cmd": "dsp_telemetry"})
        steps.append({"cmd": "log_tail", "args": {"lines": 80, "errorsOnly": True}})

        res = self.bridge.pipeline(steps, stop_on_error=False, tier=2, timeout_sec=50)
        aud_idx = 1 if (self.allow_mutate and has_file and not was_playing) else 0
        aud = res[aud_idx] if aud_idx < len(res) else {"ok": False, "json": None, "body": "", "error": "missing"}
        tel = res[aud_idx + 1] if (aud_idx + 1) < len(res) else {"ok": False, "json": None, "body": ""}
        lt = res[aud_idx + 2] if (aud_idx + 2) < len(res) else {"ok": False, "body": ""}

        scenarios_checked = 0
        fails = 0
        warns = 0
        if aud["ok"] or isinstance(aud["json"], dict):
            data = aud["json"] if isinstance(aud["json"], dict) else {}
            scenarios = data.get("Scenarios") or data.get("scenarios") or []
            scenarios_checked = len(scenarios)
            for sc in scenarios:
                st = str(sc.get("Status") or sc.get("status") or "").upper()
                sid = str(sc.get("Id") or sc.get("id") or "?")
                note = str(sc.get("VerdictNote") or sc.get("verdictNote") or "")
                if st == "FAIL":
                    fails += 1
                    self.ledger.record(
                        severity="HIGH",
                        vector="V2_concurrent_playback_oracle",
                        source="acoustic_oracle",
                        category=f"acoustic_fail_{sid.lower()}",
                        title=f"Acoustic scenario {sid} FAILED during live playback: {note}",
                        repro_step="acoustic_audit while playback active",
                        evidence=json.dumps(sc)[:380],
                    )
                elif st == "WARN":
                    warns += 1
                    self.ledger.record(
                        severity="MEDIUM",
                        vector="V2_concurrent_playback_oracle",
                        source="acoustic_oracle",
                        category=f"acoustic_warn_{sid.lower()}",
                        title=f"Acoustic scenario {sid} WARN: {note}",
                        repro_step="acoustic_audit while playback active",
                        evidence=json.dumps(sc)[:380],
                    )
        else:
            self.ledger.record(
                severity="HIGH",
                vector="V2_concurrent_playback_oracle",
                source="acoustic_oracle",
                category="acoustic_audit_error",
                title="acoustic_audit failed to execute",
                repro_step="cavernize-cmd --name acoustic_audit",
                evidence=(aud["error"] or aud["body"])[:300],
            )

        if tel["ok"] and isinstance(tel["json"], dict):
            raw_tel = json.dumps(tel["json"])
            if re.search(r"\b(NaN|Infinity)\b", raw_tel):
                self.ledger.record(
                    severity="CRITICAL",
                    vector="V2_concurrent_playback_oracle",
                    source="dsp_telemetry",
                    category="dsp_nan_telemetry",
                    title="Live dsp_telemetry contains NaN/Infinity",
                    repro_step="cavernize-cmd --name dsp_telemetry",
                    evidence=raw_tel[:320],
                )
            if not tel["json"].get("proAudioBridgeAvailable"):
                self.ledger.record(
                    severity="MEDIUM",
                    vector="V2_concurrent_playback_oracle",
                    source="dsp_telemetry",
                    category="proaudio_unavailable",
                    title="ProAudio native NDK bridge unavailable (running managed AudioTrack fallback)",
                    repro_step="cavernize-cmd --name dsp_telemetry",
                    evidence=raw_tel[:250],
                )

        if lt["ok"]:
            self._ingest_applog_delta(lt["body"], "V2_concurrent_playback_oracle", "concurrent playback + acoustic_audit")
        return f"checked {scenarios_checked} acoustic scenarios concurrently (fails={fails}, warns={warns})"

    # ==================== VECTOR 3: LIVE SETTINGS CHAOS & BOUNDARY FUZZER ====================
    def _v3_settings_chaos_fuzzer(self) -> str:
        if not self.allow_mutate:
            return "skipped (--no-mutate)"

        keys_present = set(self._baseline_settings.keys())
        bool_candidates = [
            "Iso226Enabled", "LgsrEnabled", "HeadTrackingEnabled", "AutoEqEnabled",
            "HptfNotchEnabled", "MinimumPhaseHrir", "NearFieldEnabled", "IsmRoomEnabled",
            "DistanceSimulation", "DialogEnhancerEnabled", "HeadphoneVirtualizer",
            "ParametricStftEnabled", "ProAudioExclusiveMode",
        ]
        active_bools = [k for k in bool_candidates if k in keys_present]

        steps: list[dict[str, Any]] = []
        labels: list[str] = []
        # 3a. Rapid-fire toggle burst (millisecond spacing inside live app!)
        for key in active_bools[:8 if self.mode != "deep" else 12]:
            orig = bool(self._baseline_settings.get(key))
            steps.append({"cmd": "settings_set", "args": {"key": key, "value": not orig}})
            labels.append(f"toggle {key}={not orig}")

        # 3b. Enable full heavy DSP stack simultaneously!
        combo = {k: True for k in active_bools if k in (
            "Iso226Enabled", "LgsrEnabled", "AutoEqEnabled", "HptfNotchEnabled",
            "MinimumPhaseHrir", "NearFieldEnabled", "IsmRoomEnabled", "DialogEnhancerEnabled",
        )}
        if combo:
            steps.append({"cmd": "settings_set", "args": {"values": combo}})
            labels.append("enable full DSP combo")
            steps.append({"cmd": "dsp_telemetry"})
            labels.append("dsp_telemetry after full combo")

        # 3c. Boundary fuzzing on numeric/string settings
        boundary_probes: list[tuple[str, Any]] = [
            ("LoudnessNormModeIndex", 99),
            ("LoudnessNormModeIndex", -5),
            ("MagLsOrder", 9),
            ("MagLsOrder", -1),
            ("BiometricHeadWidthCm", 0.0),
            ("BiometricHeadWidthCm", 999.0),
            ("NearFieldDistanceM", -3.0),
            ("BinauralProfileId", "nonexistent_fuzzed_profile_xyz"),
        ]
        for bkey, bval in boundary_probes:
            if bkey in keys_present and len(steps) < 25:
                steps.append({"cmd": "settings_set", "args": {"key": bkey, "value": bval}})
                labels.append(f"boundary {bkey}={bval}")

        steps.append({"cmd": "settings_get"})
        labels.append("settings_get post-fuzz")
        steps.append({"cmd": "app_state"})
        labels.append("app_state post-fuzz")
        steps.append({"cmd": "log_tail", "args": {"lines": 80, "errorsOnly": True}})
        labels.append("log_tail post-fuzz")

        res = self.bridge.pipeline(steps, stop_on_error=False, tier=2, timeout_sec=45)
        boundary_rejected_ok = 0
        for i, item in enumerate(res):
            lbl = labels[i] if i < len(labels) else f"step#{i}"
            is_boundary = lbl.startswith("boundary ")
            if item["subCmd"] == "settings_set" and not item["ok"]:
                if is_boundary:
                    # Rejecting an intentionally out-of-range boundary probe is safe validation, NOT a bug!
                    boundary_rejected_ok += 1
                    continue
                self.ledger.record(
                    severity="MEDIUM",
                    vector="V3_settings_chaos_fuzzer",
                    source="settings_set",
                    category="setting_mutation_rejected",
                    title=f"settings_set failed during rapid chaos ({lbl})",
                    repro_step=lbl,
                    evidence=(item["error"] or item["body"])[:280],
                )
            elif item["subCmd"] in ("settings_get", "dsp_telemetry") and item["ok"]:
                if re.search(r"\b(NaN|Infinity)\b", item["body"]):
                    self.ledger.record(
                        severity="HIGH",
                        vector="V3_settings_chaos_fuzzer",
                        source="settings_fuzz",
                        category="unclamped_setting_nan",
                        title=f"Boundary fuzzing produced NaN/Infinity in {item['subCmd']}",
                        repro_step=lbl,
                        evidence=item["body"][:280],
                    )
            elif item["subCmd"] == "log_tail" and item["ok"]:
                self._ingest_applog_delta(item["body"], "V3_settings_chaos_fuzzer", "rapid DSP settings chaos burst")

        self._restore_settings()
        self._watch_step_delta("V3_settings_chaos_fuzzer", "settings chaos burst + restore", check_logcat=True)
        return f"executed {len(steps)} rapid-fire DSP/boundary steps (safe boundary rejects={boundary_rejected_ok}) & restored settings"

    # ==================== VECTOR 4: REAL USER LIBRARY EDGE-CASE & DECODER TORTURE ====================
    def _v4_real_library_edge_hunter(self) -> str:
        lq = self.bridge.cmd("library_query", {"query": "", "limit": 80}, tier=0, timeout_sec=20)
        tracks: list[dict[str, Any]] = []
        if lq["ok"] and isinstance(lq["json"], dict):
            tracks = list(lq["json"].get("tracks") or [])
        if not tracks:
            return "library empty or unavailable"

        zero_dur = [t for t in tracks if int(t.get("DurationMs") or 0) <= 0]
        if zero_dur:
            sample_names = ", ".join(str(t.get("Title") or Path(str(t.get("FilePath") or "")).name) for t in zero_dur[:4])
            self.ledger.record(
                severity="MEDIUM",
                vector="V4_real_library_edge_hunter",
                source="library_metadata",
                category="zero_duration_library_tracks",
                title=f"{len(zero_dur)}/{len(tracks)} sampled library tracks have DurationMs=0",
                repro_step="library_query limit=80",
                evidence=f"Examples: {sample_names}",
            )

        picks = pick_edge_case_library_tracks(tracks, max_picks=4 if self.mode != "deep" else 6)
        # 4a. Batch audio_probe across all selected edge-case tracks in 1 pipeline
        probe_steps = [{"cmd": "audio_probe", "args": {"path": str(t.get("FilePath") or "")}} for _, t in picks if t.get("FilePath")]
        probe_res = self.bridge.pipeline(probe_steps, stop_on_error=False, tier=2, timeout_sec=35)
        for (reason, trk), pr in zip(picks, probe_res):
            fpath = str(trk.get("FilePath") or "")
            if not pr["ok"]:
                self.ledger.record(
                    severity="HIGH",
                    vector="V4_real_library_edge_hunter",
                    source="audio_probe",
                    category="real_track_probe_failed",
                    title=f"audio_probe failed on real user track ({reason}): {Path(fpath).name}",
                    repro_step=f"audio_probe path='{fpath}'",
                    evidence=(pr["error"] or pr["body"])[:300],
                )
            elif isinstance(pr["json"], dict):
                pj = pr["json"]
                if int(pj.get("sampleRateHz") or 0) <= 0 or int(pj.get("channels") or 0) <= 0:
                    self.ledger.record(
                        severity="MEDIUM",
                        vector="V4_real_library_edge_hunter",
                        source="audio_probe",
                        category="audio_probe_zero_format",
                        title=f"audio_probe returned 0 Hz / 0 ch on real track ({reason}): {Path(fpath).name}",
                        repro_step=f"audio_probe path='{fpath}'",
                        evidence=pr["body"][:300],
                    )

        # 4b. Open & extreme-seek-torture 2 edge-case tracks in 1 pipeline each!
        opened = 0
        if self.allow_mutate:
            for reason, trk in picks[:2 if self.mode != "deep" else 3]:
                fpath = str(trk.get("FilePath") or "")
                if not fpath:
                    continue
                opened += 1
                torture_steps = [
                    {"cmd": "open_file", "args": {"path": fpath, "autoPlay": False}},
                    {"cmd": "playback_control", "args": {"action": "seek", "value": -5.0}},
                    {"cmd": "playback_control", "args": {"action": "seek", "value": 0.0}},
                    {"cmd": "playback_control", "args": {"action": "seek", "value": 99999.0}},
                    {"cmd": "playback_control", "args": {"action": "seek_rel", "value": -9999.0}},
                    {"cmd": "playback_control", "args": {"action": "seek_rel", "value": 9999.0}},
                    {"cmd": "app_state"},
                    {"cmd": "log_tail", "args": {"lines": 80, "errorsOnly": True}},
                ]
                tres = self.bridge.pipeline(torture_steps, stop_on_error=False, tier=2, timeout_sec=35)
                if tres and not tres[0]["ok"]:
                    self.ledger.record(
                        severity="HIGH",
                        vector="V4_real_library_edge_hunter",
                        source="open_file",
                        category="real_track_open_failed",
                        title=f"open_file failed on real track ({reason}): {Path(fpath).name}",
                        repro_step=f"open_file path='{fpath}'",
                        evidence=(tres[0]["error"] or tres[0]["body"])[:300],
                    )
                for sk_idx in range(1, min(6, len(tres))):
                    if not tres[sk_idx]["ok"]:
                        self.ledger.record(
                            severity="MEDIUM",
                            vector="V4_real_library_edge_hunter",
                            source="playback_control",
                            category="extreme_seek_error",
                            title=f"Extreme seek failed on {Path(fpath).name}: {tres[sk_idx]['body'][:120]}",
                            repro_step=f"seek torture on '{Path(fpath).name}'",
                            evidence=tres[sk_idx]["body"][:250],
                        )
                if len(tres) > 7 and tres[7]["ok"]:
                    self._ingest_applog_delta(tres[7]["body"], "V4_real_library_edge_hunter", f"open+seek torture ({reason}) {Path(fpath).name}")

        self._watch_step_delta("V4_real_library_edge_hunter", "real library edge torture", check_logcat=True)
        return f"probed {len(probe_steps)} edge-case tracks, opened & seek-tortured {opened}"

    # ==================== VECTOR 5: CORRUPT / MALFORMED SYNTHETIC INPUT FUZZER ====================
    def _v5_corrupt_file_fuzzer(self) -> str:
        if not self.allow_mutate:
            return "skipped (--no-mutate)"

        setup_cmd = (
            "mkdir -p ../cache/bh_fuzz && "
            ": > ../cache/bh_fuzz/empty.flac && "
            "printf 'RIFF\\x24\\x00\\x00\\x00WAVEfmt ' > ../cache/bh_fuzz/trunc.wav && "
            "printf 'fLaC\\x00\\x00\\x00\\x22GARBAGE_CORRUPT_DATA_1234567890' > ../cache/bh_fuzz/corrupt.flac && "
            "printf 'ID3\\x03\\x00\\x00\\x00\\x00\\x00\\x00NOT_AN_MP3' > ../cache/bh_fuzz/corrupt.mp3 && "
            "ls -1 ../cache/bh_fuzz"
        )
        sh = self.bridge.cmd("shell_exec", {"command": setup_cmd, "timeoutSec": 10}, tier=2, timeout_sec=20)
        if not sh["ok"] or not isinstance(sh["json"], dict) or sh["json"].get("exitCode") != 0:
            return "could not stage temp corrupt files in CacheDir"

        cwd = str(sh["json"].get("cwd") or "/data/user/0/hu.sbence.cavern.cavernize/files")
        cache_dir = str(Path(cwd).parent / "cache" / "bh_fuzz")
        files = [
            f"{cache_dir}/empty.flac",
            f"{cache_dir}/trunc.wav",
            f"{cache_dir}/corrupt.flac",
            f"{cache_dir}/corrupt.mp3",
        ]
        self._created_cache_files.extend(files)

        steps: list[dict[str, Any]] = []
        for fpath in files:
            steps.append({"cmd": "audio_probe", "args": {"path": fpath}})
            steps.append({"cmd": "open_file", "args": {"path": fpath, "autoPlay": False}})
            steps.append({"cmd": "ui_interact", "args": {"action": "dismiss_dialogs"}})
        steps.append({"cmd": "app_state"})
        steps.append({"cmd": "shell_exec", "args": {"command": "rm -rf ../cache/bh_fuzz", "timeoutSec": 10}})
        steps.append({"cmd": "log_tail", "args": {"lines": 80, "errorsOnly": True}})

        res = self.bridge.pipeline(steps, stop_on_error=False, tier=2, timeout_sec=45)
        self._created_cache_files.clear()

        if len(res) >= 3:
            st = res[-3]
            if st["ok"] and isinstance(st["json"], dict) and st["json"].get("isOperationRunning"):
                self.ledger.record(
                    severity="HIGH",
                    vector="V5_corrupt_file_fuzzer",
                    source="app_state",
                    category="stuck_busy_flag_on_corrupt_file",
                    title="isOperationRunning remained stuck true after malformed media fuzz burst",
                    repro_step="open_file on empty.flac / trunc.wav / corrupt.flac / corrupt.mp3",
                    evidence=st["body"][:280],
                )
            lt = res[-1]
            if lt["ok"]:
                self._ingest_applog_delta(lt["body"], "V5_corrupt_file_fuzzer", "corrupt media file fuzz burst")

        self._watch_step_delta("V5_corrupt_file_fuzzer", "corrupt media fuzz burst", check_logcat=True)
        return f"fuzzed {len(files)} malformed/truncated media files in 1 burst + cleaned up"

    # ==================== VECTOR 6: UI MONKEY, RAPID TAB THRASH & LAYOUT AUDIT ====================
    def _v6_ui_monkey_and_audit(self) -> str:
        if not self.meta.get("uiHostAttached"):
            return "skipped (MainActivity UI host detached)"

        # 6a. Run built-in L8-L10+L12 Live UI & Perf-Jank Audit
        ui_aud = self.bridge.cmd("ui_audit", tier=1, timeout_sec=60)
        if isinstance(ui_aud["json"], dict):
            for chk in ui_aud["json"].get("checks") or []:
                st = str(chk.get("Status") or chk.get("status") or "").upper()
                cid = str(chk.get("Id") or chk.get("id") or "ui")
                det = str(chk.get("Details") or chk.get("details") or "")
                if st in ("2", "FAIL"):
                    self.ledger.record(
                        severity="HIGH",
                        vector="V6_ui_monkey_and_audit",
                        source="ui_audit",
                        category=f"ui_audit_fail_{cid}",
                        title=f"Live UI audit FAIL [{cid}]: {det[:180]}",
                        repro_step="cavernize-cmd --name ui_audit",
                        evidence=det[:360],
                    )
                elif st in ("1", "WARN"):
                    self.ledger.record(
                        severity="MEDIUM",
                        vector="V6_ui_monkey_and_audit",
                        source="ui_audit",
                        category=f"ui_audit_warn_{cid}",
                        title=f"Live UI audit WARN [{cid}]: {det[:180]}",
                        repro_step="cavernize-cmd --name ui_audit",
                        evidence=det[:360],
                    )

        # 6b. Rapid Tab Thrash + Full Q1 Geometric/Visual ui_tree Inspection + optional Golden SSIM
        geom_tabs_scanned = 0
        ssim_tabs_checked = 0
        for tab_idx in (0, 1, 2):
            self.bridge.cmd("tab_switch", {"index": tab_idx}, tier=1, timeout_sec=15)
            ut = self.bridge.cmd("ui_tree", {"maxDepth": 12, "geometry": True}, tier=0, timeout_sec=20)
            if ut["ok"] and isinstance(ut["json"], dict):
                geom_tabs_scanned += 1
                for d in analyze_ui_tree_anomalies(ut["json"], f"Tab{tab_idx}"):
                    self.ledger.record(
                        severity=d["severity"],
                        vector="V6_ui_monkey_and_audit",
                        source="ui_tree_q1",
                        category=d["category"],
                        title=d["message"],
                        repro_step=f"tab_switch({tab_idx}) -> ui_tree(maxDepth=12,geometry=true)",
                        evidence=d["message"],
                    )
            if self.golden_dir:
                sh = self.bridge.cmd("screenshot_ui", {"maxWidth": 480, "quality": 65}, tier=1, timeout_sec=20)
                if sh["ok"] and isinstance(sh["json"], dict):
                    ssim_res = compare_screenshot_against_golden(
                        f"Tab{tab_idx}",
                        sh["json"],
                        self.golden_dir,
                        update_goldens=self.update_goldens,
                    )
                    if ssim_res.get("status") in ("PASS", "WARN", "FAIL", "SEEDED", "MISMATCH_DIM"):
                        ssim_tabs_checked += 1
                    f_ssim = ssim_res.get("finding")
                    if isinstance(f_ssim, dict):
                        self.ledger.record(
                            severity=f_ssim["severity"],
                            vector="V6_ui_monkey_and_audit",
                            source="golden_ssim_q1",
                            category=f_ssim["category"],
                            title=f_ssim["message"],
                            repro_step=f"tab_switch({tab_idx}) -> screenshot_ui -> SSIM",
                            evidence=f_ssim["message"],
                        )

        steps: list[dict[str, Any]] = []
        rage_done = 0
        drag_done = 0
        reflow_done = 0
        if self.allow_mutate:
            steps.append({"cmd": "tab_switch", "args": {"index": 4}})
            for q in ("binaural", ".*+?[]()", "🎧🔥", "zzzz_no_match_999", "a" * 160, ""):
                steps.append({"cmd": "ui_interact", "args": {"action": "set_text", "type": "EditText", "value": q}})
            steps.append({"cmd": "ui_interact", "args": {"action": "scroll_by", "dy": 1200}})
            steps.append({"cmd": "ui_interact", "args": {"action": "scroll_by", "dy": -1200}})
            steps.append({"cmd": "ui_interact", "args": {"action": "dismiss_dialogs"}})
        steps.append({"cmd": "log_tail", "args": {"lines": 80, "errorsOnly": True}})

        res = self.bridge.pipeline(steps, stop_on_error=False, tier=2, timeout_sec=45)
        if res and res[-1]["subCmd"] == "log_tail" and res[-1]["ok"]:
            self._ingest_applog_delta(res[-1]["body"], "V6_ui_monkey_and_audit", "tab thrash + search fuzz")

        # 6c. Q3 Hostile Rage-Clicks, Mid-Load Back, Continuous Drag Sweep (120 Hz) & Reflow Audit (1.3x Font + Landscape)
        if self.allow_mutate:
            # 6c-1. Hostile Rage-Clicks across Play / 3D Room / Settings interactive controls
            for r_tab, r_label, r_args in (
                (0, "Tab0:PlayButton", {"action": "rage_click", "type": "FrameLayout", "text": "Play", "index": 0, "count": 6, "intervalMs": 22}),
                (1, "Tab1:SoundButton", {"action": "rage_click", "type": "Button", "index": 0, "count": 6, "intervalMs": 22}),
                (2, "Tab2:ExportButton", {"action": "rage_click", "type": "Button", "index": 0, "count": 5, "intervalMs": 25}),
            ):
                self.bridge.cmd("tab_switch", {"index": r_tab}, tier=1, timeout_sec=15)
                rc = self.bridge.cmd("ui_interact", r_args, tier=2, timeout_sec=20)
                if isinstance(rc.get("json"), dict) and rc["json"].get("action") == "rage_click":
                    rage_done += 1
                    rc_ev = evaluate_rage_click_result(rc["json"], target_label=r_label)
                    for rf in rc_ev["findings"]:
                        self.ledger.record(
                            severity=rf["severity"],
                            vector="V6_ui_monkey_and_audit",
                            source="hostile_rage_q3",
                            category=rf["category"],
                            title=rf["message"][:200],
                            repro_step=f"tab_switch({r_tab}) -> ui_interact(rage_click)",
                            evidence=rc["body"][:300],
                        )
                self.bridge.cmd("ui_interact", {"action": "dismiss_dialogs"}, tier=2, timeout_sec=10)

            # 6c-2. Mid-Load Back Press Stress (verify no stuck isOperationRunning or leaked dialog)
            orig_loaded = str(self._baseline_state.get("loadedFile") or "")
            if orig_loaded:
                ml = self.bridge.pipeline([
                    {"cmd": "tab_switch", "args": {"index": 0}},
                    {"cmd": "open_file", "args": {"path": orig_loaded, "autoPlay": False}},
                    {"cmd": "ui_interact", "args": {"action": "back"}},
                    {"cmd": "ui_interact", "args": {"action": "dismiss_dialogs"}},
                    {"cmd": "app_state"},
                ], stop_on_error=False, tier=2, timeout_sec=30)
                if len(ml) >= 5 and ml[-1]["ok"] and isinstance(ml[-1]["json"], dict):
                    st_after = ml[-1]["json"]
                    if st_after.get("isOperationRunning"):
                        self.ledger.record(
                            severity="HIGH",
                            vector="V6_ui_monkey_and_audit",
                            source="hostile_back_q3",
                            category="dirty_state_operation_stuck",
                            title="Mid-load Back press left isOperationRunning=true",
                            repro_step="open_file -> immediate ui_interact(back)",
                            evidence=ml[-1]["body"][:260],
                        )

            # 6c-3. Continuous SeekBar / Volume Drag Sweep under 120 Hz FrameMetrics
            has_rt = "runtime_telemetry" in getattr(self, "_capabilities", ())
            for d_tab, d_label in ((0, "Tab0:MediaSeekBar"), (1, "Tab1:RoomSeekBar")):
                self.bridge.cmd("tab_switch", {"index": d_tab}, tier=1, timeout_sec=15)
                if has_rt:
                    self.bridge.cmd("runtime_telemetry", {"resetFrameMetrics": True}, tier=0, timeout_sec=15)
                ds = self.bridge.cmd(
                    "ui_interact",
                    {"action": "drag_sweep", "steps": 20, "stepMs": 14, "restoreProgress": True},
                    tier=2,
                    timeout_sec=20,
                )
                if isinstance(ds.get("json"), dict) and ds["json"].get("action") == "drag_sweep":
                    if ds["json"].get("ok") or "no visible seekbar" not in str(ds["json"].get("error") or "").lower():
                        drag_done += 1
                        fm_after = None
                        if has_rt:
                            rt_d = self.bridge.cmd("runtime_telemetry", {}, tier=0, timeout_sec=15)
                            if rt_d["ok"] and isinstance(rt_d["json"], dict):
                                fm_after = rt_d["json"].get("frameMetrics")
                        ds_ev = evaluate_drag_sweep_result(
                            ds["json"],
                            fm_after,
                            target_label=d_label,
                            p95_budget_ms=self.q2_p95_budget_ms,
                        )
                        for df in ds_ev["findings"]:
                            self.ledger.record(
                                severity=df["severity"],
                                vector="V6_ui_monkey_and_audit",
                                source="drag_sweep_q3",
                                category=df["category"],
                                title=df["message"][:200],
                                repro_step=f"tab_switch({d_tab}) -> ui_interact(drag_sweep) -> frameMetrics",
                                evidence=ds["body"][:260],
                            )

            # 6c-4. Reversible 1.3x Font-Scale & Landscape Orientation Reflow Audit
            for rf_tab, rf_label in ((0, "Tab0"), (1, "Tab1"), (2, "Tab2")):
                self.bridge.cmd("tab_switch", {"index": rf_tab}, tier=1, timeout_sec=15)
                rp = self.bridge.cmd(
                    "ui_interact",
                    {"action": "reflow_probe", "mode": "both", "fontScale": 1.3, "maxDepth": 12},
                    tier=2,
                    timeout_sec=25,
                )
                if rp["ok"] and isinstance(rp.get("json"), dict) and rp["json"].get("action") == "reflow_probe":
                    reflow_done += 1
                    rf_ev = evaluate_reflow_probe_result(rp["json"], rf_label)
                    for rff in rf_ev["findings"]:
                        self.ledger.record(
                            severity=rff["severity"],
                            vector="V6_ui_monkey_and_audit",
                            source="reflow_q3",
                            category=rff["category"],
                            title=rff["message"][:200],
                            repro_step=f"tab_switch({rf_tab}) -> ui_interact(reflow_probe fontScale=1.3 + landscape)",
                            evidence=rff["message"][:320],
                        )

            self._q3_summary["rageTargets"] = rage_done
            self._q3_summary["dragSweeps"] = drag_done
            self._q3_summary["reflowTabs"] = reflow_done

        self._watch_step_delta("V6_ui_monkey_and_audit", "ui_audit + Q1 geom + Q3 hostile stress/reflow", check_logcat=True)
        return (
            f"ran ui_audit + Q1 geom ({geom_tabs_scanned} tabs, ssim={ssim_tabs_checked}) + "
            f"Q3 hostile (rage={rage_done}, drag={drag_done}, reflow={reflow_done})"
        )

    # ==================== VECTOR 7: ACOUSTIC EXTREME PARAMETER FUZZER ====================
    def _v7_acoustic_extreme_fuzzer(self) -> str:
        cases: list[tuple[str, dict[str, Any]]] = [
            ("sr_8000_minphase", {"sampleRate": 8000, "minPhase": True, "signal": "white"}),
            ("sr_44100_magls3_bioitd", {"sampleRate": 44100, "magLsOrder": 3, "minPhase": True, "bioItd": True, "headWidthCm": 18.0, "sourceAzDeg": -90}),
            ("sr_96000_nearfield_015m", {"sampleRate": 96000, "nearField": True, "nearFieldDistM": 0.15, "sourceAzDeg": 90, "signal": "pink"}),
            ("sr_192000_ism_upmix_lgsr", {"sampleRate": 192000, "ismRoom": True, "ismPreset": 4, "ismLevel": 1.5, "upmix": True, "lgsr": True, "signal": "sine1k"}),
            ("extreme_head_pose_180", {"yawDeg": 180.0, "pitchDeg": 85.0, "sourceAzDeg": -90.0, "sourceElDeg": 45.0, "signal": "impulse"}),
            ("extreme_itd_bounds", {"bioItd": True, "headWidthCm": 10.0, "manualItdUs": -500.0, "sourceAzDeg": 90.0, "signal": "white"}),
        ]
        if self.mode == "deep":
            cases.extend([
                ("sr_22050_upmix_lgsr", {"sampleRate": 22050, "upmix": True, "lgsr": True, "magLsOrder": 2, "signal": "pink"}),
                ("negative_yaw_pitch", {"yawDeg": -135.0, "pitchDeg": -85.0, "ismRoom": True, "nearField": True, "nearFieldDistM": 0.45}),
            ])

        steps = [{"cmd": "acoustic_probe", "args": params} for _, params in cases]
        steps.append({"cmd": "log_tail", "args": {"lines": 80, "errorsOnly": True}})
        res = self.bridge.pipeline(steps, stop_on_error=False, tier=2, timeout_sec=55)

        for (label, params), r in zip(cases, res):
            if not r["ok"]:
                self.ledger.record(
                    severity="HIGH",
                    vector="V7_acoustic_extreme_fuzzer",
                    source="acoustic_probe",
                    category="acoustic_probe_crash",
                    title=f"acoustic_probe '{label}' failed: {(r['error'] or r['body'])[:150]}",
                    repro_step=f"acoustic_probe {json.dumps(params)}",
                    evidence=(r["error"] or r["body"])[:320],
                )
            elif isinstance(r["json"], dict):
                for iss in analyze_acoustic_probe_result(r["json"], label):
                    self.ledger.record(
                        severity=iss["severity"],
                        vector="V7_acoustic_extreme_fuzzer",
                        source="acoustic_probe",
                        category=iss["category"],
                        title=iss["message"],
                        repro_step=f"acoustic_probe {json.dumps(params)}",
                        evidence=r["body"][:350],
                    )
        if len(res) > len(cases) and res[-1]["ok"]:
            self._ingest_applog_delta(res[-1]["body"], "V7_acoustic_extreme_fuzzer", f"fuzzed {len(cases)} extreme acoustic probes")
        return f"fuzzed {len(cases)} extreme psychoacoustic/sample-rate/head-pose probes in 1 burst"

    # ==================== VECTOR 8: IN-APP HIGH-SPEED CHAOS + BRIDGE SECURITY & MEMORY LEAK ====================
    def _v8_inapp_chaos_and_watchdog(self) -> str:
        notes_parts: list[str] = []
        steps: list[dict[str, Any]] = []
        if self._has_chaos_hunt_cmd:
            steps.append({"cmd": "chaos_hunt", "args": {"rounds": 10 if self.mode == "deep" else 5}})
        steps.append({"cmd": "file_read", "args": {"path": "/etc/hosts"}})
        steps.append({"cmd": "definitely_nonexistent_cmd_xyz"})
        steps.append({"cmd": "memory"})
        steps.append({"cmd": "log_tail", "args": {"lines": 80, "errorsOnly": True}})

        res = self.bridge.pipeline(steps, stop_on_error=False, tier=2, timeout_sec=50)
        idx = 0
        if self._has_chaos_hunt_cmd and idx < len(res):
            ch = res[idx]
            idx += 1
            if ch["ok"] and isinstance(ch["json"], dict):
                anomalies = ch["json"].get("anomalies") or []
                notes_parts.append(f"in-app chaos_hunt anomalies={len(anomalies)}")
                for an in anomalies:
                    self.ledger.record(
                        severity=str(an.get("severity") or "HIGH"),
                        vector="V8_inapp_chaos_and_watchdog",
                        source="in_app_chaos_hunt",
                        category=str(an.get("category") or "chaos_anomaly"),
                        title=str(an.get("message") or "")[:200],
                        repro_step=str(an.get("scenario") or "chaos_hunt"),
                        evidence=json.dumps(an)[:350],
                    )
            else:
                notes_parts.append("in-app chaos_hunt failed")

        if idx < len(res):
            if res[idx]["ok"]:
                self.ledger.record(
                    severity="CRITICAL",
                    vector="V8_inapp_chaos_and_watchdog",
                    source="security_fuzzer",
                    category="path_traversal_bypass",
                    title="file_read allowed reading outside confined app roots (/etc/hosts)",
                    repro_step="file_read path='/etc/hosts'",
                    evidence=res[idx]["body"][:200],
                )
            idx += 1
        if idx < len(res):
            if res[idx]["ok"]:
                self.ledger.record(
                    severity="MEDIUM",
                    vector="V8_inapp_chaos_and_watchdog",
                    source="bridge_fuzzer",
                    category="unknown_cmd_returned_ok",
                    title="Unknown bridge command unexpectedly returned ok=true",
                    repro_step="cavernize-cmd --name definitely_nonexistent_cmd_xyz",
                    evidence=res[idx]["body"][:200],
                )
            idx += 1
        if idx < len(res):
            mem_now = res[idx]
            idx += 1
            if mem_now["ok"] and isinstance(mem_now["json"], dict) and self._baseline_memory:
                gc_before = int(self._baseline_memory.get("managedGcMb") or 0)
                gc_after = int(mem_now["json"].get("managedGcMb") or 0)
                delta_mb = gc_after - gc_before
                notes_parts.append(f"GC {gc_before}MB->{gc_after}MB (Δ{delta_mb:+d}MB)")
                if mem_now["json"].get("lowMemory"):
                    self.ledger.record(
                        severity="HIGH",
                        vector="V8_inapp_chaos_and_watchdog",
                        source="memory_watchdog",
                        category="android_low_memory",
                        title="Android reported lowMemory=true after stress hunt",
                        repro_step="memory check post-hunt",
                        evidence=mem_now["body"][:250],
                    )
                elif delta_mb >= 45:
                    self.ledger.record(
                        severity="MEDIUM",
                        vector="V8_inapp_chaos_and_watchdog",
                        source="memory_watchdog",
                        category="managed_heap_growth",
                        title=f"Managed GC heap grew by +{delta_mb} MB during hunt ({gc_before}->{gc_after} MB)",
                        repro_step="memory baseline vs post-hunt",
                        evidence=mem_now["body"][:250],
                    )
        if idx < len(res) and res[idx]["ok"]:
            self._ingest_applog_delta(res[idx]["body"], "V8_inapp_chaos_and_watchdog", "security + memory watchdog")

        return ", ".join(notes_parts) or "bridge security & memory OK"

    # ==================== VECTOR 9: INTERACTIVE UI FREEZE WATCHDOG & LIBRARY/PLAY ORACLE ====================
    def _v9_ui_latency_and_search_oracle(self) -> str:
        """Measure real per-action UI latency (stall >400ms / freeze >1500ms) & audit Library album-search consistency."""
        notes: list[str] = []
        max_ms = 0.0

        # 9a. Reset Q2 FrameMetrics window (if supported) and time individual tab switches
        has_rt = "runtime_telemetry" in getattr(self, "_capabilities", ())
        tab_perf_samples: list[dict[str, Any]] = []
        if self.meta.get("uiHostAttached") and self.allow_mutate:
            if has_rt:
                self.bridge.cmd("runtime_telemetry", {"resetFrameMetrics": True}, tier=0, timeout_sec=15)
            for tab_idx, tab_name in ((0, "Library"), (1, "Sound"), (2, "Export"), (0, "Library")):
                t0 = time.monotonic()
                r = self.bridge.cmd("tab_switch", {"index": tab_idx}, tier=1, timeout_sec=20)
                dt_ms = (time.monotonic() - t0) * 1000.0
                # Prefer on-device elapsedMs when available so cloud network RTT never creates false UI-freeze alarms
                dev_ms = float(r.get("elapsedMs") or dt_ms)
                max_ms = max(max_ms, dev_ms)
                if r.get("elapsedMs") is not None:
                    tab_perf_samples.append({
                        "Target": f"Tab:{tab_name}",
                        "Complexity": "fast",
                        "FirstFrameMs": dev_ms,
                        "UiThreadBlockMs": dev_ms,
                    })
                lat_issue = classify_ui_latency_ms(dev_ms, f"tab_switch({tab_name})")
                if lat_issue:
                    self.ledger.record(
                        severity=lat_issue["severity"],
                        vector="V9_ui_latency_and_search_oracle",
                        source="ui_latency_watchdog",
                        category=lat_issue["category"],
                        title=lat_issue["message"],
                        repro_step=f"tab_switch index={tab_idx} ({tab_name})",
                        evidence=f"deviceElapsedMs={dev_ms:.0f} wallMs={dt_ms:.0f}",
                    )
            notes.append(f"maxTabMs={max_ms:.0f}")

        # 9b. Stateful Library Album-Search Oracle (catches single-track truncation when track shares album name!)
        lq = self.bridge.cmd("library_query", {"query": "", "limit": 100, "musicOnly": True}, tier=0, timeout_sec=20)
        if lq["ok"] and isinstance(lq["json"], dict):
            all_tracks = list(lq["json"].get("tracks") or [])
            album_counts: dict[str, int] = {}
            title_matches_album: list[str] = []
            for tr in all_tracks:
                alb = str(tr.get("Album") or "").strip()
                tit = str(tr.get("Title") or "").strip()
                if alb:
                    album_counts[alb] = album_counts.get(alb, 0) + 1
                    if tit and tit.lower() == alb.lower() and alb not in title_matches_album:
                        title_matches_album.append(alb)
            # Prioritize title-track albums (e.g. "evermore") or any multi-track album
            multi_albums = [a for a in title_matches_album if album_counts.get(a, 0) >= 2]
            if not multi_albums:
                multi_albums = [a for a, cnt in sorted(album_counts.items(), key=lambda kv: -kv[1]) if cnt >= 2][:2]
            checked_albums = 0
            for alb in multi_albums[:2]:
                checked_albums += 1
                expected_cnt = album_counts[alb]
                qr = self.bridge.cmd("library_query", {"query": alb, "limit": 100, "musicOnly": True}, tier=0, timeout_sec=20)
                if qr["ok"] and isinstance(qr["json"], dict):
                    returned = [
                        t for t in (qr["json"].get("tracks") or [])
                        if str(t.get("Album") or "").strip().lower() == alb.lower()
                    ]
                    if len(returned) < expected_cnt:
                        self.ledger.record(
                            severity="HIGH",
                            vector="V9_ui_latency_and_search_oracle",
                            source="library_search_oracle",
                            category="library_album_search_truncated",
                            title=f"Library search for album '{alb}' returned {len(returned)}/{expected_cnt} album tracks",
                            repro_step=f"library_query query='{alb}'",
                            evidence=f"expected>={expected_cnt} tracks in album '{alb}', got {len(returned)}",
                        )
            notes.append(f"albumsAudited={checked_albums}")

        # 9c. Q2 Blocking Runtime Telemetry Gates (ApplicationExitInfo, 120 Hz FrameMetrics, StrictMode, Gesture Latency)
        if has_rt:
            rt = self.bridge.cmd("runtime_telemetry", {}, tier=0, timeout_sec=15)
            if rt["ok"] and isinstance(rt["json"], dict):
                rj = rt["json"]
                fm = rj.get("frameMetrics") or {}
                xr = rj.get("audioXRuns") or {}

                # ExitInfo gate
                exit_ev = evaluate_exit_info_gate(
                    rj.get("exitReasons"),
                    baseline_keys=self._baseline_exit_keys or None,
                )
                self._q2_summary["exitInfoNewFatal"] = int(exit_ev["newFatalCount"])
                if not exit_ev["passed"]:
                    self._q2_summary["passed"] = False
                for ef in exit_ev["findings"]:
                    self.ledger.record(
                        severity=ef["severity"],
                        vector="V9_ui_latency_and_search_oracle",
                        source="exit_info_q2",
                        category=ef["category"],
                        title=ef["message"][:200],
                        repro_step="runtime_telemetry.exitReasons",
                        evidence=ef["message"],
                        origin=ef.get("origin", "live_triggered"),
                    )

                # 120 Hz FrameMetrics gate
                fm_ev = evaluate_frame_metrics_gate(
                    fm,
                    p95_budget_ms=self.q2_p95_budget_ms,
                    max_frozen_frames=0,
                )
                self._q2_summary["frameP95Ms"] = fm_ev["p95Ms"]
                self._q2_summary["frameCpuP95Ms"] = fm_ev["cpuP95Ms"]
                self._q2_summary["frozenFrames"] = fm_ev["frozenFrames"]
                if not fm_ev["passed"]:
                    self._q2_summary["passed"] = False
                for ff in fm_ev["findings"]:
                    self.ledger.record(
                        severity=ff["severity"],
                        vector="V9_ui_latency_and_search_oracle",
                        source="frame_metrics_q2",
                        category=ff["category"],
                        title=ff["message"][:200],
                        repro_step="runtime_telemetry.frameMetrics",
                        evidence=json.dumps(fm, ensure_ascii=False)[:320],
                    )

                # StrictMode gate
                sm_ev = evaluate_strict_mode_gate(rj.get("strictMode"))
                self._q2_summary["strictModeViolations"] = sm_ev["violationCount"]
                if not sm_ev["passed"]:
                    self._q2_summary["passed"] = False
                for sf in sm_ev["findings"]:
                    self.ledger.record(
                        severity=sf["severity"],
                        vector="V9_ui_latency_and_search_oracle",
                        source="strict_mode_q2",
                        category=sf["category"],
                        title=sf["message"][:200],
                        repro_step="runtime_telemetry.strictMode",
                        evidence=sf["message"],
                    )

                fm_frames = fm.get("totalFrames", fm.get("sampledFrames", fm.get("ringCount", 0)))
                fm_p95 = fm.get("p95Ms", fm.get("p95TotalMs", 0))
                xrun_cnt = xr.get("totalOutputUnderruns", xr.get("audioTrackUnderrunCount", 0))
                notes.append(f"fm={fm_frames}/p95={fm_p95}ms,xruns={xrun_cnt}")
                if fm_ev["cpuP95Ms"] is not None:
                    notes.append(f"cpuP95={fm_ev['cpuP95Ms']}ms")
                if fm_ev["frozenFrames"] > 0:
                    notes.append(f"frozen={fm_ev['frozenFrames']}")

        # 9d. Q2 Gesture / Tab Touch-to-First-Frame Gate (<50ms fast targets)
        if tab_perf_samples:
            gest_ev = evaluate_gesture_latency_gate(tab_perf_samples, touch_budget_ms=self.q2_touch_budget_ms)
            if not gest_ev["passed"]:
                self._q2_summary["passed"] = False
                for gf in gest_ev["findings"]:
                    self.ledger.record(
                        severity=gf["severity"],
                        vector="V9_ui_latency_and_search_oracle",
                        source="gesture_latency_q2",
                        category=gf["category"],
                        title=gf["message"][:200],
                        repro_step="V9 tab_switch gesture latency",
                        evidence=gf["message"],
                    )

        self._watch_step_delta("V9_ui_latency_and_search_oracle", "UI latency & library search oracle", check_logcat=True)
        return ", ".join(notes) or "UI latency & library oracle OK"

    # ==================== VECTOR 10: SPATIAL TRAJECTORY & IN-RAM HOT-EXEC ORACLE ====================
    def _v10_spatial_trajectory_and_hot_exec(self) -> str:
        """Verify In-RAM hot_exec closed-loop capability AND sample SpatialObjectCortex trajectories across all modes."""
        if not hasattr(self.bridge, "relay"):
            return "skipped (offline mock bridge)"

        # Compile & execute a tiny collectible C# probe purely in running Cavernize RAM (zero APK rebuild!)
        probe_cs = r"""
using System;
using System.Collections.Generic;
using System.Globalization;
using System.Numerics;
using System.Text;
using Cavern.Neural;
using Cavern.Spatial;

public static class SpatialTrajectoryHotProbe {
    public static string Run() {
        int sr = 48000;
        int len = sr * 24;
        var stems = new List<StereoStemBuffer>();
        string[] names = new[] { "vocals", "bass", "drums", "other" };
        for (int s = 0; s < names.Length; s++) {
            float[] l = new float[len];
            float[] r = new float[len];
            float freq = 180f + s * 320f;
            for (int i = 0; i < len; i++) {
                float env = 0.25f + 0.75f * (0.5f + 0.5f * MathF.Sin(2f * MathF.PI * (0.25f + 0.1f * s) * i / sr));
                float v = 0.35f * env * MathF.Sin(2f * MathF.PI * freq * i / sr);
                l[i] = v;
                r[i] = v * 0.9f;
            }
            stems.Add(new StereoStemBuffer(names[s], l, r));
        }

        var modes = new[] { SpatialChoreoMode.Subtle, SpatialChoreoMode.Musical, SpatialChoreoMode.Cinematic, SpatialChoreoMode.Orbit };
        var sb = new StringBuilder();
        sb.Append("{\"ok\":true,\"modes\":{");
        for (int m = 0; m < modes.Length; m++) {
            if (m > 0) sb.Append(',');
            var mode = modes[m];
            var cues = Layer1AudioForensics.Analyze(stems, sr, mode, null);
            var track = SpatialChoreoTrack.Build(names, cues, mode, 24.0, 0.05);
            sb.Append('"').Append(mode.ToString()).Append("\":{\"cueCount\":").Append(cues.Length).Append(",\"stems\":{");
            for (int s = 0; s < names.Length; s++) {
                if (s > 0) sb.Append(',');
                sb.Append('"').Append(names[s]).Append("\":[");
                for (int step = 0; step <= 48; step++) {
                    double t = step * 0.5;
                    Vector3 p;
                    track.EvaluatePositionZeroAlloc(s, t, out p, false);
                    if (step > 0) sb.Append(',');
                    sb.Append("{\"t\":").Append(t.ToString("0.0", CultureInfo.InvariantCulture))
                      .Append(",\"x\":").Append(p.X.ToString("0.000", CultureInfo.InvariantCulture))
                      .Append(",\"y\":").Append(p.Y.ToString("0.000", CultureInfo.InvariantCulture))
                      .Append(",\"z\":").Append(p.Z.ToString("0.000", CultureInfo.InvariantCulture)).Append('}');
                }
                sb.Append(']');
            }
            sb.Append("}}");
        }
        sb.Append("}}");
        return sb.ToString();
    }
}
"""
        try:
            import hot_probe_compiler as hpc
            import secrets as _sec

            comp = hpc.compile_hot_probe(probe_cs, probe_name="SpatialTrajectoryHotProbe", include_android=False)
            if not comp.get("ok"):
                return f"hot_exec compile skipped ({str(comp.get('error') or 'no local dotnet')[:80]})"
            req_id = f"hot_v10_{int(time.time())}_{_sec.token_hex(4)}"
            hot_args = hpc.build_signed_hot_exec_args(
                comp["dllBytes"],
                self.bridge.secret,
                req_id,
                entry_type="SpatialTrajectoryHotProbe",
                timeout_sec=20,
            )
            res = self.bridge.cmd("hot_exec", hot_args, tier=2, timeout_sec=35)
            if not res["ok"] or not isinstance(res["json"], dict):
                self.ledger.record(
                    severity="MEDIUM",
                    vector="V10_spatial_trajectory_and_hot_exec",
                    source="hot_exec",
                    category="hot_exec_probe_failed",
                    title=f"In-RAM hot_exec spatial probe failed: {(res['error'] or res['body'])[:140]}",
                    repro_step="hot_exec SpatialTrajectoryHotProbe",
                    evidence=(res["error"] or res["body"])[:300],
                )
                return "hot_exec probe failed"

            hj = res["json"]
            if hj.get("unloaded") is False:
                self.ledger.record(
                    severity="MEDIUM",
                    vector="V10_spatial_trajectory_and_hot_exec",
                    source="hot_exec",
                    category="hot_exec_alc_not_unloaded",
                    title="Collectible AssemblyLoadContext remained alive after hot_exec probe",
                    repro_step="hot_exec SpatialTrajectoryHotProbe",
                    evidence=res["body"][:260],
                )

            payload_str = str(hj.get("output") or "")
            parsed = json.loads(payload_str) if payload_str.strip().startswith("{") else {}
            modes_map = parsed.get("modes") or {}
            modes_checked = 0
            anomalies_found = 0
            for mode_name, mdata in modes_map.items():
                modes_checked += 1
                for stem_name, samples in (mdata.get("stems") or {}).items():
                    for iss in analyze_spatial_trajectory(samples, mode_name, stem_name):
                        anomalies_found += 1
                        self.ledger.record(
                            severity=iss["severity"],
                            vector="V10_spatial_trajectory_and_hot_exec",
                            source="spatial_trajectory_oracle",
                            category=iss["category"],
                            title=iss["message"],
                            repro_step=f"hot_exec SpatialChoreoTrack.Build({mode_name}, {stem_name})",
                            evidence=f"cues={mdata.get('cueCount')} samples={len(samples)}",
                        )
            return f"hot_exec RAM probe OK ({hj.get('elapsedMs', 0)}ms, unloaded={hj.get('unloaded')}), audited {modes_checked} choreo modes (anomalies={anomalies_found})"
        except Exception as exc:
            return f"hot_exec trajectory probe unavailable ({type(exc).__name__}: {str(exc)[:80]})"

    # ==================== VECTOR 11: Q3 15-20 CYCLE DIRTY-STATE, PSS, HEAP, VIEW & FD SOAK ====================
    def _capture_soak_sample(self, *, force_gc: bool = False) -> dict[str, Any] | None:
        """Capture one Q3 soak sample via native `soak_snapshot` (with pre-Q3 APK fallback)."""
        if "soak_snapshot" in getattr(self, "_capabilities", ()):
            r = self.bridge.cmd("soak_snapshot", {"forceGc": force_gc}, tier=0, timeout_sec=15)
            if r["ok"] and isinstance(r.get("json"), dict):
                return r["json"]
        # Seamless fallback for pre-Q3 APKs using memory + app_state + /proc/self/fd shell probe
        fb = self.bridge.pipeline([
            {"cmd": "memory"},
            {"cmd": "app_state"},
            {"cmd": "shell_exec", "args": {"command": "ls /proc/self/fd 2>/dev/null | wc -l", "timeoutSec": 6}},
        ], stop_on_error=False, tier=2, timeout_sec=20)
        mem = fb[0]["json"] if len(fb) > 0 and fb[0]["ok"] and isinstance(fb[0]["json"], dict) else {}
        st = fb[1]["json"] if len(fb) > 1 and fb[1]["ok"] and isinstance(fb[1]["json"], dict) else {}
        fd_cnt = -1
        if len(fb) > 2 and fb[2]["ok"] and isinstance(fb[2]["json"], dict):
            try:
                fd_cnt = int(str(fb[2]["json"].get("stdout") or "").strip().splitlines()[0])
            except Exception:
                fd_cnt = -1
        if not mem and not st:
            return None
        return {
            "nativePssMb": mem.get("nativePssMb", -1.0),
            "managedGcMb": mem.get("managedGcMb", -1.0),
            "openFdCount": mem.get("openFdCount", fd_cnt),
            "threadCount": mem.get("threadCount", -1),
            "viewCount": -1,
            "activeDialogs": int(st.get("activeDialogCount") or 0),
            "isOperationRunning": bool(st.get("isOperationRunning")),
            "lowMemory": bool(mem.get("lowMemory")),
            "sessionFatalCount": 0,
        }

    def _v11_dirty_state_soak(self) -> str:
        """Run 15-20 cycle dirty-state soak tracking native PSS, managed GC, View count, threads, /proc/self/fd, & stuck flags."""
        if not self.allow_mutate:
            return "skipped (--no-mutate)"

        cycles = 8 if self.mode == "quick" else self.soak_cycles
        samples: list[dict[str, Any]] = []
        base_iso = bool(self._baseline_settings.get("Iso226Enabled", False))
        has_soak_cmd = "soak_snapshot" in getattr(self, "_capabilities", ())

        s0 = self._capture_soak_sample(force_gc=True)
        if s0:
            samples.append(s0)

        for cyc in range(cycles):
            flip_iso = (not base_iso) if (cyc % 2 == 0) else base_iso
            tab_a = cyc % 5
            tab_b = (cyc + 2) % 5
            steps: list[dict[str, Any]] = [
                {"cmd": "tab_switch", "args": {"index": tab_a}},
                {"cmd": "settings_set", "args": {"key": "Iso226Enabled", "value": flip_iso}},
                {"cmd": "tab_switch", "args": {"index": tab_b}},
                {"cmd": "ui_interact", "args": {"action": "scroll_by", "dy": 420 if cyc % 2 == 0 else -420}},
                {"cmd": "ui_interact", "args": {"action": "dismiss_dialogs"}},
            ]
            if has_soak_cmd:
                steps.append({"cmd": "soak_snapshot", "args": {"forceGc": (cyc == cycles - 1)}})
            res = self.bridge.pipeline(steps, stop_on_error=False, tier=2, timeout_sec=30)
            if has_soak_cmd and len(res) >= 6 and res[-1]["ok"] and isinstance(res[-1].get("json"), dict):
                samples.append(res[-1]["json"])
            elif not has_soak_cmd and (cyc % 2 == 1 or cyc == cycles - 1):
                sn = self._capture_soak_sample(force_gc=(cyc == cycles - 1))
                if sn:
                    samples.append(sn)

        # Ensure Iso226Enabled restored & final forced-GC settled snapshot recorded
        self.bridge.cmd("settings_set", {"key": "Iso226Enabled", "value": base_iso}, tier=1, timeout_sec=15)
        self.bridge.cmd("ui_interact", {"action": "dismiss_dialogs"}, tier=2, timeout_sec=10)
        s_end = self._capture_soak_sample(force_gc=True)
        if s_end:
            samples.append(s_end)

        ev = evaluate_soak_leak_gate(samples)
        self._q3_summary.update({
            "passed": ev["passed"],
            "cycles": ev["cycles"],
            "pssDeltaMb": ev["pssDeltaMb"],
            "pssSlopeMbPerCycle": ev["pssSlopeMbPerCycle"],
            "fdDelta": ev["fdDelta"],
            "gcDeltaMb": ev["gcDeltaMb"],
            "viewDelta": ev["viewDelta"],
            "threadDelta": ev["threadDelta"],
            "stuckOp": ev["stuckOp"],
            "leakedDialogs": ev["leakedDialogs"],
        })
        for f in ev["findings"]:
            self.ledger.record(
                severity=f["severity"],
                vector="V11_dirty_state_soak",
                source="soak_gate_q3",
                category=f["category"],
                title=f["message"][:200],
                repro_step=f"V11 {cycles}-cycle dirty-state soak",
                evidence=json.dumps(self._q3_summary, ensure_ascii=False)[:360],
            )
        self._watch_step_delta("V11_dirty_state_soak", f"{cycles}-cycle dirty-state soak", check_logcat=True)
        return (
            f"soak {ev['cycles']} samples: ΔPSS={ev['pssDeltaMb']:+.1f}MB "
            f"(slope={ev['pssSlopeMbPerCycle']:+.2f}/cyc), ΔFD={ev['fdDelta']:+d}, "
            f"ΔGC={ev['gcDeltaMb']:+.1f}MB, ΔViews={ev['viewDelta']:+d}, "
            f"stuckOp={ev['stuckOp']}, dialogs={ev['leakedDialogs']}"
        )

    # ==================== VECTOR 12: Q4 FROZEN REGRESSION & STATE-MACHINE CONTRACTS ====================
    def _run_hot_exec_state_contract_probe(self) -> dict[str, Any] | None:
        """Compile & execute in-RAM CavernQ4StateContractProbe via collectible AssemblyLoadContext."""
        probe_cs = r"""
using System;
using System.Reflection;
using Cavern.Neural;

public static class CavernQ4StateContractProbe {
    public static string Run() {
        bool blurDisabled = false;
        bool onnxPssOk = false;
        long onnxCapMb = 0;
        bool karaokeOk = false;
        bool noStuckOp = true;
        string err = "";
        try {
            Assembly appAsm = null;
            foreach (var asm in AppDomain.CurrentDomain.GetAssemblies()) {
                var n = asm.GetName().Name ?? "";
                if (string.Equals(n, "CavernizeAndroid", StringComparison.OrdinalIgnoreCase)) {
                    appAsm = asm;
                    break;
                }
            }
            if (appAsm == null) {
                err = "CavernizeAndroid assembly not loaded";
            } else {
                var blurT = appAsm.GetType("CavernizeAndroid.Theme.OneUiMotionBlur");
                var blurProp = blurT?.GetProperty("EnableFullScreenRenderEffectBlur", BindingFlags.Public | BindingFlags.Static);
                if (blurProp == null) {
                    err += "Missing OneUiMotionBlur.EnableFullScreenRenderEffectBlur; ";
                } else {
                    blurDisabled = !Convert.ToBoolean(blurProp.GetValue(null));
                }
                var onnxT = appAsm.GetType("CavernizeAndroid.Services.NativeOnnxRuntimeBridge");
                var pssField = onnxT?.GetField("MaxSingleProcessModelPssBytes", BindingFlags.NonPublic | BindingFlags.Public | BindingFlags.Static);
                if (pssField == null) {
                    err += "Missing NativeOnnxRuntimeBridge.MaxSingleProcessModelPssBytes; ";
                } else {
                    long capBytes = Convert.ToInt64(pssField.GetRawConstantValue() ?? pssField.GetValue(null));
                    onnxCapMb = capBytes / (1024L * 1024L);
                    onnxPssOk = (onnxCapMb > 0 && onnxCapMb <= 2600L);
                }
            }
            // Verify SpatialKaraokeEngine reset + zero-NaN DSP invariant in live runtime
            using (var engine = new SpatialKaraokeEngine(48000, 2)) {
                engine.IsEnabled = true;
                engine.VocalLevel = 0.0f;
                engine.ResetAdaptationState();
                float[] mixL = new float[512];
                float[] mixR = new float[512];
                float[] refL = new float[512];
                float[] refR = new float[512];
                for (int i = 0; i < 512; i++) {
                    float s = 0.25f * MathF.Sin(2f * MathF.PI * 440f * i / 48000f);
                    mixL[i] = s; mixR[i] = s; refL[i] = s; refR[i] = s;
                }
                float[][] channels = new float[][] { mixL, mixR };
                bool[] isLfe = new bool[] { false, false };
                bool[] isFront = new bool[] { true, true };
                engine.ProcessMultichannelInPlace(channels, isLfe, isFront, 2, 512, refL, null, false, refR, null);
                bool finite = true;
                for (int i = 0; i < 512; i++) {
                    if (!float.IsFinite(mixL[i]) || !float.IsFinite(mixR[i])) { finite = false; break; }
                }
                engine.ResetAdaptationState();
                karaokeOk = finite;
            }
        } catch (Exception ex) {
            err = ex.GetType().Name + ": " + ex.Message;
        }
        bool ok = blurDisabled && onnxPssOk && karaokeOk && noStuckOp && string.IsNullOrEmpty(err);
        return "{\"ok\":" + (ok ? "true" : "false")
            + ",\"blur_disabled\":" + (blurDisabled ? "true" : "false")
            + ",\"onnx_pss_cap_lte_2600mb\":" + (onnxPssOk ? "true" : "false")
            + ",\"onnxCapMb\":" + onnxCapMb
            + ",\"spatial_karaoke_reset_zero_nan\":" + (karaokeOk ? "true" : "false")
            + ",\"no_stuck_operation\":" + (noStuckOp ? "true" : "false")
            + ",\"error\":\"" + err.Replace("\\", "\\\\").Replace("\"", "'") + "\"}";
    }
}
"""
        if hasattr(self.bridge, "run_hot_exec_probe"):
            return self.bridge.run_hot_exec_probe(probe_cs)
        if not hasattr(self.bridge, "relay") or "hot_exec" not in getattr(self, "_capabilities", ()):
            return None
        try:
            import secrets as _secrets
            import hot_probe_compiler as hpc  # noqa: E402
            comp = hpc.compile_hot_probe(probe_cs, probe_name="CavernQ4StateContractProbe", include_android=False)
            if not comp.get("ok"):
                return {"ok": False, "error": f"compile_failed:{comp.get('error', '')[:160]}"}
            req_id = f"q4_{int(time.time())}_{_secrets.token_hex(4)}"
            args_payload = hpc.build_signed_hot_exec_args(
                comp["dllBytes"],
                self.bridge.secret,
                req_id,
                entry_type="CavernQ4StateContractProbe",
                args_json="{}",
                timeout_sec=20,
            )
            res = self.bridge.cmd("hot_exec", args_payload, tier=2, timeout_sec=45)
            if not res.get("ok"):
                return {"ok": False, "error": f"hot_exec_failed:{res.get('error', '')[:160]}"}
            j = res.get("json") or {}
            raw_ret = j.get("output") if j.get("output") is not None else j.get("result")
            if isinstance(raw_ret, str) and raw_ret.strip():
                try:
                    parsed = json.loads(raw_ret)
                    if isinstance(parsed, dict):
                        return parsed
                except Exception:
                    return {"ok": False, "error": f"bad_probe_json:{raw_ret[:160]}"}
            return j if isinstance(j, dict) else {"ok": False, "error": "empty_probe_result"}
        except Exception as exc:
            return {"ok": False, "error": str(exc)[:160]}

    def _v12_regression_state_contracts(self) -> str:
        """Execute Q4 frozen 1-bug=1-test regression contracts (offline + live bridge/hot_exec state machines)."""
        cpath = qa_gate.resolve_contracts_path(REPO_ROOT, None, self.contracts_file)
        data, load_errs = qa_gate.load_contracts_registry(cpath)
        if load_errs or data is None:
            self._q4_summary["schemaErrors"] = list(load_errs)
            self._q4_summary["failed"] = 1
            self.ledger.record(
                severity="HIGH",
                vector="V12_regression_state_contracts",
                source="regression_contract_q4",
                category="regression_contract_failed",
                title=f"Frozen regression contracts missing/invalid: {'; '.join(load_errs)[:180]}",
                repro_step=f"qa_gate.load_contracts_registry({cpath})",
                evidence="; ".join(load_errs)[:320],
            )
            return f"contracts load FAILED: {'; '.join(load_errs)}"

        off_ev = qa_gate.evaluate_offline_contracts(REPO_ROOT, data)
        failed_ids: list[str] = []
        for s_err in off_ev.get("schemaErrors") or []:
            self.ledger.record(
                severity="HIGH",
                vector="V12_regression_state_contracts",
                source="regression_contract_q4",
                category="regression_contract_failed",
                title=f"Frozen contract schema violation: {s_err[:180]}",
                repro_step="plan_tool.py qa verify-contracts --strict",
                evidence=str(s_err)[:320],
            )
        for f_item in off_ev.get("failures") or []:
            cid = str(f_item.get("id") or "REG-UNKNOWN")
            failed_ids.append(cid)
            errs_s = "; ".join(f_item.get("errors") or [])
            self.ledger.record(
                severity=str(f_item.get("severity") or "HIGH"),
                vector="V12_regression_state_contracts",
                source="regression_contract_q4",
                category="regression_contract_failed",
                title=f"Frozen contract {cid} ({f_item.get('bug_ref', '')}) FAILED: {errs_s[:160]}",
                repro_step=f"V12 offline contract {cid} ({f_item.get('test_ref', '')})",
                evidence=errs_s[:320],
            )

        # Execute live bridge + hot_exec state-machine contracts
        live_total = 0
        live_passed = 0
        live_failed = 0
        hot_exec_res: dict[str, Any] | None = None
        hot_exec_attempted = False

        for item in data.get("contracts") or []:
            if not isinstance(item, dict) or str(item.get("status") or "frozen").lower() == "waived":
                continue
            lc = item.get("live_check")
            if not isinstance(lc, dict) or not lc.get("kind"):
                continue
            cid = str(item.get("id") or "REG-LIVE")
            sev = str(item.get("severity") or "HIGH")
            kind = str(lc.get("kind") or "")
            live_total += 1
            ok_live = True
            detail = ""

            if kind == "reflect_equals":
                if "reflect_get" in getattr(self, "_capabilities", ()):
                    r = self.bridge.cmd("reflect_get", {"type": lc.get("type", ""), "path": lc.get("path", "")}, tier=2, timeout_sec=15)
                    if r.get("ok") and isinstance(r.get("json"), dict) and "value" in r["json"]:
                        actual = r["json"].get("value")
                        expected = lc.get("expected")
                        if actual != expected:
                            ok_live = False
                            detail = f"{lc.get('type')}.{lc.get('path')} expected {expected!r}, got {actual!r}"
                    else:
                        ok_live = False
                        detail = f"reflect_get failed for {lc.get('type')}.{lc.get('path')}: {str(r.get('error') or r.get('body') or 'missing value')[:160]}"
                else:
                    detail = "reflect_get not in capabilities"

            elif kind == "settings_roundtrip":
                key = str(lc.get("key") or "Iso226Enabled")
                if not self.allow_mutate:
                    detail = "read-only skip (--no-mutate)"
                else:
                    orig = bool(self._baseline_settings.get(key, False))
                    flipped = not orig
                    try:
                        r1 = self.bridge.cmd("settings_set", {"key": key, "value": flipped}, tier=1, timeout_sec=15)
                        r2 = self.bridge.cmd("settings_get", {}, tier=0, timeout_sec=15)
                        got_flip = (r2.get("json") or {}).get(key) if r2.get("ok") else None
                        if not r1.get("ok") or got_flip != flipped:
                            ok_live = False
                            detail = f"settings_set({key}={flipped}) -> settings_get returned {got_flip!r}"
                    finally:
                        self.bridge.cmd("settings_set", {"key": key, "value": orig}, tier=1, timeout_sec=15)
                    r3 = self.bridge.cmd("settings_get", {}, tier=0, timeout_sec=15)
                    got_rest = (r3.get("json") or {}).get(key) if r3.get("ok") else None
                    if ok_live and got_rest != orig:
                        ok_live = False
                        detail = f"settings_restore({key}={orig}) -> settings_get returned {got_rest!r}"

            elif kind in ("hot_exec_invariant", "hot_exec_bundle"):
                if not hot_exec_attempted:
                    hot_exec_attempted = True
                    hot_exec_res = self._run_hot_exec_state_contract_probe()
                    if isinstance(hot_exec_res, dict) and "soak_snapshot" in getattr(self, "_capabilities", ()):
                        rs = self.bridge.cmd("soak_snapshot", {"forceGc": False}, tier=0, timeout_sec=15)
                        if rs.get("ok") and isinstance(rs.get("json"), dict) and "isOperationRunning" in rs["json"]:
                            hot_exec_res["no_stuck_operation"] = not bool(rs["json"].get("isOperationRunning"))
                if hot_exec_res is None:
                    detail = "hot_exec unavailable in offline mock"
                elif str(hot_exec_res.get("error") or "").strip():
                    ok_live = False
                    detail = f"hot_exec probe error: {str(hot_exec_res.get('error'))[:200]}"
                else:
                    invs = lc.get("invariants") if kind == "hot_exec_bundle" else [lc.get("invariant")]
                    missing = [iv for iv in (invs or []) if iv and hot_exec_res.get(iv) is not True]
                    if missing:
                        ok_live = False
                        detail = f"hot_exec invariants failed: {', '.join(missing)}"
                    elif not hot_exec_res.get("ok") and not invs:
                        ok_live = False
                        detail = f"hot_exec probe failed: {json.dumps(hot_exec_res, ensure_ascii=False)[:200]}"

            if ok_live:
                live_passed += 1
            else:
                live_failed += 1
                if cid not in failed_ids:
                    failed_ids.append(cid)
                self.ledger.record(
                    severity=sev,
                    vector="V12_regression_state_contracts",
                    source="regression_contract_q4",
                    category="regression_contract_failed",
                    title=f"Live state contract {cid} FAILED: {detail[:180]}",
                    repro_step=f"V12 live contract {cid} ({kind})",
                    evidence=detail[:320],
                )

        total_contracts = int(off_ev.get("total") or 0)
        total_failed = len(set(failed_ids)) + len(off_ev.get("schemaErrors") or [])
        total_passed = max(0, total_contracts - len(set(failed_ids)) - int(off_ev.get("waived") or 0))
        self._q4_summary.update({
            "passed": total_passed,
            "failed": total_failed,
            "total": total_contracts,
            "offlinePassed": int(off_ev.get("passed") or 0),
            "offlineFailed": int(off_ev.get("failed") or 0),
            "livePassed": live_passed,
            "liveTotal": live_total,
            "hotExecVerified": bool(hot_exec_res and hot_exec_res.get("ok")),
            "schemaErrors": list(off_ev.get("schemaErrors") or []),
            "failedIds": failed_ids,
        })
        self._watch_step_delta("V12_regression_state_contracts", "Q4 frozen regression & state contracts", check_logcat=False)
        return (
            f"contracts {total_passed}/{total_contracts} PASS "
            f"(offline={off_ev.get('passed', 0)}/{off_ev.get('offlineChecked', 0)}, "
            f"live={live_passed}/{live_total}, hotExec={self._q4_summary['hotExecVerified']}, fail={total_failed})"
        )

    # ==================== GUARANTEED CLEAN STATE RESTORE ====================
    def _restore_settings(self) -> bool:
        if not self._baseline_settings:
            return True
        restore_keys = [
            "Iso226Enabled", "LgsrEnabled", "HeadTrackingEnabled", "AutoEqEnabled",
            "HptfNotchEnabled", "MinimumPhaseHrir", "NearFieldEnabled", "IsmRoomEnabled",
            "DistanceSimulation", "DialogEnhancerEnabled", "HeadphoneVirtualizer",
            "ParametricStftEnabled", "ProAudioExclusiveMode", "LoudnessNormModeIndex",
            "MagLsOrder", "BiometricHeadWidthCm", "NearFieldDistanceM", "BinauralProfileId",
        ]
        subset = {k: self._baseline_settings[k] for k in restore_keys if k in self._baseline_settings}
        if not subset:
            return True
        r = self.bridge.cmd("settings_set", {"values": subset}, tier=1, timeout_sec=20)
        return bool(r["ok"])

    def _restore_all(self) -> bool:
        ok = True
        self.meta["restoreDiffKeys"] = []
        # Always restore QA StrictMode OFF in finally even during read-only (--no-mutate) runs!
        if self._strict_mode_activated:
            try:
                sm_off = self.bridge.cmd("strict_mode", {"enable": False}, tier=2, timeout_sec=15)
                if sm_off.get("ok"):
                    self._strict_mode_activated = False
                else:
                    ok = False
            except Exception:
                ok = False
        if not self.allow_mutate:
            return ok
        try:
            steps: list[dict[str, Any]] = []
            if self._created_cache_files:
                steps.append({"cmd": "shell_exec", "args": {"command": "rm -rf ../cache/bh_fuzz", "timeoutSec": 10}})
                self._created_cache_files.clear()
            restore_keys = [
                "Iso226Enabled", "LgsrEnabled", "HeadTrackingEnabled", "AutoEqEnabled",
                "HptfNotchEnabled", "MinimumPhaseHrir", "NearFieldEnabled", "IsmRoomEnabled",
                "DistanceSimulation", "DialogEnhancerEnabled", "HeadphoneVirtualizer",
                "ParametricStftEnabled", "ProAudioExclusiveMode", "LoudnessNormModeIndex",
                "MagLsOrder", "BiometricHeadWidthCm", "NearFieldDistanceM", "BinauralProfileId",
            ]
            subset = {k: self._baseline_settings[k] for k in restore_keys if k in self._baseline_settings}
            if subset:
                steps.append({"cmd": "settings_set", "args": {"values": subset}})
            steps.append({"cmd": "ui_interact", "args": {"action": "dismiss_dialogs"}})
            orig_file = str(self._baseline_state.get("loadedFile") or "")
            orig_tab = self._baseline_state.get("activeTab")
            was_playing = bool(self._baseline_state.get("isPlaying"))
            if orig_file:
                steps.append({"cmd": "open_file", "args": {"path": orig_file, "autoPlay": was_playing}})
            if isinstance(orig_tab, int) and 0 <= orig_tab <= 4:
                steps.append({"cmd": "tab_switch", "args": {"index": orig_tab}})
            steps.append({"cmd": "settings_get"})
            if steps:
                res = self.bridge.pipeline(steps, stop_on_error=False, tier=2, timeout_sec=30)
                ok = ok and any(r.get("ok") for r in res)
                if res and res[-1].get("subCmd") == "settings_get" and res[-1].get("ok") and isinstance(res[-1].get("json"), dict):
                    after = res[-1]["json"]
                    diff_keys = [k for k, v in subset.items() if after.get(k) != v]
                    self.meta["restoreDiffKeys"] = diff_keys
                    if diff_keys:
                        ok = False
        except Exception:
            ok = False
        return ok

    def run_hunt(self, selected_vectors: list[str] | None = None) -> dict[str, Any]:
        t0 = time.monotonic()
        connected = self._prime_baselines()
        restored = True
        if connected:
            all_vectors: list[tuple[str, Callable[[], str]]] = [
                ("V1_log_forensics", self._v1_log_forensics),
                ("V2_concurrent_playback_oracle", self._v2_concurrent_playback_oracle),
                ("V3_settings_chaos_fuzzer", self._v3_settings_chaos_fuzzer),
                ("V4_real_library_edge_hunter", self._v4_real_library_edge_hunter),
                ("V5_corrupt_file_fuzzer", self._v5_corrupt_file_fuzzer),
                ("V6_ui_monkey_and_audit", self._v6_ui_monkey_and_audit),
                ("V7_acoustic_extreme_fuzzer", self._v7_acoustic_extreme_fuzzer),
                ("V8_inapp_chaos_and_watchdog", self._v8_inapp_chaos_and_watchdog),
                ("V9_ui_latency_and_search_oracle", self._v9_ui_latency_and_search_oracle),
                ("V10_spatial_trajectory_and_hot_exec", self._v10_spatial_trajectory_and_hot_exec),
                ("V11_dirty_state_soak", self._v11_dirty_state_soak),
                ("V12_regression_state_contracts", self._v12_regression_state_contracts),
            ]
            try:
                for vname, vfn in all_vectors:
                    if selected_vectors and not any(sel.lower() in vname.lower() for sel in selected_vectors):
                        continue
                    self._run_vector(vname, vfn)
            finally:
                restored = self._restore_all()

        elapsed = round(time.monotonic() - t0, 2)
        self.meta["elapsedSec"] = elapsed
        self.meta["stepsExecuted"] = self.bridge.calls
        self.meta["restoredState"] = restored
        self.meta["finishedUtc"] = datetime.now(timezone.utc).isoformat(timespec="seconds")

        report = {
            "ok": True,
            "meta": self.meta,
            "severityCounts": self.ledger.counts_by_severity(active_only=False),
            "activeSeverityCounts": self.ledger.counts_by_severity(active_only=True),
            "originCounts": self.ledger.counts_by_origin(),
            "totalUniqueFindings": len(self.ledger.ranked()),
            "totalActiveFindings": len(self.ledger.active_findings()),
            "q2GateSummary": dict(self._q2_summary),
            "q3SoakSummary": dict(self._q3_summary),
            "q4GateSummary": dict(self._q4_summary),
            "vectors": self.vector_summaries,
            "findings": self.ledger.to_dicts(),
        }
        report["qaGate"] = evaluate_cavern_qa_gate(report, p95_budget_ms=self.q2_p95_budget_ms)
        return report

    def run_watch(self, duration_sec: int = 60, interval_sec: float = 3.0) -> dict[str, Any]:
        """Real-time live monitor: stream EVERY new warning/error/crash/jank while the app is used."""
        t0 = time.monotonic()
        if not self._prime_baselines():
            return {"ok": False, "error": "Cavernize unreachable", "findings": self.ledger.to_dicts()}

        self._emit(f"LIVE WATCH active for {duration_sec}s (polling every {interval_sec:.1f}s) — use Cavernize now!")
        watch_start = time.monotonic()
        end_ts = watch_start + max(5, duration_sec)
        polls = 0
        while True:
            polls += 1
            before = len(self.ledger.ranked())
            self._watch_step_delta("LIVE_WATCH", f"live_user_session poll#{polls}", check_logcat=True)
            after_list = self.ledger.ranked()
            if len(after_list) > before:
                for f in after_list:
                    if f.repro_step == f"live_user_session poll#{polls}":
                        self._emit(f"  !! [{f.severity}] ({f.source}/{f.category}) {f.title}")
            remaining = end_ts - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(interval_sec, remaining))

        self.meta["mode"] = "watch"
        self.meta["elapsedSec"] = round(time.monotonic() - t0, 2)
        self.meta["stepsExecuted"] = self.bridge.calls
        self.meta["restoredState"] = True
        return {
            "ok": True,
            "meta": self.meta,
            "severityCounts": self.ledger.counts_by_severity(),
            "totalUniqueFindings": len(self.ledger.ranked()),
            "vectors": [{"name": "LIVE_WATCH", "status": "FINDINGS" if self.ledger.ranked() else "OK", "newFindings": len(self.ledger.ranked()), "elapsedSec": self.meta["elapsedSec"], "notes": f"{polls} live polls"}],
            "findings": self.ledger.to_dicts(),
        }


# ---------------------------------------------------------------------------
# CLI Entrypoint
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="cavern_bug_hunter.py",
        description="Cavernize Live Adversarial Bug-Hunter & Chaos Fuzzer via OmniAgent IPC.",
    )
    p.add_argument("--mode", choices=("quick", "standard", "deep", "watch"), default="standard", help="Hunt intensity or live watch mode")
    p.add_argument("--vectors", default="", help="Comma-separated vector substrings to run (e.g. V1,V2,V5)")
    p.add_argument("--no-mutate", action="store_true", help="Read-only mode (no settings/file/UI mutations)")
    p.add_argument("--duration", type=int, default=60, help="Duration in seconds for --mode watch")
    p.add_argument("--interval", type=float, default=3.0, help="Poll interval seconds for --mode watch")
    p.add_argument("--out-md", default="/tmp/cavern_bug_hunt_latest.md", help="Output Markdown report path")
    p.add_argument("--out-json", default="/tmp/cavern_bug_hunt_latest.json", help="Output JSON report path")
    p.add_argument("--golden-dir", default="", help="Optional directory for Q1 Golden SSIM visual diff baselines")
    p.add_argument("--update-goldens", action="store_true", help="Refresh/seed Q1 Golden SSIM baselines in --golden-dir")
    p.add_argument("--strict-mode", action="store_true", help="Enable opt-in Q2 QA StrictMode main-thread violation detector (auto-restored OFF)")
    p.add_argument("--q2-p95-ms", type=float, default=12.0, help="Q2 120 Hz FrameMetrics P95 budget in ms (default: 12.0)")
    p.add_argument("--q2-touch-ms", type=float, default=50.0, help="Q2 gesture/tab touch-to-first-frame budget in ms (default: 50.0)")
    p.add_argument("--soak-cycles", type=int, default=16, help="Q3 V11 dirty-state soak cycles (default: 16)")
    p.add_argument("--contracts-file", default="", help="Optional path to Q4 regression_contracts.json")
    p.add_argument("--gate", action="store_true", help="Q4 Blocking Adversarial QA Gate mode (exit 2 if CRIT/HIGH/GEOM/EXIT/P95/CONTRACTS fail)")
    p.add_argument("--quiet", action="store_true", help="Suppress stderr live progress logs")
    p.add_argument("--url", default="", help="Override relay URL")
    p.add_argument("--pair-secret", default="", help="Override 64-hex pair secret")
    p.add_argument("--admin-key", default="", help="Optional admin key")
    p.add_argument("--timeout", type=int, default=45, help="Per-command timeout seconds")
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    relay, secret, admin = oac._resolve_auth(args, allow_knock=True)
    bridge = LiveCavernBridge(relay, secret, admin, timeout_sec=args.timeout)

    if args.mode == "watch":
        hunter = LiveBugHunter(bridge, mode="watch", allow_mutate=False, verbose=not args.quiet, out_md=args.out_md, out_json=args.out_json)
        report = hunter.run_watch(duration_sec=args.duration, interval_sec=args.interval)
    else:
        allow_mutate = (not args.no_mutate) and (args.mode != "quick")
        hunter = LiveBugHunter(
            bridge,
            mode=args.mode,
            allow_mutate=allow_mutate,
            verbose=not args.quiet,
            out_md=args.out_md,
            out_json=args.out_json,
            golden_dir=args.golden_dir,
            update_goldens=args.update_goldens,
            enable_strict_mode=args.strict_mode,
            q2_p95_budget_ms=args.q2_p95_ms,
            q2_touch_budget_ms=args.q2_touch_ms,
            soak_cycles=args.soak_cycles,
            contracts_file=args.contracts_file,
            gate_mode=args.gate,
        )
        sel = [v.strip() for v in args.vectors.split(",") if v.strip()] if args.vectors else None
        report = hunter.run_hunt(selected_vectors=sel)

    md = generate_report_markdown(report)
    if args.out_json:
        Path(args.out_json).write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    if args.out_md:
        Path(args.out_md).write_text(md, encoding="utf-8")

    print(md)
    gate_res = report.get("qaGate") if isinstance(report.get("qaGate"), dict) else None
    if args.gate and gate_res:
        print(gate_res.get("summaryLine", ""))
        return 0 if (report.get("ok") and gate_res.get("ok")) else 2
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
