#!/usr/bin/env python3
"""OmniAgent v4 Shared Zero-Trust E2E Protocol, Chunked Blob Transfer & Delta Bundle Codec.

Pure Python 3 stdlib (zero pip dependencies).
Bit-for-bit crypto parity with:
  - `.agents/docs/cloudflare_live_relay_worker.js` (v4)
  - `CavernSamples/CavernizeAndroid/Services/LiveHotPatchHost.cs`
  - `CavernSamples/OmniAgentAndroid/Services/OmniCrypto.cs`
"""

from __future__ import annotations

import base64
import gzip
import hashlib
import hmac
import json
import os
import re
import secrets
import struct
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

USER_AGENT = "OmniAgent-Bridge/4.0"
CHUNK_RAW_CHARS = 512 * 1024  # 512 KB per KV chunk (comfortably under 1 MB Worker body cap)

SKIP_SYNC_DIRS = {
    ".git",
    "node_modules",
    "bin",
    "obj",
    "__pycache__",
    ".playwright-cli",
    ".vs",
    ".idea",
    "NativeLibs",
    ".aistudio",
}
SKIP_SYNC_PREFIXES = (
    "public/assets/aistudio/",
)
SKIP_SYNC_FILES = {
    ".metrics.jsonl",
    ".model_modes.json",
    ".diagnostics",
    ".hook_state.json",
    ".bridge_vault.json",
    ".env",
    ".env.local",
}
SKIP_SYNC_EXTS = {
    ".so",
    ".apk",
    ".aab",
    ".onnx",
    ".dll",
    ".exe",
    ".pdb",
    ".pyc",
    ".pyo",
    ".zip",
    ".tar",
    ".gz",
    ".tgz",
    ".wav",
    ".flac",
    ".mp3",
    ".mp4",
    ".mkv",
    ".m4a",
}


def validate_https_url(raw_url: str) -> str:
    clean = (raw_url or "").strip().rstrip("/")
    parsed = urllib.parse.urlparse(clean)
    if parsed.scheme != "https" or not parsed.netloc:
        raise ValueError("Relay URL must use https:// with a valid public host.")
    host = (parsed.hostname or "").lower()
    if host in ("localhost", "127.0.0.1", "::1") or host.endswith(".local"):
        raise ValueError("Relay URL must be a public HTTPS endpoint.")
    return clean


def normalize_pair_secret_hex(raw_secret: str) -> str:
    s = (raw_secret or "").strip()
    s = re.sub(r"^(?:CAVPAIR|OMNIPAIR|PAIR)[-_:]?", "", s, flags=re.IGNORECASE)
    s = re.sub(r"[\s\-_]", "", s).lower()
    if len(s) != 64 or not re.fullmatch(r"[0-9a-f]{64}", s):
        raise ValueError("Pairing secret must be 64 hexadecimal characters (256-bit key).")
    return s


def compute_device_hash(pair_secret_hex: str) -> str:
    norm = normalize_pair_secret_hex(pair_secret_hex)
    return hashlib.sha256(f"wake-channel-v2:{norm}".encode("utf-8")).hexdigest()


def compute_agent_key(pair_secret_hex: str) -> str:
    norm = normalize_pair_secret_hex(pair_secret_hex)
    return hashlib.sha256(f"agent-access-v4:{norm}".encode("utf-8")).hexdigest()


def compute_knock_channel_id(pair_secret_hex: str) -> str:
    """Public double-hashed Smart Knock mailbox ID (cannot derive device_hash or pair_secret)."""
    dev_hash = compute_device_hash(pair_secret_hex)
    return hashlib.sha256(f"knock-channel-v5:{dev_hash}".encode("utf-8")).hexdigest()


# ==================== PURE-PYTHON NIST P-256 ECDH & SMART KNOCK v5 ====================
# Exact SEC 1 uncompressed point format (0x04 || X(32) || Y(32)) matching .NET ECDiffieHellman(nistP256).

