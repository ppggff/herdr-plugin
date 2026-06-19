# Herdr Input Method Keeper Development Notes

This document keeps the design, implementation, state model, concurrency model,
and development test notes for the plugin. For installation and day-to-day use,
start with [README.md](README.md).

Keep a stable macOS input source per Herdr pane.

Version 1 is intentionally small:

- macOS only
- Python 3 plugin logic
- `macism` as the default input-source backend
- per-Herdr-session pane state
- one optional global default input source
- one global default action: `keep`, `reset`, or `ignore`
- no rule engine
- no workspace/tab/agent/project defaults

## Goal

Herdr panes share the host macOS input source, but users often want each pane to
keep its own input method. This plugin observes pane focus changes, records the
input source associated with each pane, and restores it when focus returns.

If a pane has no stored input source yet, the plugin applies one global default
action. The default action uses the same vocabulary that a future rule engine
can reuse: `keep`, `reset`, and `ignore`.

## Dependencies

Install runtime dependencies:

```sh
brew install python
brew tap laishulu/homebrew
brew install macism
```

Python 3.9 or newer is required. Version 1 uses only Python standard-library
modules.

`macism` remains the default backend, but this repo also carries a small Swift
helper backend for input-context refresh testing.

Observed limitation: `macism` v3.1.1 only runs its TemporaryWindow workaround
when the target source is CJKV. When the plugin switches from WeType pinyin to
`com.apple.keylayout.ABC`, `macism` calls `TISSelectInputSource` directly. The
system source can report ABC while Herdr's current text input context still
handles WeType's Shift hotkey until the user focuses another app and returns.
Passing a larger wait argument to `macism` does not address this ABC target
path. The Swift helper backend with `select <id> --refresh --wait-ms 150` has
been manually validated to clear this residue in Herdr.

Reference:

- Herdr plugin docs: https://herdr.dev/docs/plugins/
- Herdr socket events: https://herdr.dev/docs/socket-api/
- macism: https://github.com/laishulu/macism

## Architecture

```text
Herdr event/action
  -> bin/ime-keeper
      -> resolves Python 3.9+
      -> src/ime_keeper.py
          -> HerdrContext
          -> JSON Config
          -> Session-scoped StateStore
          -> InputSourceBackend
              -> BackendExecutor
                  -> macism by default
```

The Python code should stay dependency-light and use standard library modules:

- `json` for config and state
- `pathlib` for paths
- `subprocess` for the configured backend executor and Herdr CLI calls
- `fcntl` for the run and focus file locks on macOS
- `logging` or a small append-only logger for debug logs

## Wrapper

`bin/ime-keeper` is a small POSIX shell wrapper. The Herdr manifest should call
this wrapper instead of calling `python3` directly because GUI or service launch
environments may not include Homebrew paths.

The wrapper should resolve Python in this order:

1. `HERDR_IME_KEEPER_PYTHON`
2. `/opt/homebrew/bin/python3`
3. `/usr/local/bin/python3`
4. `python3` on `PATH`

The wrapper must verify a candidate before using it:

```sh
"$candidate" -c 'import sys, json; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)'
```

If no valid Python is found, the wrapper should print a short diagnostic that
names every attempted path and tells the user to install Python 3.9 or newer
with Homebrew.

## Herdr CLI Calls

When the Python code calls back into Herdr, it should resolve the Herdr binary in
this order:

1. `HERDR_BIN_PATH`
2. `herdr` on `PATH`

`HERDR_BIN_PATH` is preferred because Herdr injects it into plugin runtime
commands and it points at the running Herdr binary. The `herdr` fallback is only
for manual development and smoke tests outside Herdr.

For runtime focus validation, use either the raw socket API `pane.current` with
empty params or run `herdr pane current` with `HERDR_PANE_ID` removed from the
child environment. Do not use `herdr pane current --current`, and do not leave
the event hook's `HERDR_PANE_ID` in the child environment for this check,
because Herdr treats that value as the caller pane and may return the event pane
even after focus has moved elsewhere.

## Backend Contract

The plugin logic depends on this backend interface:

```text
current() -> input_source_id
select(input_source_id) -> success/failure
list() -> optional list of known input_source_id values
```

The plugin should never call `select` blindly. All switching paths should go
through one helper:

```text
ensure_input_source(target):
  if target is empty:
    return no-op
  current = backend.current()
  if current == target:
    return already-current
  backend.select(target)
```

This avoids unnecessary macOS input-source churn when the current source already
matches the target. The explicit `doctor --select-self-test` command is the only
exception because it exists to test the backend select path.

Default macOS backend:

```text
current command: macism
select command:  macism {id}
```

Version 1 allows overriding the backend command in config so users can switch to
`im-select`, a local Swift helper, or another executable without changing code.

Local Swift helper backend:

```text
bin/herdr-ime-helper current
bin/herdr-ime-helper list
bin/herdr-ime-helper select <input_source_id> [--refresh] [--wait-ms N]
bin/herdr-ime-helper refresh [--wait-ms N]
```

`bin/herdr-ime-helper` is a POSIX wrapper around
`helpers/herdr-ime-helper.swift`. It compiles the Swift source with `swiftc` on
first run, caching the binary under `HERDR_PLUGIN_STATE_DIR/helper-build` when
Herdr provides a state directory, or under `TMPDIR` for manual runs. The wrapper
uses a small directory lock around compilation so concurrent first runs do not
write the same cached binary at the same time. The lock records a pid and
creation timestamp so a dead compiler process or old pidless lock can be
recovered without stealing an active compiler lock. The Swift code uses TIS
APIs for `current`, `list`, and `select`.
`--refresh` only creates a tiny temporary AppKit window and waits; it
intentionally contains no CJKV or policy logic. The Python plugin remains the
policy owner and may later decide when to add `--refresh` to select calls. The
actions
`set-backend-helper` and `set-backend-macism` switch `config.json` between the
helper and the default `macism` backend.

