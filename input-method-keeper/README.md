# Herdr Input Method Keeper

Keep a stable macOS input source per Herdr pane.

This plugin is useful when you use different input methods in different Herdr
panes, for example English in one pane and Chinese Pinyin in another. It listens
for Herdr pane focus events, remembers the input source for each pane, and
restores it when you return to that pane.

Version 1 is intentionally small:

- macOS only
- Python 3.9+ runtime
- two backend choices: bundled Swift helper or `macism`
- one global default input source
- one global default action: `keep`, `reset`, or `ignore`
- no rule engine yet

For design and development details, see [DEVELOPMENT.md](DEVELOPMENT.md).

## Requirements

Required:

- Herdr 0.7.0 or newer
- macOS
- Python 3.9 or newer

If `python3` is not already available:

```sh
brew install python
```

Choose one input-source backend:

| Backend | Recommended for | Dependencies | Notes |
| --- | --- | --- | --- |
| Bundled Swift helper | Most users who can install Apple's command line tools | `swiftc`, usually from Xcode Command Line Tools | Recommended. It is included in this plugin, auto-compiles on first use, and supports input-context refresh. |
| `macism` | Users who prefer an existing Homebrew tool or cannot compile Swift locally | Homebrew `macism` | Simpler runtime path, but it may leave some app input contexts stale when switching from CJK input methods to ABC. |

Fresh configs start with `macism` for compatibility. The recommended setup below
switches to the bundled Swift helper when `swiftc` is available.

To install Apple's command line tools for the Swift helper:

```sh
xcode-select --install
```

To install `macism` instead:

```sh
brew tap laishulu/homebrew
brew install macism
```

## Install From GitHub

After this repository is public, install the plugin with Herdr's GitHub
shorthand:

```sh
herdr plugin install ppggff/herdr-plugin/input-method-keeper
```

## Local Development Install

Link and enable a local checkout:

```sh
herdr plugin link /path/to/herdr-plugin/input-method-keeper
herdr plugin enable ppggff.input-method-keeper
```

Run a basic smoke check:

```sh
input-method-keeper/scripts/herdr_smoke.py --link
```

## Quick Start

1. Install the plugin.
2. Choose a backend. The bundled Swift helper is recommended when `swiftc` is
   available:

   ```sh
   herdr plugin action invoke ppggff.input-method-keeper.set-backend-helper
   ```

   Or use `macism`:

   ```sh
   herdr plugin action invoke ppggff.input-method-keeper.set-backend-macism
   ```

3. Focus Herdr.
4. Switch macOS to the input source you want as the fallback default.
5. Run the Herdr plugin action `Set default input source`.
6. Run `Use default input method action: keep`.
7. Use Herdr normally. When you switch panes, the plugin should restore each
   pane's remembered input source.

You can run those actions from Herdr's action UI, or from a shell:

```sh
herdr plugin action invoke ppggff.input-method-keeper.set-backend-helper
herdr plugin action invoke ppggff.input-method-keeper.set-default-input-source
herdr plugin action invoke ppggff.input-method-keeper.set-default-action-keep
herdr plugin action invoke ppggff.input-method-keeper.debug-on
```

The helper compiles the first time it is used. If `swiftc` is not installed,
switch to `macism` or install Xcode Command Line Tools.

Optional live dashboard:

