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
import base64
import concurrent.futures
import fnmatch
import hashlib
import io
import json
import os
import shutil
import sys
import tarfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(line_buffering=True)
    except Exception:
        pass

AGENT_DIR = Path(__file__).resolve().parent
REPO_ROOT = AGENT_DIR.parents[1] if AGENT_DIR.parent.name == ".agents" else AGENT_DIR.parent
if str(AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_DIR))

import github_direct as ghdirect
import local_capabilities as lcap
import omni_protocol as proto

_STATE_LOCK = threading.Lock()

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
            "termuxGlobalPluginPath": "/root/.gemini/config/plugins/dynamic-fractal-planner",
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
    if mount_root is None and not os.environ.get("CAVERN_FEDERATION_ROOT", "").strip():
        spec = (manifest.get("repos") or {}).get(alias) or {}
        termux_path = str(spec.get("termuxPath") or "").strip()
        if termux_path and (Path(termux_path) / ".git").exists():
            return Path(termux_path).resolve()
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


_ROOT_ONLY_EXCLUDES = frozenset({".github", ".gitignore", "package.json", "AGENTS.md"})
_ANYDEPTH_EXCLUDES = frozenset({
    ".git", ".workspaces.json", ".last_good_backup", "__pycache__",
    ".metrics.jsonl", ".model_modes.json", ".hook_state.json", ".diagnostics",
})
_NON_EXEC_EXTS = (".md", ".json", ".jsonl", ".txt", ".yml", ".yaml", ".toml", ".ini", ".cfg", ".csv", ".png", ".svg")


def _desired_mirror_mode(rel_posix: str, src_path: Path) -> int:
    low = rel_posix.lower()
    if low.endswith(_NON_EXEC_EXTS) or any(part in ("docs", "rules", "skills", "fixtures") for part in low.split("/")[:-1]):
        return 0o644
    if (low.endswith(".sh") or low.startswith(("hooks/", "scripts/", "tests/"))) and os.access(src_path, os.X_OK):
        return 0o755
    return 0o644


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
        if p in _ROOT_ONLY_EXCLUDES or "/" in p:
            if fnmatch.fnmatch(rel, p):
                return True
            continue
        if any(ch in p for ch in "*?["):
            if fnmatch.fnmatch(rel, p) or any(fnmatch.fnmatch(seg, p) for seg in parts):
                return True
            continue
        if p in _ANYDEPTH_EXCLUDES:
            if p in parts:
                return True
            continue
        for seg in parts:
            if fnmatch.fnmatch(seg, p):
                return True
        if fnmatch.fnmatch(rel, p):
            return True
    return False


