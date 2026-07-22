import contextlib
import io
import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

import ime_keeper


class FakeBackend:
    def __init__(self, currents):
        self.currents = list(currents)
        self.selected = []
        self.current_calls = 0

    def current(self):
        self.current_calls += 1
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
        self.list_tab_args = []
        self.presence = {}
        self.reconcile_timeouts = []

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
        self.list_tab_args.append(workspace_id)
        if workspace_id:
            return [tab for tab in self.tabs if tab.get("workspace_id") == workspace_id]
        return list(self.tabs)

    def list_panes(self, workspace_id=None):
        if workspace_id:
            return [pane for pane in self.panes if pane.get("workspace_id") == workspace_id]
        return list(self.panes)

    def list_panes_socket(self, timeout):
        self.reconcile_timeouts.append(timeout)
        return list(self.panes)

    def pane_presence_socket(self, pane_id, timeout):
        self.reconcile_timeouts.append(timeout)
        return self.presence.get(pane_id, "unknown")


class FailingPublicationHerdr(FakeHerdr):
    def show_notification(self, title, body):
        return ime_keeper.CommandResult(False, "", "notification unavailable", 1)

    def report_pane_status(self, pane_id, status, ttl_ms):
        return ime_keeper.CommandResult(False, "", "metadata unavailable", 1)


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


class HerdrClientTests(unittest.TestCase):
    def test_event_publications_use_socket_without_cli_fallback(self):
        client = ime_keeper.HerdrClient(
            {"HERDR_SOCKET_PATH": "/tmp/herdr.sock", "HERDR_BIN_PATH": "/usr/local/bin/herdr"}
        )
        responses = [
            {"result": {"type": "ok"}},
            {"result": {"type": "notification_show", "shown": True}},
        ]

        with mock.patch.object(client, "_socket_request", side_effect=responses) as request, mock.patch(
            "ime_keeper.herdr.subprocess.run"
        ) as run:
            metadata = client.report_pane_status("w1:p2", "ITABC", 600000)
            notification = client.show_notification("title", "body")

        self.assertTrue(metadata.ok)
        self.assertTrue(notification.ok)
        self.assertEqual(
            request.call_args_list[0].args[:2],
            (
                "pane.report_metadata",
                {
                    "pane_id": "w1:p2",
                    "source": "ppggff.input-method-keeper",
                    "tokens": {"ime": "ITABC"},
                    "ttl_ms": 600000,
                },
            ),
        )
        self.assertEqual(request.call_args_list[1].args[1]["position"], "top-right")
        run.assert_not_called()

    def test_socket_pane_list_is_all_or_nothing_and_presence_requires_explicit_not_found(self):
        client = ime_keeper.HerdrClient({"HERDR_SOCKET_PATH": "/tmp/herdr.sock"})
        malformed = {"result": {"type": "pane_list", "panes": [{"pane_id": "w1:p1"}, {}]}}
        absent = {"error": {"code": "pane_not_found", "message": "pane not found"}}
        other_error = {"error": {"code": "internal", "message": "try again"}}

        with mock.patch.object(client, "_socket_request", side_effect=[malformed, absent, other_error]):
            self.assertIsNone(client.list_panes_socket(0.25))
            self.assertEqual(client.pane_presence_socket("w1:gone", 0.2), "absent")
            self.assertEqual(client.pane_presence_socket("w1:maybe", 0.1), "unknown")

    def test_cli_pane_list_rejects_missing_or_invalid_result(self):
        client = ime_keeper.HerdrClient({})
        for response in ({}, {"result": {}}, {"result": {"panes": "invalid"}}):
            with self.subTest(response=response), mock.patch.object(
                client, "_run_herdr_json", return_value=response
            ):
                with self.assertRaisesRegex(RuntimeError, "invalid pane list"):
                    client.list_panes()


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

    def test_state_load_rejects_non_object_pane_entries(self):
        self.write_config()
        identity = ime_keeper.session_identity(
            ime_keeper.load_config(self.config_dir, readonly=True), self.env
        )
        session_dir = self.state_dir / "sessions" / identity.key
        session_dir.mkdir(parents=True)
        (session_dir / "state.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "last_focused_pane_id": "w1:p1",
                    "panes": {"w1:p1": "broken"},
                }
            ),
            encoding="utf-8",
        )

        store = ime_keeper.StateStore(self.state_dir, identity)
        state, diagnostic = store.load(readonly=True)

        self.assertIsNone(state)
        self.assertIn("pane entry", diagnostic)

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