_P256_P = 0xFFFFFFFF00000001000000000000000000000000FFFFFFFFFFFFFFFFFFFFFFFF
_P256_A = 0xFFFFFFFF00000001000000000000000000000000FFFFFFFFFFFFFFFFFFFFFFFC
_P256_B = 0x5AC635D8AA3A93E7B3EBBD55769886BC651D06B0CC53B0F63BCE3C3E27D2604B
_P256_N = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551
_P256_GX = 0x6B17D1F2E12C4247F8BCE6E563A440F277037D812DEB33A0F4A13945D898C296
_P256_GY = 0x4FE342E2FE1A7F9B8EE7EB4A7C0F9E162BCE33576B315ECECBB6406837BF51F5
_SAS_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def _p256_is_on_curve(x: int, y: int) -> bool:
    if not (0 <= x < _P256_P and 0 <= y < _P256_P):
        return False
    return (y * y - (x * x * x + _P256_A * x + _P256_B)) % _P256_P == 0


def _p256_point_add(p1: tuple[int, int] | None, p2: tuple[int, int] | None) -> tuple[int, int] | None:
    if p1 is None:
        return p2
    if p2 is None:
        return p1
    x1, y1 = p1
    x2, y2 = p2
    if x1 == x2:
        if (y1 + y2) % _P256_P == 0:
            return None
        lam = ((3 * x1 * x1 + _P256_A) * pow(2 * y1, _P256_P - 2, _P256_P)) % _P256_P
    else:
        lam = ((y2 - y1) * pow((x2 - x1) % _P256_P, _P256_P - 2, _P256_P)) % _P256_P
    x3 = (lam * lam - x1 - x2) % _P256_P
    y3 = (lam * (x1 - x3) - y1) % _P256_P
    return (x3, y3)


def _p256_scalar_mul(k: int, point: tuple[int, int]) -> tuple[int, int] | None:
    if k % _P256_N == 0:
        return None
    k = k % _P256_N
    result: tuple[int, int] | None = None
    addend: tuple[int, int] | None = point
    while k > 0:
        if k & 1:
            result = _p256_point_add(result, addend)
        addend = _p256_point_add(addend, addend)
        k >>= 1
    return result


def p256_generate_keypair() -> tuple[int, bytes]:
    """Generate ephemeral NIST P-256 (priv_int, sec1_uncompressed_65bytes)."""
    while True:
        priv = secrets.randbelow(_P256_N - 1) + 1
        pub = _p256_scalar_mul(priv, (_P256_GX, _P256_GY))
        if pub is not None:
            return priv, b"\x04" + pub[0].to_bytes(32, "big") + pub[1].to_bytes(32, "big")


def p256_encode_public_key(pub_bytes: bytes) -> str:
    if len(pub_bytes) != 65 or pub_bytes[0] != 0x04:
        raise ValueError("Expected 65-byte SEC1 uncompressed P-256 public key.")
    return base64.b64encode(pub_bytes).decode("ascii")


def p256_decode_public_key(pub_b64: str) -> tuple[int, int]:
    raw = base64.b64decode((pub_b64 or "").strip())
    if len(raw) != 65 or raw[0] != 0x04:
        raise ValueError("Invalid P-256 SEC1 public key encoding.")
    x = int.from_bytes(raw[1:33], "big")
    y = int.from_bytes(raw[33:65], "big")
    if not _p256_is_on_curve(x, y):
        raise ValueError("P-256 public key point is not on curve.")
    return (x, y)


def p256_ecdh_shared_secret(priv_int: int, peer_pub_b64: str) -> bytes:
    """Compute raw 32-byte ECDH X-coordinate shared secret matching .NET DeriveRawSecretAgreement."""
    peer_pt = p256_decode_public_key(peer_pub_b64)
    shared = _p256_scalar_mul(priv_int, peer_pt)
    if shared is None:
        raise ValueError("ECDH shared point at infinity.")
    return shared[0].to_bytes(32, "big")