## Config

Config lives at `HERDR_PLUGIN_CONFIG_DIR/config.json`.

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

Config semantics:

- `enabled = false` disables automatic event handling. Event hooks may only run
  the idempotent state policy described below, then return without calling the
  backend, switching input sources, running cleanup, or appending debug logs.
  User-invoked configuration, status, and doctor actions may still run.
- `debug = true` enables debug logging.
- `session_name` is the user-facing session label shown in state paths, status
  output, and debug logs. `"auto"` derives a readable label from
  `HERDR_SOCKET_PATH`: the default Herdr session becomes `default`, named Herdr
  sessions use their session directory name, and custom socket paths become
  `socket`. Any other non-empty value is used as the readable label.
- `default_action = "keep"` means use pane memory when present; otherwise use
  `default_input_source` if configured.
- `default_action = "reset"` is stateless: do not use pane memory, and switch
  the event or action target to `default_input_source` if configured.
- `default_action = "ignore"` is stateless: do not switch input sources and do
  not record pane input sources.
- `default_input_source` is required for `keep` to initialize a new pane and for
  `reset` to switch anything.
- Removing or blanking `default_input_source` means `keep` restores only stored
  pane memory and `reset` does nothing beyond any state clearing already done
  when entering `reset`.
- `notify_on_focus = true` shows a Herdr notification after each successful
  focus decision. This is intentionally noisy for early real-world diagnosis.
  Herdr currently renders notification bodies as one practical line, so keep
  notification text to two concise fields: title for the pane losing focus
  (`OLD  CHNG: A -> B (<pane> <workspace>)`) and body for the newly focused pane
  plus default source (`NEW  SWCH: A -> B (<pane> <workspace>) | default X`).
  Use fixed four-character action codes: `INIT`, `CHNG`, `SAME`, `SWCH`,
  `NONE`, `MISS`, and `UNKN`.
- `pane_status_on_focus = true` writes `custom_status = "IME <source>"` to the
  focused pane metadata after each successful focus decision. This makes the
  selected input source visible in `herdr pane current` even if the UI does not
  render that metadata.
- `focus_log = true` appends one compact text line per successful focus
  decision to `focus.log` in the current session state directory. This is for
  `tail -f` style observation; it is intentionally separate from JSON debug
  logs.
- `status_ttl_ms` controls how long the pane metadata status remains valid.

`keep` is the only mode that uses pane memory state. Switching
`default_action` to `reset` or `ignore` deletes the current Herdr session's pane
state. In addition, any event or action entry point that observes
`default_action = "reset"` or `default_action = "ignore"` runs the shared state
policy and clears the current session's state before doing mode-specific work.
This covers users who edit `config.json` directly instead of using plugin
actions. Switching back to `keep` starts fresh; old pane memories are not
restored.

All configuration actions write back to `config.json`. The writer should:

- load the existing JSON object
- preserve unknown keys
- update only the requested key
- write a pretty-printed JSON file to a temporary path
- atomically rename it over `config.json`

If `config.json` is missing, mutating actions and `doctor` create it from
defaults before applying the requested action. If it exists but is not valid
JSON, mutating actions and `doctor` rename the original file to
`config.json.broken.<timestamp>`, create a default config, then apply the
requested action. `status` reports the config diagnostic without writing
anything. Event hooks should fail open on invalid JSON instead of trying to
repair it during a focus change.

JSON does not support comments. If users need notes, keep them in a separate
README or config comment file, not inside `config.json`.

## Locks

Herdr gives each installed plugin one stable state directory:

- `HERDR_PLUGIN_STATE_DIR`

This directory is per plugin id, not per Herdr session. In Herdr's current code,
`HERDR_PLUGIN_STATE_DIR` is `state_dir()/plugins/<plugin-id>`. The plugin must
therefore create its own per-session state files below that directory.

The plugin uses two lock levels:

- `HERDR_PLUGIN_STATE_DIR/run.lock`
- `HERDR_PLUGIN_STATE_DIR/sessions/<session-key>/focus.lock`

`run.lock` is global to the plugin. Any code path that writes config, mutates
`state.json`, calls the backend, or performs a real input-source decision must
hold this lock. This is intentional because the macOS input source is global to
the user session; two Herdr sessions trying to restore different input sources
at the same time should be serialized.

`focus.lock` is session-scoped and used only by `pane.focused`. A focus event
tries to acquire this lock without blocking. If it cannot acquire it, another
focus worker is already active for that Herdr session; the new event writes
`focus.dirty` for that session and exits immediately. This prevents a focus
storm from filling Herdr's plugin command in-flight limit.

Actions, `pane.closed`, `tab.closed`, `pane.moved`, and `workspace.closed` do not
use `focus.lock`; they acquire `run.lock` in blocking mode because they are
short or user-triggered. Focus workers acquire `focus.lock` first, then acquire
and release `run.lock` only around policy, state, and backend decision work. No
other path may acquire `focus.lock`, so there is no lock-order cycle.

These locks are required with Herdr's current plugin runner. Herdr starts plugin
actions and event hooks as background plugin commands, records them as running,
and returns without waiting for the child process to finish. It also allows
multiple plugin commands in flight. Therefore event hooks can overlap with other
event hooks or user-triggered actions, and stale focus events can queue behind
newer work.

`config.json`, `state.json`, and `focus.dirty` writes should still use temporary
files followed by `os.replace`. The locks prevent concurrent plugin invocations
from racing; atomic replace prevents a partially written JSON file from being
observed.

All external commands must have timeouts. If `macism`, a future backend, or a
Herdr CLI lookup hangs, the invocation must fail open and release any held
locks.

## State

State lives under `HERDR_PLUGIN_STATE_DIR`, split by the plugin's own session
key.

Different Herdr sessions use different state files. Session identity should be
computed as:

1. readable session label:
   - if `session_name` is a non-empty value other than `"auto"`, use it
   - otherwise derive from `HERDR_SOCKET_PATH`: `default` for the default
     `herdr.sock`, the directory name for `.../sessions/<name>/herdr.sock`, or
     `socket` for custom socket paths
2. stable socket hash: hash `HERDR_SOCKET_PATH` when available
3. internal state key: `<slug(label)>-<socket-hash-short>` when a socket hash is
   available, otherwise `<slug(label)>`

Do not depend on a `HERDR_SESSION` environment variable. Herdr plugin runtime
context provides `HERDR_SOCKET_PATH`, plugin directories, context JSON, and
available workspace/tab/pane ids, but not a guaranteed readable session name.
The socket hash is used only to keep state files distinct; status and debug
output should show the readable label first.

Recommended layout:

```text
sessions/<session-key>/state.json
sessions/<session-key>/focus.lock
sessions/<session-key>/focus.dirty
sessions/<session-key>/debug.current
sessions/<session-key>/debug.<UTC timestamp>.log
```

State shape:

```json
{
  "version": 1,
  "session_label": "default",
  "socket_path_hash": "sha256:...",
  "last_seen_at": "2026-06-16T14:00:00+08:00",
  "last_focused_pane_id": "w1:p2",
  "panes": {
    "w1:p1": {
      "input_source_id": "com.apple.keylayout.ABC",
      "workspace_id": "w1",
      "tab_id": "w1:t1",
      "agent": "codex",
      "cwd": "/repo",
      "source": "observed",
      "updated_at": "2026-06-16T14:00:00+08:00"
    }
  }
}
```

All `state.json` updates happen while holding `run.lock` to avoid concurrent
event hooks or actions overwriting each other. `focus.dirty` is the exception:
losing focus events may write it without `run.lock`, using a temporary file and
atomic replace.
`last_seen_at` is diagnostic data for manual cross-session maintenance. Update it
whenever code writes the current session state.

State loading rules:

- Missing `state.json` means an empty v1 state.
- `status` uses a read-only state load. It must not create, repair, rename,
  clear, or rewrite state files. If the state file is invalid, `status` reports
  the diagnostic and continues with no state details.
- Event hooks, mutating actions, `doctor`, and `doctor-gc-all` use a writable
  state load while holding `run.lock`. If `state.json` is invalid JSON, has an
  unsupported version, or has an invalid top-level shape, rename it to
  `state.json.broken.<timestamp>` and start with an empty v1 state.
- If the broken-state rename fails, fail open: write debug diagnostics when
  enabled, do not switch input sources, and do not write new state over the
  unreadable file.

State policy:

Every event hook, mutating action, `doctor`, and `doctor-gc-all` runs through one
shared policy function after loading config. Actions that mutate config run the
same policy again after writing the new config. `status` is the only exception:
it is read-only and reports the effective policy without applying state changes.

```text
reconcile_state_policy(config, cause):
  if enabled is false:
    clear current session state
    return disabled
  if default_action is "ignore":
    clear current session state
    return ignore
  if default_action is "reset":
    clear current session state
    return reset
  return keep
```

This is the only place that decides whether the current session state should be
cleared because of `enabled`, `reset`, or `ignore`. Event handlers and actions
should not duplicate those checks with ad hoc state deletion.

The policy does not decide whether a command should stop. It only reconciles
state and returns the effective mode. Event handlers use the returned mode to
decide whether to continue. User-invoked actions continue after policy unless
the action itself decides otherwise; this keeps `toggle-enabled`, `debug-on`,
`debug-off`, and `doctor` usable while `enabled = false`. `status` remains
usable because it does not run the write policy.

State clearing rules:

- Clearing state means deleting `state.json` or replacing it with an empty v1
  state for the current session. It should also delete `focus.dirty` for that
  session. It should not delete `debug.current` or timestamped debug logs.
- Enabling or disabling the plugin clears the current Herdr session's state. This
  prevents stale pane memories from being restored after the plugin has been
  paused or re-enabled. After disabling, event hooks perform no state operations
  beyond the shared policy's idempotent state clear.
- Setting `default_action` to `reset` or `ignore` clears the current Herdr
  session's state. These modes do not need pane memory, so keeping old state only
  creates stale future behavior.
- Any entry point that observes `default_action = "reset"` or
  `default_action = "ignore"` through the shared policy clears current session
  state. Repeated clears are valid no-ops.

State cleanup rules:

- Cleanup events are state operations and only run when `enabled = true` and
  `default_action = "keep"`.
- On `pane.closed`, delete that pane's state entry. If it is
  `last_focused_pane_id`, clear `last_focused_pane_id`.
- On `tab.closed`, delete all pane state entries whose `tab_id` matches the
  closed tab. If `last_focused_pane_id` belongs to that tab, clear
  `last_focused_pane_id`.
- On `workspace.closed`, delete all pane state entries whose `workspace_id`
  matches the closed workspace. If `last_focused_pane_id` belongs to that
  workspace, clear `last_focused_pane_id`.
- On `pane.moved`, migrate the old pane id's state entry to the moved pane's new
  public pane id and refresh its `workspace_id`, `tab_id`, and other metadata
  from the event payload. If `last_focused_pane_id` points at the old pane id,
  update it to the new pane id.
- Version 1 does not run list-based pane cleanup and does not call
  `herdr pane list` during focus handling. Normal cleanup is driven by
  `pane.closed`, `tab.closed`, `workspace.closed`, and `pane.moved`.
- Normal events and actions only touch the current Herdr session's state file
  and focus markers.