class PaneRecordsTests(TempEnvTest):
    def test_focus_memory_is_committed_as_one_record_update(self):
        self.write_config(default_action="keep")
        context = ime_keeper.HerdrContext.from_env(self.env)
        store = ime_keeper.StateStore(self.state_dir, context.identity)
        state = ime_keeper.empty_state(context.identity)
        state["last_focused_pane_id"] = "w1:p1"
        state["panes"] = {
            "w1:p1": {
                "input_source_id": "old-source",
                "workspace_id": "w1",
                "tab_id": "w1:t1",
            },
            "w1:p2": {
                "input_source_id": "target-source",
                "workspace_id": "w1",
                "tab_id": "w1:t1",
            },
        }
        store.save(state)
        records = ime_keeper.PaneRecords(store)

        memory = records.focus_memory("w1:p2")
        records.commit_focus(
            memory,
            {
                "pane_id": "w1:p2",
                "workspace_id": "w1",
                "tab_id": "w1:t1",
                "cwd": "/repo/new",
                "agent": "codex",
            },
            observed_previous_source="observed-source",
            selected_source="target-source",
        )

        snapshot, diagnostic = records.snapshot()
        self.assertIsNone(diagnostic)
        self.assertEqual(snapshot["last_focused_pane_id"], "w1:p2")
        self.assertEqual(snapshot["panes"]["w1:p1"]["input_source_id"], "observed-source")
        self.assertEqual(snapshot["panes"]["w1:p2"]["input_source_id"], "target-source")
        self.assertEqual(snapshot["panes"]["w1:p2"]["cwd"], "/repo/new")

    def test_live_pane_audit_is_read_only_and_uses_unmatched_language(self):
        self.write_config()
        context = ime_keeper.HerdrContext.from_env(self.env)
        store = ime_keeper.StateStore(self.state_dir, context.identity)
        state = ime_keeper.empty_state(context.identity)
        state["panes"] = {"w1:p1": {}, "w1:old": {}}
        store.save(state)
        before = store.state_path.read_bytes()

        audit = ime_keeper.PaneRecords(store).audit_live_panes(["w1:p1", "w1:new"])

        self.assertEqual(audit.live, 2)
        self.assertEqual(audit.stored, 2)
        self.assertEqual(audit.unmatched_ids, ("w1:old",))
        self.assertEqual(audit.missing_ids, ("w1:new",))
        self.assertTrue(audit.maintenance_due)
        self.assertEqual(store.state_path.read_bytes(), before)

    def test_reconcile_prunes_only_confirmed_absent_and_stays_due_on_unknown(self):
        self.write_config()
        context = ime_keeper.HerdrContext.from_env(self.env)
        store = ime_keeper.StateStore(self.state_dir, context.identity)
        state = ime_keeper.empty_state(context.identity)
        state["last_focused_pane_id"] = "w1:p1"
        state["panes"] = {"w1:p1": {}, "w1:gone": {}, "w1:maybe": {}}
        store.save(state)
        records = ime_keeper.PaneRecords(store)

        result = records.reconcile_live_panes(
            ["w1:p1"],
            lambda pane_id: {"w1:gone": "absent", "w1:maybe": "unknown"}[pane_id],
        )

        snapshot, _ = records.snapshot()
        self.assertEqual(result.pruned_ids, ("w1:gone",))
        self.assertEqual(result.unknown_ids, ("w1:maybe",))
        self.assertFalse(result.completed)
        self.assertNotIn("last_reconciled_at", snapshot)
        self.assertEqual(set(snapshot["panes"]), {"w1:p1", "w1:maybe"})

    def test_reconcile_rejects_empty_or_focus_omitting_live_snapshot(self):
        self.write_config()
        context = ime_keeper.HerdrContext.from_env(self.env)
        store = ime_keeper.StateStore(self.state_dir, context.identity)
        state = ime_keeper.empty_state(context.identity)
        state["last_focused_pane_id"] = "w1:p1"
        state["panes"] = {"w1:p1": {}, "w1:old": {}}
        store.save(state)
        records = ime_keeper.PaneRecords(store)

        empty = records.reconcile_live_panes([], lambda _pane_id: "absent")
        omitted = records.reconcile_live_panes(["w1:p2"], lambda _pane_id: "absent")

        snapshot, _ = records.snapshot()
        self.assertFalse(empty.completed)
        self.assertFalse(omitted.completed)
        self.assertEqual(set(snapshot["panes"]), {"w1:p1", "w1:old"})

    def test_reconcile_socket_calls_share_one_budget(self):
        self.write_config()
        context = ime_keeper.HerdrContext.from_env(self.env)
        store = ime_keeper.StateStore(self.state_dir, context.identity)
        state = ime_keeper.empty_state(context.identity)
        state["last_focused_pane_id"] = "w1:p1"
        state["panes"] = {"w1:p1": {}, "w1:old": {}}
        store.save(state)
        herdr = FakeHerdr([])
        herdr.panes = [{"pane_id": "w1:p1"}]
        herdr.presence = {"w1:old": "absent"}

        result = ime_keeper.PaneRecords(store).reconcile_with_herdr(herdr, budget_seconds=0.25)

        self.assertTrue(result.completed)
        self.assertEqual(result.pruned_ids, ("w1:old",))
        self.assertEqual(len(herdr.reconcile_timeouts), 2)
        self.assertTrue(all(0 < value <= 0.25 for value in herdr.reconcile_timeouts))
        self.assertLessEqual(herdr.reconcile_timeouts[1], herdr.reconcile_timeouts[0])

    def test_reconcile_save_failure_keeps_persisted_records_and_timestamp_due(self):
        self.write_config()
        context = ime_keeper.HerdrContext.from_env(self.env)
        store = ime_keeper.StateStore(self.state_dir, context.identity)
        state = ime_keeper.empty_state(context.identity)
        state["last_focused_pane_id"] = "w1:p1"
        state["panes"] = {"w1:p1": {}, "w1:old": {}}
        store.save(state)
        herdr = FakeHerdr([])
        herdr.panes = [{"pane_id": "w1:p1"}]
        herdr.presence = {"w1:old": "absent"}

        with mock.patch.object(store, "save", side_effect=OSError("disk full")):
            result = ime_keeper.PaneRecords(store).reconcile_with_herdr(
                herdr, budget_seconds=0.25
            )

        snapshot, _ = ime_keeper.PaneRecords(store).snapshot()
        self.assertFalse(result.completed)
        self.assertIn("state-update-failed", result.reason)
        self.assertEqual(set(snapshot["panes"]), {"w1:p1", "w1:old"})
        self.assertNotIn("last_reconciled_at", snapshot)

    def test_automatic_reconciliation_does_not_repair_corrupt_state(self):
        self.write_config()
        context = ime_keeper.HerdrContext.from_env(self.env)
        store = ime_keeper.StateStore(self.state_dir, context.identity)
        store.session_dir.mkdir(parents=True, exist_ok=True)
        corrupt = b"{not-json\n"
        store.state_path.write_bytes(corrupt)
        herdr = FakeHerdr([])
        herdr.panes = [{"pane_id": "w1:p1"}]

        result = ime_keeper.PaneRecords(store).reconcile_with_herdr(
            herdr, budget_seconds=0.25
        )

        self.assertFalse(result.completed)
        self.assertIn("state-update-failed", result.reason)
        self.assertEqual(store.state_path.read_bytes(), corrupt)
        self.assertEqual(list(store.session_dir.glob("state.json.broken.*")), [])

    def test_reconcile_budget_expiry_does_not_commit_partial_prunes(self):
        self.write_config()
        context = ime_keeper.HerdrContext.from_env(self.env)
        store = ime_keeper.StateStore(self.state_dir, context.identity)
        state = ime_keeper.empty_state(context.identity)
        state["last_focused_pane_id"] = "w1:p1"
        state["panes"] = {"w1:p1": {}, "w1:old": {}}
        store.save(state)

        class SlowHerdr(FakeHerdr):
            def pane_presence_socket(inner_self, pane_id, timeout):
                import time

                time.sleep(0.02)
                return "absent"

        herdr = SlowHerdr([])
        herdr.panes = [{"pane_id": "w1:p1"}]

        result = ime_keeper.PaneRecords(store).reconcile_with_herdr(
            herdr, budget_seconds=0.01
        )

        snapshot, _ = ime_keeper.PaneRecords(store).snapshot()
        self.assertFalse(result.completed)
        self.assertEqual(result.reason, "budget-expired")
        self.assertEqual(set(snapshot["panes"]), {"w1:p1", "w1:old"})