def compute_knock_sas(knock_id: str, knock_req_id: str, client_pub_b64: str) -> str:
    """Compute 6-character visual verification code (XXX-XXX) bound to client's ephemeral public key."""
    canonical = f"knock-sas-v5|{knock_id.strip().lower()}|{knock_req_id.strip()}|{client_pub_b64.strip()}"
    digest = hashlib.sha256(canonical.encode("utf-8")).digest()
    chars = [_SAS_ALPHABET[digest[i] & 0x1F] for i in range(6)]
    return f"{''.join(chars[:3])}-{''.join(chars[3:])}"


def derive_knock_shared_key_hex(shared_x_bytes: bytes, knock_req_id: str) -> str:
    """Derive 32-byte (64-hex) symmetric key from raw ECDH shared X-coordinate + knockReqId."""
    if len(shared_x_bytes) != 32:
        raise ValueError("Expected 32-byte raw P-256 shared secret.")
    return hashlib.sha256(b"ecdh-knock-v5|" + knock_req_id.strip().encode("utf-8") + b"|" + shared_x_bytes).hexdigest()


def encrypt_knock_grant(priv_int: int, peer_pub_b64: str, knock_req_id: str, grant_obj: Any) -> dict[str, Any]:
    shared_x = p256_ecdh_shared_secret(priv_int, peer_pub_b64)
    sym_hex = derive_knock_shared_key_hex(shared_x, knock_req_id)
    return encrypt_envelope(sym_hex, f"knock:{knock_req_id}", grant_obj)


def decrypt_knock_grant(priv_int: int, peer_pub_b64: str, knock_req_id: str, envelope: dict[str, Any]) -> Any:
    shared_x = p256_ecdh_shared_secret(priv_int, peer_pub_b64)
    sym_hex = derive_knock_shared_key_hex(shared_x, knock_req_id)
    return decrypt_envelope(sym_hex, f"knock:{knock_req_id}", envelope)


def derive_agent_headers(
    pair_secret_hex: str,
    channel: str = "termux",
    admin_key: str = "",
) -> dict[str, str]:
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
        "X-Device-Hash": compute_device_hash(pair_secret_hex),
        "X-Agent-Key": compute_agent_key(pair_secret_hex),
        "X-Agent-Channel": (channel or "termux").strip().lower(),
    }
    if admin_key:
        headers["X-Admin-Key"] = admin_key.strip()
    return headers


def _ctr_keystream(enc_key: bytes, nonce: bytes, length: int) -> bytes:
    out = bytearray()
    counter = 0
    while len(out) < length:
        block = hashlib.sha256(enc_key + nonce + struct.pack(">I", counter)).digest()
        out.extend(block)
        counter += 1
    return bytes(out[:length])