- Cross-session leftovers are handled only by explicit maintenance. Normal
  events and ordinary actions do not scan other sessions. The `doctor-gc-all`
  action, or manual `doctor --gc-all` command, may scan `sessions/*` and delete
  session directories whose `state.json.last_seen_at` is older than a fixed 30
  day threshold. It must always skip the current session key, even when that
  session's `last_seen_at` is missing or old. For non-current sessions with
  missing or invalid `state.json`, fall back to the session directory mtime and
  delete only when that mtime is older than the same threshold. If Herdr later
  exposes a clear session lifecycle event, use that event instead of guessing
  from socket paths or pane ids.

## Event Model

Version 1 uses Herdr manifest events:

```toml
[[events]]
on = "pane.focused"
command = ["bin/ime-keeper", "event", "pane-focused"]

[[events]]
on = "pane.closed"
command = ["bin/ime-keeper", "event", "pane-closed"]

[[events]]
on = "tab.closed"
command = ["bin/ime-keeper", "event", "tab-closed"]

[[events]]
on = "pane.moved"
command = ["bin/ime-keeper", "event", "pane-moved"]

[[events]]
on = "workspace.closed"
command = ["bin/ime-keeper", "event", "workspace-closed"]
```

Important behavior:

- `pane.focused` tells us the newly focused pane.
- It does not include the previous pane.
- It does not include input-source information.
- Empirically, the event is delivered after Herdr has already updated focused
  pane state.
- `pane.moved` is needed because moving a pane across workspaces can change its
  public pane id without emitting fake close/create events.
- `tab.closed` is needed because closing a tab emits `tab.closed`; Herdr does
  not emit one `pane.closed` event for each pane that was inside the tab.

Therefore the plugin maintains `last_focused_pane_id` itself.

Event field extraction:

- All event handlers read `HERDR_PLUGIN_EVENT_JSON` as a Herdr event envelope
  with top-level `event` and `data` fields. Use `HERDR_PLUGIN_EVENT` for the
  hook's dot-name (`pane.focused`, `tab.closed`, etc.); do not depend on the
  serialized spelling of the envelope's `event` field.
- For `pane.focused`, read the newly focused pane id from `data.pane_id` and the
  workspace id from `data.workspace_id`. If no pane id is available, do not infer
  one from plugin context; the focus worker revalidates the real current pane
  before making any input-source decision.
- For `pane.closed`, read the closed pane id from `data.pane_id` and the
  workspace id from `data.workspace_id`. If no pane id is available, fail open
  and do not delete state.
- For `tab.closed`, read the closed tab id from `data.tab_id` and the workspace
  id from `data.workspace_id`. If no tab id is available, fail open and do not
  delete state.
- For `pane.moved`, read `data.previous_pane_id`,
  `data.previous_workspace_id`, `data.previous_tab_id`, and `data.pane`. The new
  public pane id is `data.pane.pane_id`; the new workspace and tab metadata come
  from `data.pane`. If any required field is missing or malformed, fail open and
  do not migrate state.
- For `workspace.closed`, read the closed workspace id from `data.workspace_id`.
  `data.workspace` may contain a final workspace snapshot, but it can be absent.
  If no workspace id is available, fail open and do not delete state.

On `pane.focused`:

1. Compute the session key from config and `HERDR_SOCKET_PATH`.
2. Try to acquire `sessions/<session-key>/focus.lock` without blocking.
3. If `focus.lock` is busy, write `sessions/<session-key>/focus.dirty` with a
   timestamp and the event pane id when available, then return immediately. Do
   not call Herdr CLI, the backend, or `reconcile_state_policy` from this
   losing focus event.
4. After winning `focus.lock`, acquire the global `run.lock` in blocking mode
   for a short initial policy check.
5. Load config and run `reconcile_state_policy`.
6. If the policy is `disabled` or `ignore`, delete `focus.dirty`, release
   `run.lock` and `focus.lock`, then return without reading backend state,
   switching input sources, writing pane memory, or appending debug logs.
7. Release `run.lock` before debounce/coalescing. Keep `focus.lock`; it does not
   block Herdr UI focus changes, only other plugin focus workers for the same
   Herdr session.
8. Enter a coalescing loop with a bounded total runtime, for example 1 second.
9. Read the currently focused pane by calling raw socket `pane.current` with
   empty params, or `herdr pane current` with `HERDR_PANE_ID` removed from the
   child environment. Do not trust the original event pane after this point; it
   is only a hint that focus changed.
10. Wait until the focused pane is stable for a short debounce window, for example
   80-120 ms, with a per-cycle maximum of 250-300 ms. If the focused pane changes
   during the debounce window, restart the cycle with the new pane.
11. Acquire `run.lock` again for the actual input-source decision.
12. Reload config and run `reconcile_state_policy` again because config may have
   changed while `run.lock` was released.
13. If the policy is `disabled` or `ignore`, delete `focus.dirty`, release
   `run.lock`, and return.
14. Read the currently focused pane again. If it no longer matches the stable
   focused pane, release `run.lock` and restart the coalescing cycle without
   selecting.
15. If the policy is `reset`, resolve the target from `default_input_source`,
   call `ensure_input_source(target)`, write debug details if enabled, clear
   `focus.dirty`, release `run.lock`, and continue the loop only if a new dirty
   marker appeared or the focused pane changed during the operation.
16. For `keep`, load current session state using writable state loading.
17. If the stable focused pane is already `last_focused_pane_id`, update
   `last_seen_at`, clear `focus.dirty`, and continue the loop only if a new dirty
   marker appeared or the focused pane changed during the operation. Release
   `run.lock` before the next cycle or before returning.
18. Compute the previous pane candidate from `last_focused_pane_id` only when it
   exists and differs from the stable focused pane.
19. If a previous pane candidate exists, read current input source with the
   configured backend as early as possible in this cycle and keep it as a
   pending observation for the previous pane. Treat this value as
   `observed_before_plugin_switch`, not as a guaranteed old pane input source.
20. If a pending observation was read, read the current focused pane again. If
   focus changed during the backend call, discard the observation, release
   `run.lock`, and restart the coalescing cycle without selecting.
