# Herdr Input Method Keeper

Keep a stable macOS input source per Herdr pane.

This plugin is useful when you use different input methods in different Herdr
panes, for example English in one pane and Chinese Pinyin in another. It listens
for Herdr pane focus events, remembers the input source for each pane, and
restores it when you return to that pane.

Version 1 is intentionally small:

- macOS only
- Python 3.9+ runtime
- `macism` default backend
- one global default input source
- one global default action: `keep`, `reset`, or `ignore`
- no rule engine yet

For design and development details, see [DEVELOPMENT.md](DEVELOPMENT.md).

## Install

Install dependencies:

```sh
brew install python
brew tap laishulu/homebrew
brew install macism
```

Link and enable the local plugin:

```sh
cd /Users/ppggff/xxx/herdr-plugin
herdr plugin link input-method-keeper
herdr plugin enable local.input-method-keeper
```

Run a basic smoke check:

```sh
input-method-keeper/scripts/herdr_smoke.py --link
```

## Quick Start

1. Focus Herdr.
2. Switch macOS to the input source you want as the fallback default.
3. Run the Herdr plugin action `Set default input source`.
4. Run `Use default input method action: keep`.
5. Use Herdr normally. When you switch panes, the plugin should restore each
   pane's remembered input source.

You can run those actions from Herdr's action UI, or from a shell:

```sh
herdr plugin action invoke set-default-input-source --plugin local.input-method-keeper
herdr plugin action invoke set-default-action-keep --plugin local.input-method-keeper
herdr plugin action invoke debug-on --plugin local.input-method-keeper
```

Optional live dashboard:

```sh
herdr plugin pane open --plugin local.input-method-keeper --entrypoint dashboard
```

Typical manual check:

1. Focus pane A and switch to input source A.
2. Focus pane B and switch to input source B.
3. Focus pane A again.
4. Pane A should return to input source A.
5. Focus pane B again.
6. Pane B should return to input source B.

## Default Actions

`default_action` controls what happens on pane focus:

- `keep`: use the stored pane input source. If the pane has no stored source,
  use `default_input_source`.
- `reset`: do not use pane memory. Switch every focused pane to
  `default_input_source`.
- `ignore`: do not read, store, or switch input sources.

Only `keep` uses pane memory. Switching to `reset` or `ignore` clears the
current Herdr session's stored pane state. Disabling the plugin also clears the
current Herdr session's state.

## Actions

Herdr exposes these actions:

Run any action with:

```sh
herdr plugin action invoke <action-id> --plugin local.input-method-keeper
```

| Action id | Effect |
| --- | --- |
| `toggle-enabled` | Toggle `enabled`; clears current session state. |
| `status` | Print config, session, current source, and stored state. Read-only. |
| `set-default-input-source` | Save the current input source as `default_input_source`. |
| `set-default-action-keep` | Use pane memory, or default source for new panes. |
| `set-default-action-reset` | Ignore pane memory and switch focused panes to the default source. Clears state. |
| `set-default-action-ignore` | Do nothing on focus changes. Clears state. |
| `debug-on` | Set `debug` to true. |
| `debug-off` | Set `debug` to false. |
| `set-backend-helper` | Use the Swift helper backend with `--refresh`. |
| `set-backend-macism` | Restore the default `macism` backend. |
| `doctor` | Run repair-capable diagnostics. |
| `doctor-gc-all` | Run diagnostics and remove old non-current session state. |

## Keybindings

The plugin does not register default keybindings. The manifest only registers
actions and event hooks. Run actions through Herdr's action UI, `herdr plugin
action invoke`, or bind those action ids in your Herdr key configuration.

## Dashboard Pane

Open the read-only dashboard while testing:

```sh
herdr plugin pane open --plugin local.input-method-keeper --entrypoint dashboard
```

Herdr returns the opened plugin pane id. Use that pane id if you want to focus
or close it later:

```sh
herdr plugin pane focus <pane-id>
herdr plugin pane close <pane-id>
```

The dashboard refreshes in place and keeps the output compact:

- current session label
- enabled/debug/default action/backend/default/current input source
- current macOS input source reported by the backend
- live Herdr workspaces and tabs
- each pane as only `pane-id=status`

It does not show the `focus.log` tail, pane cwd, agent, or update timestamps.
The live dashboard clears the screen and terminal scrollback on each refresh so
older, longer output does not remain visible. A muted `Ctrl-C to exit` hint is
shown at the bottom.

The dashboard uses ANSI colors when stdout is a TTY. `NO_COLOR` disables color.
For manual checks:

```sh
input-method-keeper/bin/ime-keeper dashboard --once --color always
input-method-keeper/bin/ime-keeper dashboard --once --color never
```

The dashboard uses foreground colors only, with no background or inverse color.
The two header/status lines are always plain text. `>` marks the focused
workspace, active tab, or focused pane; in color mode only that marker is
colored for workspace and tab. Workspace and tab labels use parentheses. The
focused pane id uses colored square brackets, and `stored ...` uses the same
muted color as the timestamp.

```text
> workspace 5 (hatch-deck)
  > tab 4 (4): >[p6]=IME ABC, p1=stored pinyin
