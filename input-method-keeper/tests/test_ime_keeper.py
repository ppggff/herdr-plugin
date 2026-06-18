import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

import ime_keeper


class FakeBackend:
    def __init__(self, currents):
        self.currents = list(currents)
        self.selected = []

    def current(self):
        if self.currents:
            return self.currents.pop(0)
        return ""

    def select(self, input_source_id):
        self.selected.append(input_source_id)
        return ime_keeper.CommandResult(True, "", "")

    def doctor(self):
        return ime_keeper.CommandResult(True, "", "")


class FailingBackend(FakeBackend):
    def __init__(self):
        super().__init__([])

    def current(self):
        raise RuntimeError("backend failed")

    def select(self, input_source_id):
        raise RuntimeError("backend failed")


class FakeHerdr:
    def __init__(self, pane_ids):
        self.pane_ids = list(pane_ids)
        self.notifications = []
        self.pane_statuses = []
        self.workspaces = []
        self.tabs = []
        self.panes = []

    def current_pane(self):
        pane_id = self.pane_ids.pop(0) if self.pane_ids else ""
        if not pane_id:
            return None
        return {
            "pane_id": pane_id,
            "workspace_id": pane_id.split(":")[0],
            "tab_id": f"{pane_id.split(':')[0]}:t1",
            "cwd": "/repo",
            "agent": "codex",
        }

    def doctor(self):
        return ime_keeper.CommandResult(True, "", "")

    def show_notification(self, title, body):
        self.notifications.append({"title": title, "body": body})
        return ime_keeper.CommandResult(True, "", "")

    def report_pane_status(self, pane_id, status, ttl_ms):
        self.pane_statuses.append(
            {"pane_id": pane_id, "status": status, "ttl_ms": ttl_ms}
        )
        return ime_keeper.CommandResult(True, "", "")

    def list_workspaces(self):
        return list(self.workspaces)

    def list_tabs(self, workspace_id=None):
        if workspace_id:
            return [tab for tab in self.tabs if tab.get("workspace_id") == workspace_id]
        return list(self.tabs)

    def list_panes(self, workspace_id=None):
        if workspace_id:
            return [pane for pane in self.panes if pane.get("workspace_id") == workspace_id]
        return list(self.panes)


class TempEnvTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.config_dir = self.root / "config"
        self.state_dir = self.root / "state"
        self.config_dir.mkdir()
        self.state_dir.mkdir()
        self.env = {
            "HERDR_PLUGIN_CONFIG_DIR": str(self.config_dir),
            "HERDR_PLUGIN_STATE_DIR": str(self.state_dir),
            "HERDR_SOCKET_PATH": "/Users/me/Library/Application Support/herdr/sessions/work/herdr.sock",
        }

    def tearDown(self):
        self.tmp.cleanup()

    def write_config(self, **overrides):
        config = ime_keeper.default_config()
        config.update(overrides)
        path = self.config_dir / "config.json"
        path.write_text(json.dumps(config), encoding="utf-8")
        return config


class SessionIdentityTests(TempEnvTest):
    def test_auto_session_name_uses_readable_label_and_socket_hash(self):
        self.write_config(session_name="auto")

        identity = ime_keeper.session_identity(
            ime_keeper.load_config(self.config_dir, readonly=True), self.env
        )

        self.assertEqual(identity.label, "work")
        self.assertRegex(identity.key, r"^work-[0-9a-f]{12}$")
        self.assertTrue(identity.socket_path_hash.startswith("sha256:"))

    def test_explicit_session_name_keeps_sessions_distinct_by_socket(self):
        self.write_config(session_name="shared")
        config = ime_keeper.load_config(self.config_dir, readonly=True)

        first = ime_keeper.session_identity(config, self.env)
        second_env = dict(self.env, HERDR_SOCKET_PATH="/tmp/other/herdr.sock")
        second = ime_keeper.session_identity(config, second_env)

        self.assertEqual(first.label, "shared")
        self.assertEqual(second.label, "shared")
        self.assertNotEqual(first.key, second.key)


