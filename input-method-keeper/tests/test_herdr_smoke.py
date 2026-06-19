import importlib.util
import io
import json
import os
import subprocess
import sys
import unittest
import contextlib
import tomllib
from pathlib import Path
from tempfile import TemporaryDirectory


ROOT = Path(__file__).resolve().parents[1]
SMOKE_PATH = ROOT / "scripts" / "herdr_smoke.py"
spec = importlib.util.spec_from_file_location("herdr_smoke", SMOKE_PATH)
herdr_smoke = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules["herdr_smoke"] = herdr_smoke
spec.loader.exec_module(herdr_smoke)


class PaneShellCaptureTests(unittest.TestCase):
    def test_pane_shell_capture_suppresses_noisy_herdr_output(self):
        token = "IME_KEEPER_CAPTURE_123_1000"
        output_path = Path("/tmp") / f"ime-keeper-capture-{token}.txt"
        calls = []
        original_getpid = herdr_smoke.os.getpid
        original_time = herdr_smoke.time.time
        original_herdr = herdr_smoke.herdr

        def fake_herdr(args, session=None, check=True, echo=True):
            calls.append((list(args), echo))
            if args[:2] == ["pane", "run"]:
                output_path.write_text("captured\n", encoding="utf-8")
                return herdr_smoke.Command(list(args), "", "", 0)
            if args[:2] == ["wait", "output"]:
                return herdr_smoke.Command(
                    list(args),
                    json.dumps({"result": {"matched_line": f"{token}:0"}}),
                    "",
                    0,
                )
            raise AssertionError(f"unexpected herdr call: {args}")

        try:
            herdr_smoke.os.getpid = lambda: 123
            herdr_smoke.time.time = lambda: 1.0
            herdr_smoke.herdr = fake_herdr

            result = herdr_smoke.pane_shell_capture("w1:p1", None, "macism")

            self.assertEqual(result.stdout, "captured\n")
            self.assertEqual(result.returncode, 0)
            self.assertEqual([echo for _, echo in calls], [False, False])
        finally:
            herdr_smoke.os.getpid = original_getpid
            herdr_smoke.time.time = original_time
            herdr_smoke.herdr = original_herdr
            try:
                output_path.unlink()
            except FileNotFoundError:
                pass


class CliOptionTests(unittest.TestCase):
    def test_real_actions_requires_full_ime(self):
        stderr = io.StringIO()

        with contextlib.redirect_stderr(stderr):
            code = herdr_smoke.main(["--real-actions"])

        self.assertEqual(code, 1)
        self.assertIn("--real-actions requires --full-ime", stderr.getvalue())


class PluginLogWaitTests(unittest.TestCase):
    def test_wait_for_event_after_uses_log_ids_not_start_milliseconds(self):
        original_plugin_logs = herdr_smoke.plugin_logs

        def fake_plugin_logs(plugin_id, session, limit=50):
            return [
                {
                    "log_id": "old",
                    "event": "pane.focused",
                    "started_unix_ms": 1000,
                    "status": "completed",
                    "exit_code": 0,
                },
                {
                    "log_id": "new",
                    "event": "pane.focused",
                    "started_unix_ms": 1000,
                    "status": "completed",
                    "exit_code": 0,
                },
            ]

        try:
            herdr_smoke.plugin_logs = fake_plugin_logs

            event = herdr_smoke.wait_for_event_after(
                "ppggff.input-method-keeper",
                "pane.focused",
                {"old"},
                None,
                timeout=0.1,
            )
        finally:
            herdr_smoke.plugin_logs = original_plugin_logs

        self.assertEqual(event["log_id"], "new")