def compute_plugin_tree_sha256(
    plugin_root: Path,
    excludes: list[str] | tuple[str, ...] | set[str] | None = None,
) -> tuple[str, str]:
    """Return deterministic `(semver, tree_sha256)` over relative path + exec bit + file sha256."""
    root = Path(plugin_root).expanduser().resolve()
    try:
        plug_scripts = root / "scripts"
        if not (plug_scripts / "self_evolve.py").is_file():
            plug_scripts = AGENT_DIR.parent / "plugins" / "dynamic-fractal-planner" / "scripts"
        if plug_scripts.is_dir() and str(plug_scripts) not in sys.path:
            sys.path.insert(0, str(plug_scripts))
        import self_evolve  # noqa: E402

        return self_evolve.compute_plugin_tree_sha256(root, excludes=excludes)
    except Exception:
        pass
    semver = _read_plugin_version(root)
    if not root.is_dir():
        return (semver, "")
    pats = list(excludes) if excludes is not None else [
        ".git", ".github", ".gitignore", "package.json", "AGENTS.md",
        ".agents/preview", ".agents/federation.json", ".agents/bridge_profile.json",
        ".agents/env_manifest.json", ".agents/host_matrix.json", ".agents/planner_profile.json",
        ".workspaces.json", ".last_good_backup", "__pycache__", "*.pyc", "*.pyo",
        ".metrics.jsonl", ".model_modes.json", ".hook_state.json", ".diagnostics",
    ]
    files: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        cur = Path(dirpath)
        rel_dir = "" if cur == root else cur.relative_to(root).as_posix()
        dirnames[:] = sorted(d for d in dirnames if not _is_excluded(f"{rel_dir}/{d}" if rel_dir else d, pats))
        for fn in sorted(filenames):
            f_rel = f"{rel_dir}/{fn}" if rel_dir else fn
            if _is_excluded(f_rel, pats):
                continue
            p = cur / fn
            if p.is_file() and not p.is_symlink():
                files.append(p)
    if not files:
        return (semver, "")
    h = hashlib.sha256()
    for p in sorted(files, key=lambda x: x.relative_to(root).as_posix()):
        rel = p.relative_to(root).as_posix()
        file_sha = hashlib.sha256(p.read_bytes()).hexdigest()
        exec_bit = "1" if os.access(p, os.X_OK) else "0"
        h.update(f"{rel}\0{exec_bit}\0{file_sha}\n".encode("utf-8"))
    return (semver, h.hexdigest())


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
                                try:
                                    os.chmod(df, _desired_mirror_mode(rel_f, sf))
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
    termux_timeout_sec: int = 18,
    explicit_files: list[str] | None = None,
    explicit_deletes: list[str] | None = None,
    progress_cb: Callable[[str], None] | None = None,
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
                progress_cb=progress_cb,
            )
            # Update federation baseline for non-active hydrated repos so future warms see clean state
            current = detect_current_repo(ws, manifest)
            if res.get("ok") and alias != current:
                baseline_hashes = _compute_dir_hashes(repo_dir)
                with _STATE_LOCK:
                    state = _read_state(mroot)
                    st_repos = state.setdefault("repos", {})
                    st_repos[alias] = {
                        "ownerRepo": owner_repo,
                        "branch": branch,
                        "headSha": res.get("headFull") or "",
                        "hydratedUtc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                        "baselineHashes": baseline_hashes,
                    }
                    _write_state(mroot, state)

            if not no_sync_termux and spec.get("termuxPath"):
                res["termuxSync"] = _try_sync_termux_repo(spec, branch, timeout_sec=termux_timeout_sec)
            return res
        except RuntimeError as exc:
            msg_exc = str(exc)
            if not any(k in msg_exc for k in ("HTTP 401", "HTTP 403", "Bad credentials", "Resource not accessible")):
                raise
            print(
                f"WARN: Local GITHUB_TOKEN rejected/lacks write for {owner_repo} ({exc}). Falling back to OmniAgent phone git push...",
                file=sys.stderr,
                flush=True,
            )

    # Fallback: push via OmniAgent phone Termux (works even when GITHUB_TOKEN is missing or read-only!)
    return _push_via_omniagent_phone(
        repo_dir,
        spec,
        commit_message,
        explicit_files=explicit_files,
        explicit_deletes=explicit_deletes,
        sync_deletes=sync_deletes,
    )