```

The dashboard pane is not special-cased by the plugin. It is handled like any
other Herdr pane, so it can also get its own remembered input source.

## Config

Config lives in Herdr's plugin config directory:

```sh
herdr plugin config-dir local.input-method-keeper
```

The file is `config.json`:

```json
{
  "enabled": true,
  "debug": false,
  "session_name": "auto",
  "default_action": "keep",
  "default_input_source": "com.apple.keylayout.ABC",
  "notify_on_focus": true,
  "pane_status_on_focus": true,
  "focus_log": true,
  "status_ttl_ms": 600000,
  "backend": {
    "name": "macism",
    "executable_candidates": [
      "/opt/homebrew/bin/macism",
      "/usr/local/bin/macism",
      "macism"
    ],
    "current_args": [],
    "select_args": ["{id}"]
  }
}
```

Most users should only change:

- `enabled`
- `debug`
- `default_action`
- `default_input_source`
- `notify_on_focus`
- `pane_status_on_focus`
- `focus_log`

`session_name = "auto"` is recommended. The plugin stores state separately per
Herdr session using the Herdr socket path, so different sessions do not share
pane memory.

JSON does not support comments. Keep notes outside `config.json`.

## Backend

The default backend is `macism`:

```text
current command: macism
select command:  macism {id}
```

You can replace the backend in `config.json` if you want to use another tool
later. The plugin expects the backend to support:

```text
current() -> input_source_id
select(input_source_id) -> success/failure
```

This repo also includes a Swift helper backend:

```sh
input-method-keeper/bin/herdr-ime-helper current
input-method-keeper/bin/herdr-ime-helper list
input-method-keeper/bin/herdr-ime-helper select com.apple.keylayout.ABC
input-method-keeper/bin/herdr-ime-helper select com.apple.keylayout.ABC --refresh --wait-ms 150
input-method-keeper/bin/herdr-ime-helper refresh --wait-ms 150
```

The helper uses macOS TIS APIs directly. `--refresh` creates a tiny temporary
AppKit window to refresh the current input context; the helper does not decide
when refresh is needed. This has manually fixed the observed WeType
`pinyin -> ABC` Shift-hotkey residue in Herdr. To use it as the backend, set
`backend` to:

```json
{
  "name": "herdr-ime-helper",
  "executable_candidates": [
    "/Users/ppggff/xxx/herdr-plugin/input-method-keeper/bin/herdr-ime-helper"
  ],
  "current_args": ["current"],
  "select_args": ["select", "{id}", "--refresh", "--wait-ms", "150"]
}
```

The same switch can be done through actions:

```sh
herdr plugin action invoke set-backend-helper --plugin local.input-method-keeper
herdr plugin action invoke set-backend-macism --plugin local.input-method-keeper
```

## Testing

Recommended checks:

```sh
input-method-keeper/scripts/herdr_smoke.py --session ime-smoke --link
input-method-keeper/scripts/herdr_smoke.py --session ime-smoke --link --complex-fake
input-method-keeper/scripts/herdr_smoke.py --session ime-smoke --link --full-ime --real-actions
```

The first command verifies manifest actions. The second drives live Herdr panes
with a fake backend and covers three-pane memory restore, quick focus changes,
`reset`, `ignore`, disable/enable, config restore, and workspace cleanup. The
third runs real `macism` inside a temporary Herdr pane and also exercises real
`reset` and `ignore`.

If auto-detection cannot find two switchable input sources, pass them explicitly:

```sh
HERDR_IME_KEEPER_TEST_SOURCE_A=com.apple.keylayout.ABC \
HERDR_IME_KEEPER_TEST_SOURCE_B=com.apple.inputmethod.SCIM.ITABC \
input-method-keeper/scripts/herdr_smoke.py --session ime-smoke --link --full-ime --real-actions
```

`--full-ime` runs `macism` inside a temporary Herdr pane. This is intentional:
macOS can report different input sources for different foreground apps/input
contexts, so running `macism` directly from another terminal may test that other
app instead of Herdr.

Use the dedicated `ime-smoke` session for live smoke tests so pane memory is
kept separate from the default Herdr session. In a sandboxed runner, the plugin
state and config directories must be writable; otherwise the smoke runner fails
its restore preflight before destructive E2E actions run.

## Troubleshooting

Run the `Diagnose input method keeper` action first. It reports:

- config directory
- state directory
- resolved Python executable
- resolved backend executable
- Herdr socket path
- session key
- current input source
- current Herdr pane

Turn on debug logs with `Enable input method keeper debug logging`, reproduce
the issue, then run `Show input method keeper status`. For a first multi-day
trial, keeping debug enabled is recommended. It appends JSON lines to the
current session's active `debug.<UTC timestamp>.log` file, for example
`debug.20260618T103000123456Z.log`. The small `debug.current` file contains
the active log filename. When the active log grows past 100 MB, the plugin
starts a new timestamped log and updates `debug.current`.

Focus debug entries include the decision context needed to diagnose most
issues:

- focused pane and previous pane
- active mode: `keep`, `reset`, or `ignore`
- `default_input_source`
- observed previous pane input source
- stored target input source
- backend current source before select
- select action: `selected`, `already-current`, or `no-target`
- skip/failure reason when no switch happens

By default, each successful focus decision also shows a compact Herdr
notification. Herdr currently renders notification bodies as a single line in
practice, so the plugin uses only the title and one body line:

```text
title: OLD  CHNG: ABC -> ITABC (<pane> <workspace>)
body:  NEW  SWCH: ITABC -> ABC (<pane> <workspace>) | default ABC
```

The action field is fixed to four characters for readability: `INIT`, `CHNG`,
`SAME`, `SWCH`, `NONE`, `MISS`, or `UNKN`. `OLD` describes the pane losing
focus; `NEW` describes the pane gaining focus. Pane markers put the pane first
and workspace second, for example `w1:p2` is shown as `(p2 w1)`. The plugin also
writes `custom_status` to the focused pane metadata, so `herdr pane current`
shows a value such as `IME ITABC`. Set `notify_on_focus` or
`pane_status_on_focus` to `false` after the trial if that is too noisy.

If `focus_log` is enabled, every successful focus decision is also appended to
`focus.log` in the current session state directory as one compact line:

```text
2026-06-18T21:00:00.000+08:00 OLD=INIT OLD_IME=unknown->ABC OLD_P=p1 OLD_W=w1 NEW=SWCH NEW_IME=ABC->ITABC NEW_P=p2 NEW_W=w1 DEFAULT=ABC TARGET=ITABC BEFORE=ABC STORED=ITABC MODE=keep ACTION=selected REASON=restored-target SESSION=default
```

Run `status` to see the exact `focus_log_path`, then follow that file. `-F`
keeps waiting even if the file has not been created by the next focus event yet:

```sh
tail -F /path/from/status/focus.log
```

Useful direct checks:

```sh
macism
macism com.apple.inputmethod.SCIM.ITABC
macism
```

If this works in a Herdr pane but not in another terminal or automation process,
that is usually an app/input-context difference. Test the plugin with:

```sh
input-method-keeper/scripts/herdr_smoke.py --link --full-ime
```

## Known Limitations

- macOS input source is not truly pane-local. The plugin approximates pane-local
  behavior by observing and restoring the host input source.
- `pane.focused` does not provide the previous pane or old input source.
- If the user changes input source in another app and returns to Herdr without a
  pane focus event, version 1 may not restore immediately.
- If macOS itself restores an app-specific input source before the event hook
  runs, the plugin may attribute that observed input source to the previous
  pane.
- With `macism` v3.1.1, switching from a CJK input method such as WeType pinyin
  to `com.apple.keylayout.ABC` may update the system input source while leaving
  Herdr's current text input context stale. A visible symptom is that WeType's
  Shift hotkey still toggles Chinese/English until focus moves to another app
  and back. This appears to need an input-context refresh, not just a longer
  wait. The Swift helper backend with `--refresh` has manually fixed this
  symptom in Herdr; the limitation remains for the default `macism` backend.
- Version 1 has no rule engine and no ignore list.
- Stale cross-session state is removed only by `doctor-gc-all` /
  `doctor --gc-all`, or by a future Herdr session lifecycle event.