21. If no previous pane candidate exists, skip the pending-observation step. If a
   pending observation exists and focus is still stable, store it for the
   previous pane, then resolve the target input source for the stable focused
   pane: use the pane's stored state if present; otherwise use
   `default_input_source` if configured.
22. Before selecting, read the current focused pane again. If it no longer
   matches the stable focused pane, release `run.lock` and restart the
   coalescing cycle without selecting.
23. Call `ensure_input_source(target)` so no select happens when the target
   already equals the current input source.
24. Save `last_focused_pane_id` as the stable focused pane and update
   `last_seen_at`.
25. Clear `focus.dirty`, then check whether a new dirty marker appeared or the
   focused pane changed while the cycle was running. If so, release `run.lock`
   and run another cycle within the total runtime limit; otherwise release
   `run.lock` and `focus.lock`.

Releasing `run.lock` during debounce does not prevent Herdr focus from changing;
that is not the goal. The guard is that `focus.lock` keeps only one focus worker
active per Herdr session, later focus events mark `focus.dirty`, and the worker
validates the current focused pane again before any backend `select`.

On `pane.closed`:

1. Acquire `run.lock`.
2. Load config and run `reconcile_state_policy`.
3. If the policy is not `keep`, return. The policy has already handled any
   required state clearing.
4. Load current session state using writable state loading.
5. Read the closed pane id using the event field extraction rules.
6. If the closed pane id is missing, fail open.
7. Delete that pane's state entry.
8. Clear `last_focused_pane_id` if it points at the closed pane.
9. Save state and write debug details if enabled.
10. Release `run.lock`.

On `tab.closed`:

1. Acquire `run.lock`.
2. Load config and run `reconcile_state_policy`.
3. If the policy is not `keep`, return. The policy has already handled any
   required state clearing.
4. Load current session state using writable state loading.
5. Read the closed tab id using the event field extraction rules.
6. If the closed tab id is missing, fail open.
7. Delete pane state entries whose `tab_id` matches.
8. Clear `last_focused_pane_id` if that pane belonged to the closed tab.
9. Save state and write debug details if enabled.
10. Release `run.lock`.

On `pane.moved`:

1. Acquire `run.lock`.
2. Load config and run `reconcile_state_policy`.
3. If the policy is not `keep`, return. The policy has already handled any
   required state clearing.
4. Load current session state using writable state loading.
5. Read `previous_pane_id`, `previous_workspace_id`, `previous_tab_id`, and
   moved `pane` using the event field extraction rules.
6. If required fields are missing or malformed, fail open: write debug details
   if enabled, do not migrate state, and return.
7. If the old pane id has a state entry, move it to `pane.pane_id` and refresh
   metadata from the moved `pane`.
8. If `last_focused_pane_id` points at the old pane id, update it to the new
   pane id.
9. Save state and write debug details if enabled.
10. Release `run.lock`.

On `workspace.closed`:

1. Acquire `run.lock`.
2. Load config and run `reconcile_state_policy`.
3. If the policy is not `keep`, return. The policy has already handled any
   required state clearing.
4. Load current session state using writable state loading.
5. Read the closed workspace id using the event field extraction rules.
6. If the closed workspace id is missing, fail open.
7. Delete pane state entries whose `workspace_id` matches.
8. Clear `last_focused_pane_id` if that pane belonged to the closed workspace.
9. Save state and write debug details if enabled.
10. Release `run.lock`.

The input source read during focus handling should be treated as
`observed_before_plugin_switch`, not as a guaranteed "old pane input source".
macOS may have changed the input source during app focus restoration before the
event hook runs.

All external commands must have short timeouts:

- backend `current` and `select`: 2 seconds each
- Herdr CLI metadata lookups: 1 second each

On timeout or command failure, the plugin should fail open: write debug
diagnostics when enabled, avoid switching input sources, save state only when it
is safe to do so, and release any held locks. Disabled, `ignore`, and `reset`
focus paths should not write fallback focus metadata.

## Performance Guardrails

The hot path is `pane.focused`. Version 1 should avoid work that is not needed
to make a focus decision:

- `pane.focused` uses a session-scoped nonblocking `focus.lock`. Losing focus
  events only write `focus.dirty` and exit, so they do not wait behind long
  backend calls.
- The focus worker releases `run.lock` while waiting for focus to stabilize.
  `focus.lock` coalesces plugin workers for the current Herdr session; it does
  not and should not block Herdr UI focus changes.
- `disabled` and `ignore` do not call the backend or Herdr CLI after the focus
  worker has acquired `run.lock`.
- `reset` coalesces to the stable current focused pane, then uses
  `ensure_input_source` for `default_input_source`.
- `keep` reads backend `current` only when focus actually moves to a different
  pane and there is a previous pane candidate to update.
- `keep` skips backend `select` when the target input source already equals the
  current input source.
- `pane.closed`, `tab.closed`, `pane.moved`, and `workspace.closed` only mutate
  state; they do not call the backend.
- Focus handling does not call `herdr pane get` or `herdr pane list`. It may
  call `herdr pane current` or raw socket `pane.current` only for stale focus
  validation. Pane lifecycle state is maintained by pane close, tab close, pane
  move, and workspace close events.
- Backend and Herdr CLI subprocess calls must use short timeouts so one slow
  executable does not hold `run.lock` indefinitely.

## Actions

Version 1 exposes these actions:

```text
toggle-enabled
status
set-default-input-source
set-default-action-keep
set-default-action-reset
set-default-action-ignore
debug-on
debug-off
set-backend-helper
set-backend-macism
doctor
doctor-gc-all
```

Action behavior:

- Mutating actions acquire `run.lock`, load config, and run
  `reconcile_state_policy` before doing action-specific work. Actions that write
  config run `reconcile_state_policy` again after saving the new config. This is
  idempotent and keeps manual `config.json` edits from reviving old pane memory.