def _build_termux_batch_sync_script(entries: list[dict[str, Any]]) -> str:
    """Build a self-contained pure-stdlib Python script that syncs all pushed repos in parallel on Termux."""
    payload_json = json.dumps(entries, separators=(",", ":"), ensure_ascii=False)
    return f"""
import concurrent.futures, hashlib, json, os, shutil, subprocess
from pathlib import Path

ENTRIES = json.loads({json.dumps(payload_json)})
GLOBAL_ROOT_EXCLUDES = {{".github", ".gitignore", "package.json", "AGENTS.md"}}
GLOBAL_ANYDEPTH_EXCLUDES = {{".git", ".workspaces.json", ".last_good_backup", "__pycache__", ".metrics.jsonl", ".model_modes.json", ".hook_state.json", ".diagnostics"}}
GLOBAL_EXCLUDES = GLOBAL_ROOT_EXCLUDES | GLOBAL_ANYDEPTH_EXCLUDES

def git_blob_sha1(raw: bytes) -> str:
    return hashlib.sha1(f"blob {{len(raw)}}\\x00".encode("ascii") + raw).hexdigest()

def sync_global_plugin(src_dir: Path, dst_dir: Path) -> dict:
    if not src_dir.is_dir():
        return {{"ok": False, "error": "source_missing"}}
    copied = 0
    for dirpath, dirnames, filenames in os.walk(src_dir):
        cur = Path(dirpath)
        rel_dir = cur.relative_to(src_dir).as_posix()
        dirnames[:] = [
            d for d in sorted(dirnames)
            if d not in GLOBAL_ANYDEPTH_EXCLUDES
            and not (rel_dir == "." and d in GLOBAL_ROOT_EXCLUDES)
            and (rel_dir != ".agents" or d != "preview")
        ]
        for fn in sorted(filenames):
            if fn in GLOBAL_ANYDEPTH_EXCLUDES or (rel_dir == "." and fn in GLOBAL_ROOT_EXCLUDES) or fn.endswith((".pyc", ".pyo")):
                continue
            rel_f = fn if rel_dir == "." else f"{{rel_dir}}/{{fn}}"
            if rel_f.startswith((".agents/preview/", ".agents/federation.json", ".agents/bridge_profile.json", ".agents/env_manifest.json", ".agents/host_matrix.json", ".agents/planner_profile.json")):
                continue
            sf = cur / fn
            df = dst_dir / rel_f
            sb = sf.read_bytes()
            if not df.is_file() or df.read_bytes() != sb:
                df.parent.mkdir(parents=True, exist_ok=True)
                df.write_bytes(sb)
                low = rel_f.lower()
                is_doc = low.endswith((".md", ".json", ".jsonl", ".txt", ".yml", ".yaml", ".toml", ".ini", ".cfg", ".csv", ".png", ".svg")) or any(pt in ("docs", "rules", "skills", "fixtures") for pt in low.split("/")[:-1])
                mode = 0o755 if (not is_doc and (low.endswith(".sh") or low.startswith(("hooks/", "scripts/", "tests/"))) and os.access(sf, os.X_OK)) else 0o644
                try:
                    os.chmod(df, mode)
                except OSError:
                    pass
                copied += 1
    return {{"ok": True, "copied": copied}}

def sync_one(ent: dict) -> dict:
    alias = ent.get("alias", "")
    repo = Path(str(ent.get("termuxPath") or "")).expanduser()
    remote = str(ent.get("remote") or "origin")
    branch = str(ent.get("branch") or "main")
    if not (repo / ".git").exists():
        return {{"alias": alias, "ok": False, "status": "not_cloned"}}
    r_fetch = subprocess.run(["git", "fetch", remote, branch], cwd=str(repo), capture_output=True, text=True, timeout=45, check=False)
    if r_fetch.returncode != 0:
        return {{"alias": alias, "ok": False, "error": (r_fetch.stderr or r_fetch.stdout)[-240:]}}

    reconciled = []
    preserved = []
    stashed = False
    if ent.get("reconcileMirror"):
        remote_ref = f"{{remote}}/{{branch}}"
        remote_blobs = {{}}
        r_tree = subprocess.run(["git", "ls-tree", "-r", "-z", remote_ref], cwd=str(repo), capture_output=True, timeout=20, check=False)
        if r_tree.returncode == 0:
            for item in r_tree.stdout.split(b"\\x00"):
                if not item or b"\\t" not in item:
                    continue
                mb, pb = item.split(b"\\t", 1)
                ps = mb.decode("utf-8", errors="ignore").split()
                if len(ps) >= 3 and ps[1] == "blob":
                    remote_blobs[pb.decode("utf-8", errors="replace").replace("\\\\", "/")] = ps[2]
        mirrored = {{}}
        st_path = Path(str(ent.get("mirrorStatePath") or "/root/.gemini/antigravity/ai_studio_bridge/flight_recorder/live_mirror_state.json")).expanduser()
        if st_path.is_file():
            try:
                loaded = json.loads(st_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict) and isinstance(loaded.get("mirrored"), dict):
                    mirrored = loaded["mirrored"]
            except Exception:
                pass
        r_st = subprocess.run(["git", "status", "--porcelain", "-z", "-uall"], cwd=str(repo), capture_output=True, timeout=15, check=False)
        if r_st.returncode == 0:
            parts = r_st.stdout.split(b"\\x00")
            i = 0
            while i < len(parts):
                row = parts[i]
                if len(row) >= 4:
                    code = row[:2].decode("utf-8", errors="ignore")
                    rel = row[3:].decode("utf-8", errors="replace").replace("\\\\", "/").lstrip("/")
                    if "R" in code or "C" in code:
                        i += 1
                    full = repo / rel
                    is_untracked = code == "??"
                    match_mirror = False
                    match_remote = False
                    if full.is_file():
                        raw = full.read_bytes()
                        if rel in mirrored and hashlib.sha256(raw).hexdigest() == mirrored[rel].get("sha256"):
                            match_mirror = True
                        if remote_blobs.get(rel) == git_blob_sha1(raw):
                            match_remote = True
                    if match_mirror or match_remote:
                        if is_untracked:
                            try:
                                full.unlink()
                            except OSError:
                                pass
                        else:
                            subprocess.run(["git", "checkout", "--", rel], cwd=str(repo), capture_output=True, timeout=10, check=False)
                        reconciled.append(rel)
                    elif rel:
                        preserved.append(rel)
                i += 1
        if preserved:
            r_sh = subprocess.run(["git", "stash", "push", "-u", "-m", "omni-ship-preserve"], cwd=str(repo), capture_output=True, text=True, timeout=20, check=False)
            stashed = r_sh.returncode == 0 and "No local changes" not in (r_sh.stdout or "")

    r_pull = subprocess.run(["git", "pull", "--ff-only", remote, branch], cwd=str(repo), capture_output=True, text=True, timeout=45, check=False)
    mode = "ff-only"
    if r_pull.returncode != 0:
        r_pull = subprocess.run(["git", "pull", "--rebase", remote, branch], cwd=str(repo), capture_output=True, text=True, timeout=45, check=False)
        mode = "rebase"
    if stashed:
        subprocess.run(["git", "stash", "pop"], cwd=str(repo), capture_output=True, text=True, timeout=20, check=False)
    r_head = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=str(repo), capture_output=True, text=True, timeout=10, check=False)
    out = {{
        "alias": alias,
        "ok": r_pull.returncode == 0,
        "mode": mode,
        "head": r_head.stdout.strip(),
        "reconciled": len(reconciled),
        "preservedManual": preserved[:10],
    }}
    if r_pull.returncode != 0:
        out["error"] = (r_pull.stderr or r_pull.stdout)[-240:]
    gpath = str(ent.get("globalPluginPath") or "").strip()
    if out["ok"] and gpath:
        out["globalSync"] = sync_global_plugin(repo, Path(gpath).expanduser())
    return out

results = {{}}
with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, len(ENTRIES))) as pool:
    futs = {{pool.submit(sync_one, e): e.get("alias", "") for e in ENTRIES}}
    for fut in concurrent.futures.as_completed(futs):
        a = futs[fut]
        try:
            results[a] = fut.result()
        except Exception as exc:
            results[a] = {{"alias": a, "ok": False, "error": str(exc)}}
print(json.dumps({{"ok": all(v.get("ok") for v in results.values()), "repos": results}}, separators=(",", ":")))
""".strip()


