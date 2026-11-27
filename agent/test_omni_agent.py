#!/usr/bin/env python3
"""Unit tests for OmniAgent v4 Protocol, Termux/Debian Daemon, Antigravity Bridge & Hot-Probe Compiler."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

AGENT_DIR = Path(__file__).resolve().parent
if str(AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_DIR))

import bridge_auth as bauth
import deploy_worker
import flight_recorder as fr
import omni_agent_client as oac
import omni_agent_daemon as daemon
import omni_protocol as proto


class OmniAgentProtocolTests(unittest.TestCase):
    KEY = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"

    def test_key_normalization_and_headers(self) -> None:
        self.assertEqual(proto.normalize_pair_secret_hex("CAVPAIR-" + self.KEY.upper()), self.KEY)
        h = proto.derive_agent_headers(self.KEY, channel="termux")
        self.assertEqual(len(h["X-Device-Hash"]), 64)
        self.assertEqual(len(h["X-Agent-Key"]), 64)
        self.assertNotEqual(h["X-Device-Hash"], h["X-Agent-Key"])
        self.assertEqual(h["X-Agent-Channel"], "termux")

    def test_e2e_envelope_roundtrip_and_tamper_reject(self) -> None:
        payload = {"cmd": "git_commit_push", "args": {"message": "feat: test", "big": "x" * 2048}}
        env = proto.encrypt_envelope(self.KEY, "cmd:req123", payload)
        self.assertEqual(env["v"], 4)
        self.assertTrue(env["gz"])
        dec = proto.decrypt_envelope(self.KEY, "cmd:req123", env)
        self.assertEqual(dec, payload)

        with self.assertRaises(ValueError):
            proto.decrypt_envelope(self.KEY, "cmd:wrong_ctx", env)

        tampered = dict(env)
        tampered["ct"] = env["ct"][:-4] + ("AAAA" if not env["ct"].endswith("AAAA") else "BBBB")
        with self.assertRaises(ValueError):
            proto.decrypt_envelope(self.KEY, "cmd:req123", tampered)

    def test_hot_exec_hmac_signature(self) -> None:
        dll = b"MZ" + b"\x00" * 512
        sha1, sig1 = proto.sign_hot_exec(self.KEY, "hot_1", 1700000000, dll)
        sha2, sig2 = proto.sign_hot_exec(self.KEY, "hot_1", 1700000000, dll)
        self.assertEqual((sha1, sig1), (sha2, sig2))
        self.assertEqual(len(sha1), 64)
        self.assertEqual(len(sig1), 64)
        _, sig_other = proto.sign_hot_exec(self.KEY, "hot_2", 1700000000, dll)
        self.assertNotEqual(sig1, sig_other)

    def test_delta_bundle_roundtrip_and_path_traversal_guard(self) -> None:
        with tempfile.TemporaryDirectory() as src_tmp, tempfile.TemporaryDirectory() as dst_tmp:
            src, dst = Path(src_tmp), Path(dst_tmp)
            (src / "dir").mkdir()
            (src / "dir" / "hello.cs").write_text("public class A {}", encoding="utf-8")
            (dst / "old.txt").write_text("remove me", encoding="utf-8")

            bundle = proto.pack_delta_bundle(src, ["dir/hello.cs"], deleted_paths=["old.txt"])
            res = proto.apply_delta_bundle(dst, bundle, allow_delete=True)
            self.assertEqual(res["written"], ["dir/hello.cs"])
            self.assertEqual(res["deleted"], ["old.txt"])
            self.assertEqual((dst / "dir" / "hello.cs").read_text(encoding="utf-8"), "public class A {}")
            self.assertFalse((dst / "old.txt").exists())

            bad_bundle = {
                "format": "omni-delta-v1",
                "files": [{"path": "../escape.txt", "sha256": "", "gz_b64": ""}],
                "deleted": [],
            }
            with self.assertRaises(ValueError):
                proto.apply_delta_bundle(dst, bad_bundle)

    def test_antigravity_discover_and_compact_sync(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ag = root / "antigravity"
            (ag / "brain" / "conv1").mkdir(parents=True)
            (ag / "knowledge").mkdir(parents=True)
            (ag / "conversations").mkdir(parents=True)
            (ag / "conversations" / "test.pb").write_bytes(b"\x08\x01binary")
            repo = root / "Cavern"
            repo.mkdir()

            disc = daemon.discover_antigravity(ag)
            self.assertTrue(disc["found"])
            self.assertIn("brain", disc["entries"])

            sync = daemon.sync_antigravity_session(
                ag,
                repo,
                {
                    "sessionId": "w41_test",
                    "title": "W41 OmniAgent",
                    "summary": "Compact token-diet handoff.",
                    "commit": "abc1234",
                    "files": ["a.cs", "b.py"],
                },
            )
            self.assertTrue(sync["ok"])
            self.assertTrue(Path(sync["sessionFile"]).is_file())
            self.assertTrue(Path(sync["latestFile"]).is_file())
            self.assertTrue(Path(sync["knowledgeMirror"]).is_file())
            # Verify binary .pb file was NEVER modified or touched
            self.assertEqual((ag / "conversations" / "test.pb").read_bytes(), b"\x08\x01binary")

    def test_security_mode_smart_never_nags(self) -> None:
        ok, note = daemon.verify_biometric_if_needed("smart", "git_commit_push")
        self.assertTrue(ok)
        self.assertEqual(note, "smart-hmac")

        ok_t, note_t = daemon.verify_biometric_if_needed("trusted", "shell_exec")
        self.assertTrue(ok_t)
        self.assertEqual(note_t, "trusted")

    def test_daemon_parallel_and_ping(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            d = daemon.OmniAgentDaemon(
                relay_url="https://example.workers.dev",
                pair_secret_hex=self.KEY,
                repo_path=Path(tmp),
                security_mode="smart",
            )
            p = d.handle_command({"cmd": "ping", "args": {}})
            self.assertTrue(p["ok"])
            self.assertEqual(p["agentVersion"], daemon.AGENT_VERSION)

            par = d.handle_command({
                "cmd": "parallel_exec",
                "args": {
                    "tasks": [
                        {"id": "t1", "command": "echo one"},
                        {"id": "t2", "command": "echo two"},
                    ]
                },
            })
            self.assertTrue(par["ok"])
            self.assertEqual(len(par["results"]), 2)

    def test_deploy_worker_multipart_preserves_kv_bindings(self) -> None:
        body, ctype = deploy_worker.build_worker_multipart(
            b"export default { async fetch() { return new Response('ok'); } };",
            main_module="worker.js",
            boundary="TESTBOUNDARY123",
        )
        self.assertEqual(ctype, "multipart/form-data; boundary=TESTBOUNDARY123")
        text = body.decode("utf-8")
        self.assertIn('"main_module":"worker.js"', text)
        self.assertIn('"kv_namespace"', text)
        self.assertIn('"secret_text"', text)
        self.assertIn("Content-Type: application/javascript+module", text)
        self.assertIn("export default", text)

    def test_p256_ecdh_sas_and_knock_grant_roundtrip(self) -> None:
        priv_a, pub_a_bytes = proto.p256_generate_keypair()
        priv_b, pub_b_bytes = proto.p256_generate_keypair()
        pub_a = proto.p256_encode_public_key(pub_a_bytes)
        pub_b = proto.p256_encode_public_key(pub_b_bytes)

        s_ab = proto.p256_ecdh_shared_secret(priv_a, pub_b)
        s_ba = proto.p256_ecdh_shared_secret(priv_b, pub_a)
        self.assertEqual(len(s_ab), 32)
        self.assertEqual(s_ab, s_ba)

        kid = proto.compute_knock_channel_id(self.KEY)
        self.assertEqual(len(kid), 64)
        self.assertNotEqual(kid, proto.compute_device_hash(self.KEY))

        sas1 = proto.compute_knock_sas(kid, "k_test_1", pub_a)
        sas2 = proto.compute_knock_sas(kid, "k_test_1", pub_a)
        sas_diff = proto.compute_knock_sas(kid, "k_test_1", pub_b)
        self.assertEqual(sas1, sas2)
        self.assertRegex(sas1, r"^[A-Z2-9]{3}-[A-Z2-9]{3}$")
        self.assertNotEqual(sas1, sas_diff)

        grant = {"mode": "workspace", "sessionCode": "CAV-TEST-1234", "pairSecret": self.KEY, "tier": 2}
        enc = proto.encrypt_knock_grant(priv_b, pub_a, "k_test_1", grant)
        dec = proto.decrypt_knock_grant(priv_a, pub_b, "k_test_1", enc)
        self.assertEqual(dec, grant)

        with self.assertRaises(ValueError):
            proto.decrypt_knock_grant(priv_a, pub_b, "k_wrong_id", enc)

    def test_bridge_auth_vault_priority_expiry_and_sync_exclusion(self) -> None:
        self.assertFalse(proto.should_include_workspace_path(".agents/.bridge_vault.json"))
        self.assertFalse(proto.should_include_workspace_path(".env.local"))
        with tempfile.TemporaryDirectory() as tmp:
            t = Path(tmp)
            prof_p = t / "bridge_profile.json"
            vault_p = t / ".bridge_vault.json"
            kid = proto.compute_knock_channel_id(self.KEY)
            bauth.save_public_profile(relay_url="https://demo.workers.dev", knock_id=kid, profile_path=prof_p)
            bauth.save_vault(
                relay_url="https://vault.workers.dev",
                session_token="CAV-LIVE-1111-2222-3333",
                session_ttl_seconds=600,
                vault_path=vault_p,
            )
            r = bauth.resolve_connection(profile_path=prof_p, vault_path=vault_p)
            self.assertEqual(r["relayUrl"], "https://vault.workers.dev")
            self.assertEqual(r["relaySource"], "vault")
            self.assertEqual(r["sessionToken"], "CAV-LIVE-1111-2222-3333")
            self.assertEqual(r["knockId"], kid)

            # Force expired session token -> auto-pruned cleanly
            vdata = json.loads(vault_p.read_text(encoding="utf-8"))
            vdata["sessionExpiresUnix"] = 100
            vault_p.write_text(json.dumps(vdata), encoding="utf-8")
            r2 = bauth.resolve_connection(profile_path=prof_p, vault_path=vault_p)
            self.assertEqual(r2["sessionToken"], "")

    def test_deploy_worker_v6_sqlite_do_multipart_and_worker_name_extract(self) -> None:
        body, ctype = deploy_worker.build_worker_multipart(
            b"export class HubCoordinator {} export default { async fetch() { return new Response('ok'); } };",
            main_module="worker.js",
            boundary="TESTV6DO",
            enable_durable_object=True,
            include_sqlite_migration=True,
        )
        self.assertEqual(ctype, "multipart/form-data; boundary=TESTV6DO")
        text = body.decode("utf-8")
        self.assertIn('"durable_object_namespace"', text)
        self.assertIn('"name":"HUB_DO"', text)
        self.assertIn('"class_name":"HubCoordinator"', text)
        self.assertIn('"new_sqlite_classes":["HubCoordinator"]', text)
        self.assertIn('"kv_namespace"', text)
        self.assertIn('"secret_text"', text)
        self.assertEqual(
            deploy_worker.infer_worker_name_from_url("https://cavernize-relay.example.workers.dev"),
            "cavernize-relay",
        )

    def test_live_bridge_session_expired_constant_and_vault_callback(self) -> None:
        import os

        candidates = [
            AGENT_DIR.parent / "plugins" / "dynamic-fractal-planner" / "scripts",
            Path(os.environ.get("CAVERN_FEDERATION_ROOT", "/tmp/cavern-federation")) / "fractal" / "scripts",
        ]
        plugin_scripts = next((p for p in candidates if (p / "live_bridge_client.py").is_file()), None)
        if plugin_scripts is None:
            self.skipTest("live_bridge_client.py not mounted locally")
        if str(plugin_scripts) not in sys.path:
            sys.path.insert(0, str(plugin_scripts))
        import live_bridge_client as lbc

        self.assertEqual(lbc.EXIT_SESSION_EXPIRED, 4)
        self.assertTrue(callable(lbc.main))

    def test_aistudio_attachments_excluded_from_workspace_sync(self) -> None:
        self.assertFalse(proto.should_include_workspace_path("public/assets/aistudio/cavern_1_lossless.txt"))
        self.assertFalse(proto.should_include_workspace_path("public/assets/aistudio/sub/img.png"))
        self.assertTrue(proto.should_include_workspace_path("Cavern/Neural/MvsepAntiArtifactPipeline.cs"))

    def test_cavernize_cmd_payload_uses_args_object_and_tier(self) -> None:
        p = oac.build_cavernize_cmd_payload("shell_exec", '{"command":"ls -la"}', tier=2, timeout_sec=90)
        self.assertEqual(p["cmd"], "shell_exec")
        self.assertEqual(p["args"], {"command": "ls -la"})
        self.assertEqual(p["tier"], 2)
        self.assertEqual(p["timeoutSec"], 80)
        self.assertNotIn("argsJson", p)
        self.assertNotIn("maxTier", p)
        with self.assertRaises(ValueError):
            oac.build_cavernize_cmd_payload("ping", '["not-an-object"]', tier=0)

    def test_flight_recorder_auto_diff_rollback_detection_and_restore(self) -> None:
        with tempfile.TemporaryDirectory() as ws_tmp, tempfile.TemporaryDirectory() as fr_tmp:
            ws = Path(ws_tmp)
            fr_dir = Path(fr_tmp)
            (ws / "src").mkdir()
            (ws / "src" / "core.cs").write_text("// baseline v1\n", encoding="utf-8")
            (ws / "public" / "assets" / "aistudio").mkdir(parents=True)
            (ws / "public" / "assets" / "aistudio" / "upload.txt").write_text("ignored", encoding="utf-8")

            init_r = fr.init_session(
                workspace=ws,
                local_dir=fr_dir,
                session_id="test_w46",
                wave="W46",
                goal="Test anti-rollback",
                requests=["Keep progress on cancel"],
                use_remote=False,
            )
            self.assertTrue(init_r["ok"])
            self.assertEqual(init_r["checkpoint"]["seq"], 1)

            # Edit existing file + create a new file
            (ws / "src" / "core.cs").write_text("// upgraded v2 FP16 pipeline\n", encoding="utf-8")
            (ws / "src" / "new_stage.cs").write_text("public sealed class NewStage {}\n", encoding="utf-8")

            save_r = fr.save_checkpoint(
                workspace=ws,
                local_dir=fr_dir,
                phase="implementation",
                summary="Implemented 3-stage pipeline",
                findings=["batch=2 rejected 0.466x", "FP16 approved 1.396x"],
                completed=["Baseline captured"],
                pending=["Run CI parity"],
                next_step="Run check_android_cs.py",
                auto_diff=True,
                use_remote=False,
            )
            self.assertTrue(save_r["ok"])
            self.assertEqual(save_r["checkpoint"]["seq"], 2)
            self.assertIn("src/core.cs", save_r["checkpoint"]["fileHashes"])
            self.assertIn("src/new_stage.cs", save_r["checkpoint"]["fileHashes"])
            self.assertNotIn("public/assets/aistudio/upload.txt", save_r["checkpoint"]["fileHashes"])

            # Simulate AI Studio turn cancellation & workspace rollback!
            (ws / "src" / "core.cs").write_text("// baseline v1\n", encoding="utf-8")
            (ws / "src" / "new_stage.cs").unlink()

            rec = fr.recover_session(workspace=ws, local_dir=fr_dir, use_remote=False)
            self.assertTrue(rec["found"])
            self.assertTrue(rec["rollback"]["rollbackDetected"])
            self.assertEqual(len(rec["rollback"]["reverted"]), 1)
            self.assertEqual(rec["rollback"]["reverted"][0]["path"], "src/core.cs")
            self.assertEqual(rec["rollback"]["missing"], ["src/new_stage.cs"])
            self.assertIn("restore", rec["restoreCommand"])

            # Dry-run restore
            dry = fr.restore_session(workspace=ws, local_dir=fr_dir, dry_run=True, use_remote=False)
            self.assertTrue(dry["ok"])
            self.assertEqual(sorted(dry["wouldWrite"]), ["src/core.cs", "src/new_stage.cs"])
            self.assertFalse((ws / "src" / "new_stage.cs").exists())

            # Real restore
            rest = fr.restore_session(workspace=ws, local_dir=fr_dir, use_remote=False)
            self.assertTrue(rest["ok"])
            self.assertEqual((ws / "src" / "core.cs").read_text(encoding="utf-8"), "// upgraded v2 FP16 pipeline\n")
            self.assertEqual((ws / "src" / "new_stage.cs").read_text(encoding="utf-8"), "public sealed class NewStage {}\n")

            # After restore, rollbackDetected must be False
            rec2 = fr.recover_session(workspace=ws, local_dir=fr_dir, use_remote=False)
            self.assertFalse(rec2["rollback"]["rollbackDetected"])

            # Close session after commit/push
            cls = fr.close_session(local_dir=fr_dir, commit="deadbeef", note="Committed", use_remote=False)
            self.assertEqual(cls["checkpoint"]["status"], "closed")
            rec3 = fr.recover_session(workspace=ws, local_dir=fr_dir, use_remote=False)
            self.assertFalse(rec3["rollback"]["rollbackDetected"])

    def test_flight_recorder_multitier_recency_and_bundle_codec(self) -> None:
        cands = {
            "local": {"sessionId": "s1", "seq": 2, "timestampUnix": 1000.0},
            "cloud": {"sessionId": "s1", "seq": 4, "timestampUnix": 1020.0},
            "termux": {"sessionId": "s1", "seq": 3, "timestampUnix": 1010.0},
        }
        tier, cp = fr.pick_freshest_checkpoint(cands)
        self.assertEqual(tier, "cloud")
        self.assertEqual(cp["seq"], 4)

        bundle = {"format": "omni-delta-v1", "files": [], "deleted": []}
        _, b64, sha = fr.encode_bundle_gz_b64(bundle)
        decoded = fr.decode_bundle_gz_b64(b64, expected_sha256=sha)
        self.assertEqual(decoded, bundle)
        with self.assertRaises(ValueError):
            fr.decode_bundle_gz_b64(b64, expected_sha256="0" * 64)

    def test_local_capabilities_priority_and_missing_alert(self) -> None:
        import local_capabilities as lcap
        import github_direct as ghdirect

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            manifest = root / "env_manifest.json"
            rt_file = root / "runtime_env.json"
            vault = root / "vault.json"

            manifest.write_text(
                json.dumps(
                    {
                        "schema": 1,
                        "ownerRepo": "guumachadooo/Cavern",
                        "defaultBranch": "master",
                        "variables": [
                            {"name": "GITHUB_TOKEN", "aliases": ["GH_TOKEN"], "expectedInAiStudio": True, "capability": "githubDirectPushAndCi"},
                            {"name": "CLOUDFLARE_API_TOKEN", "aliases": [], "expectedInAiStudio": True, "capability": "cloudflareDirectDeploy"},
                            {"name": "CAVERN_BRIDGE_PAIR_SECRET", "aliases": [], "expectedInAiStudio": True, "capability": "omniAgentZeroKnockAuth"},
                            {"name": "CAVERN_RELAY_URL", "aliases": [], "expectedInAiStudio": True, "capability": "omniAgentZeroKnockAuth"},
                        ],
                    }
                ),
                encoding="utf-8",
            )
            rt_file.write_text(json.dumps({"GITHUB_TOKEN": "ghp_dummy_test", "CAVERN_RELAY_URL": "https://example.workers.dev"}), encoding="utf-8")
            vault.write_text(json.dumps({"pairSecret": "ab" * 32}), encoding="utf-8")

            report = lcap.inspect_capabilities(
                manifest_path=manifest,
                runtime_file=rt_file,
                vault_path=vault,
                allow_proc_scan=False,
            )
            self.assertEqual(report["presentCount"], 3)
            self.assertEqual(report["missingExpectedCount"], 1)
            self.assertEqual(report["missingExpectedVars"][0]["name"], "CLOUDFLARE_API_TOKEN")
            self.assertIn("CLOUDFLARE_API_TOKEN", report["alert"])
            self.assertEqual(report["routing"]["githubPush"], "local_github_api")
            self.assertEqual(report["routing"]["cloudflareWorkerDeploy"], "phone_omniagent")
            self.assertEqual(report["routing"]["omniAgentAuth"], "local_zero_knock")

            # Ensure ZERO secret values appear anywhere in the serialized report!
            dumped = json.dumps(report)
            self.assertNotIn("ghp_dummy_test", dumped)
            self.assertNotIn("ab" * 32, dumped)

            # Verify standard git blob sha1 for "hello\n" == ce013625030ba8dba906f756967f9e9ca394464a
            self.assertEqual(ghdirect.git_blob_sha1(b"hello\n"), "ce013625030ba8dba906f756967f9e9ca394464a")

    def test_termux_atomic_sync_runner_live_mirror_and_manual_edit_protection(self) -> None:
        import base64
        import gzip
        import os
        import subprocess

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = root / "src_aistudio"
            repo = root / "phone_repo"
            ag = root / "antigravity"
            fr_dir = ag / "ai_studio_bridge" / "flight_recorder"
            (ag / "knowledge").mkdir(parents=True)
            src.mkdir()
            repo.mkdir()

            # Initialize a real git repo on the "phone" side
            for fname, txt in (("a.cs", "// a v1\n"), ("b.cs", "// b v1\n")):
                (src / fname).write_text(txt, encoding="utf-8")
                (repo / fname).write_text(txt, encoding="utf-8")
            subprocess.run(["git", "init"], cwd=str(repo), check=True, capture_output=True)
            subprocess.run(["git", "config", "user.email", "test@local"], cwd=str(repo), check=True, capture_output=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=str(repo), check=True, capture_output=True)
            subprocess.run(["git", "add", "."], cwd=str(repo), check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=str(repo), check=True, capture_output=True)

            # Simulate AI Studio editing a.cs + creating c.cs
            (src / "a.cs").write_text("// a v2 from AI Studio\n", encoding="utf-8")
            (src / "c.cs").write_text("// c new from AI Studio\n", encoding="utf-8")
            bundle1 = proto.pack_delta_bundle(src, ["a.cs", "c.cs"])
            _, b64_1, sha_1 = fr.encode_bundle_gz_b64(bundle1)
            cp1 = {
                "schema": 1,
                "sessionId": "w48_live",
                "seq": 2,
                "status": "active",
                "timestampUnix": 1795450000.0,
                "updatedUtc": "2026-11-23T16:20:00Z",
                "wave": "W48",
                "phase": "live-mirror",
                "summary": "Edited a.cs and c.cs",
                "nextStep": "Continue W48",
                "fileHashes": {e["path"]: e["sha256"] for e in bundle1["files"]},
                "deletedFiles": [],
                "bundleSha256": sha_1,
                "hasBundle": True,
            }
            payload1 = {
                "frDir": str(fr_dir),
                "agDir": str(ag),
                "repoDir": str(repo),
                "checkpoint": cp1,
                "diaryMd": fr.render_diary_markdown(cp1),
                "bundle": bundle1,
                "bundleGzB64": b64_1,
                "applyLiveMirror": True,
                "syncAntigravityHandoff": True,
            }
            env = dict(os.environ)
            env["FR_PAYLOAD_B64"] = base64.b64encode(
                gzip.compress(json.dumps(payload1, separators=(",", ":")).encode("utf-8"))
            ).decode("ascii")
            r1 = subprocess.run(
                [sys.executable, "-B", "-c", fr.build_termux_atomic_sync_script()],
                env=env,
                capture_output=True,
                text=True,
                check=True,
            )
            out1 = json.loads(r1.stdout.strip().splitlines()[-1])
            self.assertTrue(out1["ok"])
            self.assertTrue(out1["agHandoff"])
            self.assertEqual(sorted(out1["liveMirror"]["written"]), ["a.cs", "c.cs"])
            self.assertEqual((repo / "a.cs").read_text(encoding="utf-8"), "// a v2 from AI Studio\n")
            self.assertEqual((repo / "c.cs").read_text(encoding="utf-8"), "// c new from AI Studio\n")
            self.assertTrue((fr_dir / "latest.json").is_file())
            self.assertTrue((fr_dir / "LATEST_DIARY.md").is_file())
            self.assertTrue((fr_dir / "latest_bundle.json.gz.b64").is_file())
            self.assertTrue((ag / "ai_studio_bridge" / "LATEST_HANDOFF.md").is_file())
            self.assertTrue((ag / "knowledge" / "ai_studio_latest_sync.md").is_file())

            # Now user manually edits b.cs on phone in Antigravity!
            (repo / "b.cs").write_text("// USER MANUAL PHONE EDIT\n", encoding="utf-8")
            # AI Studio edits both a.cs (v3) and b.cs (AI v2)
            (src / "a.cs").write_text("// a v3 from AI Studio\n", encoding="utf-8")
            (src / "b.cs").write_text("// b AI version\n", encoding="utf-8")
            bundle2 = proto.pack_delta_bundle(src, ["a.cs", "b.cs"])
            _, b64_2, sha_2 = fr.encode_bundle_gz_b64(bundle2)
            cp2 = dict(cp1, seq=3, bundleSha256=sha_2, fileHashes={e["path"]: e["sha256"] for e in bundle2["files"]})
            payload2 = dict(payload1, checkpoint=cp2, diaryMd=fr.render_diary_markdown(cp2), bundle=bundle2, bundleGzB64=b64_2)
            env["FR_PAYLOAD_B64"] = base64.b64encode(
                gzip.compress(json.dumps(payload2, separators=(",", ":")).encode("utf-8"))
            ).decode("ascii")
            r2 = subprocess.run(
                [sys.executable, "-B", "-c", fr.build_termux_atomic_sync_script()],
                env=env,
                capture_output=True,
                text=True,
                check=True,
            )
            out2 = json.loads(r2.stdout.strip().splitlines()[-1])
            self.assertTrue(out2["ok"])
            self.assertEqual(out2["liveMirror"]["written"], ["a.cs"])
            self.assertEqual(out2["liveMirror"]["skippedConflicts"], ["b.cs"])
            # a.cs updated to v3, while user's manual edit to b.cs stayed 100% untouched!
            self.assertEqual((repo / "a.cs").read_text(encoding="utf-8"), "// a v3 from AI Studio\n")
            self.assertEqual((repo / "b.cs").read_text(encoding="utf-8"), "// USER MANUAL PHONE EDIT\n")

    def test_live_sync_watcher_zero_empty_push_and_close_baseline_refresh(self) -> None:
        import live_sync_watcher as lsw

        with tempfile.TemporaryDirectory() as ws_tmp, tempfile.TemporaryDirectory() as fr_tmp:
            ws = Path(ws_tmp)
            fr_dir = Path(fr_tmp)
            (ws / "Core.cs").write_text("// v1\n", encoding="utf-8")

            watcher = lsw.LiveSyncWatcher(
                workspace=ws,
                local_dir=fr_dir,
                poll_sec=0.5,
                debounce_sec=0.0,
                use_remote=False,
                apply_live_mirror=False,
            )
            # Clean startup must NEVER push (protects prior account's remote checkpoint)
            r0 = watcher.sync_once(force_immediate=True)
            self.assertTrue(r0["ok"])
            self.assertFalse(r0["synced"])
            self.assertEqual(r0["reason"], "clean")

            # Edit file -> watcher syncs immediately
            (ws / "Core.cs").write_text("// v2 live\n", encoding="utf-8")
            r1 = watcher.sync_once(force_immediate=True)
            self.assertTrue(r1["ok"])
            self.assertTrue(r1["synced"])
            self.assertEqual(r1["changed"], ["Core.cs"])
            self.assertTrue((fr_dir / "watcher_status.json").is_file())

            # No new edits -> deduplicated (zero redundant syncs)
            r2 = watcher.sync_once(force_immediate=True)
            self.assertFalse(r2["synced"])
            self.assertEqual(r2["reason"], "already_synced")

            # Close session with workspace -> refreshes baseline & clears pending
            cls = fr.close_session(
                workspace=ws,
                local_dir=fr_dir,
                commit="cafebabe",
                note="Shipped",
                next_step="Start W48",
                use_remote=False,
            )
            self.assertEqual(cls["checkpoint"]["nextStep"], "Start W48")
            self.assertEqual(cls["checkpoint"]["pending"], [])

            # Watcher detects refreshed baseline -> returns clean
            r3 = watcher.sync_once(force_immediate=True)
            self.assertFalse(r3["synced"])
            self.assertEqual(r3["reason"], "clean")

    def test_file_budget_guard_roundtrip_and_ship_skip_ci_detector(self) -> None:
        scripts_dir = Path(__file__).resolve().parents[1] / "scripts"
        if (scripts_dir / "file_budget_guard.py").is_file():
            if str(scripts_dir) not in sys.path:
                sys.path.insert(0, str(scripts_dir))
            import file_budget_guard as fbg

            with tempfile.TemporaryDirectory() as td:
                repo = Path(td)
                (repo / "Legacy" / "Sub").mkdir(parents=True)
                (repo / "Legacy" / "a.txt").write_text("alpha\n", encoding="utf-8")
                (repo / "Legacy" / "Sub" / "b.bin").write_bytes(b"\x00\x01\x02\xff")
                (repo / "Keep.cs").write_text("// keep\n", encoding="utf-8")

                out_arch = repo / "archive" / "legacy.tar.gz"
                packed = fbg.pack_paths(repo, ["Legacy"], out_arch, remove_sources=True)
                self.assertTrue(packed["ok"])
                self.assertEqual(packed["packedFiles"], 2)
                self.assertEqual(packed["removedSources"], 2)
                self.assertFalse((repo / "Legacy" / "a.txt").exists())

                verified = fbg.verify_archive(out_arch)
                self.assertTrue(verified["ok"])
                self.assertEqual(verified["fileCount"], 2)

                restored = fbg.unpack_archive(out_arch, repo)
                self.assertTrue(restored["ok"])
                self.assertEqual((repo / "Legacy" / "a.txt").read_text(encoding="utf-8"), "alpha\n")
                self.assertEqual((repo / "Legacy" / "Sub" / "b.bin").read_bytes(), b"\x00\x01\x02\xff")

        # Smart [skip ci] detector
        self.assertFalse(oac._needs_apk_ci(["README.md", ".agents/docs/HANDOFF.md", ".github/workflows/FastQualityGuard.yml"]))
        self.assertTrue(oac._needs_apk_ci(["Cavern/Spatial/Foo.cs"]))
        self.assertTrue(oac._needs_apk_ci([".github/workflows/BuildAndroid.yml"]))

    def test_repo_federation_detection_extract_and_mirrors(self) -> None:
        import io
        import tarfile
        import repo_federation as rfed

        manifest = rfed.load_manifest()
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ws_cav = root / "ws_cavern"
            (ws_cav / "Cavern").mkdir(parents=True)
            (ws_cav / "Cavern" / "Cavern.csproj").write_text("<Project/>", encoding="utf-8")
            (ws_cav / "CavernSamples" / "CavernizeAndroid").mkdir(parents=True)
            (ws_cav / "CavernSamples" / "CavernizeAndroid" / "CavernizeAndroid.csproj").write_text("<Project/>", encoding="utf-8")
            self.assertEqual(rfed.detect_current_repo(ws_cav, manifest), "cavern")

            ws_omni = root / "ws_omni"
            (ws_omni / "OmniAgentAndroid").mkdir(parents=True)
            (ws_omni / "OmniAgentAndroid" / "OmniAgentAndroid.csproj").write_text("<Project/>", encoding="utf-8")
            (ws_omni / "worker").mkdir(parents=True)
            (ws_omni / "worker" / "cloudflare_live_relay_worker.js").write_text("// worker", encoding="utf-8")
            self.assertEqual(rfed.detect_current_repo(ws_omni, manifest), "omniagent")

            ws_frac = root / "ws_fractal"
            (ws_frac / "scripts").mkdir(parents=True)
            (ws_frac / "plugin.json").write_text("{}", encoding="utf-8")
            (ws_frac / "scripts" / "verify_universal.py").write_text("# ok", encoding="utf-8")
            self.assertEqual(rfed.detect_current_repo(ws_frac, manifest), "fractal")

            # Safe tarball extraction (strips GitHub root folder & blocks path traversal)
            buf = io.BytesIO()
            with tarfile.open(fileobj=buf, mode="w:gz") as tf:
                info = tarfile.TarInfo("repo-main/agent/hello.py")
                payload = b"print('ok')\n"
                info.size = len(payload)
                tf.addfile(info, io.BytesIO(payload))
            ext_dir = root / "extracted"
            cnt = rfed._safe_extract_github_tarball(buf.getvalue(), ext_dir)
            self.assertEqual(cnt, 1)
            self.assertEqual((ext_dir / "agent" / "hello.py").read_text(encoding="utf-8"), "print('ok')\n")

            bad_buf = io.BytesIO()
            with tarfile.open(fileobj=bad_buf, mode="w:gz") as tf:
                info = tarfile.TarInfo("repo-main/../../escape.txt")
                info.size = 3
                tf.addfile(info, io.BytesIO(b"bad"))
            with self.assertRaises(ValueError):
                rfed._safe_extract_github_tarball(bad_buf.getvalue(), root / "bad_out")

            # Mirror sync with exclusions (fractal -> cavern)
            mount = root / "mount"
            (mount / "fractal" / "scripts").mkdir(parents=True)
            (mount / "fractal" / "plugin.json").write_text('{"version":"2.10.0"}', encoding="utf-8")
            (mount / "fractal" / "scripts" / "verify_universal.py").write_text("# v2", encoding="utf-8")
            (mount / "fractal" / "package.json").write_text('{"name":"shim"}', encoding="utf-8")
            (mount / "fractal" / "AGENTS.md").write_text("# root only", encoding="utf-8")
            (mount / "omniagent" / "agent").mkdir(parents=True)
            (mount / "omniagent" / "agent" / "x.py").write_text("# x", encoding="utf-8")
            (mount / "omniagent" / "worker").mkdir(parents=True)
            (mount / "omniagent" / "worker" / "cloudflare_live_relay_worker.js").write_text("// w6", encoding="utf-8")

            (ws_cav / "CavernSamples" / "CavernizeAndroid" / "Theme").mkdir(parents=True)
            (ws_cav / "CavernSamples" / "CavernizeAndroid" / "Views").mkdir(parents=True)
            (ws_cav / "CavernSamples" / "CavernizeAndroid" / "Assets" / "fonts").mkdir(parents=True)
            (ws_cav / "CavernSamples" / "CavernizeAndroid" / "Theme" / "OneUiTheme.cs").write_text("// theme", encoding="utf-8")
            (ws_cav / "CavernSamples" / "CavernizeAndroid" / "Views" / "OneUiIconView.cs").write_text("// icons", encoding="utf-8")
            (ws_cav / "CavernSamples" / "CavernizeAndroid" / "Views" / "OneUiComponents.cs").write_text("// comps", encoding="utf-8")
            (ws_cav / "CavernSamples" / "CavernizeAndroid" / "Assets" / "fonts" / "sf_pro_android_ui.ttf").write_bytes(b"FONT")

            res = rfed.sync_mirrors(ws_cav, mirror_name="all", apply=True, mount_root=mount)
            self.assertTrue(res["ok"])
            self.assertTrue((ws_cav / ".agents" / "plugins" / "dynamic-fractal-planner" / "plugin.json").is_file())
            self.assertFalse((ws_cav / ".agents" / "plugins" / "dynamic-fractal-planner" / "package.json").exists())
            self.assertFalse((ws_cav / ".agents" / "plugins" / "dynamic-fractal-planner" / "AGENTS.md").exists())
            self.assertEqual((ws_cav / ".agents" / "agent" / "x.py").read_text(encoding="utf-8"), "# x")
            self.assertEqual((ws_cav / ".agents" / "docs" / "cloudflare_live_relay_worker.js").read_text(encoding="utf-8"), "// w6")
            self.assertEqual((mount / "omniagent" / "OmniAgentAndroid" / "SharedOneUi" / "OneUiTheme.cs").read_text(encoding="utf-8"), "// theme")
            self.assertEqual((mount / "omniagent" / "OmniAgentAndroid" / "Assets" / "fonts" / "sf_pro_android_ui.ttf").read_bytes(), b"FONT")

    def test_local_fs_loopback_server_security_and_browse(self) -> None:
        import urllib.error
        import urllib.parse
        import urllib.request
        import omni_agent_daemon as daemon

        pair_hex = "a1" * 32
        key = daemon.derive_local_fs_key(pair_hex)
        self.assertEqual(len(key), 64)

        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            (repo / "sub").mkdir()
            (repo / "hello.txt").write_text("line1\nline2\n", encoding="utf-8")

            srv = daemon.start_local_fs_server(pair_hex, repo, host="127.0.0.1", port=0)
            self.assertIsNotNone(srv)
            assert srv is not None
            port = srv.server_address[1]
            base = f"http://127.0.0.1:{port}"
            try:
                # 1. Missing/invalid HMAC key must be rejected with 401
                with self.assertRaises(urllib.error.HTTPError) as ctx:
                    urllib.request.urlopen(f"{base}/local/health", timeout=5)
                self.assertEqual(ctx.exception.code, 401)

                # 2. Valid key lists allowed repo directory
                q = urllib.parse.urlencode({"path": str(repo)})
                req = urllib.request.Request(f"{base}/local/fs/list?{q}", headers={"X-Omni-Local-Key": key})
                with urllib.request.urlopen(req, timeout=5) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                self.assertTrue(data["ok"])
                names = [e["name"] for e in data["entries"]]
                self.assertEqual(names[0], "sub")  # folders sorted first
                self.assertIn("hello.txt", names)

                # 3. Valid key reads file preview
                q2 = urllib.parse.urlencode({"path": str(repo / "hello.txt")})
                req2 = urllib.request.Request(f"{base}/local/fs/read?{q2}", headers={"X-Omni-Local-Key": key})
                with urllib.request.urlopen(req2, timeout=5) as resp2:
                    rd = json.loads(resp2.read().decode("utf-8"))
                self.assertTrue(rd["ok"])
                self.assertEqual(rd["lineCount"], 2)
                self.assertIn("line2", rd["content"])

                # 4. Path traversal outside allowed roots must be blocked with 403
                q3 = urllib.parse.urlencode({"path": "/etc/passwd"})
                req3 = urllib.request.Request(f"{base}/local/fs/read?{q3}", headers={"X-Omni-Local-Key": key})
                with self.assertRaises(urllib.error.HTTPError) as ctx3:
                    urllib.request.urlopen(req3, timeout=5)
                self.assertEqual(ctx3.exception.code, 403)

                # 5. /local/triad must return 200 JSON without timeout_sec TypeError even on real git repo
                import subprocess

                cav_repo = repo / "Cavern"
                cav_repo.mkdir()
                (cav_repo / "README.md").write_text("# test\n", encoding="utf-8")
                subprocess.run(["git", "init"], cwd=str(cav_repo), check=True, capture_output=True)
                subprocess.run(["git", "config", "user.email", "t@local"], cwd=str(cav_repo), check=True, capture_output=True)
                subprocess.run(["git", "config", "user.name", "Test"], cwd=str(cav_repo), check=True, capture_output=True)
                subprocess.run(["git", "add", "."], cwd=str(cav_repo), check=True, capture_output=True)
                subprocess.run(["git", "commit", "-m", "init"], cwd=str(cav_repo), check=True, capture_output=True)

                triad_res = daemon._inspect_local_triad(cav_repo)
                self.assertTrue(triad_res["ok"])
                cav_entry = next(r for r in triad_res["repos"] if r["alias"] == "cavern")
                self.assertTrue(cav_entry["exists"])
                self.assertTrue(len(cav_entry["head"]) >= 7)

                req_triad = urllib.request.Request(f"{base}/local/triad", headers={"X-Omni-Local-Key": key})
                with urllib.request.urlopen(req_triad, timeout=5) as resp_t:
                    td_json = json.loads(resp_t.read().decode("utf-8"))
                self.assertTrue(td_json["ok"])

                # 6. /local/logs/save must accept authenticated log mirror
                log_body = json.dumps({"fileName": "omniagent_test.log", "content": "[INFO] test log line\n"}).encode("utf-8")
                req_log = urllib.request.Request(
                    f"{base}/local/logs/save",
                    data=log_body,
                    headers={"X-Omni-Local-Key": key, "Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(req_log, timeout=5) as resp_l:
                    ld_json = json.loads(resp_l.read().decode("utf-8"))
                self.assertTrue(ld_json["ok"])
                self.assertTrue(Path(ld_json["savedPath"]).is_file())

                # 7. OmniAgentDaemon must expose self.device_hash matching X-Device-Hash
                inst = daemon.OmniAgentDaemon("https://example.workers.dev", pair_hex, repo)
                self.assertEqual(inst.device_hash, inst.headers["X-Device-Hash"])
            finally:
                srv.shutdown()
                srv.server_close()

    def test_host_aware_capabilities_antigravity_vs_aistudio_and_local_omni_config(self) -> None:
        import os
        import local_capabilities as lcap
        import repo_federation as rfed

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            manifest = root / "env_manifest.json"
            rt_missing = root / "nonexistent_rt_env.json"
            vault = root / "vault.json"
            omni_cfg = root / "omni_config.json"

            manifest.write_text(
                json.dumps(
                    {
                        "schema": 1,
                        "ownerRepo": "guumachadooo/Cavern",
                        "defaultBranch": "master",
                        "variables": [
                            {"name": "GITHUB_TOKEN", "aliases": ["GH_TOKEN"], "expectedInAiStudio": True, "capability": "githubDirectPushAndCi"},
                            {"name": "CLOUDFLARE_API_TOKEN", "aliases": [], "expectedInAiStudio": True, "capability": "cloudflareDirectDeploy"},
                            {"name": "CAVERN_BRIDGE_PAIR_SECRET", "aliases": [], "expectedInAiStudio": True, "capability": "omniAgentZeroKnockAuth"},
                            {"name": "CAVERN_RELAY_URL", "aliases": [], "expectedInAiStudio": True, "capability": "omniAgentZeroKnockAuth"},
                        ],
                    }
                ),
                encoding="utf-8",
            )
            omni_cfg.write_text(
                json.dumps({"pair_secret": "cd" * 32, "relay_url": "https://omni-test.workers.dev"}),
                encoding="utf-8",
            )

            ag_rep = lcap.inspect_capabilities(
                manifest_path=manifest,
                runtime_file=rt_missing,
                vault_path=vault,
                allow_proc_scan=False,
                omni_config_path=omni_cfg,
                host_override="antigravity-termux",
            )
            self.assertEqual(ag_rep["hostProfile"]["hostId"], "antigravity-termux")
            self.assertEqual(ag_rep["missingExpectedCount"], 0)
            self.assertEqual(ag_rep["missingExpectedVars"], [])
            self.assertEqual(ag_rep["alert"], "")
            self.assertEqual(ag_rep["routing"]["githubPush"], "native_git")
            self.assertEqual(ag_rep["routing"]["omniAgentAuth"], "local_omni_config")
            self.assertEqual(ag_rep["routing"]["omniAgentScope"], "android_ipc_and_hot_exec_only")
            self.assertIn("GITHUB_TOKEN", ag_rep["aiStudioExpectedVarsMissing"])
            self.assertNotIn("cd" * 32, json.dumps(ag_rep))

            empty_cfg = root / "empty_omni_config.json"
            cloud_rep = lcap.inspect_capabilities(
                manifest_path=manifest,
                runtime_file=rt_missing,
                vault_path=vault,
                allow_proc_scan=False,
                omni_config_path=empty_cfg,
                host_override="ai-studio-cloud",
            )
            self.assertEqual(cloud_rep["hostProfile"]["hostId"], "ai-studio-cloud")
            self.assertEqual(cloud_rep["missingExpectedCount"], 4)
            self.assertIn("GITHUB_TOKEN", cloud_rep["alert"])

            # repo_federation.get_repo_path prefers existing local git termuxPath when mount_root=None
            ws_cav = root / "ws_cavern"
            (ws_cav / "Cavern").mkdir(parents=True)
            (ws_cav / "Cavern" / "Cavern.csproj").write_text("<Project/>", encoding="utf-8")
            (ws_cav / "CavernSamples" / "CavernizeAndroid").mkdir(parents=True)
            (ws_cav / "CavernSamples" / "CavernizeAndroid" / "CavernizeAndroid.csproj").write_text("<Project/>", encoding="utf-8")
            local_omni = root / "local_omni_git"
            (local_omni / ".git").mkdir(parents=True)
            fake_manifest = {
                "mountRoot": str(root / "fed_mount"),
                "repos": {
                    "cavern": {"detectFiles": ["Cavern/Cavern.csproj", "CavernSamples/CavernizeAndroid/CavernizeAndroid.csproj"]},
                    "omniagent": {"detectFiles": ["OmniAgentAndroid/OmniAgentAndroid.csproj"], "termuxPath": str(local_omni)},
                },
            }
            prev_fed = os.environ.pop("CAVERN_FEDERATION_ROOT", None)
            try:
                self.assertEqual(rfed.get_repo_path("omniagent", ws_cav, fake_manifest, mount_root=None), local_omni.resolve())
                explicit_mount = root / "explicit_mount"
                self.assertEqual(rfed.get_repo_path("omniagent", ws_cav, fake_manifest, mount_root=explicit_mount), explicit_mount / "omniagent")
            finally:
                if prev_fed is not None:
                    os.environ["CAVERN_FEDERATION_ROOT"] = prev_fed

    def test_daemon_safe_ff_only_pull_quarantine_and_never_rebase(self) -> None:
        import socket
        import subprocess
        import urllib.request
        from unittest import mock

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            remote_cav = root / "remote_cavern.git"
            remote_omni = root / "remote_omni.git"
            subprocess.run(["git", "init", "--bare", str(remote_cav)], check=True, capture_output=True)
            subprocess.run(["git", "init", "--bare", str(remote_omni)], check=True, capture_output=True)

            def _seed_and_clone(remote: Path, local: Path) -> Path:
                seed = root / f"seed_{local.name}"
                subprocess.run(["git", "clone", str(remote), str(seed)], check=True, capture_output=True)
                subprocess.run(["git", "config", "user.email", "t@local"], cwd=str(seed), check=True, capture_output=True)
                subprocess.run(["git", "config", "user.name", "Test"], cwd=str(seed), check=True, capture_output=True)
                (seed / "README.md").write_text("v1\n", encoding="utf-8")
                subprocess.run(["git", "add", "."], cwd=str(seed), check=True, capture_output=True)
                subprocess.run(["git", "commit", "-m", "c1"], cwd=str(seed), check=True, capture_output=True)
                subprocess.run(["git", "push", "origin", "HEAD:master"], cwd=str(seed), check=True, capture_output=True)
                subprocess.run(["git", "clone", "-b", "master", str(remote), str(local)], check=True, capture_output=True)
                (seed / "README.md").write_text("v2\n", encoding="utf-8")
                subprocess.run(["git", "add", "."], cwd=str(seed), check=True, capture_output=True)
                subprocess.run(["git", "commit", "-m", "c2"], cwd=str(seed), check=True, capture_output=True)
                subprocess.run(["git", "push", "origin", "HEAD:master"], cwd=str(seed), check=True, capture_output=True)
                return seed

            cav_local = root / "Cavern"
            omni_local = root / "OmniAgent"
            _seed_and_clone(remote_cav, cav_local)
            _seed_and_clone(remote_omni, omni_local)

            # Dirty Cavern repo must be quarantined and NEVER call `git pull --rebase`
            (cav_local / "dirty.tmp").write_text("unsaved", encoding="utf-8")
            head_before = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=str(cav_local), text=True).strip()
            recorded_cmds: list[list[str]] = []
            orig_run = daemon.run_subprocess

            def _spy_run(cmd, cwd=None, timeout=30):
                recorded_cmds.append(list(cmd))
                return orig_run(cmd, cwd=cwd, timeout=timeout)

            with mock.patch.object(daemon, "run_subprocess", side_effect=_spy_run):
                q_res = daemon._safe_ff_only_pull(cav_local)
            self.assertFalse(q_res["ok"])
            self.assertEqual(q_res["status"], "quarantined_dirty")
            self.assertGreaterEqual(q_res["dirtyCount"], 1)
            head_after = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=str(cav_local), text=True).strip()
            self.assertEqual(head_before, head_after)
            self.assertFalse(any("--rebase" in c for c in recorded_cmds), f"Unexpected rebase call: {recorded_cmds}")

            # Clean OmniAgent repo must fast-forward via `--ff-only`
            recorded_cmds.clear()
            with mock.patch.object(daemon, "run_subprocess", side_effect=_spy_run):
                p_res = daemon._safe_ff_only_pull(omni_local, remote="origin", branch="master")
            self.assertTrue(p_res["ok"])
            self.assertEqual(p_res["status"], "pulled")
            self.assertEqual(p_res["dirtyCount"], 0)
            self.assertTrue(any(c[:3] == ["git", "pull", "--ff-only"] for c in recorded_cmds))
            self.assertFalse(any("--rebase" in c for c in recorded_cmds))
            self.assertEqual((omni_local / "README.md").read_text(encoding="utf-8"), "v2\n")

            # /local/git/pull_all HTTP endpoint reports mixed triad quarantine + SO_KEEPALIVE enabled
            srv = daemon.start_local_fs_server(self.KEY, cav_local, host="127.0.0.1", port=0)
            self.assertIsNotNone(srv)
            assert srv is not None
            try:
                keepalive = srv.socket.getsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE)
                self.assertNotEqual(keepalive, 0)
                key = daemon.derive_local_fs_key(self.KEY)
                base = f"http://127.0.0.1:{srv.server_address[1]}"
                req = urllib.request.Request(
                    f"{base}/local/git/pull_all",
                    data=b"{}",
                    headers={"X-Omni-Local-Key": key, "Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=10) as resp:
                    body = json.loads(resp.read().decode("utf-8"))
                by_alias = {r["alias"]: r for r in body["results"]}
                self.assertEqual(by_alias["cavern"]["status"], "quarantined_dirty")
                self.assertFalse(by_alias["cavern"]["ok"])
                self.assertEqual(by_alias["omniagent"]["status"], "pulled")
                self.assertTrue(by_alias["omniagent"]["ok"])
            finally:
                srv.shutdown()
                srv.server_close()

    def test_daemon_errno_104_connection_reset_detection_and_exponential_backoff(self) -> None:
        import errno
        import urllib.error
        from unittest import mock

        self.assertTrue(daemon._is_connection_reset_error(ConnectionResetError(errno.ECONNRESET, "Connection reset by peer")))
        self.assertTrue(daemon._is_connection_reset_error(OSError(errno.ECONNRESET, "reset")))
        self.assertTrue(daemon._is_connection_reset_error(urllib.error.URLError(ConnectionResetError(104, "Connection reset by peer"))))
        self.assertTrue(daemon._is_connection_reset_error(RuntimeError("Errno 104 connection reset")))
        self.assertFalse(daemon._is_connection_reset_error(ValueError("unrelated error")))

        with tempfile.TemporaryDirectory() as td:
            d = daemon.OmniAgentDaemon("https://example.workers.dev", self.KEY, Path(td))
            sleeps: list[float] = []
            call_count = 0

            def _fake_http_json(method, url, headers=None, payload=None, timeout=20):
                nonlocal call_count
                call_count += 1
                raise ConnectionResetError(errno.ECONNRESET, "Connection reset by peer")

            def _fake_sleep(sec: float) -> None:
                sleeps.append(sec)
                if len(sleeps) >= 3:
                    d.running = False

            with mock.patch.object(daemon.proto, "http_json", side_effect=_fake_http_json), \
                 mock.patch.object(d, "probe_heartbeat_interval", return_value=9999.0), \
                 mock.patch.object(d, "send_heartbeat", return_value=None), \
                 mock.patch.object(daemon, "start_local_fs_server", return_value=None), \
                 mock.patch.object(daemon.time, "sleep", side_effect=_fake_sleep):
                d.run_forever()

            self.assertEqual(sleeps, [2.0, 4.0, 8.0])
            self.assertGreaterEqual(call_count, 3)

    def test_android_etag_and_user_initiated_job_invariants(self) -> None:
        root_hint = AGENT_DIR.parent.parent if AGENT_DIR.parent.name == ".agents" else AGENT_DIR.parent.parent / "Cavern"
        omni_hint = (AGENT_DIR.parent if AGENT_DIR.parent.name == "OmniAgent" else root_hint.parent / "OmniAgent")

        app_update = root_hint / "CavernSamples" / "CavernizeAndroid" / "Services" / "AppUpdateService.cs"
        if app_update.is_file():
            src = app_update.read_text(encoding="utf-8")
            self.assertIn("IfNoneMatch", src)
            self.assertIn("HttpStatusCode.NotModified", src)
            self.assertIn("CachedApiEntry", src)

        omni_update = omni_hint / "OmniAgentAndroid" / "Services" / "OmniUpdateService.cs"
        if omni_update.is_file():
            src_u = omni_update.read_text(encoding="utf-8")
            self.assertIn("IfNoneMatch", src_u)
            self.assertIn("HttpStatusCode.NotModified", src_u)
            self.assertIn("ETag", src_u)

        watchdog = omni_hint / "OmniAgentAndroid" / "Services" / "OmniWatchdogJobService.cs"
        fgs = omni_hint / "OmniAgentAndroid" / "Services" / "OmniAgentForegroundService.cs"
        if watchdog.is_file() and fgs.is_file():
            src_w = watchdog.read_text(encoding="utf-8")
            src_f = fgs.read_text(encoding="utf-8")
            self.assertIn("SetUserInitiated(true)", src_w)
            self.assertIn("ScheduleUserInitiated", src_w)
            self.assertIn("OmniAgentBinder", src_f)

    def test_wake_linux_daemon_and_single_shot_auto_wake(self) -> None:
        from unittest import mock

        # 1. Already online -> zero launch_termux_daemon dispatches
        with mock.patch.object(oac, "_is_channel_online", return_value=True), \
             mock.patch.object(oac, "send_agent_command") as m_send:
            res = oac.wake_linux_daemon("https://relay.test", self.KEY, wait_sec=2.0)
        self.assertTrue(res["ok"])
        self.assertTrue(res["alreadyOnline"])
        self.assertFalse(res["woke"])
        m_send.assert_not_called()

        # 2. Termux offline + android offline -> clean error, no dispatch
        with mock.patch.object(oac, "_is_channel_online", return_value=False), \
             mock.patch.object(oac, "send_agent_command") as m_send:
            res_off = oac.wake_linux_daemon("https://relay.test", self.KEY, wait_sec=2.0)
        self.assertFalse(res_off["ok"])
        self.assertIn("android channel is offline", res_off["error"])
        m_send.assert_not_called()

        # 3. Termux offline + android online -> dispatches launch_termux_daemon once & succeeds when heartbeat appears
        checks = {"termux": 0}

        def _fake_online(relay, secret, ch, admin=""):
            if ch == "android":
                return True
            if ch == "termux":
                checks["termux"] += 1
                return checks["termux"] >= 2  # offline on initial check, online on first poll
            return False

        with mock.patch.object(oac, "_is_channel_online", side_effect=_fake_online), \
             mock.patch.object(oac, "send_agent_command", return_value={"ok": True, "dispatched": True}) as m_send, \
             mock.patch.object(oac.time, "sleep", return_value=None):
            res_wake = oac.wake_linux_daemon("https://relay.test", self.KEY, wait_sec=3.0, poll_interval=0.2)
        self.assertTrue(res_wake["ok"])
        self.assertTrue(res_wake["woke"])
        self.assertFalse(res_wake["alreadyOnline"])
        m_send.assert_called_once()
        self.assertEqual(m_send.call_args.args[2], "launch_termux_daemon")
        self.assertEqual(m_send.call_args.kwargs["channel"], "android")
        self.assertFalse(m_send.call_args.kwargs["auto_wake"])

    def test_dual_lane_semantic_preservation_and_termux_handoff(self) -> None:
        import base64
        import gzip
        import os
        import subprocess
        import flight_recorder as fr

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ws = root / "ws"
            ws.mkdir()
            (ws / "demo.txt").write_text("v1\n", encoding="utf-8")
            local_dir = root / "fr_local"
            phone_bridge = root / "phone_bridge"
            phone_fr = phone_bridge / "flight_recorder"
            phone_repo = root / "phone_repo"
            phone_repo.mkdir()
            (phone_repo / "demo.txt").write_text("v1\n", encoding="utf-8")

            fr.init_session(workspace=ws, local_dir=local_dir, session_id="sess_dual_1", goal="Initial goal", use_remote=False)
            pin_res = fr.pin_session(
                workspace=ws,
                local_dir=local_dir,
                goal="Ship Instant Resume",
                completed=["Blueprint 100/100"],
                pending=["Slice R6 tests", "Ship"],
                next_step="Run test_omni_agent.py",
                summary="Slices R1-R5 wired",
                use_remote=False,
                background=False,
            )
            self.assertTrue(pin_res["ok"])
            self.assertTrue((local_dir / "semantic.json").is_file())

            # Simulate an auto_mirror save arriving with generic auto-mirror text
            (ws / "demo.txt").write_text("v2\n", encoding="utf-8")
            auto_res = fr.save_checkpoint(
                workspace=ws,
                local_dir=local_dir,
                summary="Live auto-mirror (1 modified, 0 deleted): demo.txt",
                auto_diff=True,
                source="auto_mirror",
                use_remote=False,
            )
            self.assertTrue(auto_res["ok"])
            cp = auto_res["checkpoint"]
            self.assertEqual(cp["goal"], "Ship Instant Resume")
            self.assertEqual(cp["pending"], ["Slice R6 tests", "Ship"])
            self.assertEqual(cp["nextStep"], "Run test_omni_agent.py")
            self.assertEqual(cp["summary"], "Slices R1-R5 wired")
            self.assertEqual(cp["source"], "auto_mirror")

            # Verify semantic.json was NOT overwritten by auto_mirror
            sem = fr.read_local_semantic(local_dir)
            self.assertIsNotNone(sem)
            self.assertEqual(sem["goal"], "Ship Instant Resume")
            self.assertEqual(sem["nextStep"], "Run test_omni_agent.py")

            # Verify Termux atomic sync runner preserves semantic.json & LATEST_HANDOFF.md across auto_mirror
            script = fr.build_termux_atomic_sync_script()
            phone_ag = root / "phone_ag"
            phone_bridge = phone_ag / "ai_studio_bridge"
            phone_fr = phone_bridge / "flight_recorder"
            manual_payload = {
                "frDir": str(phone_fr),
                "agDir": str(phone_ag),
                "repoDir": str(phone_repo),
                "checkpoint": {**cp, "source": "manual"},
                "semanticCheckpoint": sem,
                "diaryMd": fr.render_diary_markdown(cp),
                "applyLiveMirror": False,
                "syncAntigravityHandoff": True,
            }
            for payload in (
                manual_payload,
                {
                    "frDir": str(phone_fr),
                    "agDir": str(phone_ag),
                    "repoDir": str(phone_repo),
                    "checkpoint": {
                        **cp,
                        "source": "auto_mirror",
                        "summary": "Auto live-mirror (1 modified/added, 0 deleted)",
                        "nextStep": "",
                        "pending": [],
                        "goal": "",
                    },
                    "diaryMd": fr.render_diary_markdown(cp),
                    "applyLiveMirror": False,
                    "syncAntigravityHandoff": True,
                },
            ):
                b64 = base64.b64encode(gzip.compress(json.dumps(payload).encode("utf-8"))).decode("ascii")
                env = dict(os.environ)
                env["FR_PAYLOAD_B64"] = b64
                proc = subprocess.run([sys.executable, "-B", "-c", script], env=env, capture_output=True, text=True, check=True)
                out = json.loads(proc.stdout.strip().splitlines()[-1])
                self.assertTrue(out["ok"])

            handoff_md = (phone_bridge / "LATEST_HANDOFF.md").read_text(encoding="utf-8")
            self.assertIn("Ship Instant Resume", handoff_md)
            self.assertIn("Run test_omni_agent.py", handoff_md)
            self.assertIn("Slices R1-R5 wired", handoff_md)
            phone_latest = json.loads((phone_fr / "latest.json").read_text(encoding="utf-8"))
            self.assertEqual(phone_latest["goal"], "Ship Instant Resume")
            self.assertEqual(phone_latest["nextStep"], "Run test_omni_agent.py")

    def test_live_sync_watcher_hydration_filter_and_remote_rollback_guard(self) -> None:
        import hashlib
        from unittest import mock
        import flight_recorder as fr
        import live_sync_watcher as lsw

        # (a) Hydration filter silently adopts files whose content matches GitHub HEAD blob SHA-1
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ws = root / "ws"
            ws.mkdir()
            (ws / "hydrated.txt").write_text("old-local\n", encoding="utf-8")
            local_dir = root / "fr_local"

            watcher = lsw.LiveSyncWatcher(ws, local_dir, debounce_sec=0.0, use_remote=True, apply_live_mirror=False)
            watcher.ensure_baseline()
            head_bytes = b"exact-github-head-content\n"
            (ws / "hydrated.txt").write_bytes(head_bytes)
            head_blob = lsw._git_blob_sha1(head_bytes)

            with mock.patch.object(watcher, "_get_github_head_blobs", return_value={"hydrated.txt": head_blob}), \
                 mock.patch.object(fr, "save_checkpoint") as m_save:
                res = watcher.sync_once(force_immediate=True)
            self.assertFalse(res["synced"])
            self.assertEqual(res["reason"], "hydration_filtered")
            self.assertEqual(res["filtered"], 1)
            m_save.assert_not_called()

        # (b) Fresh container without local latest.json aborts push when remote active checkpoint shows rollback
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ws = root / "ws"
            ws.mkdir()
            (ws / "app.cs").write_text("older-version\n", encoding="utf-8")
            local_dir = root / "fr_fresh"

            watcher = lsw.LiveSyncWatcher(ws, local_dir, debounce_sec=0.0, use_remote=True, apply_live_mirror=False)
            watcher.ensure_baseline()
            (ws / "app.cs").write_text("container-rehydrated-edit\n", encoding="utf-8")

            remote_active_cp = {
                "sessionId": "prev-session-123",
                "seq": 4,
                "status": "active",
                "hasBundle": True,
                "goal": "Important previous work",
                "fileHashes": {
                    "app.cs": hashlib.sha256(b"newer-remote-work\n").hexdigest(),
                },
            }
            with mock.patch.object(watcher, "_get_github_head_blobs", return_value={}), \
                 mock.patch.object(fr, "_try_resolve_remote_auth", return_value=("https://relay.test", self.KEY, "")), \
                 mock.patch.object(fr, "read_cloud_tier", return_value=(remote_active_cp, None, "")), \
                 mock.patch.object(fr, "save_checkpoint") as m_save:
                res = watcher.sync_once(force_immediate=True)
            self.assertFalse(res["synced"])
            self.assertEqual(res["reason"], "remote_rollback_protected")
            status_obj = json.loads((local_dir / "watcher_status.json").read_text(encoding="utf-8"))
            self.assertEqual(status_obj["note"], "paused_remote_rollback_protected")
            m_save.assert_not_called()

    def test_resume_packet_compact_and_auto_restore_in_one_call(self) -> None:
        import flight_recorder as fr

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ws = root / "ws"
            docs = ws / ".agents" / "docs"
            docs.mkdir(parents=True)
            (docs / "HANDOFF.md").write_text("# Session Handoff\nHANDOFF|host=ai-studio-cloud\nDONE|init\nNEXT|continue\n", encoding="utf-8")
            (docs / "EXTRA_REQUESTS.md").write_text(
                "# Extra Request Queue\n## Pending Extra Requests\n- REQ|fast-check -> verify instant resume\n## Completed Requests\n",
                encoding="utf-8",
            )
            (ws / "target.txt").write_text("baseline-v1\n", encoding="utf-8")
            local_dir = root / "fr_local"

            fr.init_session(workspace=ws, local_dir=local_dir, session_id="sess_resume_1", goal="Resume Test Goal", use_remote=False)
            fr.pin_session(
                workspace=ws,
                local_dir=local_dir,
                goal="Resume Test Goal",
                pending=["Step A", "Step B"],
                next_step="Finish Step A",
                summary="Edited target.txt",
                use_remote=False,
                background=False,
            )
            (ws / "target.txt").write_text("edited-v2-important\n", encoding="utf-8")
            fr.save_checkpoint(workspace=ws, local_dir=local_dir, summary="Saved target.txt v2", auto_diff=True, use_remote=False)

            # Simulate AI Studio rollback reverting target.txt back to baseline
            (ws / "target.txt").write_text("baseline-v1\n", encoding="utf-8")

            packet = fr.resume_session(
                workspace=ws,
                local_dir=local_dir,
                auto_restore=True,
                use_remote=False,
                skip_github=True,
                skip_termux_diff=True,
            )
            self.assertTrue(packet["ok"])
            self.assertEqual(packet["verdict"], "RESTORED_AND_READY")
            self.assertEqual((ws / "target.txt").read_text(encoding="utf-8"), "edited-v2-important\n")
            self.assertEqual(packet["checkpoint"]["goal"], "Resume Test Goal")
            self.assertEqual(packet["checkpoint"]["pending"], ["Step A", "Step B"])

            rendered = fr.render_resume_packet(packet)
            lines = [ln for ln in rendered.splitlines() if ln.strip()]
            self.assertLessEqual(len(lines), 35)
            self.assertIn("GOAL|Resume Test Goal", rendered)
            self.assertIn("PENDING|Step A ; Step B", rendered)
            self.assertIn("EXTRA_REQ|", rendered)
            self.assertIn("AUTO_RESTORE|", rendered)

    def test_termux_content_addressed_reconcile_ff_pull_preserves_manual_edits(self) -> None:
        import shutil
        import subprocess
        import flight_recorder as fr

        if not shutil.which("git"):
            self.skipTest("git binary not available")

        def _git(cwd: Path, *args: str) -> str:
            r = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True)
            return r.stdout.strip()

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            bare = root / "remote.git"
            seed = root / "seed"
            phone = root / "phone"
            state_path = root / "live_mirror_state.json"

            _git(root, "init", "--bare", str(bare))
            _git(root, "clone", str(bare), str(seed))
            _git(seed, "config", "user.email", "test@example.com")
            _git(seed, "config", "user.name", "Test User")
            _git(seed, "checkout", "-b", "master")

            (seed / "tracked.txt").write_text("base-tracked\n", encoding="utf-8")
            (seed / "manual.txt").write_text("base-manual\n", encoding="utf-8")
            _git(seed, "add", ".")
            _git(seed, "commit", "-m", "init")
            _git(seed, "push", "-u", "origin", "master")

            # Clone phone repo at commit 1
            _git(root, "clone", str(bare), str(phone))
            _git(phone, "config", "user.email", "phone@example.com")
            _git(phone, "config", "user.name", "Phone User")

            # Advance remote by 1 commit: modify tracked.txt and add new_remote.txt
            (seed / "tracked.txt").write_text("new-tracked-on-origin\n", encoding="utf-8")
            (seed / "new_remote.txt").write_text("brand-new-on-origin\n", encoding="utf-8")
            _git(seed, "add", ".")
            _git(seed, "commit", "-m", "advance")
            _git(seed, "push", "origin", "master")
            expected_head = _git(seed, "rev-parse", "--short", "HEAD")

            # Simulate stale phone state:
            # (a) tracked.txt already matches origin/master (NOT listed in live_mirror_state.json)
            (phone / "tracked.txt").write_text("new-tracked-on-origin\n", encoding="utf-8")
            # (b) untracked new_remote.txt already matches origin/master
            (phone / "new_remote.txt").write_text("brand-new-on-origin\n", encoding="utf-8")
            # (c) genuine manual edit to manual.txt NOT in origin/master
            (phone / "manual.txt").write_text("my-local-phone-edit\n", encoding="utf-8")
            state_path.write_text(json.dumps({"mirrored": {}}), encoding="utf-8")

            script = fr.build_termux_reconcile_pull_script("origin", "master", state_path=str(state_path))
            proc = subprocess.run([sys.executable, "-B", "-c", script], cwd=str(phone), capture_output=True, text=True, check=True)
            res = json.loads(proc.stdout.strip().splitlines()[-1])

            self.assertTrue(res["ok"], res)
            self.assertEqual(res["mode"], "ff-only")
            self.assertEqual(res["head"], expected_head)
            self.assertIn("tracked.txt", res["reconciled"])
            self.assertIn("new_remote.txt", res["reconciled"])
            self.assertIn("manual.txt", res["preservedManual"])
            self.assertEqual((phone / "manual.txt").read_text(encoding="utf-8"), "my-local-phone-edit\n")
            self.assertEqual((phone / "tracked.txt").read_text(encoding="utf-8"), "new-tracked-on-origin\n")
            self.assertEqual((phone / "new_remote.txt").read_text(encoding="utf-8"), "brand-new-on-origin\n")

    def test_pick_cavernize_channel_auto_fallback(self) -> None:
        orig_http = proto.http_json
        try:
            proto.http_json = lambda *a, **kw: (
                200,
                {
                    "ok": True,
                    "channels": {
                        "android": {"online": False, "standbyReachable": True},
                        "cavernize": {"online": True},
                    },
                },
            )
            self.assertEqual(oac.pick_cavernize_channel("https://example.workers.dev", self.KEY, "auto"), "cavernize")
            self.assertEqual(oac.pick_cavernize_channel("https://example.workers.dev", self.KEY, "android"), "android")

            proto.http_json = lambda *a, **kw: (
                200,
                {
                    "ok": True,
                    "channels": {
                        "android": {"online": True},
                        "cavernize": {"online": True},
                    },
                },
            )
            self.assertEqual(oac.pick_cavernize_channel("https://example.workers.dev", self.KEY, "auto"), "android")
        finally:
            proto.http_json = orig_http

    def test_q2_cavernize_watchdog_native_and_timed_ping_fallback(self) -> None:
        from unittest import mock

        payload = oac.build_cavernize_watchdog_payload(timeout_sec=1, reset_baseline=True)
        self.assertEqual(payload, {"timeoutSec": 2, "resetBaseline": True})

        # 1. Native OmniAgent `cavernize_watchdog` command succeeds directly when advertised
        native_resp = {
            "ok": True,
            "state": "ALIVE",
            "alive": True,
            "pid": 14200,
            "sessionId": "sid-a",
            "elapsedMs": 14,
        }
        st_with_cap = (200, {"ok": True, "channels": {"android": {"capabilities": ["ping", "cavernize_cmd", "cavernize_watchdog"]}}})
        with mock.patch.object(proto, "http_json", return_value=st_with_cap), \
             mock.patch.object(oac, "send_agent_command", return_value=native_resp) as m_send:
            w1 = oac.probe_cavernize_watchdog("https://example.workers.dev", self.KEY, timeout_sec=6)
        self.assertTrue(w1["ok"])
        self.assertEqual(w1["state"], "ALIVE")
        self.assertEqual(w1["pid"], 14200)
        self.assertEqual(w1["mode"], "external_omniagent")
        self.assertEqual(m_send.call_args.args[2], "cavernize_watchdog")

        # 2. Older OmniAgent APK without `cavernize_watchdog` capability skips straight to externally timed `cavernize_cmd ping`
        ping_body = json.dumps({"status": "online", "pid": 14201, "sessionId": "sid-b", "uiHostAttached": True, "appVersion": "2.9.83"})
        st_no_cap = (200, {"ok": True, "channels": {"android": {"capabilities": ["ping", "cavernize_cmd"]}}})
        with mock.patch.object(proto, "http_json", return_value=st_no_cap), \
             mock.patch.object(oac, "send_agent_command", return_value={"ok": True, "elapsedMs": 9, "body": ping_body}) as m_fb:
            w2 = oac.probe_cavernize_watchdog("https://example.workers.dev", self.KEY, timeout_sec=6)
        self.assertTrue(w2["ok"])
        self.assertTrue(w2["alive"])
        self.assertEqual(w2["state"], "ALIVE")
        self.assertEqual(w2["pid"], 14201)
        self.assertEqual(w2["mode"], "external_timed_ping_fallback")
        m_fb.assert_called_once()
        self.assertEqual(m_fb.call_args.args[2], "cavernize_cmd")

        # 3. Fallback timed ping times out -> classified as UNRESPONSIVE_ANR
        with mock.patch.object(proto, "http_json", return_value=st_no_cap), \
             mock.patch.object(oac, "send_agent_command", return_value={"ok": False, "error": "Timeout waiting for android response"}):
            w3 = oac.probe_cavernize_watchdog("https://example.workers.dev", self.KEY, timeout_sec=5)
        self.assertFalse(w3["alive"])
        self.assertEqual(w3["state"], "UNRESPONSIVE_ANR")


if __name__ == "__main__":
    unittest.main(verbosity=2)