- `toggle-enabled`: read `enabled` from `config.json`, write the opposite value,
  and clear the current Herdr session's state before returning.
- `status`: print config, user-facing session name, internal session key,
  current pane, current input source, stored pane state, default action, and
  default input source. It is strictly read-only: it must not create, repair,
  rename, clear, or rewrite config/state files, and it must not run
  `reconcile_state_policy`.
- `set-default-input-source`: read the current input source and write it to
  `config.json` as `default_input_source`.
- `set-default-action-keep`: write `default_action = "keep"` to `config.json`.
  This does not restore state previously cleared by `reset` or `ignore`.
- `set-default-action-reset`: write `default_action = "reset"` to `config.json`
  and clear the current Herdr session's state.
- `set-default-action-ignore`: write `default_action = "ignore"` to
  `config.json` and clear the current Herdr session's state.
- `debug-on`: write `debug = true` to `config.json`.
- `debug-off`: write `debug = false` to `config.json`.
- `set-backend-helper`: write the bundled Swift helper backend config to
  `config.json`.
- `set-backend-macism`: write the default `macism` backend config to
  `config.json`.
- `doctor`: acquire `run.lock`, verify the wrapper, resolved Python, backend
  executor, Herdr env, `HERDR_BIN_PATH`, config dir, state dir, config/state
  loading and repair, shared state policy, and current input source read. It may
  write repaired config/state and may clear current session state through
  `reconcile_state_policy`, but it must not call backend `select` by default.
- `doctor-gc-all`: perform the normal doctor checks, then scan all session
  directories and delete only those whose `last_seen_at` is older than a fixed
  30 day threshold. It must skip the current session key.

`status` is the only read-only inspection action. `doctor` is allowed to write
because it is a repair/diagnostic action, so it follows the same policy and state
repair paths as event hooks and mutating actions.

The CLI can also support `set-default-action <value>` for manual use, but the
manifest exposes one fixed action per value because Herdr actions do not prompt
for arguments.

The CLI can also support `doctor --select-self-test` for manual troubleshooting.
It should perform the normal doctor checks, then select the current input source
to verify the backend select path. The manifest should not expose this as a
default action because it may have a macOS input-source side effect even though
it selects the current source id.

When `enabled` is false, event hooks run only the shared policy's idempotent
state clear and then return. User-invoked configuration, status, and doctor
actions may still run so the user can inspect or change `config.json`.

## Dashboard Pane

The manifest exposes one plugin pane entrypoint:

```toml
[[panes]]
id = "dashboard"
title = "Input method keeper dashboard"
placement = "split"
command = ["sh", "bin/ime-keeper", "dashboard"]
```

Plugin panes resolve the first command through `PATH`, so the pane entrypoint
uses `sh` and passes the repo-local wrapper as an argument. Action and event
commands can continue to use `bin/ime-keeper` directly.

Open it with:

```sh
herdr plugin pane open --plugin ppggff.input-method-keeper --entrypoint dashboard
```

`bin/ime-keeper dashboard` is read-only. It refreshes in place once per second
by default, clears the current screen plus terminal scrollback on each live
refresh, and can be run manually with `--once` for tests:

```sh
bin/ime-keeper dashboard --once
bin/ime-keeper dashboard --interval 2
bin/ime-keeper dashboard --once --color always
```

The dashboard collects:

- effective config and session identity
- backend executable and backend-reported current input source
- current session state file contents
- live Herdr workspaces, tabs, and panes from Herdr CLI list commands. Tabs are
  collected with one global `tab list` call when available, falling back to
  per-workspace tab calls only if the global call fails.

Rendered output should stay compact enough for one screen. Header details are
limited to the current session, enabled/debug/action, default/current input
source, backend name, and live/state pane counts. Workspace and tab labels give
context, and panes render as only `pane-id=status`. Use `>` as the focused
marker for workspace, tab, and pane. Render workspace and tab labels in
parentheses, for example `workspace 5 (hatch-deck)` and `tab 4 (4)`, so numeric
tab labels are not confused with tab numbers. Header/status lines must remain
plain text even in color mode. In color mode, only the focused marker is colored
for workspace and tab; the focused pane id uses square brackets with the same
color as the marker. Stored state uses the muted timestamp color. Do not use
ANSI background or inverse-video styles. Do not render focus log tails, cwd,
agent, or update timestamps in the dashboard. Keep the muted `Ctrl-C to exit`
hint at the bottom.

Color is ANSI-only and dependency-free. `--color auto` is the default: enable
color when stdout is a TTY, disable it for pipes/tests, disable it when
`NO_COLOR` is set, and disable it when `TERM=dumb`. `--color always` and
`--color never` are manual overrides.

It must not acquire `run.lock`, mutate config, repair broken files, clear state,
or select an input source. Backend `current` and Herdr list failures should be
rendered as diagnostics, not treated as fatal. The dashboard pane is deliberately
not ignored by focus handling; it is just another Herdr pane and can have its
own remembered input source.

## Draft Manifest