class StateStoreTests(TempEnvTest):
    def test_status_read_only_state_load_does_not_repair_invalid_state(self):
        self.write_config()
        identity = ime_keeper.session_identity(
            ime_keeper.load_config(self.config_dir, readonly=True), self.env
        )
        session_dir = self.state_dir / "sessions" / identity.key
        session_dir.mkdir(parents=True)
        state_path = session_dir / "state.json"
        state_path.write_text("{broken", encoding="utf-8")

        store = ime_keeper.StateStore(self.state_dir, identity)
        state, diagnostic = store.load(readonly=True)

        self.assertIsNone(state)
        self.assertIn("invalid", diagnostic)
        self.assertEqual(state_path.read_text(encoding="utf-8"), "{broken")
        self.assertEqual(list(session_dir.glob("state.json.broken.*")), [])

    def test_writable_state_load_repairs_invalid_state(self):
        self.write_config()
        identity = ime_keeper.session_identity(
            ime_keeper.load_config(self.config_dir, readonly=True), self.env
        )
        session_dir = self.state_dir / "sessions" / identity.key
        session_dir.mkdir(parents=True)
        (session_dir / "state.json").write_text("{broken", encoding="utf-8")

        store = ime_keeper.StateStore(self.state_dir, identity)
        state, diagnostic = store.load(readonly=False)

        self.assertEqual(state["version"], 1)
        self.assertIn("repaired", diagnostic)
        self.assertEqual(len(list(session_dir.glob("state.json.broken.*"))), 1)

    def test_reconcile_policy_clears_current_session_state_and_dirty_marker(self):
        self.write_config(default_action="reset")
        identity = ime_keeper.session_identity(
            ime_keeper.load_config(self.config_dir, readonly=True), self.env
        )
        store = ime_keeper.StateStore(self.state_dir, identity)
        store.save(ime_keeper.empty_state(identity))
        store.mark_dirty({"pane_id": "w1:p1"})

        mode = ime_keeper.reconcile_state_policy(
            ime_keeper.load_config(self.config_dir, readonly=True), store, "test"
        )

        self.assertEqual(mode, "reset")
        self.assertFalse(store.state_path.exists())
        self.assertFalse(store.dirty_path.exists())


class BackendTests(unittest.TestCase):
    def test_ensure_input_source_skips_select_when_already_current(self):
        backend = FakeBackend(["com.apple.keylayout.ABC"])

        result = ime_keeper.ensure_input_source(backend, "com.apple.keylayout.ABC")

        self.assertEqual(result, "already-current")
        self.assertEqual(backend.selected, [])


class DebugLoggingTests(TempEnvTest):
    def test_debug_log_rotation_threshold_is_100mb(self):
        self.assertEqual(ime_keeper.DEBUG_LOG_MAX_BYTES, 100 * 1024 * 1024)

    def test_debug_log_uses_timestamped_current_filename(self):
        self.write_config(debug=True)
        context = ime_keeper.HerdrContext.from_env(self.env)
        store = ime_keeper.StateStore(self.state_dir, context.identity)

        ime_keeper.log_debug(store, context.config, {"event": "test"})

        current_name = store.debug_current_path.read_text(encoding="utf-8").strip()
        self.assertRegex(current_name, r"^debug\.\d{8}T\d{12}Z\.log$")
        self.assertTrue((store.session_dir / current_name).exists())
        self.assertFalse((store.session_dir / "debug.log").exists())

    def test_debug_log_rotation_switches_current_timestamped_file(self):
        self.write_config(debug=True)
        context = ime_keeper.HerdrContext.from_env(self.env)
        store = ime_keeper.StateStore(self.state_dir, context.identity)
        store.session_dir.mkdir(parents=True)
        old_path = store.session_dir / "debug.20260618T010203000001Z.log"
        old_path.write_text("old log line\n", encoding="utf-8")
        store.debug_current_path.write_text(old_path.name + "\n", encoding="utf-8")
        original_limit = ime_keeper.DEBUG_LOG_MAX_BYTES

        try:
            ime_keeper.DEBUG_LOG_MAX_BYTES = 1
            ime_keeper.log_debug(store, context.config, {"event": "test"})
        finally:
            ime_keeper.DEBUG_LOG_MAX_BYTES = original_limit

        rotated = list(store.session_dir.glob("debug.*.log"))
        self.assertEqual(len(rotated), 2)
        current_name = store.debug_current_path.read_text(encoding="utf-8").strip()
        self.assertRegex(current_name, r"^debug\.\d{8}T\d{12}Z\.log$")
        self.assertNotEqual(current_name, old_path.name)
        self.assertEqual(old_path.read_text(encoding="utf-8"), "old log line\n")
        self.assertFalse((store.session_dir / "debug.log.1").exists())

    def test_debug_log_migrates_legacy_debug_log_to_timestamped_file(self):
        self.write_config(debug=True)
        context = ime_keeper.HerdrContext.from_env(self.env)
        store = ime_keeper.StateStore(self.state_dir, context.identity)
        store.session_dir.mkdir(parents=True)
        store.debug_path.write_text("legacy log line\n", encoding="utf-8")

        ime_keeper.log_debug(store, context.config, {"event": "test"})

        current_name = store.debug_current_path.read_text(encoding="utf-8").strip()
        log_path = store.session_dir / current_name
        lines = log_path.read_text(encoding="utf-8").splitlines()
        self.assertRegex(current_name, r"^debug\.\d{8}T\d{12}Z\.log$")
        self.assertEqual(lines[0], "legacy log line")
        self.assertEqual(json.loads(lines[-1])["event"], "test")
        self.assertFalse(store.debug_path.exists())


