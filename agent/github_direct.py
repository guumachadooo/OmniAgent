#!/usr/bin/env python3
"""Direct Local GitHub Git Data API Push & CI Inspector for AI Studio.

Uses local `GITHUB_TOKEN` (resolved via `local_capabilities.py`, never logged)
to commit & push multi-file workspace diffs directly to GitHub in ~1-3 seconds
without routing through the phone, and to inspect GitHub Actions CI runs
(`Build Android APK`, etc.) directly.
"""

from __future__ import annotations

import base64
import hashlib
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

AGENT_DIR = Path(__file__).resolve().parent
REPO_ROOT = AGENT_DIR.parents[1] if AGENT_DIR.parent.name == ".agents" else AGENT_DIR.parent
if str(AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_DIR))

import local_capabilities as lcap
import omni_protocol as proto

GITHUB_API = "https://api.github.com"


def _gh_request(
    method: str,
    path_or_url: str,
    token: str,
    body: dict[str, Any] | None = None,
    timeout: int = 45,
) -> tuple[int, dict[str, Any]]:
    url = path_or_url if path_or_url.startswith("https://") else f"{GITHUB_API}{path_or_url}"
    headers = {
        "User-Agent": proto.USER_AGENT,
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data: bytes | None = None
    if body is not None:
        data = json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
    req = urllib.request.Request(url, data=data, headers=headers, method=method.upper())
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return resp.status, (json.loads(raw) if raw.strip() else {})
    except urllib.error.HTTPError as err:
        raw = err.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(raw) if raw.strip() else {"message": str(err)}
        except Exception:
            parsed = {"message": raw or str(err)}
        return err.code, parsed


def git_blob_sha1(raw: bytes) -> str:
    return hashlib.sha1(f"blob {len(raw)}\0".encode("ascii") + raw).hexdigest()


def diff_workspace_against_github(
    repo_root: Path,
    owner_repo: str = "guumachadooo/Cavern",
    branch: str = "master",
    token: str = "",
    explicit_files: list[str] | None = None,
    explicit_deletes: list[str] | None = None,
    sync_deletes: bool = False,
) -> dict[str, Any]:
    """Compare local workspace against GitHub branch HEAD using Git blob SHA-1."""
    tok = token or lcap.get_secret("GITHUB_TOKEN", aliases=("GH_TOKEN",))
    st_ref, ref_data = _gh_request("GET", f"/repos/{owner_repo}/git/ref/heads/{urllib.parse.quote(branch)}", tok)
    if st_ref != 200 or "object" not in ref_data:
        raise RuntimeError(f"Failed reading ref heads/{branch} (HTTP {st_ref}): {ref_data.get('message', ref_data)}")
    head_sha = str(ref_data["object"]["sha"])

    st_commit, commit_data = _gh_request("GET", f"/repos/{owner_repo}/git/commits/{head_sha}", tok)
    if st_commit != 200 or "tree" not in commit_data:
        raise RuntimeError(f"Failed reading commit {head_sha} (HTTP {st_commit}): {commit_data}")
    base_tree_sha = str(commit_data["tree"]["sha"])

    st_tree, tree_data = _gh_request("GET", f"/repos/{owner_repo}/git/trees/{base_tree_sha}?recursive=1", tok)
    if st_tree != 200 or "tree" not in tree_data:
        raise RuntimeError(f"Failed reading tree {base_tree_sha} (HTTP {st_tree}): {tree_data}")
    if tree_data.get("truncated"):
        raise RuntimeError("GitHub recursive tree response was truncated; refusing partial diff.")

    gh_blobs: dict[str, tuple[str, str]] = {}
    for node in tree_data["tree"]:
        if node.get("type") == "blob":
            rel = str(node.get("path") or "")
            gh_blobs[rel] = (str(node.get("sha") or ""), str(node.get("mode") or "100644"))

    changed: list[str] = []
    deleted: list[str] = list(explicit_deletes or [])

    if explicit_files:
        for rel in explicit_files:
            clean = rel.replace("\\", "/").lstrip("/")
            if not proto.should_include_workspace_path(clean):
                continue
            full = repo_root / clean
            if not full.is_file():
                continue
            lsha = git_blob_sha1(full.read_bytes())
            if gh_blobs.get(clean, ("", ""))[0] != lsha:
                changed.append(clean)
    else:
        local_manifest = proto.build_workspace_manifest(repo_root)
        for rel in sorted(local_manifest.keys()):
            full = repo_root / rel
            lsha = git_blob_sha1(full.read_bytes())
            if gh_blobs.get(rel, ("", ""))[0] != lsha:
                changed.append(rel)
        if sync_deletes:
            for rel in sorted(gh_blobs.keys()):
                if not proto.should_include_workspace_path(rel):
                    continue
                if rel not in local_manifest and rel not in deleted:
                    deleted.append(rel)

    return {
        "headSha": head_sha,
        "baseTreeSha": base_tree_sha,
        "ghBlobs": gh_blobs,
        "changed": changed,
        "deleted": deleted,
    }


def push_workspace_direct(
    repo_root: Path,
    commit_message: str,
    *,
    owner_repo: str = "guumachadooo/Cavern",
    branch: str = "master",
    token: str = "",
    explicit_files: list[str] | None = None,
    explicit_deletes: list[str] | None = None,
    sync_deletes: bool = False,
) -> dict[str, Any]:
    """Create an atomic multi-file commit on GitHub directly via Git Data API."""
    tok = token or lcap.get_secret("GITHUB_TOKEN", aliases=("GH_TOKEN",))
    if not tok:
        raise RuntimeError("Missing GITHUB_TOKEN for direct local GitHub push.")
    if not commit_message.strip():
        raise ValueError("Commit message is required for direct GitHub push.")

    diff = diff_workspace_against_github(
        repo_root,
        owner_repo=owner_repo,
        branch=branch,
        token=tok,
        explicit_files=explicit_files,
        explicit_deletes=explicit_deletes,
        sync_deletes=sync_deletes,
    )
    changed: list[str] = diff["changed"]
    deleted: list[str] = diff["deleted"]
    gh_blobs: dict[str, tuple[str, str]] = diff["ghBlobs"]

    if not changed and not deleted:
        return {
            "ok": True,
            "via": "local_github_api",
            "head": diff["headSha"][:8],
            "headFull": diff["headSha"],
            "changed": [],
            "deleted": [],
            "message": "Workspace already 100% identical to GitHub HEAD (0 changed files).",
        }

    tree_entries: list[dict[str, Any]] = []
    for rel in changed:
        full = repo_root / rel
        raw = full.read_bytes()
        st_b, b_resp = _gh_request(
            "POST",
            f"/repos/{owner_repo}/git/blobs",
            tok,
            {"content": base64.b64encode(raw).decode("ascii"), "encoding": "base64"},
        )
        if st_b not in (200, 201) or not b_resp.get("sha"):
            raise RuntimeError(f"Failed creating blob for {rel} (HTTP {st_b}): {b_resp}")
        mode = gh_blobs.get(rel, ("", "100644"))[1]
        if mode not in ("100644", "100755"):
            mode = "100755" if rel.endswith(".sh") else "100644"
        tree_entries.append({"path": rel, "mode": mode, "type": "blob", "sha": b_resp["sha"]})

    for rel in deleted:
        if rel in gh_blobs:
            tree_entries.append({"path": rel, "mode": "100644", "type": "blob", "sha": None})

    st_t, t_resp = _gh_request(
        "POST",
        f"/repos/{owner_repo}/git/trees",
        tok,
        {"base_tree": diff["baseTreeSha"], "tree": tree_entries},
    )
    if st_t not in (200, 201) or not t_resp.get("sha"):
        raise RuntimeError(f"Failed creating Git tree (HTTP {st_t}): {t_resp}")

    st_c, c_resp = _gh_request(
        "POST",
        f"/repos/{owner_repo}/git/commits",
        tok,
        {
            "message": commit_message.strip(),
            "tree": t_resp["sha"],
            "parents": [diff["headSha"]],
        },
    )
    if st_c not in (200, 201) or not c_resp.get("sha"):
        raise RuntimeError(f"Failed creating Git commit (HTTP {st_c}): {c_resp}")
    new_sha = str(c_resp["sha"])

    st_u, u_resp = _gh_request(
        "PATCH",
        f"/repos/{owner_repo}/git/refs/heads/{urllib.parse.quote(branch)}",
        tok,
        {"sha": new_sha, "force": False},
    )
    if st_u != 200:
        raise RuntimeError(f"Failed updating ref heads/{branch} to {new_sha[:8]} (HTTP {st_u}): {u_resp}")

    return {
        "ok": True,
        "via": "local_github_api",
        "ownerRepo": owner_repo,
        "branch": branch,
        "previousHead": diff["headSha"][:8],
        "head": new_sha[:8],
        "headFull": new_sha,
        "changedCount": len(changed),
        "deletedCount": len(deleted),
        "changed": changed,
        "deleted": deleted,
        "commitUrl": f"https://github.com/{owner_repo}/commit/{new_sha}",
    }


def fetch_ci_runs(
    *,
    owner_repo: str = "guumachadooo/Cavern",
    workflow: str = "BuildAndroid.yml",
    limit: int = 5,
    token: str = "",
) -> dict[str, Any]:
    """Fetch recent GitHub Actions workflow runs directly via local GITHUB_TOKEN."""
    tok = token or lcap.get_secret("GITHUB_TOKEN", aliases=("GH_TOKEN",))
    st, data = _gh_request(
        "GET",
        f"/repos/{owner_repo}/actions/workflows/{urllib.parse.quote(workflow)}/runs?per_page={max(1, min(20, int(limit)))}",
        tok,
    )
    if st != 200:
        return {"ok": False, "http": st, "error": data.get("message", data)}
    runs = []
    for r in data.get("workflow_runs") or []:
        runs.append(
            {
                "id": r.get("id"),
                "runNumber": r.get("run_number"),
                "status": r.get("status"),
                "conclusion": r.get("conclusion"),
                "headSha": str(r.get("head_sha") or "")[:8],
                "title": r.get("display_title"),
                "createdAt": r.get("created_at"),
                "updatedAt": r.get("updated_at"),
                "htmlUrl": r.get("html_url"),
            }
        )
    return {"ok": True, "via": "local_github_api" if tok else "public_github_api", "workflow": workflow, "runs": runs}


_EPHEMERAL_WORKSPACE_FILES: tuple[str, ...] = (
    ".agents/.bridge_vault.json",
    ".agents/plugins/dynamic-fractal-planner/.metrics.jsonl",
    ".agents/plugins/dynamic-fractal-planner/.model_modes.json",
)
_BUILD_ROOT_PREFIXES: tuple[str, ...] = ("Cavern/", "Cavern.Format/", "CavernSamples/")


def clean_workspace_artifacts(repo_root: Path) -> dict[str, Any]:
    """Remove generated caches/build outputs/runtime dotfiles so workspace matches GitHub 1:1."""
    import os
    import shutil

    root = Path(repo_root).resolve()
    removed_files: list[str] = []
    removed_dirs: list[str] = []

    # Migrate legacy workspace vault to /tmp before removing it so credentials are never lost
    legacy_vault = root / ".agents" / ".bridge_vault.json"
    tmp_vault = Path("/tmp/.cavern_bridge_vault.json")
    if legacy_vault.is_file() and not tmp_vault.is_file():
        try:
            tmp_vault.write_bytes(legacy_vault.read_bytes())
            os.chmod(tmp_vault, 0o600)
        except OSError:
            pass

    for rel in _EPHEMERAL_WORKSPACE_FILES:
        p = root / rel
        if p.is_file():
            try:
                p.unlink()
                removed_files.append(rel)
            except OSError:
                pass

    for dirpath, dirnames, filenames in os.walk(root, topdown=True):
        cur_dir = Path(dirpath)
        rel_dir = cur_dir.relative_to(root).as_posix()
        keep_dirs: list[str] = []
        for dn in dirnames:
            if dn in (".git", "node_modules"):
                continue
            child = cur_dir / dn
            child_rel = f"{rel_dir}/{dn}" if rel_dir != "." else dn
            is_build_dir = dn in ("bin", "obj") and any(child_rel.startswith(pref) for pref in _BUILD_ROOT_PREFIXES)
            if dn == "__pycache__" or is_build_dir:
                try:
                    shutil.rmtree(child)
                    removed_dirs.append(child_rel)
                except OSError:
                    pass
                continue
            keep_dirs.append(dn)
        dirnames[:] = keep_dirs

        for fn in filenames:
            if fn.endswith((".pyc", ".pyo")):
                fp = cur_dir / fn
                try:
                    fp.unlink()
                    removed_files.append(fp.relative_to(root).as_posix())
                except OSError:
                    pass

    return {
        "removedFiles": sorted(removed_files),
        "removedDirs": sorted(removed_dirs),
        "removedCount": len(removed_files) + len(removed_dirs),
    }


def verify_github_parity(
    repo_root: Path,
    *,
    owner_repo: str = "guumachadooo/Cavern",
    branch: str = "master",
    token: str = "",
    clean: bool = False,
) -> dict[str, Any]:
    """Verify exact 1:1 full-workspace parity (every file on disk except .git/node_modules) vs GitHub HEAD."""
    import os

    root = Path(repo_root).resolve()
    cleaned = clean_workspace_artifacts(root) if clean else None

    diff = diff_workspace_against_github(
        root,
        owner_repo=owner_repo,
        branch=branch,
        token=token,
        sync_deletes=True,
    )
    gh_blobs: dict[str, tuple[str, str]] = diff["ghBlobs"]

    local_all: dict[str, str] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [dn for dn in dirnames if dn not in (".git", "node_modules")]
        for fn in filenames:
            fp = Path(dirpath) / fn
            rel = fp.relative_to(root).as_posix()
            local_all[rel] = git_blob_sha1(fp.read_bytes())

    local_keys = set(local_all.keys())
    gh_keys = set(gh_blobs.keys())

    modified = sorted(k for k in (local_keys & gh_keys) if local_all[k] != gh_blobs[k][0])
    local_only = sorted(local_keys - gh_keys)
    remote_only = sorted(gh_keys - local_keys)
    is_exact = (not modified) and (not local_only) and (not remote_only)

    return {
        "ok": is_exact,
        "ownerRepo": owner_repo,
        "branch": branch,
        "head": diff["headSha"][:8],
        "headFull": diff["headSha"],
        "trackedCount": len(gh_keys),
        "localCount": len(local_keys),
        "modifiedCount": len(modified),
        "localOnlyCount": len(local_only),
        "remoteOnlyCount": len(remote_only),
        "modified": modified,
        "localOnly": local_only,
        "remoteOnly": remote_only,
        "cleaned": cleaned,
        "message": (
            f"100% EXACT 1:1 PARITY with GitHub {owner_repo}@{branch} ({diff['headSha'][:8]}): 0 modified, 0 local-only, 0 remote-only."
            if is_exact
            else f"PARITY DRIFT vs {owner_repo}@{branch} ({diff['headSha'][:8]}): {len(modified)} modified, {len(local_only)} local-only, {len(remote_only)} remote-only."
        ),
    }