```toml
id = "ppggff.input-method-keeper"
name = "Input Method Keeper"
version = "0.1.0"
min_herdr_version = "0.7.0"
description = "Keep macOS input sources stable per Herdr pane."
platforms = ["macos"]

[[panes]]
id = "dashboard"
title = "Input method keeper dashboard"
placement = "split"
command = ["sh", "bin/ime-keeper", "dashboard"]

[[actions]]
id = "toggle-enabled"
title = "Toggle input method keeper"
command = ["bin/ime-keeper", "toggle-enabled"]

[[actions]]
id = "status"
title = "Show input method keeper status"
command = ["bin/ime-keeper", "status"]

[[actions]]
id = "set-default-input-source"
title = "Set default input source"
command = ["bin/ime-keeper", "set-default-input-source"]

[[actions]]
id = "set-default-action-keep"
title = "Use default input method action: keep"
command = ["bin/ime-keeper", "set-default-action", "keep"]

[[actions]]
id = "set-default-action-reset"
title = "Use default input method action: reset"
command = ["bin/ime-keeper", "set-default-action", "reset"]

[[actions]]
id = "set-default-action-ignore"
title = "Use default input method action: ignore"
command = ["bin/ime-keeper", "set-default-action", "ignore"]

[[actions]]
id = "debug-on"
title = "Enable input method keeper debug logging"
command = ["bin/ime-keeper", "debug-on"]

[[actions]]
id = "debug-off"
title = "Disable input method keeper debug logging"
command = ["bin/ime-keeper", "debug-off"]

[[actions]]
id = "set-backend-helper"
title = "Use Swift input method helper"
command = ["bin/ime-keeper", "set-backend-helper"]

[[actions]]
id = "set-backend-macism"
title = "Use macism input method backend"
command = ["bin/ime-keeper", "set-backend-macism"]

[[actions]]
id = "doctor"
title = "Diagnose input method keeper"
command = ["bin/ime-keeper", "doctor"]

[[actions]]
id = "doctor-gc-all"
title = "Diagnose and clean old input method keeper state"
command = ["bin/ime-keeper", "doctor", "--gc-all"]

[[events]]
on = "pane.focused"
command = ["bin/ime-keeper", "event", "pane-focused"]

[[events]]
on = "pane.closed"
command = ["bin/ime-keeper", "event", "pane-closed"]

[[events]]
on = "tab.closed"
command = ["bin/ime-keeper", "event", "tab-closed"]

[[events]]
on = "pane.moved"
command = ["bin/ime-keeper", "event", "pane-moved"]

[[events]]
on = "workspace.closed"
command = ["bin/ime-keeper", "event", "workspace-closed"]
```

## Keybindings

Users can bind actions in Herdr config:

```toml
[[keys.command]]
key = "prefix+i"
type = "plugin_action"
command = "ppggff.input-method-keeper.toggle-enabled"
description = "toggle input method keeper"

[[keys.command]]
key = "prefix+shift+i"
type = "plugin_action"
command = "ppggff.input-method-keeper.status"
description = "input method keeper status"
```

## Debug Logging

When debug is enabled, each event/action should log:

- timestamp
- session label
- session key
- event/action name
- active mode
- focused pane and previous pane
- default input source
- observed previous pane input source
- stored target input source
- backend current source before select
- select action: `selected`, `already-current`, or `no-target`
- skip/failure reason
- cleanup or pane-move details when applicable

Debug logs must avoid unbounded growth. Version 1 writes directly to a
timestamped active file such as `debug.20260618T103000123456Z.log`.
`debug.current` stores the active filename. When the active file grows past
100 MB, create a new timestamped file and update `debug.current`.

## Manual Smoke Tests

Run local smoke tests from this repository before installing the plugin into a
live Herdr session:

```sh
cd input-method-keeper
export HERDR_PLUGIN_CONFIG_DIR="$(mktemp -d)"
export HERDR_PLUGIN_STATE_DIR="$(mktemp -d)"
export HERDR_IME_KEEPER_PYTHON="$(command -v python3)"

bin/ime-keeper status
bin/ime-keeper doctor
bin/ime-keeper set-default-input-source
bin/ime-keeper set-default-action keep
bin/ime-keeper toggle-enabled
bin/ime-keeper toggle-enabled
```

`status` is read-only and should not create `config.json` or state files.
`doctor` may create or repair config/state. `set-default-input-source` requires
the configured backend to be available, so with the default config it requires
`macism`.

Use the explicit select self-test only when backend switching should be tested:

```sh
bin/ime-keeper doctor --select-self-test
```

This reads the current input source and then calls the backend select command
with that same input source id. It is intentionally not exposed as a default
Herdr action.

After installing or reloading the plugin in Herdr, run these live checks:

1. Run the `doctor` action and confirm the JSON shows the expected
   `herdr_socket_path`, `session_key`, backend executable, and current input
   source.
2. Run `set-default-input-source` while the desired default macOS input source
   is active.
3. With `default_action = "keep"`, focus pane A, switch macOS input source,
   focus pane B, switch to another source, then focus pane A again. Pane A
   should return to its remembered input source.
4. Run `set-default-action-reset`; focusing any pane should switch to
   `default_input_source` and the current session state file should be removed.
5. Run `set-default-action-ignore`; focusing panes should not call the backend
   select path and should not create pane state.

The repository also includes an automated Herdr smoke runner:

```sh
input-method-keeper/scripts/herdr_smoke.py --session ime-smoke --link
```

This links/enables the local plugin if needed, verifies the manifest action
list, invokes `status` and `doctor` through Herdr, then polls Herdr plugin logs
until those actions finish with exit code 0.

Use a dedicated Herdr session for live smoke tests. `--session ime-smoke` uses a
separate Herdr socket such as
`~/.config/herdr/sessions/ime-smoke/herdr.sock`, so the plugin derives a
different session key under `HERDR_PLUGIN_STATE_DIR/sessions/`. That keeps smoke
pane memory separate from the default Herdr session. The plugin config directory
is still shared by plugin id, so the smoke runner backs up and restores
`config.json` around tests that write a fake backend config. State backup and
restore are scoped to the current smoke session directory only.

When running inside Codex or another sandbox, the live smoke runner must be able
to write both:

```text
~/.local/state/herdr/plugins/ppggff.input-method-keeper
~/.config/herdr/plugins/config/ppggff.input-method-keeper
```

The runner performs a state-restore write preflight after `status` and before
destructive E2E actions such as `set-default-action-reset` or `toggle-enabled`.
If the sandbox cannot write the plugin state directory or config directory, smoke
fails before or during the E2E setup. Do not bypass this by executing restore
scripts through a Herdr pane; that can inject commands into an interactive
terminal. Instead, either grant the sandbox these writable roots or run live
smoke from a normal terminal.

