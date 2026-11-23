#!/usr/bin/env python3
"""Zero-Quota 3-Repo Workspace Federation Manager (Cavern + OmniAgent + Dynamic Fractal Planner).

Solves the single-repo GitHub import constraint in AI Studio:
  - Whichever of the 3 repos is imported as `/app/applet`, the preview server calls
    `repo_federation.py --warm` at container boot (0 LLM tokens / 0 quota).
  - Active workspace is symlinked into `/tmp/cavern-federation/<current>`.
  - The 2 sibling repos are fast-hydrated into `/tmp/cavern-federation/<sibling>` via
    single-request GitHub tarballs (outside `/app/applet`, preserving the 999-file cap
    and keeping `verify_github_parity` 100% clean).
  - Protects local dirty edits in `/tmp/cavern-federation/<alias>` unless `--force`.
  - Supports 1-command `status`, `hydrate`, `diff`, `push`, and `sync-mirrors` across
    all 3 repos (direct `GITHUB_TOKEN` Git Data API or automatic OmniAgent Termux fallback).

Pure Python 3 stdlib (zero pip dependencies). Never stores secrets in any repo.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import fnmatch
import hashlib
import io
import json
import os
import shutil
import sys
import tarfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

AGENT_DIR = Path(__file__).resolve().parent
REPO_ROOT = AGENT_DIR.parents[1] if AGENT_DIR.parent.name == ".agents" else AGENT_DIR.parent
if str(AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_DIR))

import github_direct as ghdirect
import local_capabilities as lcap
import omni_protocol as proto

DEFAULT_MOUNT_ROOT = Path("/tmp/cavern-federation")
STATE_FILENAME = ".federation_state.json"
STATUS_FILENAME = "federation_status.json"
DEFAULT_MANIFEST: dict[str, Any] = {
    "schema": 1,
    "mountRoot": str(DEFAULT_MOUNT_ROOT),
    "repos": {
        "cavern": {
            "ownerRepo": "guumachadooo/Cavern",
            "branch": "master",
            "role": "Primary C#/.NET spatial DSP & Cavernize Android application",
            "detectFiles": [
                "Cavern/Cavern.csproj",
                "CavernSamples/CavernizeAndroid/CavernizeAndroid.csproj",
            ],
            "termuxPath": "/root/.gemini/antigravity/scratch/Cavern",
            "ciWorkflow": "BuildAndroid.yml",
        },
        "omniagent": {
            "ownerRepo": "guumachadooo/OmniAgent",
            "branch": "main",
            "role": "One UI 9 Android companion APK, Cloudflare Hub v6 worker & Python/Termux bridge daemon",
            "detectFiles": [
                "OmniAgentAndroid/OmniAgentAndroid.csproj",
                "worker/cloudflare_live_relay_worker.js",
            ],
            "termuxPath": "/root/.gemini/antigravity/scratch/OmniAgent",
            "ciWorkflow": "BuildOmniAgent.yml",
        },
        "fractal": {
            "ownerRepo": "guumachadooo/dynamic-fractal-planner",
            "branch": "main",
            "role": "Universal Dynamic Fractal Planner plugin",
            "detectFiles": [
                "plugin.json",
                "scripts/verify_universal.py",
            ],
            "termuxPath": "/root/.gemini/antigravity/scratch/dynamic-fractal-planner",
            "termuxGlobalPluginPath": "/root/.gemini/antigravity/plugins/dynamic-fractal-planner",
            "ciWorkflow": "",
        },
    },
    "mirrors": [],
}


def load_manifest(workspace_root: Path | None = None, manifest_path: Path | None = None) -> dict[str, Any]:
    candidates: list[Path] = []
    if manifest_path:
        candidates.append(Path(manifest_path))
    if workspace_root:
        candidates.append(Path(workspace_root) / ".agents" / "federation.json")
    candidates.append(REPO_ROOT / ".agents" / "federation.json")
    for cand in candidates:
        if cand.is_file():
            try:
                data = json.loads(cand.read_text(encoding="utf-8"))
                if isinstance(data, dict) and isinstance(data.get("repos"), dict):
                    return data
            except Exception:
                pass
    return json.loads(json.dumps(DEFAULT_MANIFEST))


def get_mount_root(manifest: dict[str, Any], override: Path | None = None) -> Path:
    if override is not None:
        return Path(override)
    env_override = os.environ.get("CAVERN_FEDERATION_ROOT", "").strip()
    if env_override:
        return Path(env_override)
    return Path(str(manifest.get("mountRoot") or DEFAULT_MOUNT_ROOT))


def detect_current_repo(workspace_root: Path, manifest: dict[str, Any]) -> str:
    ws = Path(workspace_root).resolve()
    repos = manifest.get("repos") or {}
    for alias, spec in repos.items():
        detect_files = spec.get("detectFiles") or []
        if detect_files and all((ws / rel).is_file() for rel in detect_files):
            return str(alias)
    for alias, spec in repos.items():
        detect_files = spec.get("detectFiles") or []
        if any((ws / rel).is_file() for rel in detect_files):
            return str(alias)
    return "unknown"


def _read_state(mount_root: Path) -> dict[str, Any]:
    p = mount_root / STATE_FILENAME
    if not p.is_file():
        return {"repos": {}}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data.setdefault("repos", {})
            return data
    except Exception:
        pass
    return {"repos": {}}


def _write_state(mount_root: Path, state: dict[str, Any]) -> None:
    mount_root.mkdir(parents=True, exist_ok=True)
    tmp = mount_root / f".{STATE_FILENAME}.tmp.{os.getpid()}"
    tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, mount_root / STATE_FILENAME)


def _compute_dir_hashes(root: Path) -> dict[str, str]:
    hashes: dict[str, str] = {}
    if not root.is_dir():
        return hashes
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in (".git", "__pycache__", "node_modules", "bin", "obj"))
        for fn in sorted(filenames):
            if fn.endswith((".pyc", ".pyo")):
                continue
            fp = Path(dirpath) / fn
            if not fp.is_file():
                continue
            rel = fp.relative_to(root).as_posix()
            hashes[rel] = hashlib.sha256(fp.read_bytes()).hexdigest()
    return hashes


def _detect_local_dirty(repo_dir: Path, baseline_hashes: dict[str, str]) -> list[str]:
    if not baseline_hashes or not repo_dir.is_dir():
        return []
    current = _compute_dir_hashes(repo_dir)
    dirty: list[str] = []
    for k in sorted(set(baseline_hashes.keys()) | set(current.keys())):
        if baseline_hashes.get(k) != current.get(k):
            dirty.append(k)
    return dirty


def _fetch_remote_head_sha(owner_repo: str, branch: str, token: str = "") -> tuple[str, str]:
    st, data = ghdirect._gh_request(
        "GET",
        f"/repos/{owner_repo}/git/ref/heads/{urllib.parse.quote(branch)}",
        token,
        timeout=20,
    )
    if st == 200 and isinstance(data.get("object"), dict) and data["object"].get("sha"):
        return str(data["object"]["sha"]), ""
    return "", f"HTTP {st}: {data.get('message', data)}"


def _download_repo_tarball(owner_repo: str, branch: str, token: str = "", timeout: int = 45) -> bytes:
    if token:
        url = f"https://api.github.com/repos/{owner_repo}/tarball/{urllib.parse.quote(branch)}"
        headers = {
            "User-Agent": proto.USER_AGENT,
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
        }
    else:
        url = f"https://codeload.github.com/{owner_repo}/tar.gz/refs/heads/{urllib.parse.quote(branch)}"
        headers = {"User-Agent": proto.USER_AGENT}
    req = urllib.request.Request(url, headers=headers, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _safe_extract_github_tarball(tar_bytes: bytes, dest_dir: Path) -> int:
    """Safely extract GitHub single-root tarball into dest_dir (strips top-level folder, blocks traversal)."""
    if dest_dir.exists():
        shutil.rmtree(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_resolved = dest_dir.resolve()
    count = 0
    with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:gz") as tf:
        for member in tf.getmembers():
            if not member.isfile():
                continue
            parts = Path(member.name).parts
            if len(parts) < 2:
                continue
            rel = Path(*parts[1:])
            if rel.is_absolute() or ".." in rel.parts:
                raise ValueError(f"Unsafe tarball entry rejected: {member.name}")
            target = (dest_dir / rel).resolve()
            if not str(target).startswith(str(dest_resolved) + os.sep):
                raise ValueError(f"Tarball path escape rejected: {member.name}")
            target.parent.mkdir(parents=True, exist_ok=True)
            src = tf.extractfile(member)
            if src is None:
                continue
            target.write_bytes(src.read())
            if member.mode & 0o111:
                try:
                    os.chmod(target, 0o755)
                except OSError:
                    pass
            count += 1
    return count


def get_repo_path(
    alias: str,
    workspace_root: Path,
    manifest: dict[str, Any],
    mount_root: Path | None = None,
) -> Path:
    cur = detect_current_repo(workspace_root, manifest)
    if alias == cur:
        return Path(workspace_root).resolve()
    return get_mount_root(manifest, mount_root) / alias


def ensure_current_symlink(workspace_root: Path, current_alias: str, mount_root: Path) -> Path | None:
    if not current_alias or current_alias == "unknown":
        return None
    mount_root.mkdir(parents=True, exist_ok=True)
    link_path = mount_root / current_alias
    ws_resolved = Path(workspace_root).resolve()
    if link_path.is_symlink():
        try:
            if link_path.resolve() == ws_resolved:
                return link_path
        except OSError:
            pass
        link_path.unlink()
    elif link_path.exists():
        shutil.rmtree(link_path)
    link_path.symlink_to(ws_resolved)
    return link_path


def hydrate_repo(
    alias: str,
    workspace_root: Path,
    manifest: dict[str, Any],
    *,
    mount_root: Path | None = None,
    force: bool = False,
    token: str = "",
) -> dict[str, Any]:
    repos = manifest.get("repos") or {}
    if alias not in repos:
        raise ValueError(f"Unknown repo alias '{alias}'. Expected one of: {', '.join(sorted(repos))}")
    spec = repos[alias]
    owner_repo = str(spec["ownerRepo"])
    branch = str(spec.get("branch") or "main")
    mroot = get_mount_root(manifest, mount_root)
    mroot.mkdir(parents=True, exist_ok=True)
    current_alias = detect_current_repo(workspace_root, manifest)

    if alias == current_alias:
        ensure_current_symlink(workspace_root, current_alias, mroot)
        return {
            "alias": alias,
            "ownerRepo": owner_repo,
            "branch": branch,
            "status": "active_workspace",
            "path": str(Path(workspace_root).resolve()),
            "mountLink": str(mroot / alias),
            "ok": True,
        }

    dest_dir = mroot / alias
    if dest_dir.is_symlink():
        dest_dir.unlink()

    tok = token or lcap.get_secret("GITHUB_TOKEN", aliases=("GH_TOKEN",))
    state = _read_state(mroot)
    prev = (state.get("repos") or {}).get(alias) or {}
    prev_sha = str(prev.get("headSha") or "")
    baseline = prev.get("baselineHashes") if isinstance(prev.get("baselineHashes"), dict) else {}

    dirty = _detect_local_dirty(dest_dir, baseline) if dest_dir.is_dir() else []
    if dirty and not force:
        return {
            "alias": alias,
            "ownerRepo": owner_repo,
            "branch": branch,
            "status": "preserved_dirty",
            "path": str(dest_dir),
            "head": prev_sha[:8],
            "dirtyCount": len(dirty),
            "dirtySample": dirty[:10],
            "ok": True,
        }

    remote_sha, sha_err = _fetch_remote_head_sha(owner_repo, branch, tok)
    if remote_sha and prev_sha == remote_sha and dest_dir.is_dir() and not force:
        return {
            "alias": alias,
            "ownerRepo": owner_repo,
            "branch": branch,
            "status": "cached",
            "path": str(dest_dir),
            "head": remote_sha[:8],
            "ok": True,
        }

    t0 = time.time()
    try:
        tar_bytes = _download_repo_tarball(owner_repo, branch, tok)
    except urllib.error.HTTPError as err:
        # Public fallback if token lacks scope or hit rate limit
        if tok and err.code in (401, 403, 404):
            tar_bytes = _download_repo_tarball(owner_repo, branch, "")
        else:
            return {
                "alias": alias,
                "ownerRepo": owner_repo,
                "branch": branch,
                "status": "error",
                "error": f"Tarball download failed (HTTP {err.code}): {err.reason} {sha_err}".strip(),
                "ok": False,
            }
    except Exception as exc:
        return {
            "alias": alias,
            "ownerRepo": owner_repo,
            "branch": branch,
            "status": "error",
            "error": str(exc),
            "ok": False,
        }

    staging = mroot / f".staging_{alias}_{os.getpid()}"
    try:
        file_count = _safe_extract_github_tarball(tar_bytes, staging)
        if dest_dir.exists():
            shutil.rmtree(dest_dir)
        os.replace(staging, dest_dir)
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)

    new_baseline = _compute_dir_hashes(dest_dir)
    elapsed_ms = int((time.time() - t0) * 1000)
    return {
        "alias": alias,
        "ownerRepo": owner_repo,
        "branch": branch,
        "status": "hydrated",
        "path": str(dest_dir),
        "head": remote_sha[:8] if remote_sha else "",
        "headFull": remote_sha,
        "fileCount": file_count,
        "elapsedMs": elapsed_ms,
        "baselineHashes": new_baseline,
        "ok": True,
    }


def hydrate_all(
    workspace_root: Path,
    *,
    manifest_path: Path | None = None,
    mount_root: Path | None = None,
    only: list[str] | None = None,
    force: bool = False,
) -> dict[str, Any]:
    ws = Path(workspace_root).resolve()
    manifest = load_manifest(ws, manifest_path)
    mroot = get_mount_root(manifest, mount_root)
    mroot.mkdir(parents=True, exist_ok=True)
    current = detect_current_repo(ws, manifest)
    ensure_current_symlink(ws, current, mroot)

    all_aliases = list((manifest.get("repos") or {}).keys())
    targets = [a for a in (only or all_aliases) if a in all_aliases]
    tok = lcap.get_secret("GITHUB_TOKEN", aliases=("GH_TOKEN",))

    results: dict[str, dict[str, Any]] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, len(targets))) as pool:
        futs = {
            pool.submit(
                hydrate_repo,
                alias,
                ws,
                manifest,
                mount_root=mroot,
                force=force,
                token=tok,
            ): alias
            for alias in targets
        }
        for fut in concurrent.futures.as_completed(futs):
            alias = futs[fut]
            try:
                results[alias] = fut.result()
            except Exception as exc:
                results[alias] = {"alias": alias, "status": "error", "error": str(exc), "ok": False}

    # Persist state atomically in single thread
    state = _read_state(mroot)
    state_repos = state.setdefault("repos", {})
    utc_now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    public_repos: dict[str, Any] = {}
    for alias in targets:
        r = dict(results[alias])
        baseline = r.pop("baselineHashes", None)
        head_full = r.pop("headFull", "")
        if r.get("status") == "hydrated" and isinstance(baseline, dict):
            state_repos[alias] = {
                "ownerRepo": r.get("ownerRepo", ""),
                "branch": r.get("branch", ""),
                "headSha": head_full,
                "hydratedUtc": utc_now,
                "fileCount": r.get("fileCount", len(baseline)),
                "baselineHashes": baseline,
            }
        public_repos[alias] = r

    state["activeWorkspace"] = str(ws)
    state["activeRepo"] = current
    state["updatedUtc"] = utc_now
    _write_state(mroot, state)

    summary = {
        "ok": all(bool(r.get("ok")) for r in public_repos.values()),
        "activeRepo": current,
        "activeWorkspace": str(ws),
        "mountRoot": str(mroot),
        "updatedUtc": utc_now,
        "repos": public_repos,
    }
    try:
        (mroot / STATUS_FILENAME).write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    except OSError:
        pass
    return summary


def inspect_federation_status(
    workspace_root: Path,
    *,
    manifest_path: Path | None = None,
    mount_root: Path | None = None,
    check_remote_diff: bool = False,
) -> dict[str, Any]:
    ws = Path(workspace_root).resolve()
    manifest = load_manifest(ws, manifest_path)
    mroot = get_mount_root(manifest, mount_root)
    current = detect_current_repo(ws, manifest)
    state = _read_state(mroot)
    state_repos = state.get("repos") or {}

    tok = lcap.get_secret("GITHUB_TOKEN", aliases=("GH_TOKEN",))
    pair_secret = lcap.get_secret("CAVERN_BRIDGE_PAIR_SECRET")
    relay_url = lcap.get_secret("CAVERN_RELAY_URL")
    has_profile = (ws / ".agents" / "bridge_profile.json").is_file()

    if tok:
        write_mode = "local_github_api"
    elif pair_secret and relay_url:
        write_mode = "omniagent_zero_knock_phone_git"
    elif has_profile:
        write_mode = "smart_knock_phone_git"
    else:
        write_mode = "read_only_missing_token"

    repo_reports: dict[str, Any] = {}
    for alias, spec in (manifest.get("repos") or {}).items():
        is_active = alias == current
        rpath = ws if is_active else (mroot / alias)
        exists = rpath.is_dir()
        st_rec = state_repos.get(alias) or {}
        baseline = st_rec.get("baselineHashes") if isinstance(st_rec.get("baselineHashes"), dict) else {}
        dirty = [] if is_active else (_detect_local_dirty(rpath, baseline) if exists else [])
        entry: dict[str, Any] = {
            "ownerRepo": spec["ownerRepo"],
            "branch": spec.get("branch", "main"),
            "active": is_active,
            "path": str(rpath),
            "hydrated": exists,
            "head": str(st_rec.get("headSha") or "")[:8],
            "hydratedUtc": st_rec.get("hydratedUtc", ""),
            "dirtyCount": len(dirty),
            "dirtyFiles": dirty[:15],
            "termuxPath": spec.get("termuxPath", ""),
        }
        if check_remote_diff and exists:
            try:
                d = ghdirect.diff_workspace_against_github(
                    rpath,
                    owner_repo=spec["ownerRepo"],
                    branch=spec.get("branch", "main"),
                    token=tok,
                    sync_deletes=False,
                )
                entry["remoteHead"] = d["headSha"][:8]
                entry["changedVsGithub"] = d["changed"]
                entry["changedCount"] = len(d["changed"])
            except Exception as exc:
                entry["remoteDiffError"] = str(exc)
        repo_reports[alias] = entry

    return {
        "ok": True,
        "activeRepo": current,
        "mountRoot": str(mroot),
        "writeMode": write_mode,
        "allHydrated": all(bool(r["hydrated"]) for r in repo_reports.values()),
        "repos": repo_reports,
    }


def _is_excluded(rel_posix: str, patterns: list[str]) -> bool:
    rel = rel_posix.replace("\\", "/").strip("/")
    if not rel:
        return False
    parts = rel.split("/")
    for pat in patterns:
        p = pat.replace("\\", "/").strip("/")
        if not p:
            continue
        if rel == p or rel.startswith(p + "/"):
            return True
        for seg in parts:
            if fnmatch.fnmatch(seg, p):
                return True
        if fnmatch.fnmatch(rel, p):
            return True
    return False


def sync_mirrors(
    workspace_root: Path,
    *,
    from_repo: str = "",
    mirror_name: str = "all",
    apply: bool = False,
    manifest_path: Path | None = None,
    mount_root: Path | None = None,
) -> dict[str, Any]:
    """Check or apply declared file mirrors across the 3 federated repos."""
    ws = Path(workspace_root).resolve()
    manifest = load_manifest(ws, manifest_path)
    mroot = get_mount_root(manifest, mount_root)
    mirrors = manifest.get("mirrors") or []
    report: list[dict[str, Any]] = []

    for m in mirrors:
        name = str(m.get("name") or "")
        if mirror_name != "all" and name != mirror_name:
            continue
        src_alias = str(m["sourceRepo"])
        dst_alias = str(m["targetRepo"])
        if from_repo and from_repo == dst_alias:
            src_alias, dst_alias = dst_alias, src_alias
            reverse = True
        elif from_repo and from_repo != src_alias:
            continue
        else:
            reverse = False

        src_root = get_repo_path(src_alias, ws, manifest, mroot)
        dst_root = get_repo_path(dst_alias, ws, manifest, mroot)
        if not src_root.is_dir() or not dst_root.is_dir():
            report.append(
                {
                    "mirror": name,
                    "ok": False,
                    "error": f"Missing hydrated repo dir: src={src_root.is_dir()} ({src_alias}), dst={dst_root.is_dir()} ({dst_alias})",
                }
            )
            continue

        copied: list[str] = []
        unchanged = 0
        for mapping in m.get("mappings") or []:
            raw_src = str(mapping["dst" if reverse else "src"])
            raw_dst = str(mapping["src" if reverse else "dst"])
            excludes = list(mapping.get("exclude") or [])
            s_path = (src_root / raw_src).resolve() if raw_src != "." else src_root.resolve()
            d_path = (dst_root / raw_dst).resolve() if raw_dst != "." else dst_root.resolve()

            if s_path.is_file():
                s_bytes = s_path.read_bytes()
                if not d_path.is_file() or d_path.read_bytes() != s_bytes:
                    if apply:
                        d_path.parent.mkdir(parents=True, exist_ok=True)
                        d_path.write_bytes(s_bytes)
                    copied.append(f"{src_alias}:{raw_src} -> {dst_alias}:{raw_dst}")
                else:
                    unchanged += 1
            elif s_path.is_dir():
                for dirpath, dirnames, filenames in os.walk(s_path):
                    cur = Path(dirpath)
                    rel_dir = cur.relative_to(s_path).as_posix()
                    dirnames[:] = [
                        dn
                        for dn in sorted(dirnames)
                        if not _is_excluded(dn if rel_dir == "." else f"{rel_dir}/{dn}", excludes)
                    ]
                    for fn in sorted(filenames):
                        rel_f = fn if rel_dir == "." else f"{rel_dir}/{fn}"
                        if _is_excluded(rel_f, excludes):
                            continue
                        sf = cur / fn
                        df = d_path / rel_f
                        s_bytes = sf.read_bytes()
                        if not df.is_file() or df.read_bytes() != s_bytes:
                            if apply:
                                df.parent.mkdir(parents=True, exist_ok=True)
                                df.write_bytes(s_bytes)
                                if os.access(sf, os.X_OK):
                                    try:
                                        os.chmod(df, 0o755)
                                    except OSError:
                                        pass
                            copied.append(f"{dst_alias}:{raw_dst.rstrip('/')}/{rel_f}" if raw_dst != "." else f"{dst_alias}:{rel_f}")
                        else:
                            unchanged += 1

        report.append(
            {
                "mirror": name,
                "direction": f"{src_alias} -> {dst_alias}",
                "applied": bool(apply),
                "changedCount": len(copied),
                "unchangedCount": unchanged,
                "changed": copied,
                "ok": True,
            }
        )

    return {"ok": all(bool(r.get("ok")) for r in report), "mirrors": report}


def push_federated_repo(
    alias: str,
    commit_message: str,
    workspace_root: Path,
    *,
    manifest_path: Path | None = None,
    mount_root: Path | None = None,
    sync_deletes: bool = False,
    no_sync_termux: bool = False,
    explicit_files: list[str] | None = None,
    explicit_deletes: list[str] | None = None,
) -> dict[str, Any]:
    """Push any federated repo (`cavern`, `omniagent`, `fractal`) directly via GitHub API or Termux fallback."""
    ws = Path(workspace_root).resolve()
    manifest = load_manifest(ws, manifest_path)
    mroot = get_mount_root(manifest, mount_root)
    repos = manifest.get("repos") or {}
    if alias not in repos:
        raise ValueError(f"Unknown repo alias '{alias}'")
    spec = repos[alias]
    owner_repo = str(spec["ownerRepo"])
    branch = str(spec.get("branch") or "main")
    repo_dir = get_repo_path(alias, ws, manifest, mroot)
    if not repo_dir.is_dir():
        raise RuntimeError(f"Repo '{alias}' is not hydrated at {repo_dir}. Run `repo_federation.py hydrate` first.")

    tok = lcap.get_secret("GITHUB_TOKEN", aliases=("GH_TOKEN",))
    if tok:
        try:
            res = ghdirect.push_workspace_direct(
                repo_dir,
                commit_message,
                owner_repo=owner_repo,
                branch=branch,
                token=tok,
                explicit_files=explicit_files or None,
                explicit_deletes=explicit_deletes or None,
                sync_deletes=sync_deletes,
            )
            # Update federation baseline for non-active hydrated repos so future warms see clean state
            current = detect_current_repo(ws, manifest)
            if res.get("ok") and alias != current:
                state = _read_state(mroot)
                st_repos = state.setdefault("repos", {})
                st_repos[alias] = {
                    "ownerRepo": owner_repo,
                    "branch": branch,
                    "headSha": res.get("headFull") or "",
                    "hydratedUtc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "baselineHashes": _compute_dir_hashes(repo_dir),
                }
                _write_state(mroot, state)

            if not no_sync_termux and spec.get("termuxPath"):
                res["termuxSync"] = _try_sync_termux_repo(spec, branch)
            return res
        except RuntimeError as exc:
            if "HTTP 403" not in str(exc) and "Resource not accessible" not in str(exc):
                raise
            print(f"WARN: Local GITHUB_TOKEN lacks Contents:Write for {owner_repo} ({exc}). Falling back to OmniAgent phone git push...")

    # Fallback: push via OmniAgent phone Termux (works even when GITHUB_TOKEN is missing or read-only!)
    return _push_via_omniagent_phone(
        repo_dir,
        spec,
        commit_message,
        explicit_files=explicit_files,
        explicit_deletes=explicit_deletes,
        sync_deletes=sync_deletes,
    )


def _try_sync_termux_repo(spec: dict[str, Any], branch: str) -> dict[str, Any]:
    termux_path = str(spec.get("termuxPath") or "").strip()
    owner_repo = str(spec.get("ownerRepo") or "").strip()
    if not termux_path:
        return {"attempted": False}
    try:
        import bridge_auth as bauth
        import omni_agent_client as oac

        conn = bauth.resolve_connection()
        if not (conn["relayUrl"] and conn["pairSecret"]):
            return {"attempted": False, "reason": "no_pair_secret"}
        if not oac._is_channel_online(conn["relayUrl"], conn["pairSecret"], "termux", conn["adminKey"]):
            return {"attempted": False, "reason": "termux_offline"}
        # Pull if repo already cloned on Termux (fast & safe; never fails the GitHub push)
        cmd = (
            f"if [ -d {termux_path}/.git ]; then "
            f"git -C {termux_path} fetch origin && git -C {termux_path} pull --rebase origin {branch} && git -C {termux_path} rev-parse --short HEAD; "
            f"else echo NOT_CLONED:{owner_repo}; fi"
        )
        r = oac.send_agent_command(
            conn["relayUrl"],
            conn["pairSecret"],
            "shell_exec",
            {"command": cmd, "timeoutSec": 60},
            channel="termux",
            admin_key=conn["adminKey"],
            timeout_sec=70,
        )
        out = str(r.get("stdout") or "").strip()
        return {"attempted": True, "ok": bool(r.get("ok")), "output": out[-240:]}
    except Exception as exc:
        return {"attempted": True, "ok": False, "error": str(exc)}


def _push_via_omniagent_phone(
    repo_dir: Path,
    spec: dict[str, Any],
    commit_message: str,
    *,
    explicit_files: list[str] | None = None,
    explicit_deletes: list[str] | None = None,
    sync_deletes: bool = False,
) -> dict[str, Any]:
    import bridge_auth as bauth
    import omni_agent_client as oac

    conn = bauth.resolve_connection()
    relay = conn["relayUrl"]
    secret = conn["pairSecret"]
    admin = conn["adminKey"]
    if relay and not secret and conn.get("knockId"):
        kr = bauth.request_smart_knock(
            relay,
            conn["knockId"],
            requested_scope="workspace",
            tier=2,
            duration_seconds=3600,
            reason=f"Push {spec['ownerRepo']} via Termux",
            admin_key=admin,
            persist_vault=True,
        )
        secret = str((kr.get("grant") or {}).get("pairSecret") or "").strip()
        if secret:
            secret = proto.normalize_pair_secret_hex(secret)
    if not relay or not secret:
        raise RuntimeError(
            f"Cannot push {spec['ownerRepo']}: GITHUB_TOKEN is not set and OmniAgent phone auth is unavailable."
        )

    termux_path = str(spec.get("termuxPath") or "")
    owner_repo = str(spec["ownerRepo"])
    branch = str(spec.get("branch") or "main")
    # Ensure repo is cloned on Termux if needed
    ensure_cmd = (
        f"if [ ! -d {termux_path}/.git ]; then "
        f"mkdir -p $(dirname {termux_path}) && git clone https://github.com/{owner_repo}.git {termux_path}; fi"
    )
    oac.send_agent_command(
        relay,
        secret,
        "shell_exec",
        {"command": ensure_cmd, "timeoutSec": 90},
        channel="termux",
        admin_key=admin,
        timeout_sec=100,
    )

    push_ns = argparse.Namespace(
        workspace=str(repo_dir),
        commit=commit_message,
        via="phone",
        owner_repo=owner_repo,
        branch=branch,
        remote="origin",
        no_push=False,
        no_sync_termux=True,
        pull_first=True,
        sync_deletes=sync_deletes,
        files=explicit_files or [],
        delete=explicit_deletes or [],
        url=relay,
        pair_secret=secret,
        admin_key=admin,
        channel="termux",
        repo=termux_path,
        timeout=180,
    )
    rc = oac.cmd_push_workspace(push_ns)
    return {"ok": rc == 0, "via": "phone_omniagent", "ownerRepo": owner_repo, "branch": branch}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Zero-Quota 3-Repo Workspace Federation Manager.")
    parser.add_argument("--workspace", default=str(REPO_ROOT), help="Active workspace root (default: auto-detected)")
    parser.add_argument("--mount-root", default="", help="Override federation mount root (default: /tmp/cavern-federation)")
    parser.add_argument("--warm", action="store_true", help="Zero-quota background warm-hydrate invoked by preview server.mjs")

    sub = parser.add_subparsers(dest="action", required=False)

    p_st = sub.add_parser("status", help="Show status of all 3 federated repos")
    p_st.add_argument("--remote-diff", action="store_true", help="Also diff against GitHub HEAD")

    p_hy = sub.add_parser("hydrate", help="Hydrate missing/stale sibling repos into /tmp/cavern-federation")
    p_hy.add_argument("--only", nargs="*", default=None)
    p_hy.add_argument("--force", action="store_true", help="Overwrite local edits in sibling mount dir")

    p_df = sub.add_parser("diff", help="Diff one or all federated repos against GitHub HEAD")
    p_df.add_argument("target", nargs="?", default="all")

    p_ps = sub.add_parser("push", help="Push one or all modified federated repos to GitHub (+ sync Termux)")
    p_ps.add_argument("target", help="cavern | omniagent | fractal | all")
    p_ps.add_argument("--commit", required=True)
    p_ps.add_argument("--sync-deletes", action="store_true")
    p_ps.add_argument("--no-sync-termux", action="store_true")
    p_ps.add_argument("--files", nargs="*")
    p_ps.add_argument("--delete", nargs="*")

    p_sm = sub.add_parser("sync-mirrors", help="Check or apply shared file mirrors across the 3 repos")
    p_sm.add_argument("--from", dest="from_repo", default="", help="Source repo alias (cavern|omniagent|fractal)")
    p_sm.add_argument("--mirror", default="all", help="Mirror name or 'all'")
    p_sm.add_argument("--apply", action="store_true", help="Actually write mirrored files (default is dry-run)")

    args = parser.parse_args(argv)
    ws = Path(args.workspace).resolve()
    mroot = Path(args.mount_root) if args.mount_root else None

    if args.warm or args.action == "hydrate":
        res = hydrate_all(
            ws,
            mount_root=mroot,
            only=getattr(args, "only", None),
            force=bool(getattr(args, "force", False)),
        )
        print(json.dumps(res, indent=2, ensure_ascii=False))
        return 0 if res.get("ok") else 1

    action = args.action or "status"
    if action == "status":
        res = inspect_federation_status(ws, mount_root=mroot, check_remote_diff=bool(getattr(args, "remote_diff", False)))
        print(json.dumps(res, indent=2, ensure_ascii=False))
        return 0

    if action == "diff":
        manifest = load_manifest(ws)
        aliases = list((manifest.get("repos") or {}).keys())
        targets = aliases if args.target == "all" else [args.target]
        tok = lcap.get_secret("GITHUB_TOKEN", aliases=("GH_TOKEN",))
        out: dict[str, Any] = {}
        for a in targets:
            spec = manifest["repos"][a]
            rpath = get_repo_path(a, ws, manifest, mroot)
            if not rpath.is_dir():
                out[a] = {"ok": False, "error": f"Not hydrated at {rpath}"}
                continue
            d = ghdirect.diff_workspace_against_github(
                rpath,
                owner_repo=spec["ownerRepo"],
                branch=spec.get("branch", "main"),
                token=tok,
                sync_deletes=True,
            )
            out[a] = {
                "ok": True,
                "ownerRepo": spec["ownerRepo"],
                "branch": spec.get("branch", "main"),
                "path": str(rpath),
                "head": d["headSha"][:8],
                "changed": d["changed"],
                "deleted": d["deleted"],
            }
        print(json.dumps({"ok": True, "diffs": out}, indent=2, ensure_ascii=False))
        return 0

    if action == "push":
        manifest = load_manifest(ws)
        aliases = list((manifest.get("repos") or {}).keys())
        targets = aliases if args.target == "all" else [args.target]
        pushed: dict[str, Any] = {}
        all_ok = True
        for a in targets:
            r = push_federated_repo(
                a,
                args.commit,
                ws,
                mount_root=mroot,
                sync_deletes=bool(args.sync_deletes),
                no_sync_termux=bool(args.no_sync_termux),
                explicit_files=args.files,
                explicit_deletes=args.delete,
            )
            pushed[a] = r
            if not r.get("ok"):
                all_ok = False
        print(json.dumps({"ok": all_ok, "pushed": pushed}, indent=2, ensure_ascii=False))
        return 0 if all_ok else 1

    if action == "sync-mirrors":
        res = sync_mirrors(
            ws,
            from_repo=args.from_repo,
            mirror_name=args.mirror,
            apply=bool(args.apply),
            mount_root=mroot,
        )
        print(json.dumps(res, indent=2, ensure_ascii=False))
        return 0 if res.get("ok") else 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
