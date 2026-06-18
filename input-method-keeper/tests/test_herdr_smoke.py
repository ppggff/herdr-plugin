import importlib.util
import io
import json
import sys
import unittest
import contextlib
from pathlib import Path


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


if __name__ == "__main__":
    unittest.main()