To clean up the dedicated smoke session:

```sh
herdr session stop ime-smoke
herdr session delete ime-smoke
```

To test the real Herdr pane focus/event flow without depending on macOS having
two switchable input sources available in the current automation context, run:

```sh
input-method-keeper/scripts/herdr_smoke.py --session ime-smoke --link --fake-backend
```

This creates a temporary Herdr workspace, writes a temporary plugin config that
uses a fake input-source backend under `/tmp`, focuses two real Herdr panes back
and forth, waits for each `pane.focused` hook to finish, verifies the remembered
per-pane input source behavior, restores the previous plugin config, and closes
the temporary workspace.

For a broader regression pass over plugin logic, run:

```sh
input-method-keeper/scripts/herdr_smoke.py --session ime-smoke --link --complex-fake
```

This still uses a fake backend, but it drives a live Herdr session through a
more complex sequence: three-pane memory restore, quick focus changes,
`reset`, `ignore`, disabled mode, re-enable, config restore, and workspace
cleanup. This should be the main repeatable E2E check before changing focus,
state, lock, or action behavior.

To test the default `macism` backend against real macOS input sources, run:

```sh
input-method-keeper/scripts/herdr_smoke.py --session ime-smoke --link --full-ime
```

`--full-ime` requires two input source ids that `macism` can actually switch to
from a Herdr pane context. The smoke runner creates a temporary Herdr workspace
and runs `macism` inside that pane for both auto-detection and simulated user
input-source changes. This matters because macOS can report different current
input sources for different foreground apps/input contexts; running `macism`
directly from another terminal or automation process may test that other app
instead of Herdr. If auto-detection cannot find two sources, pass them
explicitly:

```sh
HERDR_IME_KEEPER_TEST_SOURCE_A=com.apple.keylayout.ABC \
HERDR_IME_KEEPER_TEST_SOURCE_B=com.apple.inputmethod.SCIM.ITABC \
input-method-keeper/scripts/herdr_smoke.py --session ime-smoke --link --full-ime
```

To also exercise real backend action behavior, add `--real-actions`:

```sh
input-method-keeper/scripts/herdr_smoke.py --session ime-smoke --link --full-ime --real-actions
```

This extends the real macOS test with `reset` and `ignore`. It intentionally
keeps the real-backend path narrower than `--complex-fake` because it changes
the user's actual macOS input source and depends on Herdr's foreground input
context.

## Known Limitations

- macOS input source is not truly pane-local. The plugin approximates pane-local
  behavior by observing and restoring the host input source.
- `pane.focused` does not provide the previous pane or old input source.
- If the user changes input source in another app and returns to Herdr without a
  pane focus event, version 1 may not restore immediately.
- If macOS itself restores a document/app-specific input source before the event
  hook runs, the plugin may attribute that observed input source to the previous
  pane.
- With `macism` v3.1.1, switching from WeType pinyin to ABC can leave Herdr's
  text input context stale even though `macism` reports ABC. The Swift helper
  backend's `--refresh` path has manually fixed this symptom in Herdr.
- Version 1 has no rule engine and no ignore list. Use
  `default_action = "ignore"` or `enabled = false` to pause the plugin globally,
  or switch `default_action` to `reset` when all panes should use the default
  input source without pane memory.
- Version 1 does not infer closed Herdr sessions. Stale cross-session state is
  removed only by `doctor-gc-all` / `doctor --gc-all` using an age threshold, or
  by a future Herdr session lifecycle event if one becomes available.
- A future long-running monitor could improve this by observing macOS input
  source changes in real time and associating them with the currently focused
  Herdr pane.

## Future Ideas

- Reduce the `pane.focused` `run.lock` critical section. The current
  implementation keeps state updates serialized and uses short external-command
  timeouts, but still calls backend current/select and Herdr status publishing
  while holding `run.lock`. A future refactor should snapshot/load state under
  the lock, run backend/Herdr I/O outside the lock where possible, then re-enter
  the lock with a generation or last-seen validation before saving state.
- A simple TUI settings pane could build on the dashboard once the current
  behavior has been used for a while. Keep it as a settings and explicit-action
  surface, not a replacement for the automatic focus handler.
- Candidate TUI operations: toggle debug, switch backend, set default input
  source, set default action, inspect/focus a pane, and possibly reset selected
  pane state.
- Keep the compact read-only dashboard as the low-risk default. Avoid adding a
  heavy dependency or reviving the rule engine just to support the TUI.
- If the TUI can focus another pane, expect the settings pane to lose keyboard
  focus after that operation. That behavior is acceptable and should be designed
  deliberately.

## Implementation Order

1. Add manifest, `bin/ime-keeper` wrapper, default config example, and
   `src/ime_keeper.py` CLI skeleton.
2. Implement Python and Herdr binary resolution, including `HERDR_BIN_PATH`.
3. Implement backend commands: `current`, `select`, `doctor`.
4. Implement session label/key derivation from `session_name` and
   `HERDR_SOCKET_PATH`.
5. Implement `run.lock`, per-session `focus.lock`, `focus.dirty`, and atomic JSON
   writes.
6. Implement config loading and writing for `config.json`.
7. Implement session state store and the shared `reconcile_state_policy`.
8. Implement `default_action` and `default_input_source`, including stateless
   `reset` and `ignore` modes.
9. Implement `pane.focused` single-flight/coalescing event flow.
10. Implement `pane.closed`, `tab.closed`, `pane.moved`, and `workspace.closed`
   cleanup or migration for `keep` mode.
11. Implement actions, including state clearing on enable/disable and when
   switching to `reset` or `ignore`.
12. Add `doctor-gc-all` / `doctor --gc-all` age-based cross-session cleanup that
   skips the current session key.
13. Add manual smoke test instructions.
14. Add debug log rotation.