class EventParsingTests(unittest.TestCase):
    def test_parse_tab_closed_event_uses_data_fields(self):
        event = {
            "event": "tab_closed",
            "data": {"tab_id": "w1:t2", "workspace_id": "w1"},
        }

        parsed = ime_keeper.parse_event("tab.closed", event)

        self.assertEqual(parsed["tab_id"], "w1:t2")
        self.assertEqual(parsed["workspace_id"], "w1")

    def test_parse_pane_moved_event_uses_data_pane_snapshot(self):
        event = {
            "event": "pane_moved",
            "data": {
                "previous_pane_id": "w1:p1",
                "previous_workspace_id": "w1",
                "previous_tab_id": "w1:t1",
                "pane": {
                    "pane_id": "w2:p1",
                    "workspace_id": "w2",
                    "tab_id": "w2:t1",
                    "cwd": "/repo2",
                    "agent": "codex",
                },
            },
        }

        parsed = ime_keeper.parse_event("pane.moved", event)

        self.assertEqual(parsed["previous_pane_id"], "w1:p1")
        self.assertEqual(parsed["pane"]["pane_id"], "w2:p1")
        self.assertEqual(parsed["pane"]["workspace_id"], "w2")


class EventHandlerTests(TempEnvTest):
    def test_focus_reset_backend_failure_fails_open(self):
        self.write_config(default_action="reset", default_input_source="abc", debug=True)
        event = {"event": "pane_focused", "data": {"pane_id": "w1:p1", "workspace_id": "w1"}}

        code = ime_keeper.handle_event(
            "pane-focused",
            self.env,
            backend=FailingBackend(),
            herdr=FakeHerdr(["w1:p1", "w1:p1", "w1:p1"]),
            event=event,
            debounce_seconds=0,
        )

        self.assertEqual(code, 0)

    def test_tab_closed_removes_panes_for_that_tab(self):
        self.write_config(default_action="keep")
        context = ime_keeper.HerdrContext.from_env(self.env)
        store = ime_keeper.StateStore(self.state_dir, context.identity)
        state = ime_keeper.empty_state(context.identity)
        state["last_focused_pane_id"] = "w1:p1"
        state["panes"] = {
            "w1:p1": {"input_source_id": "abc", "workspace_id": "w1", "tab_id": "w1:t1"},
            "w1:p2": {"input_source_id": "abc", "workspace_id": "w1", "tab_id": "w1:t2"},
        }
        store.save(state)
        event = {"event": "tab_closed", "data": {"tab_id": "w1:t1", "workspace_id": "w1"}}

        ime_keeper.handle_event(
            "tab-closed", self.env, backend=FakeBackend([]), herdr=FakeHerdr([]), event=event
        )

        state, _ = store.load(readonly=True)
        self.assertNotIn("w1:p1", state["panes"])
        self.assertIn("w1:p2", state["panes"])
        self.assertIsNone(state["last_focused_pane_id"])

    def test_focus_keep_records_previous_source_and_restores_target(self):
        self.write_config(
            default_action="keep",
            default_input_source="com.apple.keylayout.ABC",
        )
        context = ime_keeper.HerdrContext.from_env(self.env)
        store = ime_keeper.StateStore(self.state_dir, context.identity)
        state = ime_keeper.empty_state(context.identity)
        state["last_focused_pane_id"] = "w1:p1"
        state["panes"] = {
            "w1:p2": {
                "input_source_id": "com.apple.inputmethod.SCIM.ITABC",
                "workspace_id": "w1",
                "tab_id": "w1:t1",
            }
        }
        store.save(state)
        event = {"event": "pane_focused", "data": {"pane_id": "w1:p2", "workspace_id": "w1"}}
        backend = FakeBackend(["com.apple.keylayout.ABC", "com.apple.keylayout.ABC"])
        herdr = FakeHerdr(["w1:p2", "w1:p2", "w1:p2", "w1:p2"])

        ime_keeper.handle_event(
            "pane-focused",
            self.env,
            backend=backend,
            herdr=herdr,
            event=event,
            debounce_seconds=0,
        )

        state, _ = store.load(readonly=True)
        self.assertEqual(
            state["panes"]["w1:p1"]["input_source_id"], "com.apple.keylayout.ABC"
        )
        self.assertEqual(state["last_focused_pane_id"], "w1:p2")
        self.assertEqual(backend.selected, ["com.apple.inputmethod.SCIM.ITABC"])

    def test_focus_keep_publishes_default_notification_and_pane_status(self):
        self.write_config(
            default_action="keep",
            default_input_source="com.apple.keylayout.ABC",
        )
        context = ime_keeper.HerdrContext.from_env(self.env)
        store = ime_keeper.StateStore(self.state_dir, context.identity)
        state = ime_keeper.empty_state(context.identity)
        state["last_focused_pane_id"] = "w1:p1"
        state["panes"] = {
            "w1:p2": {
                "input_source_id": "com.apple.inputmethod.SCIM.ITABC",
                "workspace_id": "w1",
                "tab_id": "w1:t1",
            }
        }
        store.save(state)
        event = {"event": "pane_focused", "data": {"pane_id": "w1:p2", "workspace_id": "w1"}}
        backend = FakeBackend(["com.apple.keylayout.ABC", "com.apple.keylayout.ABC"])
        herdr = FakeHerdr(["w1:p2", "w1:p2", "w1:p2", "w1:p2"])

        ime_keeper.handle_event(
            "pane-focused",
            self.env,
            backend=backend,
            herdr=herdr,
            event=event,
            debounce_seconds=0,
        )

        self.assertEqual(
            herdr.pane_statuses,
            [{"pane_id": "w1:p2", "status": "IME ITABC", "ttl_ms": 600000}],
        )
        self.assertEqual(herdr.notifications[0]["title"], "OLD  INIT: unknown -> ABC (p1 w1)")
        self.assertEqual(
            herdr.notifications[0]["body"],
            "NEW  SWCH: ABC -> ITABC (p2 w1) | default ABC",
        )
        focus_lines = store.focus_log_path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(focus_lines), 1)
        self.assertRegex(focus_lines[0], r"^\d{4}-\d{2}-\d{2}T")
        self.assertIn(
            " OLD=INIT OLD_IME=unknown->ABC OLD_P=p1 OLD_W=w1 "
            "NEW=SWCH NEW_IME=ABC->ITABC NEW_P=p2 NEW_W=w1 "
            "DEFAULT=ABC TARGET=ITABC BEFORE=ABC STORED=ITABC "
            "MODE=keep ACTION=selected REASON=restored-target SESSION=work",
            focus_lines[0],
        )

    def test_focus_keep_debug_log_contains_decision_context(self):
        self.write_config(
            default_action="keep",
            default_input_source="com.apple.keylayout.ABC",
            debug=True,
        )
        context = ime_keeper.HerdrContext.from_env(self.env)
        store = ime_keeper.StateStore(self.state_dir, context.identity)
        state = ime_keeper.empty_state(context.identity)
        state["last_focused_pane_id"] = "w1:p1"
        state["panes"] = {
            "w1:p2": {
                "input_source_id": "com.apple.inputmethod.SCIM.ITABC",
                "workspace_id": "w1",
                "tab_id": "w1:t1",
            }
        }
        store.save(state)
        event = {"event": "pane_focused", "data": {"pane_id": "w1:p2", "workspace_id": "w1"}}
        backend = FakeBackend(["com.apple.keylayout.ABC", "com.apple.keylayout.ABC"])
        herdr = FakeHerdr(["w1:p2", "w1:p2", "w1:p2", "w1:p2", "w1:p2", "w1:p2"])

        ime_keeper.handle_event(
            "pane-focused",
            self.env,
            backend=backend,
            herdr=herdr,
            event=event,
            debounce_seconds=0,
        )

        current_name = store.debug_current_path.read_text(encoding="utf-8").strip()
        log_path = store.session_dir / current_name
        log_entry = json.loads(log_path.read_text(encoding="utf-8").splitlines()[-1])
        self.assertEqual(log_entry["event"], "pane-focused")
        self.assertEqual(log_entry["mode"], "keep")
        self.assertEqual(log_entry["pane_id"], "w1:p2")
        self.assertEqual(log_entry["previous_pane_id"], "w1:p1")
        self.assertEqual(log_entry["default_input_source"], "com.apple.keylayout.ABC")
        self.assertEqual(log_entry["target_input_source"], "com.apple.inputmethod.SCIM.ITABC")
        self.assertEqual(log_entry["stored_target_input_source"], "com.apple.inputmethod.SCIM.ITABC")
        self.assertEqual(log_entry["observed_previous_input_source"], "com.apple.keylayout.ABC")
        self.assertEqual(log_entry["backend_current_before_select"], "com.apple.keylayout.ABC")
        self.assertEqual(log_entry["select_action"], "selected")
        self.assertEqual(log_entry["reason"], "restored-target")


