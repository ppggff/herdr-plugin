import io
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import ime_keeper


class FakeBackend:
    executable = "/fake/helper"

    def __init__(self, current="com.example.ITABC"):
        self.value = current

    def current(self):
        return self.value


class FailingBackend(FakeBackend):
    def current(self):
        raise RuntimeError("backend unavailable")


class FakeHerdr:
    def list_workspaces(self):
        return []

    def list_tabs(self, workspace_id=None):
        return []

    def list_panes(self, workspace_id=None):
        return []


class V04Test(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.config_dir = root / "config"
        self.state_dir = root / "state"
        self.config_dir.mkdir()
        self.state_dir.mkdir()
        self.env = {
            "HERDR_PLUGIN_CONFIG_DIR": str(self.config_dir),
            "HERDR_PLUGIN_STATE_DIR": str(self.state_dir),
            "HERDR_SOCKET_PATH": "/tmp/herdr/sessions/work/herdr.sock",
            "TERM": "dumb",
        }

    def tearDown(self):
        self.tmp.cleanup()

    def write_config(self, **overrides):
        config = ime_keeper.default_config()
        config.update(overrides)
        (self.config_dir / "config.json").write_text(json.dumps(config), encoding="utf-8")
        return config

    def store_with_pane(self):
        config = ime_keeper.load_config(self.config_dir, readonly=True)
        store = ime_keeper.StateStore(
            self.state_dir, ime_keeper.session_identity(config, self.env)
        )
        state = ime_keeper.empty_state(store.identity)
        state["panes"]["w1:p1"] = {"input_source_id": "com.apple.keylayout.ABC"}
        store.save(state)
        store.mark_dirty({"pane_id": "w1:p1"})
        return store

    def test_settings_once_is_read_only_and_plain(self):
        output = io.StringIO()
        result = ime_keeper.run_settings(
            self.env,
            backend=FakeBackend(),
            herdr=FakeHerdr(),
            once=True,
            output=output,
        )

        self.assertEqual(result, 0)
        self.assertIn("Input Method Keeper", output.getvalue())
        self.assertIn("Enabled", output.getvalue())
        self.assertNotIn("\x1b[", output.getvalue())
        self.assertFalse((self.config_dir / "config.json").exists())
        self.assertFalse((self.state_dir / "run.lock").exists())

    def test_nonempty_clear_requires_confirmation_and_preview_writes_no_user_data(self):
        self.write_config(enabled=True)
        store = self.store_with_pane()
        service = ime_keeper.MutationService(self.env)
        before_config = (self.config_dir / "config.json").read_bytes()
        before_state = store.state_path.read_bytes()
        before_dirty = store.dirty_path.read_bytes()

        result = service.apply("toggle-enabled", backend=FakeBackend(), interactive=True)

        self.assertEqual(result.status, "confirmation_required")
        self.assertEqual(result.record_count, 1)
        self.assertEqual((self.config_dir / "config.json").read_bytes(), before_config)
        self.assertEqual(store.state_path.read_bytes(), before_state)
        self.assertEqual(store.dirty_path.read_bytes(), before_dirty)

        applied = service.confirm(result.token)
        self.assertEqual(applied.status, "applied")
        self.assertFalse(ime_keeper.load_config(self.config_dir)["enabled"])
        self.assertFalse(store.state_path.exists())
        self.assertFalse(store.dirty_path.exists())

    def test_changed_state_or_dirty_marker_rejects_confirmation_without_writing_config(self):
        self.write_config(enabled=True)
        store = self.store_with_pane()
        service = ime_keeper.MutationService(self.env)
        preview = service.apply("toggle-enabled", backend=FakeBackend(), interactive=True)
        store.mark_dirty({"pane_id": "w1:p2"})

        result = service.confirm(preview.token)

        self.assertEqual(result.status, "preview_stale")
        self.assertTrue(ime_keeper.load_config(self.config_dir)["enabled"])
        self.assertTrue(store.state_path.exists())
        self.assertTrue(store.dirty_path.exists())

    def test_invalid_state_is_unknown_and_cannot_be_cleared_without_confirmation(self):
        self.write_config(enabled=True)
        config = ime_keeper.load_config(self.config_dir)
        store = ime_keeper.StateStore(
            self.state_dir, ime_keeper.session_identity(config, self.env)
        )
        store.session_dir.mkdir(parents=True)
        store.state_path.write_text("not-json", encoding="utf-8")

        result = ime_keeper.MutationService(self.env).apply(
            "toggle-enabled", backend=FakeBackend(), interactive=True
        )

        self.assertEqual(result.status, "confirmation_required")
        self.assertIsNone(result.record_count)
        self.assertTrue(result.state_unknown)
        self.assertEqual(store.state_path.read_text(encoding="utf-8"), "not-json")

    def test_default_source_confirmation_retains_displayed_source(self):
        self.write_config(enabled=False)
        store = self.store_with_pane()
        backend = FakeBackend("com.example.First")
        service = ime_keeper.MutationService(self.env)
        preview = service.apply(
            "set-default-input-source", backend=backend, interactive=True
        )
        rendered = ime_keeper.render_settings(
            {"config": ime_keeper.load_config(self.config_dir)}, confirmation=preview.token
        )
        backend.value = "com.example.Second"

        result = service.confirm(preview.token)

        self.assertEqual(result.status, "applied")
        self.assertIn("com.example.First", rendered)
        self.assertEqual(
            ime_keeper.load_config(self.config_dir)["default_input_source"],
            "com.example.First",
        )
        self.assertFalse(store.state_path.exists())

    def test_dirty_writer_racing_confirmed_clear_survives_after_clear(self):
        self.write_config(enabled=True)
        store = self.store_with_pane()
        service = ime_keeper.MutationService(self.env)
        preview = service.apply("toggle-enabled", backend=FakeBackend(), interactive=True)
        entered_write = threading.Event()
        allow_clear = threading.Event()
        writer_done = threading.Event()

        from ime_keeper import mutations

        real_write = mutations.write_config

        def paused_write(*args, **kwargs):
            real_write(*args, **kwargs)
            entered_write.set()
            allow_clear.wait(2)

        def dirty_writer():
            entered_write.wait(2)
            store.mark_dirty({"pane_id": "w1:new"})
            writer_done.set()

        writer = threading.Thread(target=dirty_writer)
        writer.start()
        with mock.patch.object(mutations, "write_config", side_effect=paused_write):
            confirmer = threading.Thread(target=lambda: service.confirm(preview.token))
            confirmer.start()
            self.assertTrue(entered_write.wait(2))
            self.assertFalse(writer_done.wait(0.05))
            allow_clear.set()
            confirmer.join(2)
        writer.join(2)

        self.assertTrue(writer_done.is_set())
        self.assertTrue(store.dirty_path.exists())
        self.assertIn("w1:new", store.dirty_path.read_text(encoding="utf-8"))

    def test_controller_navigation_choice_cancel_and_unknown_key_are_side_effect_free(self):
        self.write_config(default_action="keep")
        controller = ime_keeper.SettingsController(
            self.env, FakeHerdr(), fixed_backend=FakeBackend()
        )
        controller.refresh()
        before = (self.config_dir / "config.json").read_bytes()

        self.assertTrue(controller.handle_key("unknown"))
        controller.handle_key("down")
        controller.handle_key("enter")
        self.assertIsNotNone(controller.choice)
        controller.handle_key("right")
        controller.handle_key("escape")

        self.assertIsNone(controller.choice)
        self.assertEqual((self.config_dir / "config.json").read_bytes(), before)

    def test_invalid_config_requires_confirmation_before_repair_or_backup(self):
        path = self.config_dir / "config.json"
        path.write_text("broken", encoding="utf-8")
        service = ime_keeper.MutationService(self.env)

        preview = service.apply("debug-on", backend=FakeBackend(), interactive=True)

        self.assertEqual(preview.status, "confirmation_required")
        self.assertEqual(path.read_text(encoding="utf-8"), "broken")
        self.assertEqual(list(self.config_dir.glob("config.json.broken.*")), [])
        applied = service.confirm(preview.token)
        self.assertEqual(applied.status, "applied")
        self.assertTrue(ime_keeper.load_config(self.config_dir)["debug"])
        self.assertEqual(len(list(self.config_dir.glob("config.json.broken.*"))), 1)

    def test_confirm_failure_stays_in_controller_and_keeps_confirmation(self):
        self.write_config(enabled=True)
        self.store_with_pane()
        controller = ime_keeper.SettingsController(
            self.env, FakeHerdr(), fixed_backend=FakeBackend()
        )
        controller.refresh()
        controller.handle_key("enter")
        self.assertIsNotNone(controller.confirmation)

        with mock.patch.object(controller.service, "confirm", side_effect=OSError("disk full")), mock.patch(
            "sys.stderr", new=io.StringIO()
        ) as stderr:
            self.assertTrue(controller.handle_key("enter"))

        self.assertIsNotNone(controller.confirmation)
        self.assertEqual(controller.result_line, "Error: disk full")
        self.assertIn("OSError: disk full", stderr.getvalue())

    def test_unknown_escape_sequence_is_not_treated_as_escape(self):
        from ime_keeper import settings

        stream = io.StringIO("\x1b[5")
        with mock.patch.object(settings.select, "select", return_value=([stream], [], [])):
            self.assertEqual(settings._read_key(stream), "unknown")

    def test_terminal_is_restored_after_unexpected_key_reader_failure(self):
        from ime_keeper import settings

        class FakeTTY(io.StringIO):
            def isatty(self):
                return True

            def fileno(self):
                return 42

        output = io.StringIO()
        terminal_state = [1, 2, 3]
        with mock.patch.object(settings.termios, "tcgetattr", return_value=terminal_state), mock.patch.object(
            settings.termios, "tcsetattr"
        ) as restore, mock.patch.object(settings.tty, "setcbreak"), mock.patch.object(
            settings, "_read_key", side_effect=RuntimeError("reader failed")
        ):
            with self.assertRaisesRegex(RuntimeError, "reader failed"):
                ime_keeper.run_settings(
                    self.env,
                    backend=FakeBackend(),
                    herdr=FakeHerdr(),
                    input_stream=FakeTTY(),
                    output=output,
                )

        restore.assert_called_once_with(42, settings.termios.TCSADRAIN, terminal_state)
        self.assertIn("\x1b[?25l", output.getvalue())
        self.assertIn("\x1b[?25h", output.getvalue())

    def test_plain_render_is_clipped_for_narrow_terminal(self):
        data = {
            "config": self.write_config(),
            "backend": {"name": "herdr-ime-helper", "healthy": True},
            "diagnostics": [],
        }
        rendered = ime_keeper.render_settings(data, color_enabled=False, width=32)

        self.assertTrue(all(len(line) <= 32 for line in rendered.splitlines()))
        self.assertNotIn("\x1b[", rendered)

    def test_eof_quits_and_remaining_navigation_keys_are_safe(self):
        self.write_config()
        controller = ime_keeper.SettingsController(
            self.env, FakeHerdr(), fixed_backend=FakeBackend()
        )
        controller.refresh()

        controller.handle_key("up")
        self.assertEqual(controller.selected, 4)
        controller.handle_key("j")
        controller.handle_key("k")
        self.assertEqual(controller.selected, 4)
        self.assertFalse(controller.handle_key("eof"))
        self.assertFalse(controller.handle_key("q"))

    def test_no_color_and_unavailable_backend_render_as_health_not_crash(self):
        output = io.StringIO()
        env = dict(self.env, NO_COLOR="1")

        result = ime_keeper.run_settings(
            env,
            backend=FailingBackend(),
            herdr=FakeHerdr(),
            once=True,
            color_mode="auto",
            output=output,
        )

        self.assertEqual(result, 0)
        self.assertIn("health=error", output.getvalue())
        self.assertIn("backend_current_failed", output.getvalue())
        self.assertNotIn("\x1b[", output.getvalue())

    def test_config_and_state_changes_each_invalidate_confirmation(self):
        for changed in ("config", "state"):
            with self.subTest(changed=changed):
                self.write_config(enabled=True)
                store = self.store_with_pane()
                service = ime_keeper.MutationService(self.env)
                preview = service.apply(
                    "toggle-enabled", backend=FakeBackend(), interactive=True
                )
                if changed == "config":
                    config = ime_keeper.load_config(self.config_dir)
                    config["backend"] = dict(config["backend"], name="changed-backend")
                    ime_keeper.write_config(self.config_dir, config)
                else:
                    state, _ = ime_keeper.PaneRecords(store).snapshot()
                    state["panes"]["w1:p2"] = {"input_source_id": "new"}
                    store.save(state)

                result = service.confirm(preview.token)

                self.assertEqual(result.status, "preview_stale")
                self.assertTrue(store.state_path.exists())

    def test_every_popup_mutation_matches_cli_action_result(self):
        cases = (
            ("toggle-enabled", None, ["toggle-enabled"], {}),
            ("set-default-action", "reset", ["set-default-action", "reset"], {}),
            ("set-default-input-source", None, ["set-default-input-source"], {}),
            ("set-backend-helper", None, ["set-backend-helper"], {}),
            ("set-backend-macism", None, ["set-backend-macism"], {}),
            ("debug-on", None, ["debug-on"], {}),
            ("debug-off", None, ["debug-off"], {"debug": True}),
        )
        for mutation, value, argv, overrides in cases:
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as left_tmp, tempfile.TemporaryDirectory() as right_tmp:
                environments = []
                stores = []
                for root_text in (left_tmp, right_tmp):
                    root = Path(root_text)
                    config_dir = root / "config"
                    state_dir = root / "state"
                    config_dir.mkdir()
                    state_dir.mkdir()
                    env = dict(
                        self.env,
                        HERDR_PLUGIN_CONFIG_DIR=str(config_dir),
                        HERDR_PLUGIN_STATE_DIR=str(state_dir),
                    )
                    config = ime_keeper.default_config()
                    config.update(overrides)
                    ime_keeper.write_config(config_dir, config)
                    store = ime_keeper.StateStore(
                        state_dir, ime_keeper.session_identity(config, env)
                    )
                    state = ime_keeper.empty_state(store.identity)
                    state["panes"]["w1:p1"] = {"input_source_id": "ABC"}
                    store.save(state)
                    environments.append(env)
                    stores.append(store)

                service = ime_keeper.MutationService(environments[0])
                popup = service.apply(
                    mutation, value=value, backend=FakeBackend(), interactive=True
                )
                if popup.status == "confirmation_required":
                    popup = service.confirm(popup.token)
                cli_code = ime_keeper.main(
                    argv, env=environments[1], backend=FakeBackend(), herdr=FakeHerdr()
                )

                self.assertEqual(popup.status, "applied")
                self.assertEqual(cli_code, 0)
                self.assertEqual(
                    ime_keeper.load_config(Path(environments[0]["HERDR_PLUGIN_CONFIG_DIR"])),
                    ime_keeper.load_config(Path(environments[1]["HERDR_PLUGIN_CONFIG_DIR"])),
                )
                self.assertEqual(stores[0].state_path.exists(), stores[1].state_path.exists())

    def test_enum_left_right_apply_and_confirmation_cancel_key_states(self):
        self.write_config(default_action="keep")
        controller = ime_keeper.SettingsController(
            self.env, FakeHerdr(), fixed_backend=FakeBackend()
        )
        controller.refresh()
        controller.handle_key("down")
        controller.handle_key("enter")
        controller.handle_key("right")
        controller.handle_key("left")
        controller.handle_key("right")
        controller.handle_key("enter")
        self.assertEqual(ime_keeper.load_config(self.config_dir)["default_action"], "reset")

        self.write_config(enabled=True, default_action="keep")
        self.store_with_pane()
        controller.refresh()
        controller.selected = 0
        controller.handle_key("enter")
        self.assertIsNotNone(controller.confirmation)
        controller.handle_key("escape")
        self.assertIsNone(controller.confirmation)
        self.assertTrue(ime_keeper.load_config(self.config_dir)["enabled"])

    def test_v04_paths_never_acquire_run_or_focus_lock_from_dirty_guard(self):
        self.write_config(enabled=True)
        store = self.store_with_pane()
        active_dirty = threading.local()
        original_enter = ime_keeper.FileLock.__enter__
        original_release = ime_keeper.FileLock.release

        def checked_enter(lock):
            if getattr(active_dirty, "value", False) and lock.path.name in {
                "run.lock",
                "focus.lock",
            }:
                raise AssertionError(f"reverse lock order: {lock.path.name}")
            result = original_enter(lock)
            if lock.path.name == "dirty.lock" and lock.acquired:
                active_dirty.value = True
            return result

        def checked_release(lock):
            if lock.path.name == "dirty.lock":
                active_dirty.value = False
            return original_release(lock)

        with mock.patch.object(ime_keeper.FileLock, "__enter__", checked_enter), mock.patch.object(
            ime_keeper.FileLock, "release", checked_release
        ):
            service = ime_keeper.MutationService(self.env)
            preview = service.apply(
                "toggle-enabled", backend=FakeBackend(), interactive=True
            )
            self.assertEqual(service.confirm(preview.token).status, "applied")
            store.mark_dirty({"pane_id": "w1:new"})
            store.clear_dirty()


if __name__ == "__main__":
    unittest.main()
