#!/usr/bin/env python3
"""Hot-Probe C# Compiler for Live Execution Inside Running Cavernize Android (`hot_exec`).

Compiles any standalone C# probe/experiment/DSP-test/UI-test snippet locally in ~1-2s using the
cached portable .NET 11 SDK + Cavern.dll + Cavern.Format.dll + Cavernize.Logic.dll +
CavernizeAndroid shadow ref + Microsoft.Android.Ref -> produces a tiny collectible IL `.dll`
ready to execute purely in RAM inside Cavernize Android via `AssemblyLoadContext.LoadFromStream`
(100% W^X compliant, HMAC-SHA256 signed, zero APK rebuild/reinstall needed!).
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

_AGENT_DIR = Path(__file__).resolve().parent
_CANDIDATE_ROOT = _AGENT_DIR.parents[1] if _AGENT_DIR.parent.name == ".agents" else _AGENT_DIR.parent
_FED_CAVERN = Path(os.environ.get("CAVERN_FEDERATION_ROOT", "/tmp/cavern-federation")) / "cavern"
REPO_ROOT = _CANDIDATE_ROOT if (_CANDIDATE_ROOT / ".agents" / "scripts" / "check_android_cs.py").is_file() else _FED_CAVERN
SCRIPTS_DIR = REPO_ROOT / ".agents" / "scripts"
if SCRIPTS_DIR.is_dir() and str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))
if str(_AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(_AGENT_DIR))

try:
    import check_android_cs as ci_checker
except ImportError:
    ci_checker = None  # type: ignore[assignment]
import omni_protocol as proto


def _ensure_cavern_refs(repo: Path, dotnet_bin: str, env: dict[str, str], include_android: bool = True) -> list[Path]:
    """Ensure compiled Cavern*.dll and Microsoft.Android.Ref assemblies exist for hot-probe compilation."""
    cache_dir = Path.home() / ".cache" / "cavern-android-ref"
    hot_refs_dir = Path("/tmp/cavern-hot-probe-refs")
    hot_refs_dir.mkdir(parents=True, exist_ok=True)

    refs: list[Path] = []
    # Build Cavern, Cavern.Format, Cavernize.Logic once into /tmp/cavern-hot-probe-refs
    for proj_rel, dll_name in (
        ("Cavern/Cavern.csproj", "Cavern.dll"),
        ("Cavern.Format/Cavern.Format.csproj", "Cavern.Format.dll"),
        ("CavernSamples/Reusable/Cavernize.Logic/Cavernize.Logic.csproj", "Cavernize.Logic.dll"),
    ):
        cached = hot_refs_dir / dll_name
        proj = repo / proj_rel
        # Rebuild if missing or older than newest source
        if not cached.is_file():
            rc, out = ci_checker._run_layer_build(dotnet_bin, proj, repo, env)
            if rc != 0:
                raise RuntimeError(f"Failed building {proj_rel}:\n{out}")
            built = list((proj.parent / "bin" / "Release").rglob(dll_name))
            if not built:
                raise RuntimeError(f"Could not find compiled {dll_name}")
            shutil.copy2(built[0], cached)
        refs.append(cached)

    if include_android:
        target_major, _min_sdk, _nowarns = ci_checker._read_android_csproj_meta(repo)
        api, pkg, ver = ci_checker._resolve_android_ref_target(cache_dir, target_major)
        android_refs = ci_checker._ensure_android_refs(cache_dir, api, pkg, ver)
        refs.extend(android_refs)

        # Include CavernizeAndroid shadow assembly if already compiled by check_android_cs
        shadow_dll = Path("/tmp/cavern-android-typecheck/bin/Release")
        if shadow_dll.is_dir():
            cands = list(shadow_dll.rglob("CavernizeAndroid.TypeCheck.dll")) or list(shadow_dll.rglob("CavernizeAndroidShadow.dll"))
            if cands:
                refs.append(cands[0])

    return refs


def compile_hot_probe(
    cs_source: str,
    probe_name: str = "HotProbe",
    include_android: bool = True,
    repo: Path | None = None,
) -> dict[str, Any]:
    """Compile C# hot-probe source to an in-memory collectible .NET assembly (.dll bytes)."""
    repo_root = (repo or REPO_ROOT).resolve()
    cache_dir = Path.home() / ".cache" / "cavern-android-ref"
    target_major, _min_sdk, _nowarns = ci_checker._read_android_csproj_meta(repo_root)
    dotnet_bin, sdk_major, sdk_ver = ci_checker._resolve_dotnet(cache_dir, target_major, allow_bootstrap=True)
    if not dotnet_bin:
        raise RuntimeError("No .NET SDK found for hot-probe compilation.")

    env = ci_checker._build_dotnet_env(dotnet_bin)
    refs = _ensure_cavern_refs(repo_root, dotnet_bin, env, include_android=include_android)

    work_dir = Path(tempfile.mkdtemp(prefix="cavern-hot-probe-"))
    t0 = time.time()
    try:
        safe_name = "".join(ch for ch in probe_name if ch.isalnum() or ch == "_") or "HotProbe"
        (work_dir / "Probe.cs").write_text(cs_source, encoding="utf-8")
        tfm = f"net{sdk_major}.0"
        ref_xml = "\n".join(
            f'    <Reference Include="{r.stem}"><HintPath>{r}</HintPath><Private>false</Private></Reference>'
            for r in refs
        )
        csproj = f"""<Project Sdk="Microsoft.NET.Sdk">
  <PropertyGroup>
    <TargetFramework>{tfm}</TargetFramework>
    <OutputType>Library</OutputType>
    <AssemblyName>{safe_name}_{int(time.time())}</AssemblyName>
    <Nullable>disable</Nullable>
    <ImplicitUsings>enable</ImplicitUsings>
    <AllowUnsafeBlocks>true</AllowUnsafeBlocks>
    <Optimize>true</Optimize>
    <DebugType>none</DebugType>
    <Deterministic>true</Deterministic>
    <NoWarn>$(NoWarn);CA1416;CA1422;CS1701;CS1702</NoWarn>
  </PropertyGroup>
  <ItemGroup>
{ref_xml}
  </ItemGroup>
</Project>
"""
        csproj_path = work_dir / f"{safe_name}.csproj"
        csproj_path.write_text(csproj, encoding="utf-8")

        res = subprocess.run(
            [dotnet_bin, "build", str(csproj_path), "-c", "Release", "-m:1", "/p:UseSharedCompilation=false", "--nologo", "-v:minimal"],
            cwd=str(work_dir),
            env=env,
            capture_output=True,
            text=True,
            timeout=90,
            check=False,
        )
        combined = (res.stdout + "\n" + res.stderr).strip()
        if res.returncode != 0:
            return {"ok": False, "error": combined, "elapsedMs": int((time.time() - t0) * 1000)}

        dlls = list((work_dir / "bin" / "Release").rglob(f"{safe_name}_*.dll"))
        if not dlls:
            return {"ok": False, "error": "Compiled .dll not found.", "elapsedMs": int((time.time() - t0) * 1000)}
        dll_bytes = dlls[0].read_bytes()
        return {
            "ok": True,
            "assemblyName": dlls[0].stem,
            "sdkVersion": sdk_ver,
            "dllBytes": dll_bytes,
            "dllBase64": base64.b64encode(dll_bytes).decode("ascii"),
            "sizeBytes": len(dll_bytes),
            "sha256": hashlib.sha256(dll_bytes).hexdigest(),
            "elapsedMs": int((time.time() - t0) * 1000),
        }
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