class CliTests(TempEnvTest):
    def test_status_does_not_create_missing_config(self):
        stdout = io.StringIO()

        with contextlib.redirect_stdout(stdout):
            code = ime_keeper.main(["status"], env=self.env, backend=FakeBackend(["abc"]))

        self.assertEqual(code, 0)
        self.assertFalse((self.config_dir / "config.json").exists())
        self.assertIn("config_missing", stdout.getvalue())

    def test_status_does_not_write_debug_log(self):
        self.write_config(debug=True)
        context = ime_keeper.HerdrContext.from_env(self.env)
        store = ime_keeper.StateStore(self.state_dir, context.identity)
        stdout = io.StringIO()

        with contextlib.redirect_stdout(stdout):
            code = ime_keeper.main(["status"], env=self.env, backend=FakeBackend(["abc"]))

        self.assertEqual(code, 0)
        self.assertFalse(store.debug_path.exists())
        self.assertFalse(store.debug_current_path.exists())
        self.assertEqual(list(store.session_dir.glob("debug.*.log")), [])

    def test_dashboard_once_renders_config_live_panes_state_and_focus_log(self):
        self.write_config(
            debug=True,
            default_action="keep",
            default_input_source="com.apple.keylayout.ABC",
        )
        context = ime_keeper.HerdrContext.from_env(self.env)
        store = ime_keeper.StateStore(self.state_dir, context.identity)
        state = ime_keeper.empty_state(context.identity)
        state["last_focused_pane_id"] = "w1:p2"
        state["panes"] = {
            "w1:p1": {
                "workspace_id": "w1",
                "tab_id": "w1:t1",
                "agent": "codex",
                "cwd": "/repo",
                "input_source_id": "com.apple.keylayout.ABC",
                "updated_at": "2026-06-18T12:00:00+00:00",
            },
            "w1:p2": {
                "workspace_id": "w1",
                "tab_id": "w1:t2",
                "agent": "claude",
                "cwd": "/repo/cn",
                "input_source_id": "com.tencent.inputmethod.wetype.pinyin",
                "updated_at": "2026-06-18T12:01:00+00:00",
            },
        }
        store.save(state)
        store.session_dir.mkdir(parents=True, exist_ok=True)
        store.focus_log_path.write_text("focus-tail-entry\n", encoding="utf-8")
        herdr = FakeHerdr([])
        herdr.workspaces = [
            {
                "workspace_id": "w1",
                "label": "repo",
                "number": 1,
                "focused": True,
                "active_tab_id": "w1:t2",
            }
        ]
        herdr.tabs = [
            {"workspace_id": "w1", "tab_id": "w1:t1", "label": "en", "number": 1},
            {"workspace_id": "w1", "tab_id": "w1:t2", "label": "cn", "number": 2},
        ]
        herdr.panes = [
            {
                "workspace_id": "w1",
                "tab_id": "w1:t2",
                "pane_id": "w1:p2",
                "focused": True,
                "custom_status": "IME pinyin",
                "agent": "claude",
                "agent_status": "working",
                "cwd": "/repo/cn",
            }
        ]
        stdout = io.StringIO()

        with contextlib.redirect_stdout(stdout):
            code = ime_keeper.main(
                ["dashboard", "--once"],
                env=self.env,
                backend=FakeBackend(["com.tencent.inputmethod.wetype.pinyin"]),
                herdr=herdr,
            )

        output = stdout.getvalue()
        self.assertEqual(code, 0)
        self.assertIn("Input Method Keeper Dashboard", output)
        self.assertIn("session=work", output)
        self.assertIn("enabled=on debug=on action=keep backend=macism", output)
        self.assertIn("default=ABC current=pinyin", output)
        self.assertIn("* workspace 1 repo w1 active=w1:t2", output)
        self.assertIn("* tab 2 cn w1:t2", output)
        self.assertIn("* p2   live       stored=pinyin", output)
        self.assertIn("status=IME pinyin", output)
        self.assertIn("p1   state-only stored=ABC", output)
        self.assertIn("focus-tail-entry", output)

    def test_set_backend_helper_and_macism_write_backend_config(self):
        self.write_config()

        self.assertEqual(
            ime_keeper.main(["set-backend-helper"], env=self.env, backend=FakeBackend(["abc"])),
            0,
        )
        helper_config = ime_keeper.load_config(self.config_dir, readonly=True)
        self.assertEqual(helper_config["backend"]["name"], "herdr-ime-helper")
        self.assertEqual(helper_config["backend"]["current_args"], ["current"])
        self.assertEqual(
            helper_config["backend"]["select_args"],
            ["select", "{id}", "--refresh", "--wait-ms", "150"],
        )
        self.assertTrue(helper_config["backend"]["executable_candidates"][0].endswith("/bin/herdr-ime-helper"))

        self.assertEqual(
            ime_keeper.main(["set-backend-macism"], env=self.env, backend=FakeBackend(["abc"])),
            0,
        )
        macism_config = ime_keeper.load_config(self.config_dir, readonly=True)
        self.assertEqual(macism_config["backend"]["name"], "macism")
        self.assertEqual(macism_config["backend"]["current_args"], [])
        self.assertEqual(macism_config["backend"]["select_args"], ["{id}"])

    def test_doctor_does_not_select_by_default(self):
        self.write_config()
        backend = FakeBackend(["abc"])
        stdout = io.StringIO()

        with contextlib.redirect_stdout(stdout):
            code = ime_keeper.main(["doctor"], env=self.env, backend=backend)

        self.assertEqual(code, 0)
        self.assertEqual(backend.selected, [])

    def test_doctor_select_self_test_selects_current_input_source(self):
        self.write_config()
        backend = FakeBackend(["abc"])
        stdout = io.StringIO()

        with contextlib.redirect_stdout(stdout):
            code = ime_keeper.main(["doctor", "--select-self-test"], env=self.env, backend=backend)

        self.assertEqual(code, 0)
        self.assertEqual(backend.selected, ["abc"])
        output = json.loads(stdout.getvalue())
        self.assertEqual(output["backend_select_self_test"]["target"], "abc")
        self.assertTrue(output["backend_select_self_test"]["ok"])


if __name__ == "__main__":
    unittest.main()