class BackendTests(unittest.TestCase):
    def test_ensure_input_source_skips_select_when_already_current(self):
        backend = FakeBackend(["com.apple.keylayout.ABC"])

        result = ime_keeper.ensure_input_source(backend, "com.apple.keylayout.ABC")

        self.assertEqual(result, "already-current")
        self.assertEqual(backend.selected, [])

    def test_ensure_input_source_reuses_validated_known_current(self):
        backend = FakeBackend(["must-not-be-read"])

        result = ime_keeper.ensure_input_source_details(
            backend, "com.apple.keylayout.ABC", known_current="com.apple.keylayout.ABC"
        )

        self.assertEqual(result["action"], "already-current")
        self.assertEqual(backend.current_calls, 0)

    def test_backend_executor_does_not_split_string_args_or_crash_on_null_args(self):
        config = ime_keeper.default_config()
        config["backend"] = {
            "executable_candidates": ["macism"],
            "current_args": "current",
            "select_args": None,
        }

        backend = ime_keeper.BackendExecutor(config)

        self.assertEqual(backend.current_args, ["current"])
        self.assertEqual(backend.select_args, ["{id}"])


class ConfigTests(unittest.TestCase):
    def test_fresh_config_prefers_verified_native_helper(self):
        with mock.patch("ime_keeper.config.native_helper_available", return_value=True):
            config = ime_keeper.default_config()

        self.assertEqual(config["backend"]["name"], "herdr-ime-helper")
        self.assertEqual(config["backend"]["current_args"], ["current"])
        self.assertFalse(config["notify_on_focus"])

    def test_existing_notification_setting_survives_quiet_default(self):
        config = ime_keeper.merge_config({"notify_on_focus": True})

        self.assertTrue(config["notify_on_focus"])

    def test_existing_macism_config_is_not_migrated_when_native_helper_exists(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_dir = Path(temp_dir)
            (config_dir / "config.json").write_text(
                json.dumps({"backend": {"name": "macism"}}),
                encoding="utf-8",
            )
            with mock.patch("ime_keeper.config.native_helper_available", return_value=True):
                config = ime_keeper.load_config(config_dir, readonly=True)

        self.assertEqual(config["backend"]["name"], "macism")
        self.assertEqual(config["backend"]["current_args"], [])
        self.assertEqual(config["backend"]["select_args"], ["{id}"])

    def test_existing_helper_config_rebinds_wrapper_to_current_plugin(self):
        config = ime_keeper.merge_config(
            {
                "backend": {
                    "name": "herdr-ime-helper",
                    "executable_candidates": ["/old/plugin/bin/herdr-ime-helper"],
                    "current_args": ["current"],
                    "select_args": ["select", "{id}", "--refresh", "--wait-ms", "150"],
                }
            }
        )

        self.assertEqual(
            config["backend"]["executable_candidates"],
            [str(ROOT / "bin" / "herdr-ime-helper")],
        )

    def test_custom_helper_backend_path_is_not_rebound(self):
        custom_path = "/opt/custom/input-source-adapter"
        config = ime_keeper.merge_config(
            {
                "backend": {
                    "name": "herdr-ime-helper",
                    "executable_candidates": [custom_path],
                    "current_args": ["current"],
                    "select_args": ["select", "{id}", "--refresh", "--wait-ms", "150"],
                }
            }
        )

        self.assertEqual(config["backend"]["executable_candidates"], [custom_path])

    def test_merge_config_parses_common_string_booleans(self):
        config = ime_keeper.merge_config(
            {
                "enabled": "false",
                "debug": "1",
                "notify_on_focus": "0",
                "pane_status_on_focus": "true",
                "focus_log": "false",
            }
        )

        self.assertFalse(config["enabled"])
        self.assertTrue(config["debug"])
        self.assertFalse(config["notify_on_focus"])
        self.assertTrue(config["pane_status_on_focus"])
        self.assertFalse(config["focus_log"])

    def test_switching_from_stateless_policy_clears_stale_records(self):
        config = ime_keeper.default_config()
        config["default_action"] = "reset"

        updated, clear_records = ime_keeper.apply_config_mutation(
            config,
            "set-default-action",
            value="keep",
        )

        self.assertEqual(updated["default_action"], "keep")
        self.assertTrue(clear_records)


class DebugLoggingTests(TempEnvTest):
    def test_v03_log_budgets_are_fixed(self):
        self.assertEqual(ime_keeper.DEBUG_LOG_MAX_BYTES, 10 * 1024 * 1024)
        self.assertEqual(ime_keeper.FOCUS_LOG_MAX_BYTES, 5 * 1024 * 1024)

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
        original_limit = ime_keeper.logs.DEBUG_LOG_MAX_BYTES

        try:
            ime_keeper.logs.DEBUG_LOG_MAX_BYTES = 1
            ime_keeper.log_debug(store, context.config, {"event": "test"})
        finally:
            ime_keeper.logs.DEBUG_LOG_MAX_BYTES = original_limit

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

    def test_focus_log_rotation_keeps_active_path_and_two_segments(self):
        self.write_config()
        context = ime_keeper.HerdrContext.from_env(self.env)
        store = ime_keeper.StateStore(self.state_dir, context.identity)
        store.session_dir.mkdir(parents=True)
        original_limit = ime_keeper.logs.FOCUS_LOG_MAX_BYTES

        try:
            ime_keeper.logs.FOCUS_LOG_MAX_BYTES = 1
            for index in range(4):
                ime_keeper.logs.append_focus_line(store, f"line-{index}")
        finally:
            ime_keeper.logs.FOCUS_LOG_MAX_BYTES = original_limit

        self.assertTrue(store.focus_log_path.exists())
        self.assertEqual(store.focus_log_path.read_text(encoding="utf-8"), "line-3\n")
        self.assertEqual(len(list(store.session_dir.glob("focus.*.log"))), 2)
        health = ime_keeper.log_health(store)
        self.assertEqual(health["focus"]["segments"], 3)

    def test_concurrent_log_rotation_keeps_valid_files_and_pointer(self):
        self.write_config(debug=True)
        context = ime_keeper.HerdrContext.from_env(self.env)
        store = ime_keeper.StateStore(self.state_dir, context.identity)
        errors = []
        original_focus_limit = ime_keeper.logs.FOCUS_LOG_MAX_BYTES
        original_debug_limit = ime_keeper.logs.DEBUG_LOG_MAX_BYTES

        def write_logs(index):
            try:
                ime_keeper.logs.append_focus_line(store, f"focus-{index}")
                ime_keeper.log_debug(store, context.config, {"event": "thread", "index": index})
            except Exception as exc:
                errors.append(exc)

        try:
            ime_keeper.logs.FOCUS_LOG_MAX_BYTES = 1
            ime_keeper.logs.DEBUG_LOG_MAX_BYTES = 1
            threads = [threading.Thread(target=write_logs, args=(index,)) for index in range(12)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
        finally:
            ime_keeper.logs.FOCUS_LOG_MAX_BYTES = original_focus_limit
            ime_keeper.logs.DEBUG_LOG_MAX_BYTES = original_debug_limit

        self.assertEqual(errors, [])
        self.assertTrue(store.focus_log_path.exists())
        current_name = store.debug_current_path.read_text(encoding="utf-8").strip()
        self.assertTrue((store.session_dir / current_name).exists())
        self.assertLessEqual(len(list(store.session_dir.glob("focus.*.log"))), 2)
        self.assertLessEqual(len(list(store.session_dir.glob("debug.*.log"))), 3)

    def test_debug_log_retention_keeps_current_pointer_and_three_segments(self):
        self.write_config(debug=True)
        context = ime_keeper.HerdrContext.from_env(self.env)
        store = ime_keeper.StateStore(self.state_dir, context.identity)
        store.session_dir.mkdir(parents=True)
        names = [
            f"debug.20260722T01020{index}000001Z.log" for index in range(1, 5)
        ]
        for name in names:
            (store.session_dir / name).write_text("old\n", encoding="utf-8")
        store.debug_current_path.write_text(names[-1] + "\n", encoding="utf-8")

        ime_keeper.log_debug(store, context.config, {"event": "new"})

        retained = list(store.session_dir.glob("debug.*.log"))
        current_name = store.debug_current_path.read_text(encoding="utf-8").strip()
        self.assertEqual(len(retained), 3)
        self.assertTrue((store.session_dir / current_name).exists())


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
    def test_post_maintenance_check_never_skips_dirty_marker(self):
        self.write_config()
        context = ime_keeper.HerdrContext.from_env(self.env)
        store = ime_keeper.StateStore(self.state_dir, context.identity)
        store.mark_dirty({"pane_id": "w1:p2"})

        self.assertTrue(
            ime_keeper.focus.should_loop_again(store, FakeHerdr(["w1:p1"]), "w1:p1")
        )

    def test_stable_snapshot_uses_second_tokens_for_metadata_decision(self):
        class SnapshotHerdr(FakeHerdr):
            def __init__(inner_self):
                super().__init__([])
                inner_self.snapshots = [
                    {"pane_id": "w1:p1", "tokens": {"ime": "ABC"}},
                    {"pane_id": "w1:p1", "tokens": {}},
                ]

            def current_pane(inner_self):
                if inner_self.snapshots:
                    return inner_self.snapshots.pop(0)
                return {"pane_id": "w1:p1", "tokens": {}}

        herdr = SnapshotHerdr()
        pane = ime_keeper.focus.stable_current_pane(herdr, 0)
        self.assertEqual(pane.get("tokens"), {})

    def test_automatic_reconciliation_fails_open_on_config_reload_error(self):
        self.write_config(default_action="keep")
        context = ime_keeper.HerdrContext.from_env(self.env)
        store = ime_keeper.StateStore(self.state_dir, context.identity)
        herdr = FakeHerdr([])

        with mock.patch.object(
            ime_keeper.focus, "load_config", side_effect=OSError("config unavailable")
        ):
            ime_keeper.focus.attempt_due_reconciliation(
                context, store, herdr, context.config
            )

        self.assertEqual(herdr.reconcile_timeouts, [])

    def test_busy_run_lock_leaves_automatic_reconciliation_due_for_retry(self):
        self.write_config(default_action="keep")
        context = ime_keeper.HerdrContext.from_env(self.env)
        store = ime_keeper.StateStore(self.state_dir, context.identity)
        state = ime_keeper.empty_state(context.identity)
        state["last_focused_pane_id"] = "w1:p1"
        state["panes"] = {"w1:p1": {}}
        store.save(state)
        herdr = FakeHerdr([])
        herdr.panes = [{"pane_id": "w1:p1"}]

        with ime_keeper.FileLock(ime_keeper.run_lock_path(self.state_dir), blocking=True):
            ime_keeper.focus.attempt_due_reconciliation(
                context, store, herdr, context.config
            )

        snapshot, _ = ime_keeper.PaneRecords(store).snapshot()
        self.assertNotIn("last_reconciled_at", snapshot)
        self.assertEqual(herdr.reconcile_timeouts, [])

    def test_automatic_reconciliation_rechecks_policy_before_state_update(self):
        self.write_config(default_action="reset")
        context = ime_keeper.HerdrContext.from_env(self.env)
        store = ime_keeper.StateStore(self.state_dir, context.identity)
        state = ime_keeper.empty_state(context.identity)
        state["last_focused_pane_id"] = "w1:p1"
        state["panes"] = {"w1:p1": {}}
        store.save(state)
        herdr = FakeHerdr([])
        herdr.panes = [{"pane_id": "w1:p1"}]

        ime_keeper.focus.attempt_due_reconciliation(context, store, herdr, context.config)

        self.assertFalse(store.state_path.exists())
        self.assertEqual(herdr.reconcile_timeouts, [])
    def test_due_reconciliation_runs_after_restore_and_prunes_confirmed_absent(self):
        self.write_config(default_action="keep", debug=True)
        context = ime_keeper.HerdrContext.from_env(self.env)
        store = ime_keeper.StateStore(self.state_dir, context.identity)
        state = ime_keeper.empty_state(context.identity)
        state["last_focused_pane_id"] = "w1:p1"
        state["panes"] = {
            "w1:p1": {"input_source_id": "abc"},
            "w1:p2": {"input_source_id": "target"},
            "w1:old": {"input_source_id": "old"},
        }
        store.save(state)
        backend = FakeBackend(["abc", "must-not-be-read"])
        herdr = FakeHerdr(["w1:p2"] * 10)
        herdr.panes = [{"pane_id": "w1:p1"}, {"pane_id": "w1:p2"}]
        herdr.presence = {"w1:old": "absent"}

        code = ime_keeper.handle_event(
            "pane-focused",
            self.env,
            backend=backend,
            herdr=herdr,
            event={"event": "pane_focused", "data": {"pane_id": "w1:p2"}},
            debounce_seconds=0,
        )

        snapshot, _ = ime_keeper.PaneRecords(store).snapshot()
        self.assertEqual(code, 0)
        self.assertEqual(backend.current_calls, 1)
        self.assertEqual(backend.selected, ["target"])
        self.assertNotIn("w1:old", snapshot["panes"])
        self.assertIn("last_reconciled_at", snapshot)

    def test_concurrent_focus_commit_waits_for_reconciliation_snapshot_commit(self):
        self.write_config(default_action="keep")
        context = ime_keeper.HerdrContext.from_env(self.env)
        store = ime_keeper.StateStore(self.state_dir, context.identity)
        state = ime_keeper.empty_state(context.identity)
        state["last_focused_pane_id"] = "w1:p1"
        state["panes"] = {"w1:p1": {}, "w1:old": {}}
        store.save(state)
        writer_started = threading.Event()
        writer_finished = threading.Event()
        writer_threads = []

        class ConcurrentCommitHerdr(FakeHerdr):
            def pane_presence_socket(inner_self, pane_id, timeout):
                def commit_focus():
                    writer_started.set()
                    with ime_keeper.FileLock(
                        ime_keeper.run_lock_path(self.state_dir), blocking=True
                    ):
                        records = ime_keeper.PaneRecords(store)
                        memory = records.focus_memory("w1:new")
                        records.commit_focus(
                            memory,
                            {"pane_id": "w1:new", "workspace_id": "w1"},
                            observed_previous_source=None,
                            selected_source="new-ime",
                        )
                    writer_finished.set()

                thread = threading.Thread(target=commit_focus)
                writer_threads.append(thread)
                thread.start()
                self.assertTrue(writer_started.wait(1))
                self.assertFalse(writer_finished.is_set())
                return "absent"

        herdr = ConcurrentCommitHerdr([])
        herdr.panes = [{"pane_id": "w1:p1"}]
        ime_keeper.focus.attempt_due_reconciliation(
            context, store, herdr, context.config
        )
        for thread in writer_threads:
            thread.join(1)

        snapshot, _ = ime_keeper.PaneRecords(store).snapshot()
        self.assertTrue(writer_finished.is_set())
        self.assertNotIn("w1:old", snapshot["panes"])
        self.assertEqual(snapshot["panes"]["w1:new"]["input_source_id"], "new-ime")

    def test_focus_arriving_during_maintenance_is_restored_before_focus_lock_releases(self):
        self.write_config(default_action="keep")
        context = ime_keeper.HerdrContext.from_env(self.env)
        store = ime_keeper.StateStore(self.state_dir, context.identity)
        state = ime_keeper.empty_state(context.identity)
        state["last_focused_pane_id"] = "w1:p1"
        state["panes"] = {
            "w1:p1": {"input_source_id": "abc"},
            "w1:p2": {"input_source_id": "target"},
            "w1:p3": {"input_source_id": "third"},
            "w1:old": {},
        }
        store.save(state)

        class DirtyDuringPresenceHerdr(FakeHerdr):
            def pane_presence_socket(inner_self, pane_id, timeout):
                store.mark_dirty({"pane_id": "w1:p3"})
                return "absent"

        herdr = DirtyDuringPresenceHerdr(["w1:p2"] * 6 + ["w1:p3"] * 10)
        herdr.panes = [
            {"pane_id": "w1:p1"},
            {"pane_id": "w1:p2"},
            {"pane_id": "w1:p3"},
        ]
        backend = FakeBackend(["abc", "target"])

        code = ime_keeper.handle_event(
            "pane-focused",
            self.env,
            backend=backend,
            herdr=herdr,
            event={"event": "pane_focused", "data": {"pane_id": "w1:p2"}},
            debounce_seconds=0,
        )

        snapshot, _ = ime_keeper.PaneRecords(store).snapshot()
        self.assertEqual(code, 0)
        self.assertEqual(backend.selected, ["target", "third"])
        self.assertEqual(snapshot["last_focused_pane_id"], "w1:p3")
        self.assertFalse(store.dirty_path.exists())

    def test_unchanged_live_metadata_token_is_not_republished(self):
        self.write_config(pane_status_on_focus=True, notify_on_focus=False)
        context = ime_keeper.HerdrContext.from_env(self.env)
        store = ime_keeper.StateStore(self.state_dir, context.identity)
        herdr = FakeHerdr([])

        ime_keeper.focus.publish_focus_status(
            store,
            context.config,
            herdr,
            "w1:p1",
            "com.apple.keylayout.ABC",
            "keep",
            current_tokens={"ime": "ABC"},
        )

        self.assertEqual(herdr.pane_statuses, [])

    def test_focus_publication_failures_are_logged_without_failing_event(self):
        self.write_config(
            default_action="reset",
            default_input_source="com.apple.keylayout.ABC",
            debug=False,
            notify_on_focus=True,
        )
        env = {
            **self.env,
            "HERDR_PLUGIN_EVENT_JSON": json.dumps(
                {
                    "event": "pane_focused",
                    "data": {"pane_id": "w1:p1", "workspace_id": "w1"},
                }
            ),
        }
        stderr = io.StringIO()

        with contextlib.redirect_stderr(stderr):
            code = ime_keeper.main(
                ["event", "pane-focused"],
                env=env,
                backend=FakeBackend(["com.apple.keylayout.ABC"]),
                herdr=FailingPublicationHerdr(["w1:p1"] * 5),
            )

        warning = json.loads(stderr.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(warning["level"], "warning")
        self.assertEqual(warning["event"], "focus-status")
        self.assertEqual(warning["pane_id"], "w1:p1")
        self.assertEqual(
            warning["errors"],
            [
                "pane_status_failed: metadata unavailable",
                "notification_failed: notification unavailable",
            ],
        )

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

    def test_focus_reset_does_not_select_when_current_pane_confirmation_fails(self):
        self.write_config(default_action="reset", default_input_source="abc", debug=True)
        event = {"event": "pane_focused", "data": {"pane_id": "w1:p1", "workspace_id": "w1"}}
        backend = FakeBackend(["other"])

        code = ime_keeper.handle_event(
            "pane-focused",
            self.env,
            backend=backend,
            herdr=FakeHerdr(["w1:p1"]),
            event=event,
            debounce_seconds=0,
        )

        self.assertEqual(code, 0)
        self.assertEqual(backend.selected, [])

    def test_focus_keep_does_not_select_when_current_pane_confirmation_fails(self):
        self.write_config(default_action="keep", default_input_source="default")
        context = ime_keeper.HerdrContext.from_env(self.env)
        store = ime_keeper.StateStore(self.state_dir, context.identity)
        state = ime_keeper.empty_state(context.identity)
        state["last_focused_pane_id"] = "w1:p1"
        state["panes"] = {
            "w1:p2": {
                "input_source_id": "target",
                "workspace_id": "w1",
                "tab_id": "w1:t1",
            }
        }
        store.save(state)
        backend = FakeBackend(["observed", "before-select"])
        event = {"event": "pane_focused", "data": {"pane_id": "w1:p2", "workspace_id": "w1"}}

        code = ime_keeper.handle_event(
            "pane-focused",
            self.env,
            backend=backend,
            herdr=FakeHerdr(["w1:p2"]),
            event=event,
            debounce_seconds=0,
        )

        self.assertEqual(code, 0)
        self.assertEqual(backend.selected, [])

    def test_focus_keep_repairs_invalid_pane_entry_state(self):
        self.write_config(default_action="keep", default_input_source="abc")
        context = ime_keeper.HerdrContext.from_env(self.env)
        store = ime_keeper.StateStore(self.state_dir, context.identity)
        store.session_dir.mkdir(parents=True)
        store.state_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "last_focused_pane_id": "w1:p1",
                    "panes": {"w1:p2": "broken"},
                }
            ),
            encoding="utf-8",
        )

        code = ime_keeper.handle_event(
            "pane-focused",
            self.env,
            backend=FakeBackend(["abc"]),
            herdr=FakeHerdr(["w1:p2", "w1:p2", "w1:p2", "w1:p2", "w1:p2"]),
            event={"event": "pane_focused", "data": {"pane_id": "w1:p2", "workspace_id": "w1"}},
            debounce_seconds=0,
        )

        self.assertEqual(code, 0)
        self.assertEqual(len(list(store.session_dir.glob("state.json.broken.*"))), 1)
        state, diagnostic = store.load(readonly=True)
        self.assertIsNone(diagnostic)
        self.assertEqual(state["last_focused_pane_id"], "w1:p2")

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

    def test_pane_closed_coerces_event_id_before_clearing_last_focus(self):
        self.write_config(default_action="keep")
        context = ime_keeper.HerdrContext.from_env(self.env)
        store = ime_keeper.StateStore(self.state_dir, context.identity)
        state = ime_keeper.empty_state(context.identity)
        state["last_focused_pane_id"] = "123"
        state["panes"] = {
            "123": {"input_source_id": "abc", "workspace_id": "w1", "tab_id": "w1:t1"},
        }
        store.save(state)
        event = {"event": "pane_closed", "data": {"pane_id": 123, "workspace_id": "w1"}}

        ime_keeper.handle_event(
            "pane-closed", self.env, backend=FakeBackend([]), herdr=FakeHerdr([]), event=event
        )

        state, _ = store.load(readonly=True)
        self.assertNotIn("123", state["panes"])
        self.assertIsNone(state["last_focused_pane_id"])

    def test_focus_keep_records_previous_source_and_restores_target(self):
        self.write_config(
            default_action="keep",
            default_input_source="com.apple.keylayout.ABC",
            notify_on_focus=True,
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
        herdr = FakeHerdr(["w1:p2", "w1:p2", "w1:p2", "w1:p2", "w1:p2"])

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

    def test_focus_keep_same_pane_does_not_read_backend_for_status(self):
        self.write_config(default_action="keep", default_input_source="abc")
        context = ime_keeper.HerdrContext.from_env(self.env)
        store = ime_keeper.StateStore(self.state_dir, context.identity)
        state = ime_keeper.empty_state(context.identity)
        state["last_focused_pane_id"] = "w1:p2"
        state["panes"] = {
            "w1:p2": {
                "input_source_id": "stored",
                "workspace_id": "w1",
                "tab_id": "w1:t1",
            }
        }
        store.save(state)
        backend = FakeBackend([])

        ime_keeper.handle_event(
            "pane-focused",
            self.env,
            backend=backend,
            herdr=FakeHerdr(["w1:p2", "w1:p2", "w1:p2", "w1:p2"]),
            event={"event": "pane_focused", "data": {"pane_id": "w1:p2", "workspace_id": "w1"}},
            debounce_seconds=0,
        )

        self.assertEqual(backend.current_calls, 0)
        self.assertEqual(backend.selected, [])

    def test_focus_keep_publishes_default_notification_and_pane_status(self):
        self.write_config(
            default_action="keep",
            default_input_source="com.apple.keylayout.ABC",
            notify_on_focus=True,
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
        herdr = FakeHerdr(["w1:p2", "w1:p2", "w1:p2", "w1:p2", "w1:p2"])

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
            [{"pane_id": "w1:p2", "status": "ITABC", "ttl_ms": 600000}],
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
        entries = [
            json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()
        ]
        log_entry = next(entry for entry in entries if entry["event"] == "pane-focused")
        self.assertEqual(log_entry["event"], "pane-focused")
        self.assertEqual(log_entry["mode"], "keep")
        self.assertEqual(log_entry["pane_id"], "w1:p2")
        self.assertEqual(log_entry["previous_pane_id"], "w1:p1")
        self.assertEqual(log_entry["default_input_source"], "com.apple.keylayout.ABC")
        self.assertEqual(log_entry["target_input_source"], "com.apple.inputmethod.SCIM.ITABC")
        self.assertEqual(log_entry["stored_target_input_source"], "com.apple.inputmethod.SCIM.ITABC")
        timing_entry = next(entry for entry in entries if entry["event"] == "focus-timings")
        for field in (
            "lock_wait_ms",
            "stabilize_ms",
            "backend_current_ms",
            "backend_select_ms",
            "metadata_ms",
            "notification_ms",
            "total_ms",
            "coalesced_events",
        ):
            self.assertIn(field, timing_entry)
        self.assertEqual(log_entry["observed_previous_input_source"], "com.apple.keylayout.ABC")
        self.assertEqual(log_entry["backend_current_before_select"], "com.apple.keylayout.ABC")
        self.assertEqual(log_entry["select_action"], "selected")
        self.assertEqual(log_entry["reason"], "restored-target")

    def test_pane_moved_missing_metadata_does_not_migrate_or_overwrite_state(self):
        self.write_config(default_action="keep")
        context = ime_keeper.HerdrContext.from_env(self.env)
        store = ime_keeper.StateStore(self.state_dir, context.identity)
        state = ime_keeper.empty_state(context.identity)
        state["last_focused_pane_id"] = "w1:p1"
        state["panes"] = {
            "w1:p1": {
                "input_source_id": "abc",
                "workspace_id": "w1",
                "tab_id": "w1:t1",
            }
        }
        store.save(state)
        event = {
            "event": "pane_moved",
            "data": {
                "previous_pane_id": "w1:p1",
                "previous_workspace_id": "w1",
                "previous_tab_id": "w1:t1",
                "pane": {"pane_id": "w2:p1"},
            },
        }

        ime_keeper.handle_event(
            "pane-moved", self.env, backend=FakeBackend([]), herdr=FakeHerdr([]), event=event
        )

        state, _ = store.load(readonly=True)
        self.assertIn("w1:p1", state["panes"])
        self.assertNotIn("w2:p1", state["panes"])
        self.assertEqual(state["panes"]["w1:p1"]["workspace_id"], "w1")
        self.assertEqual(state["panes"]["w1:p1"]["tab_id"], "w1:t1")

    def test_missing_event_pane_id_marks_dirty_without_context_fallback(self):
        self.write_config(default_action="keep")
        env = dict(
            self.env,
            HERDR_PLUGIN_EVENT_JSON=json.dumps({"event": "pane_focused", "data": {}}),
            HERDR_PLUGIN_CONTEXT_JSON=json.dumps(
                {"focused_pane_id": "w1:p9", "workspace_id": "w1"}
            ),
        )
        context = ime_keeper.HerdrContext.from_env(env)
        store = ime_keeper.StateStore(self.state_dir, context.identity)
        store.session_dir.mkdir(parents=True)

        with ime_keeper.FileLock(store.focus_lock_path, blocking=True):
            code = ime_keeper.handle_event(
                "pane-focused",
                env,
                backend=FakeBackend([]),
                herdr=FakeHerdr([]),
                debounce_seconds=0,
            )

        self.assertEqual(code, 0)
        dirty = json.loads(store.dirty_path.read_text(encoding="utf-8"))
        self.assertNotIn("pane_id", dirty)


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

    def test_status_reports_read_only_pane_health(self):
        self.write_config()
        context = ime_keeper.HerdrContext.from_env(self.env)
        store = ime_keeper.StateStore(self.state_dir, context.identity)
        state = ime_keeper.empty_state(context.identity)
        state["panes"] = {"w1:p1": {}, "w1:old": {}}
        store.save(state)
        before = store.state_path.read_bytes()
        herdr = FakeHerdr([])
        herdr.panes = [{"pane_id": "w1:p1"}, {"pane_id": "w1:new"}]
        stdout = io.StringIO()

        with contextlib.redirect_stdout(stdout):
            code = ime_keeper.main(
                ["status"], env=self.env, backend=FakeBackend(["abc"]), herdr=herdr
            )

        payload = json.loads(stdout.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(payload["pane_health"]["live"], 2)
        self.assertEqual(payload["pane_health"]["stored"], 2)
        self.assertEqual(payload["pane_health"]["unmatched_ids"], ["w1:old"])
        self.assertEqual(payload["pane_health"]["missing_ids"], ["w1:new"])
        self.assertEqual(store.state_path.read_bytes(), before)

    def test_dashboard_once_renders_compact_pane_status(self):
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
                "tokens": {"ime": "pinyin"},
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
        self.assertIn("IME Keeper", output)
        self.assertIn("session=work enabled=on debug=on action=keep", output)
        self.assertIn("default=ABC current=pinyin", output)
        self.assertIn("backend=macism panes=live:1/state:2", output)
        self.assertIn("> workspace 1 (repo)", output)
        self.assertIn("> tab 2 (cn): >[p2]=pinyin", output)
        self.assertIn("tab 1 (en): p1=stored ABC", output)
        self.assertIn("Ctrl-C to exit", output)
        self.assertNotIn("focus-tail-entry", output)
        self.assertNotIn("focus.log tail", output)
        self.assertNotIn("cwd=", output)
        self.assertNotIn("agent=", output)
        self.assertNotIn("\033[", output)

        color_stdout = io.StringIO()
        with contextlib.redirect_stdout(color_stdout):
            code = ime_keeper.main(
                ["dashboard", "--once", "--color", "always"],
                env=self.env,
                backend=FakeBackend(["com.tencent.inputmethod.wetype.pinyin"]),
                herdr=herdr,
            )

        self.assertEqual(code, 0)
        color_output = color_stdout.getvalue()
        self.assertIn("\033[", color_output)
        self.assertNotIn(";7", color_output)
        first_two_lines = "\n".join(color_output.splitlines()[:2])
        self.assertNotIn("\033[", first_two_lines)
        self.assertIn(
            "\033[1;34m>\033[0m\033[1;34m[\033[0mp2\033[1;34m]\033[0m=\033[32mpinyin\033[0m",
            color_output,
        )
        self.assertIn("p1=\033[2mstored ABC\033[0m", color_output)
        self.assertIn("\033[2mCtrl-C to exit\033[0m", color_output)

    def test_dashboard_collects_tabs_once_when_global_tab_list_works(self):
        self.write_config()
        herdr = FakeHerdr([])
        herdr.workspaces = [
            {"workspace_id": "w1", "number": 1},
            {"workspace_id": "w2", "number": 2},
        ]
        herdr.tabs = [
            {"workspace_id": "w1", "tab_id": "w1:t1"},
            {"workspace_id": "w2", "tab_id": "w2:t1"},
        ]

        data = ime_keeper.collect_dashboard_data(self.env, FakeBackend(["abc"]), herdr)

        self.assertEqual([tab.get("tab_id") for tab in data["tabs"]], ["w1:t1", "w2:t1"])
        self.assertEqual(herdr.list_tab_args, [None])

    def test_dashboard_suppresses_health_for_invalid_pane_snapshot(self):
        self.write_config()
        context = ime_keeper.HerdrContext.from_env(self.env)
        store = ime_keeper.StateStore(self.state_dir, context.identity)
        state = ime_keeper.empty_state(context.identity)
        state["panes"] = {"w1:p1": {}}
        store.save(state)
        before = store.state_path.read_bytes()
        herdr = FakeHerdr([])
        herdr.panes = [{"pane_id": "w1:p1"}, {}]

        data = ime_keeper.collect_dashboard_data(self.env, FakeBackend(["abc"]), herdr)

        self.assertIsNone(data["pane_health"])
        self.assertIn("pane_list_invalid", data["diagnostics"])
        self.assertEqual(store.state_path.read_bytes(), before)

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

    def test_set_backend_macism_repairs_invalid_backend_arg_config(self):
        config = ime_keeper.default_config()
        config["backend"]["current_args"] = None
        (self.config_dir / "config.json").write_text(json.dumps(config), encoding="utf-8")

        code = ime_keeper.main(["set-backend-macism"], env=self.env)

        self.assertEqual(code, 0)
        repaired = ime_keeper.load_config(self.config_dir, readonly=True)
        self.assertEqual(repaired["backend"]["name"], "macism")
        self.assertEqual(repaired["backend"]["current_args"], [])
        self.assertEqual(repaired["backend"]["select_args"], ["{id}"])

    def test_doctor_creates_missing_config(self):
        stdout = io.StringIO()

        with contextlib.redirect_stdout(stdout):
            code = ime_keeper.main(
                ["doctor"], env=self.env, backend=FakeBackend(["abc"]), herdr=FakeHerdr([])
            )

        self.assertEqual(code, 0)
        self.assertTrue((self.config_dir / "config.json").exists())
        self.assertEqual(ime_keeper.load_config(self.config_dir)["default_action"], "keep")

    def test_doctor_does_not_select_by_default(self):
        self.write_config()
        backend = FakeBackend(["abc"])
        stdout = io.StringIO()

        with contextlib.redirect_stdout(stdout):
            code = ime_keeper.main(["doctor"], env=self.env, backend=backend)

        self.assertEqual(code, 0)
        self.assertEqual(backend.selected, [])

    def test_doctor_gc_all_forces_guarded_current_session_reconciliation(self):
        self.write_config()
        context = ime_keeper.HerdrContext.from_env(self.env)
        store = ime_keeper.StateStore(self.state_dir, context.identity)
        state = ime_keeper.empty_state(context.identity)
        state["last_focused_pane_id"] = "w1:p1"
        state["panes"] = {"w1:p1": {}, "w1:old": {}}
        store.save(state)
        herdr = FakeHerdr([])
        herdr.panes = [{"pane_id": "w1:p1"}]
        herdr.presence = {"w1:old": "absent"}
        stdout = io.StringIO()

        with contextlib.redirect_stdout(stdout):
            code = ime_keeper.main(
                ["doctor", "--gc-all"],
                env=self.env,
                backend=FakeBackend(["abc"]),
                herdr=herdr,
            )

        payload = json.loads(stdout.getvalue())
        snapshot, _ = ime_keeper.PaneRecords(store).snapshot()
        self.assertEqual(code, 0)
        self.assertEqual(payload["reconciliation"]["pruned_ids"], ["w1:old"])
        self.assertTrue(payload["reconciliation"]["completed"])
        self.assertIn("last_reconciled_at", snapshot)
        self.assertEqual(set(snapshot["panes"]), {"w1:p1"})

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