def encrypt_envelope(
    pair_secret_hex: str,
    context_id: str,
    payload_obj: Any,
    nonce: bytes | None = None,
) -> dict[str, Any]:
    """Encrypt-then-MAC (SHA256-CTR + HMAC-SHA256) v4 envelope for commands, results & blobs."""
    key = bytes.fromhex(normalize_pair_secret_hex(pair_secret_hex))
    iv = nonce if nonce is not None else secrets.token_bytes(16)
    if len(iv) != 16:
        raise ValueError("Nonce must be 16 bytes.")
    enc_key = hmac.new(key, b"omni-enc-v4:" + iv, hashlib.sha256).digest()
    mac_key = hmac.new(key, b"omni-mac-v4:" + iv, hashlib.sha256).digest()
    raw_plain = json.dumps(payload_obj, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    compressed = gzip.compress(raw_plain, compresslevel=6)
    use_gz = len(compressed) < len(raw_plain)
    plain = compressed if use_gz else raw_plain
    ks = _ctr_keystream(enc_key, iv, len(plain))
    ct = bytes(a ^ b for a, b in zip(plain, ks))
    gz_flag = b"1" if use_gz else b"0"
    mac_input = b"omni-v4|" + context_id.encode("utf-8") + b"|" + gz_flag + b"|" + iv + ct
    tag = hmac.new(mac_key, mac_input, hashlib.sha256).digest()
    return {
        "v": 4,
        "gz": use_gz,
        "nonce": base64.b64encode(iv).decode("ascii"),
        "ct": base64.b64encode(ct).decode("ascii"),
        "tag": base64.b64encode(tag).decode("ascii"),
    }


def decrypt_envelope(
    pair_secret_hex: str,
    context_id: str,
    envelope: dict[str, Any],
) -> Any:
    """Verify HMAC-SHA256 tag before decrypting v4 envelope."""
    if not isinstance(envelope, dict) or int(envelope.get("v") or 0) != 4:
        raise ValueError("Unsupported OmniAgent envelope version (expected v4).")
    key = bytes.fromhex(normalize_pair_secret_hex(pair_secret_hex))
    iv = base64.b64decode(envelope["nonce"])
    ct = base64.b64decode(envelope["ct"])
    tag = base64.b64decode(envelope["tag"])
    use_gz = bool(envelope.get("gz", False))
    gz_flag = b"1" if use_gz else b"0"
    mac_key = hmac.new(key, b"omni-mac-v4:" + iv, hashlib.sha256).digest()
    mac_input = b"omni-v4|" + context_id.encode("utf-8") + b"|" + gz_flag + b"|" + iv + ct
    expected_tag = hmac.new(mac_key, mac_input, hashlib.sha256).digest()
    if not hmac.compare_digest(tag, expected_tag):
        raise ValueError("Envelope HMAC verification failed (wrong pairing secret or tampered payload).")
    enc_key = hmac.new(key, b"omni-enc-v4:" + iv, hashlib.sha256).digest()
    ks = _ctr_keystream(enc_key, iv, len(ct))
    plain = bytes(a ^ b for a, b in zip(ct, ks))
    if use_gz:
        plain = gzip.decompress(plain)
    return json.loads(plain.decode("utf-8"))


def sign_hot_exec(
    pair_secret_hex: str,
    request_id: str,
    timestamp_unix: int,
    dll_bytes: bytes,
) -> tuple[str, str]:
    """Compute (dllSha256Hex, hmacSigHex) matching LiveHotPatchHost.cs verification."""
    key = bytes.fromhex(normalize_pair_secret_hex(pair_secret_hex))
    dll_sha = hashlib.sha256(dll_bytes).hexdigest()
    canonical = f"hot-exec-v1|{request_id}|{timestamp_unix}|{dll_sha}"
    sig = hmac.new(key, canonical.encode("utf-8"), hashlib.sha256).hexdigest()
    return dll_sha, sig


def http_json(
    method: str,
    url: str,
    headers: dict[str, str],
    body_obj: dict[str, Any] | None = None,
    timeout: int = 30,
) -> tuple[int, dict[str, Any]]:
    data = None
    req_headers = dict(headers)
    if body_obj is not None:
        data = json.dumps(body_obj, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        req_headers["Content-Type"] = "application/json; charset=utf-8"
    req = urllib.request.Request(url, data=data, headers=req_headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return resp.status, (json.loads(raw) if raw.strip() else {})
    except urllib.error.HTTPError as err:
        raw = err.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(raw) if raw.strip() else {"ok": False, "error": str(err)}
        except Exception:
            parsed = {"ok": False, "error": raw or str(err)}
        return err.code, parsed


def upload_encrypted_blob(
    relay_url: str,
    pair_secret_hex: str,
    payload_obj: Any,
    admin_key: str = "",
    blob_id: str | None = None,
    meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Encrypt payload_obj and upload in <=512KB chunks to Cloudflare /v1/blob/*."""
    base = validate_https_url(relay_url)
    bid = blob_id or f"b_{int(time.time())}_{secrets.token_hex(6)}"
    env = encrypt_envelope(pair_secret_hex, f"blob:{bid}", payload_obj)
    serialized = json.dumps(env, separators=(",", ":"), ensure_ascii=False)
    total_bytes = len(serialized.encode("utf-8"))
    sha = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
    chunks = [serialized[i : i + CHUNK_RAW_CHARS] for i in range(0, len(serialized), CHUNK_RAW_CHARS)] or [""]
    headers = derive_agent_headers(pair_secret_hex, channel="termux", admin_key=admin_key)
    for idx, ch in enumerate(chunks):
        st, resp = http_json(
            "POST",
            f"{base}/v1/blob/chunk",
            headers,
            {"blobId": bid, "index": idx, "data": ch},
            timeout=45,
        )
        if st >= 400 or not resp.get("ok"):
            raise RuntimeError(f"Blob chunk {idx} upload failed (HTTP {st}): {resp}")
    commit_body: dict[str, Any] = {"blobId": bid, "totalChunks": len(chunks), "sha256": sha, "bytes": total_bytes}
    if meta:
        commit_body["meta"] = meta
    st, meta_resp = http_json(
        "POST",
        f"{base}/v1/blob/commit",
        headers,
        commit_body,
        timeout=30,
    )
    if st >= 400 or not meta_resp.get("ok"):
        raise RuntimeError(f"Blob commit failed (HTTP {st}): {meta_resp}")
    return {"blobId": bid, "totalChunks": len(chunks), "sha256": sha, "bytes": total_bytes}


def download_encrypted_blob(
    relay_url: str,
    pair_secret_hex: str,
    blob_id: str,
    admin_key: str = "",
    delete_after: bool = True,
) -> Any:
    """Download chunked E2E-encrypted blob from Cloudflare /v1/blob/* and decrypt."""
    base = validate_https_url(relay_url)
    headers = derive_agent_headers(pair_secret_hex, channel="termux", admin_key=admin_key)
    st, meta_resp = http_json("GET", f"{base}/v1/blob/meta?blobId={urllib.parse.quote(blob_id)}", headers, timeout=30)
    if st >= 400 or not meta_resp.get("ok"):
        raise RuntimeError(f"Blob meta lookup failed (HTTP {st}): {meta_resp}")
    meta = meta_resp["meta"]
    total = int(meta["totalChunks"])
    parts: list[str] = []
    for idx in range(total):
        st, ch_resp = http_json(
            "GET",
            f"{base}/v1/blob/chunk?blobId={urllib.parse.quote(blob_id)}&index={idx}",
            headers,
            timeout=45,
        )
        if st >= 400 or not ch_resp.get("ok"):
            raise RuntimeError(f"Blob chunk {idx} download failed (HTTP {st}): {ch_resp}")
        parts.append(ch_resp["data"])
    serialized = "".join(parts)
    actual_sha = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
    expected_sha = str(meta.get("sha256") or "")
    if expected_sha and actual_sha != expected_sha:
        raise ValueError(f"Blob SHA-256 mismatch: {actual_sha} != {expected_sha}")
    if delete_after:
        try:
            http_json("DELETE", f"{base}/v1/blob/delete?blobId={urllib.parse.quote(blob_id)}", headers, timeout=15)
        except Exception:
            pass
    env = json.loads(serialized)
    return decrypt_envelope(pair_secret_hex, f"blob:{blob_id}", env)


def should_include_workspace_path(rel_posix: str) -> bool:
    rel = rel_posix.replace("\\", "/").lstrip("/")
    if not rel:
        return False
    for prefix in SKIP_SYNC_PREFIXES:
        if rel.startswith(prefix) or rel == prefix.rstrip("/"):
            return False
    parts = rel.split("/")
    for p in parts[:-1]:
        if p in SKIP_SYNC_DIRS:
            return False
    name = parts[-1]
    if name in SKIP_SYNC_FILES:
        return False
    ext = Path(name).suffix.lower()
    if ext in SKIP_SYNC_EXTS:
        return False
    return True


def build_workspace_manifest(root_dir: Path, max_file_bytes: int = 2 * 1024 * 1024) -> dict[str, dict[str, Any]]:
    """Build deterministic {rel_posix: {sha256, size}} manifest for 2-way sync."""
    manifest: dict[str, dict[str, Any]] = {}
    root = root_dir.resolve()
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_SYNC_DIRS)
        for fn in sorted(filenames):
            if fn in SKIP_SYNC_FILES:
                continue
            full = Path(dirpath) / fn
            try:
                if full.is_symlink():
                    continue
                rel = full.relative_to(root).as_posix()
                if not should_include_workspace_path(rel):
                    continue
                st = full.stat()
                if st.st_size > max_file_bytes:
                    continue
                data = full.read_bytes()
                manifest[rel] = {
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "size": len(data),
                }
            except OSError:
                continue
    return manifest


def pack_delta_bundle(
    root_dir: Path,
    changed_paths: list[str],
    deleted_paths: list[str] | None = None,
) -> dict[str, Any]:
    """Pack changed/added/deleted workspace files into a verified delta bundle dict."""
    root = root_dir.resolve()
    files_out: list[dict[str, Any]] = []
    for rel in sorted(set(changed_paths)):
        norm = rel.replace("\\", "/").lstrip("/")
        if ".." in Path(norm).parts or not should_include_workspace_path(norm):
            continue
        full = (root / norm).resolve()
        try:
            full.relative_to(root)
        except ValueError:
            continue
        if not full.is_file():
            continue
        raw = full.read_bytes()
        gz = gzip.compress(raw, compresslevel=6)
        files_out.append({
            "path": norm,
            "sha256": hashlib.sha256(raw).hexdigest(),
            "size": len(raw),
            "gz_b64": base64.b64encode(gz).decode("ascii"),
            "exec": bool(os.access(full, os.X_OK)),
        })
    clean_deletes = []
    for rel in sorted(set(deleted_paths or [])):
        norm = rel.replace("\\", "/").lstrip("/")
        if norm and ".." not in Path(norm).parts and should_include_workspace_path(norm):
            clean_deletes.append(norm)
    return {
        "format": "omni-delta-v1",
        "files": files_out,
        "deleted": clean_deletes,
    }


def apply_delta_bundle(
    root_dir: Path,
    bundle: dict[str, Any],
    allow_delete: bool = True,
) -> dict[str, Any]:
    """Verify per-file SHA-256 and apply delta bundle safely inside root_dir."""
    if not isinstance(bundle, dict) or bundle.get("format") != "omni-delta-v1":
        raise ValueError("Invalid delta bundle format (expected omni-delta-v1).")
    root = root_dir.resolve()
    written: list[str] = []
    deleted: list[str] = []
    for entry in bundle.get("files", []):
        rel = str(entry.get("path", "")).replace("\\", "/").lstrip("/")
        if not rel or ".." in Path(rel).parts:
            raise ValueError(f"Unsafe path in bundle: {rel}")
        target = (root / rel).resolve()
        target.relative_to(root)  # raises ValueError on traversal
        raw = gzip.decompress(base64.b64decode(entry["gz_b64"]))
        actual_sha = hashlib.sha256(raw).hexdigest()
        expected_sha = str(entry.get("sha256", ""))
        if expected_sha and actual_sha != expected_sha:
            raise ValueError(f"SHA-256 mismatch on '{rel}': {actual_sha} != {expected_sha}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
        if entry.get("exec"):
            try:
                os.chmod(target, 0o755)
            except OSError:
                pass
        written.append(rel)
    if allow_delete:
        for rel in bundle.get("deleted", []):
            norm = str(rel).replace("\\", "/").lstrip("/")
            if not norm or ".." in Path(norm).parts:
                continue
            target = (root / norm).resolve()
            target.relative_to(root)
            if target.is_file():
                target.unlink()
                deleted.append(norm)
    return {"written": written, "deleted": deleted}
