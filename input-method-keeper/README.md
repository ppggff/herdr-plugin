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
| `doctor` | Run repair-capable diagnostics. |
| `doctor-gc-all` | Run diagnostics and remove old non-current session state. |

## Keybindings

The plugin does not register default keybindings. The manifest only registers
actions and event hooks. Run actions through Herdr's action UI, `herdr plugin
action invoke`, or bind those action ids in your Herdr key configuration.

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

## Testing

Recommended checks:

```sh
input-method-keeper/scripts/herdr_smoke.py --link
input-method-keeper/scripts/herdr_smoke.py --link --complex-fake
input-method-keeper/scripts/herdr_smoke.py --link --full-ime --real-actions
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
input-method-keeper/scripts/herdr_smoke.py --link --full-ime --real-actions
```

`--full-ime` runs `macism` inside a temporary Herdr pane. This is intentional:
macOS can report different input sources for different foreground apps/input
contexts, so running `macism` directly from another terminal may test that other
app instead of Herdr.

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
current session's `debug.log` and rotates at 100 MB to timestamped files such
as `debug.20260618T103000123456Z.log`.

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
- Version 1 has no rule engine and no ignore list.
- Stale cross-session state is removed only by `doctor-gc-all` /
  `doctor --gc-all`, or by a future Herdr session lifecycle event.