```sh
herdr plugin pane open --plugin ppggff.input-method-keeper --entrypoint dashboard
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
herdr plugin action invoke ppggff.input-method-keeper.<action-id>
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
| `set-backend-macism` | Use the `macism` backend. |
| `doctor` | Run repair-capable diagnostics. |
| `doctor-gc-all` | Run diagnostics and remove old non-current session state. |

## Keybindings

The plugin does not register default keybindings. The manifest only registers
actions and event hooks. Run actions through Herdr's action UI, `herdr plugin
action invoke`, or bind those action ids in your Herdr key configuration.

## Dashboard Pane

Open the read-only dashboard while testing:

```sh
herdr plugin pane open --plugin ppggff.input-method-keeper --entrypoint dashboard
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
herdr plugin config-dir ppggff.input-method-keeper
```

The file is `config.json`. After the recommended Swift helper setup, the main
fields look like this:

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
    "name": "herdr-ime-helper",
    "executable_candidates": [
      "/path/to/herdr-plugin/input-method-keeper/bin/herdr-ime-helper"
    ],
    "current_args": ["current"],
    "select_args": ["select", "{id}", "--refresh", "--wait-ms", "150"]
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

The backend is the small command-line tool that reads and switches the macOS
input source. The plugin owns pane memory, config, state, notifications, and
Herdr integration; the backend only provides:

```text
current() -> input_source_id
select(input_source_id) -> success/failure
```

### Bundled Swift Helper

Recommended when `swiftc` is available:

```sh
herdr plugin action invoke ppggff.input-method-keeper.set-backend-helper
```

The helper uses macOS TIS APIs directly. It is not compiled during
`herdr plugin install`; `bin/herdr-ime-helper` compiles
`helpers/herdr-ime-helper.swift` automatically the first time the helper runs.
When Herdr launches it, the compiled binary is cached under:

```text
HERDR_PLUGIN_STATE_DIR/helper-build/herdr-ime-helper
```

For direct manual runs outside Herdr, it caches under `TMPDIR`. It recompiles
when the Swift source is newer than the cached binary.

Manual helper commands:

```sh
input-method-keeper/bin/herdr-ime-helper current
input-method-keeper/bin/herdr-ime-helper list
input-method-keeper/bin/herdr-ime-helper select com.apple.keylayout.ABC
input-method-keeper/bin/herdr-ime-helper select com.apple.keylayout.ABC --refresh --wait-ms 150
input-method-keeper/bin/herdr-ime-helper refresh --wait-ms 150
```

`--refresh` creates a tiny temporary AppKit window to refresh the current input
context; the helper does not decide when refresh is needed. The plugin's
helper backend currently uses `select <id> --refresh --wait-ms 150`. This has
manually fixed the observed WeType `pinyin -> ABC` Shift-hotkey residue in
Herdr.

After `set-backend-helper`, `config.json` contains:

```json
{
  "name": "herdr-ime-helper",
  "executable_candidates": [
    "/path/to/herdr-plugin/input-method-keeper/bin/herdr-ime-helper"
  ],
  "current_args": ["current"],
  "select_args": ["select", "{id}", "--refresh", "--wait-ms", "150"]
}
```

### macism Backend

Use `macism` when you prefer the Homebrew tool or do not have `swiftc`:

```sh
herdr plugin action invoke ppggff.input-method-keeper.set-backend-macism
```

A fresh config currently starts with this backend for compatibility:

```text
current command: macism
select command:  macism {id}
```

With `macism` v3.1.1, switching from a CJK input method such as WeType pinyin
to `com.apple.keylayout.ABC` may update the system input source while leaving
Herdr's current text input context stale. Use the Swift helper if you see that
behavior.

## Supporting Other Operating Systems

The current release is declared as macOS-only in `herdr-plugin.toml` because the
bundled helper and the tested backend path use macOS input-source APIs. Treat
`platforms` as the plugin author's support contract, not a setting normal users
should edit after install. A user can fork or locally link a modified copy for
experiments, but the public manifest should only list OSes this repo actually
supports and tests.

The plugin design is still backend-oriented: pane memory, config, state, Herdr
events, and dashboard rendering live in Python, while OS-specific input-method
work belongs in a helper command.

To support another OS, provide a helper executable with the same backend
contract:

```text
current           # print the current input source id to stdout
select <id>       # switch to the requested input source and exit 0 on success
list              # optional, print known input source ids
```

Then configure it in `config.json`:

```json
{
  "backend": {
    "name": "my-os-helper",
    "executable_candidates": ["/path/to/my-os-helper", "my-os-helper"],
    "current_args": ["current"],
    "select_args": ["select", "{id}"]
  }
}
```

For a built-in helper, add a small config profile action like
`set-backend-helper`, document the dependency, and test the full focus flow
before this repository adds that OS to `platforms`.

Unix-like systems are the smaller step because the plugin already uses Python,
POSIX shell wrappers, and `fcntl` file locks. Windows support would need more
work: a non-POSIX launcher, a Windows file-lock implementation, and a Windows
input-method helper before declaring `platforms = ["windows"]`.

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
third currently runs real `macism` inside a temporary Herdr pane and also
exercises real `reset` and `ignore`.

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
kept separate from the default Herdr session. The smoke runner only backs up and
restores the selected session's state files. In a sandboxed runner, the plugin
state and config directories must be writable; otherwise the smoke runner fails
its restore preflight or E2E setup before destructive actions run. It does not
fall back to writing config or state through a Herdr pane.

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
