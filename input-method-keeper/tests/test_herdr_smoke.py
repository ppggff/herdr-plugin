import importlib.util
import io
import json
import shlex
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


class StateBackupTests(unittest.TestCase):
    def test_backup_restore_tracks_only_session_state_files(self):
        with TemporaryDirectory() as temp_dir:
            state_dir = Path(temp_dir)
            session_a = state_dir / "sessions" / "default-a"
            session_b = state_dir / "sessions" / "default-b"
            session_a.mkdir(parents=True)
            (state_dir / "run.lock").write_text("lock-before", encoding="utf-8")
            (session_a / "state.json").write_text('{"panes":{"p1":{}}}\n', encoding="utf-8")
            (session_a / "focus.dirty").write_text("dirty-before", encoding="utf-8")
            (session_a / "focus.log").write_text("log-before\n", encoding="utf-8")

            backup = herdr_smoke.backup_state(state_dir)

            (state_dir / "run.lock").write_text("lock-after", encoding="utf-8")
            (session_a / "state.json").write_text('{"panes":{"p2":{}}}\n', encoding="utf-8")
            (session_a / "focus.dirty").unlink()
            (session_a / "focus.log").write_text("log-after\n", encoding="utf-8")
            session_b.mkdir()
            (session_b / "state.json").write_text('{"panes":{"test":{}}}\n', encoding="utf-8")

            herdr_smoke.restore_state(backup)

            self.assertEqual((session_a / "state.json").read_text(encoding="utf-8"), '{"panes":{"p1":{}}}\n')
            self.assertEqual((session_a / "focus.dirty").read_text(encoding="utf-8"), "dirty-before")
            self.assertFalse((session_b / "state.json").exists())
            self.assertEqual((session_a / "focus.log").read_text(encoding="utf-8"), "log-after\n")
            self.assertEqual((state_dir / "run.lock").read_text(encoding="utf-8"), "lock-after")

    def test_state_dir_from_status_uses_focus_log_path(self):
        status = {
            "focus_log_path": "/tmp/herdr/plugins/local.input-method-keeper/sessions/default/focus.log"
        }

        self.assertEqual(
            herdr_smoke.state_dir_from_status(status),
            Path("/tmp/herdr/plugins/local.input-method-keeper"),
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

    def test_restore_state_from_pane_replays_backup_payload(self):
        with TemporaryDirectory() as temp_dir:
            state_dir = Path(temp_dir)
            session_a = state_dir / "sessions" / "default-a"
            session_b = state_dir / "sessions" / "default-b"
            session_a.mkdir(parents=True)
            (session_a / "state.json").write_text('{"panes":{"p1":{}}}\n', encoding="utf-8")
            backup = herdr_smoke.backup_state(state_dir)
            (session_a / "state.json").write_text('{"panes":{"p2":{}}}\n', encoding="utf-8")
            session_b.mkdir()
            (session_b / "state.json").write_text('{"panes":{"test":{}}}\n', encoding="utf-8")

            original_current_pane = herdr_smoke.current_pane
            original_herdr = herdr_smoke.herdr
            calls = []

            def fake_current_pane(session):
                return {"pane_id": "pane-1"}

            def fake_herdr(args, session=None, check=True, echo=True):
                calls.append(list(args))
                if args[:2] == ["pane", "run"]:
                    subprocess.run(
                        shlex.split(args[3]),
                        check=True,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                    )
                return herdr_smoke.Command(list(args), json.dumps({"result": {}}), "", 0)

            try:
                herdr_smoke.current_pane = fake_current_pane
                herdr_smoke.herdr = fake_herdr
                herdr_smoke.restore_state_from_pane(backup, None)
            finally:
                herdr_smoke.current_pane = original_current_pane
                herdr_smoke.herdr = original_herdr

            self.assertEqual((session_a / "state.json").read_text(encoding="utf-8"), '{"panes":{"p1":{}}}\n')
            self.assertFalse((session_b / "state.json").exists())
            self.assertEqual(calls[0][:3], ["pane", "run", "pane-1"])
            self.assertEqual(calls[1][:2], ["wait", "output"])


class ManifestCoverageTests(unittest.TestCase):
    def test_required_actions_match_manifest_actions(self):
        manifest = tomllib.loads((ROOT / "herdr-plugin.toml").read_text(encoding="utf-8"))
        manifest_actions = {action["id"] for action in manifest.get("actions", [])}

        self.assertEqual(herdr_smoke.REQUIRED_ACTIONS, manifest_actions)


if __name__ == "__main__":
    unittest.main()