def build_signed_hot_exec_args(
    dll_bytes: bytes,
    pair_secret_hex: str,
    request_id: str,
    entry_type: str = "",
    args_json: str = "{}",
    timeout_sec: int = 15,
) -> dict[str, Any]:
    ts = int(time.time())
    dll_sha, sig = proto.sign_hot_exec(pair_secret_hex, request_id, ts, dll_bytes)
    return {
        "requestId": request_id,
        "timestampUnix": ts,
        "dllSha256": dll_sha,
        "signature": sig,
        "dllBase64": base64.b64encode(dll_bytes).decode("ascii"),
        "entryType": entry_type,
        "argsJson": args_json,
        "timeoutSec": timeout_sec,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Compile a C# hot-probe DLL for Cavernize Android hot_exec.")
    parser.add_argument("--file", help="Path to C# probe source file")
    parser.add_argument("--code", help="Inline C# probe source code")
    parser.add_argument("--name", default="HotProbe")
    parser.add_argument("--no-android-refs", action="store_true")
    parser.add_argument("--out", help="Optional output path to save compiled .dll")
    args = parser.parse_args()

    if args.file:
        src = Path(args.file).read_text(encoding="utf-8")
    elif args.code:
        src = args.code
    else:
        print("ERROR: Provide --file or --code.", file=sys.stderr)
        return 2

    res = compile_hot_probe(src, probe_name=args.name, include_android=not args.no_android_refs)
    if not res["ok"]:
        print(res["error"], file=sys.stderr)
        return 1
    if args.out:
        Path(args.out).write_bytes(res["dllBytes"])
    summary = {k: v for k, v in res.items() if k not in ("dllBytes", "dllBase64")}
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
