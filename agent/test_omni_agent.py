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


if __name__ == "__main__":
    unittest.main(verbosity=2)