def sync_termux_repos_batch(
    repo_entries: list[dict[str, Any]],
    *,
    timeout_sec: int = 18,
    url_arg: str = "",
    pair_secret_arg: str = "",
    admin_key_arg: str = "",
    progress_cb: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Sync 1..N Termux repos in ONE batched parallel RPC with bounded wait (never hangs `ship`)."""
    valid = [e for e in repo_entries if str(e.get("termuxPath") or "").strip()]
    if not valid:
        return {"attempted": False, "ok": True, "reason": "no_repos"}
    t0 = time.monotonic()
    try:
        import bridge_auth as bauth
        import omni_agent_client as oac

        conn = bauth.resolve_connection(
            cli_url=url_arg,
            cli_pair_secret=pair_secret_arg,
            cli_admin_key=admin_key_arg,
        )
        if not (conn["relayUrl"] and conn["pairSecret"]):
            return {"attempted": False, "ok": True, "reason": "no_pair_secret"}
        if not oac._is_channel_online(conn["relayUrl"], conn["pairSecret"], "termux", conn["adminKey"]):
            return {"attempted": False, "ok": True, "reason": "termux_offline"}

        aliases = [str(e.get("alias") or "") for e in valid]
        script = _build_termux_batch_sync_script(valid)
        script_b64 = base64.b64encode(script.encode("utf-8")).decode("ascii")
        cmd = f"python3 -B -c \"import base64; exec(base64.b64decode('{script_b64}').decode('utf-8'))\""

        if int(timeout_sec) <= 0:
            if progress_cb:
                progress_cb(f"termux: dispatching async batch sync for {','.join(aliases)}...")
            req_id = oac.enqueue_agent_command(
                conn["relayUrl"],
                conn["pairSecret"],
                "shell_exec",
                {"command": cmd, "timeoutSec": 60},
                channel="termux",
                admin_key=conn["adminKey"],
            )
            return {
                "attempted": True,
                "ok": True,
                "mode": "async_queued",
                "requestId": req_id,
                "repos": aliases,
                "elapsedMs": int((time.monotonic() - t0) * 1000),
            }

        if progress_cb:
            progress_cb(f"termux: batch-pulling {','.join(aliases)} (max wait {timeout_sec}s)...")
        try:
            r = oac.send_agent_command(
                conn["relayUrl"],
                conn["pairSecret"],
                "shell_exec",
                {"command": cmd, "timeoutSec": 60},
                channel="termux",
                admin_key=conn["adminKey"],
                timeout_sec=int(timeout_sec),
                auto_wake=False,
            )
        except TimeoutError:
            elapsed_ms = int((time.monotonic() - t0) * 1000)
            if progress_cb:
                progress_cb(f"termux: still running after {timeout_sec}s — continuing (sync completes on phone in background)")
            return {
                "attempted": True,
                "ok": True,
                "mode": "async_queued",
                "repos": aliases,
                "elapsedMs": elapsed_ms,
                "note": f"Dispatched to Termux; continuing after {timeout_sec}s bounded wait.",
            }

        parsed = None
        if r.get("ok"):
            lines = [ln.strip() for ln in str(r.get("stdout") or "").splitlines() if ln.strip()]
            if lines:
                try:
                    parsed = json.loads(lines[-1])
                except Exception:
                    parsed = None
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        if isinstance(parsed, dict):
            return {
                "attempted": True,
                "ok": bool(parsed.get("ok")),
                "mode": "batched_sync",
                "repos": parsed.get("repos") or {},
                "elapsedMs": elapsed_ms,
            }
        return {
            "attempted": True,
            "ok": bool(r.get("ok")),
            "mode": "batched_sync",
            "output": str(r.get("stdout") or r.get("stderr") or r.get("error") or "")[-240:],
            "elapsedMs": elapsed_ms,
        }
    except Exception as exc:
        return {
            "attempted": True,
            "ok": False,
            "error": str(exc),
            "elapsedMs": int((time.monotonic() - t0) * 1000),
        }


def _try_sync_termux_repo(spec: dict[str, Any], branch: str, *, timeout_sec: int = 18) -> dict[str, Any]:
    termux_path = str(spec.get("termuxPath") or "").strip()
    if not termux_path:
        return {"attempted": False}
    owner_repo = str(spec.get("ownerRepo") or "").strip()
    alias = owner_repo.split("/")[-1].lower() if owner_repo else "repo"
    entry = {
        "alias": alias,
        "termuxPath": termux_path,
        "branch": branch,
        "remote": "origin",
        "reconcileMirror": alias == "cavern",
        "globalPluginPath": str(spec.get("termuxGlobalPluginPath") or "").strip(),
    }
    res = sync_termux_repos_batch([entry], timeout_sec=timeout_sec)
    if isinstance(res.get("repos"), dict) and alias in res["repos"]:
        sub = res["repos"][alias]
        return {
            "attempted": True,
            "ok": bool(sub.get("ok")),
            "mode": res.get("mode", "batched_sync"),
            "head": sub.get("head", ""),
            "elapsedMs": res.get("elapsedMs", 0),
            **({"error": sub["error"]} if sub.get("error") else {}),
            **({"globalSync": sub["globalSync"]} if sub.get("globalSync") else {}),
        }
    return res


def _read_plugin_version(plugin_dir: Path) -> str:
    pj = plugin_dir / "plugin.json"
    if not pj.is_file():
        return ""
    try:
        return str(json.loads(pj.read_text(encoding="utf-8")).get("version") or "").strip()
    except Exception:
        return ""


def _semver_tuple(ver: str) -> tuple[int, int, int]:
    parts = [p for p in str(ver or "").strip().lstrip("v").split(".") if p.isdigit()]
    nums = [int(p) for p in parts[:3]]
    while len(nums) < 3:
        nums.append(0)
    return (nums[0], nums[1], nums[2])


def auto_sync_plugin_fleet(
    workspace_root: Path,
    *,
    force: bool = False,
    no_sync_termux: bool = False,
    termux_timeout_sec: int = 18,
    manifest_path: Path | None = None,
    mount_root: Path | None = None,
    progress_cb: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Mirror newer embedded plugin from Cavern -> fractal & omniagent locally, then push both in PARALLEL."""
    t0 = time.monotonic()
    ws = Path(workspace_root).resolve()
    manifest = load_manifest(ws, manifest_path)
    mroot = get_mount_root(manifest, mount_root)
    cavern_plugin = ws / ".agents" / "plugins" / "dynamic-fractal-planner"
    fractal_dir = get_repo_path("fractal", ws, manifest, mroot)
    omni_dir = get_repo_path("omniagent", ws, manifest, mroot)
    if not cavern_plugin.is_dir() or not fractal_dir.is_dir():
        return {"attempted": False, "ok": True, "reason": "federation_not_hydrated", "pushedAliases": []}

    cav_ver = _read_plugin_version(cavern_plugin)
    frac_ver = _read_plugin_version(fractal_dir)
    omni_ver = _read_plugin_version(omni_dir / ".agents" / "plugins" / "dynamic-fractal-planner") if omni_dir.is_dir() else ""

    if not force and _semver_tuple(cav_ver) < _semver_tuple(frac_ver):
        return {"attempted": False, "ok": True, "reason": f"fractal_newer({frac_ver}>{cav_ver})", "pushedAliases": []}

    out: dict[str, Any] = {
        "attempted": True,
        "ok": True,
        "version": cav_ver,
        "before": {"cavern": cav_ver, "fractal": frac_ver, "omniagent": omni_ver},
        "pushes": {},
        "pushedAliases": [],
    }

    # 1. Perform BOTH local mirror copies first (<0.1s) so fractal & omniagent GitHub pushes can run in PARALLEL!
    m1 = sync_mirrors(ws, from_repo="cavern", mirror_name="fractal-embedded-plugin", apply=True, manifest_path=manifest_path, mount_root=mroot)
    c1 = sum(int(r.get("changedCount") or 0) for r in (m1.get("mirrors") or []))
    c2 = 0
    if omni_dir.is_dir():
        m2 = sync_mirrors(ws, from_repo="fractal", mirror_name="fractal-embedded-plugin-omniagent", apply=True, manifest_path=manifest_path, mount_root=mroot)
        c2 = sum(int(r.get("changedCount") or 0) for r in (m2.get("mirrors") or []))

    push_jobs: list[tuple[str, str]] = []
    if c1 > 0 or force:
        # Push fractal WITHOUT [skip ci] so ReleasePlugin.yml tests & publishes GitHub Release v<ver>
        push_jobs.append(("fractal", f"chore(plugin): sync dynamic-fractal-planner v{cav_ver}"))
    if omni_dir.is_dir() and (c2 > 0 or force or _semver_tuple(omni_ver) < _semver_tuple(cav_ver)):
        # Push omniagent WITH [skip ci] to avoid pointless APK rebuild on plugin sync
        push_jobs.append(("omniagent", f"chore(plugin): sync dynamic-fractal-planner v{cav_ver} [skip ci]"))

    if push_jobs:
        if progress_cb:
            progress_cb(f"plugin-fleet: pushing {', '.join(a for a, _ in push_jobs)} in parallel (mirrored fractal={c1}, omniagent={c2})...")
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(push_jobs)) as pool:
            futs = {
                pool.submit(
                    push_federated_repo,
                    alias,
                    msg,
                    ws,
                    manifest_path=manifest_path,
                    mount_root=mount_root,
                    sync_deletes=False,
                    no_sync_termux=True,
                    progress_cb=progress_cb,
                ): alias
                for alias, msg in push_jobs
            }
            for fut in concurrent.futures.as_completed(futs):
                alias = futs[fut]
                try:
                    res = fut.result()
                except Exception as exc:
                    res = {"ok": False, "error": str(exc)}
                out["pushes"][alias] = res
                if res.get("ok"):
                    if int(res.get("changedCount") or 0) > 0 or int(res.get("deletedCount") or 0) > 0:
                        out["pushedAliases"].append(alias)
                else:
                    out["ok"] = False

    out["pushedAliases"] = sorted(out["pushedAliases"])

    # If invoked standalone (not deferred by cmd_ship), sync all pushed repos on Termux in ONE batched RPC.
    if not no_sync_termux and out["pushedAliases"]:
        repos_cfg = manifest.get("repos") or {}
        batch_entries = [
            {
                "alias": a,
                "termuxPath": str((repos_cfg.get(a) or {}).get("termuxPath") or ""),
                "branch": str((repos_cfg.get(a) or {}).get("branch") or "main"),
                "remote": "origin",
                "reconcileMirror": False,
                "globalPluginPath": str((repos_cfg.get(a) or {}).get("termuxGlobalPluginPath") or ""),
            }
            for a in out["pushedAliases"]
        ]
        out["termuxSync"] = sync_termux_repos_batch(
            batch_entries,
            timeout_sec=termux_timeout_sec,
            progress_cb=progress_cb,
        )

    out["elapsedMs"] = int((time.monotonic() - t0) * 1000)
    return out


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

        def _diff_one(a: str) -> dict[str, Any]:
            spec = manifest["repos"][a]
            rpath = get_repo_path(a, ws, manifest, mroot)
            if not rpath.is_dir():
                return {"ok": False, "error": f"Not hydrated at {rpath}"}
            d = ghdirect.diff_workspace_against_github(
                rpath,
                owner_repo=spec["ownerRepo"],
                branch=spec.get("branch", "main"),
                token=tok,
                sync_deletes=True,
            )
            return {
                "ok": True,
                "ownerRepo": spec["ownerRepo"],
                "branch": spec.get("branch", "main"),
                "path": str(rpath),
                "head": d["headSha"][:8],
                "changed": d["changed"],
                "deleted": d["deleted"],
            }

        out: dict[str, Any] = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, len(targets))) as pool:
            futs = {pool.submit(_diff_one, a): a for a in targets}
            for fut in concurrent.futures.as_completed(futs):
                a = futs[fut]
                try:
                    out[a] = fut.result()
                except Exception as exc:
                    out[a] = {"ok": False, "error": str(exc)}
        ordered = {a: out[a] for a in targets if a in out}
        print(json.dumps({"ok": True, "diffs": ordered}, indent=2, ensure_ascii=False), flush=True)
        return 0

    if action == "push":
        manifest = load_manifest(ws)
        aliases = list((manifest.get("repos") or {}).keys())
        targets = aliases if args.target == "all" else [args.target]
        t_start = time.monotonic()

        def _cb(msg: str) -> None:
            print(f"[repos +{time.monotonic() - t_start:.1f}s] {msg}", file=sys.stderr, flush=True)

        pushed: dict[str, Any] = {}
        all_ok = True
        with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, len(targets))) as pool:
            futs = {
                pool.submit(
                    push_federated_repo,
                    a,
                    args.commit,
                    ws,
                    mount_root=mroot,
                    sync_deletes=bool(args.sync_deletes),
                    no_sync_termux=True,
                    explicit_files=args.files,
                    explicit_deletes=args.delete,
                    progress_cb=_cb,
                ): a
                for a in targets
            }
            for fut in concurrent.futures.as_completed(futs):
                a = futs[fut]
                try:
                    r = fut.result()
                except Exception as exc:
                    r = {"ok": False, "error": str(exc)}
                pushed[a] = r
                if not r.get("ok"):
                    all_ok = False

        termux_batch: dict[str, Any] | None = None
        if not bool(args.no_sync_termux):
            pushed_aliases = [
                a for a in targets
                if pushed.get(a, {}).get("ok")
                and (int(pushed[a].get("changedCount") or 0) > 0 or int(pushed[a].get("deletedCount") or 0) > 0)
            ]
            if pushed_aliases:
                repos_cfg = manifest.get("repos") or {}
                batch_entries = [
                    {
                        "alias": a,
                        "termuxPath": str((repos_cfg.get(a) or {}).get("termuxPath") or ""),
                        "branch": str((repos_cfg.get(a) or {}).get("branch") or "main"),
                        "remote": "origin",
                        "reconcileMirror": a == "cavern",
                        "globalPluginPath": str((repos_cfg.get(a) or {}).get("termuxGlobalPluginPath") or ""),
                    }
                    for a in pushed_aliases
                ]
                termux_batch = sync_termux_repos_batch(batch_entries, timeout_sec=18, progress_cb=_cb)

        ordered_pushed = {a: pushed[a] for a in targets if a in pushed}
        payload_out: dict[str, Any] = {"ok": all_ok, "pushed": ordered_pushed}
        if termux_batch is not None:
            payload_out["termuxSync"] = termux_batch
        print(json.dumps(payload_out, indent=2, ensure_ascii=False), flush=True)
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