class StateBackupTests(unittest.TestCase):
    def test_backup_restore_tracks_only_current_session_state_files(self):
        with TemporaryDirectory() as temp_dir:
            state_dir = Path(temp_dir)
            session_a = state_dir / "sessions" / "default-a"
            session_b = state_dir / "sessions" / "default-b"
            session_a.mkdir(parents=True)
            session_b.mkdir()
            (state_dir / "run.lock").write_text("lock-before", encoding="utf-8")
            (session_a / "state.json").write_text('{"panes":{"p1":{}}}\n', encoding="utf-8")
            (session_a / "focus.dirty").write_text("dirty-before", encoding="utf-8")
            (session_a / "focus.log").write_text("log-before\n", encoding="utf-8")
            (session_b / "state.json").write_text('{"panes":{"other":{}}}\n', encoding="utf-8")

            backup = herdr_smoke.backup_state(session_a)

            (state_dir / "run.lock").write_text("lock-after", encoding="utf-8")
            (session_a / "state.json").write_text('{"panes":{"p2":{}}}\n', encoding="utf-8")
            (session_a / "focus.dirty").unlink()
            (session_a / "focus.log").write_text("log-after\n", encoding="utf-8")
            (session_b / "state.json").write_text('{"panes":{"test":{}}}\n', encoding="utf-8")

            herdr_smoke.restore_state(backup)

            self.assertEqual((session_a / "state.json").read_text(encoding="utf-8"), '{"panes":{"p1":{}}}\n')
            self.assertEqual((session_a / "focus.dirty").read_text(encoding="utf-8"), "dirty-before")
            self.assertEqual((session_b / "state.json").read_text(encoding="utf-8"), '{"panes":{"test":{}}}\n')
            self.assertEqual((session_a / "focus.log").read_text(encoding="utf-8"), "log-after\n")
            self.assertEqual((state_dir / "run.lock").read_text(encoding="utf-8"), "lock-after")

    def test_session_dir_from_status_uses_focus_log_parent(self):
        status = {
            "focus_log_path": "/tmp/herdr/plugins/ppggff.input-method-keeper/sessions/default/focus.log"
        }

        self.assertEqual(
            herdr_smoke.session_dir_from_status(status),
            Path("/tmp/herdr/plugins/ppggff.input-method-keeper/sessions/default"),
        )

    def test_write_state_backup_file_falls_back_when_tmp_write_is_denied(self):
        with TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "state.json"
            original_write_bytes = herdr_smoke.Path.write_bytes

            def fake_write_bytes(path, data):
                if path.name.endswith(".restore-tmp"):
                    raise PermissionError("tmp denied")
                return original_write_bytes(path, data)

            try:
                herdr_smoke.Path.write_bytes = fake_write_bytes
                herdr_smoke.write_state_backup_file(target, b"restored")
            finally:
                herdr_smoke.Path.write_bytes = original_write_bytes

            self.assertEqual(target.read_bytes(), b"restored")
            self.assertFalse((Path(temp_dir) / "state.json.restore-tmp").exists())

    def test_state_restore_preflight_checks_write_and_delete_permissions(self):
        with TemporaryDirectory() as temp_dir:
            state_dir = Path(temp_dir)
            session_a = state_dir / "sessions" / "default-a"
            session_a.mkdir(parents=True)
            state_path = session_a / "state.json"
            state_path.write_text('{"panes":{"p1":{}}}\n', encoding="utf-8")
            backup = herdr_smoke.backup_state(session_a)

            herdr_smoke.assert_state_restore_writable(backup)

            self.assertEqual(state_path.read_text(encoding="utf-8"), '{"panes":{"p1":{}}}\n')
            self.assertFalse(list(session_a.glob(".smoke-restore-probe-*")))

    def test_state_restore_preflight_fails_before_destructive_actions_when_unwritable(self):
        with TemporaryDirectory() as temp_dir:
            state_dir = Path(temp_dir)
            session_a = state_dir / "sessions" / "default-a"
            session_a.mkdir(parents=True)
            (session_a / "state.json").write_text('{"panes":{"p1":{}}}\n', encoding="utf-8")
            backup = herdr_smoke.backup_state(session_a)
            original_write_state_backup_file = herdr_smoke.write_state_backup_file

            def fake_write_state_backup_file(path, data):
                raise PermissionError("denied")

            try:
                herdr_smoke.write_state_backup_file = fake_write_state_backup_file
                with self.assertRaises(herdr_smoke.SmokeFailure) as caught:
                    herdr_smoke.assert_state_restore_writable(backup)
            finally:
                herdr_smoke.write_state_backup_file = original_write_state_backup_file

            self.assertIn("state restore preflight failed", str(caught.exception))


class ConfigWriteTests(unittest.TestCase):
    def test_write_config_for_smoke_does_not_fallback_to_pane_on_permission_error(self):
        original_write_config = herdr_smoke.write_config

        def fake_write_config(path, config):
            raise PermissionError("denied")

        try:
            herdr_smoke.write_config = fake_write_config

            with self.assertRaises(PermissionError):
                herdr_smoke.write_config_for_smoke(Path("/tmp/config.json"), {"enabled": True})
        finally:
            herdr_smoke.write_config = original_write_config

    def test_restore_config_for_smoke_does_not_fallback_to_pane_on_permission_error(self):
        original_restore_config = herdr_smoke.restore_config

        def fake_restore_config(path, data):
            raise PermissionError("denied")

        try:
            herdr_smoke.restore_config = fake_restore_config

            with self.assertRaises(PermissionError):
                herdr_smoke.restore_config_for_smoke(Path("/tmp/config.json"), b"{}")
        finally:
            herdr_smoke.restore_config = original_restore_config


class HelperWrapperTests(unittest.TestCase):
    def test_helper_recovers_stale_pidless_compile_lock(self):
        with TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            fake_swiftc = temp_path / "swiftc"
            fake_swiftc.write_text(
                """#!/bin/sh
set -eu
out=""
while [ "$#" -gt 0 ]; do
  if [ "$1" = "-o" ]; then
    shift
    out="$1"
  fi
  shift || true
done
if [ -z "$out" ]; then
  printf '%s\\n' 'missing -o' >&2
  exit 2
fi
cat > "$out" <<'BIN'
#!/bin/sh
case "${1:-}" in
  current) printf '%s\\n' 'fake.current' ;;
  *) printf '%s\\n' "fake ${1:-}" ;;
esac
BIN
chmod +x "$out"
""",
                encoding="utf-8",
            )
            fake_swiftc.chmod(0o755)
            state_dir = temp_path / "state"
            lock_dir = state_dir / "helper-build" / ".compile.lock"
            lock_dir.mkdir(parents=True)
            os.utime(lock_dir, (946684800, 946684800))

            env = os.environ.copy()
            env["HERDR_PLUGIN_STATE_DIR"] = str(state_dir)
            env["HERDR_IME_HELPER_SWIFTC"] = str(fake_swiftc)
            result = subprocess.run(
                [str(ROOT / "bin" / "herdr-ime-helper"), "current"],
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "fake.current\n")
            self.assertFalse(lock_dir.exists())
            self.assertTrue((state_dir / "helper-build" / "herdr-ime-helper").is_file())


class ManifestCoverageTests(unittest.TestCase):
    def test_required_actions_match_manifest_actions(self):
        manifest = tomllib.loads((ROOT / "herdr-plugin.toml").read_text(encoding="utf-8"))
        manifest_actions = {action["id"] for action in manifest.get("actions", [])}

        self.assertEqual(herdr_smoke.REQUIRED_ACTIONS, manifest_actions)


if __name__ == "__main__":
    unittest.main()
