# Publishing Checklist

This checklist follows the Herdr plugin docs for GitHub-installable plugins.

## Required for GitHub install

- Keep `herdr-plugin.toml` in the plugin directory.
- Publish a normal public GitHub repository.
- Install with the GitHub shorthand:

```sh
herdr plugin install ppggff/herdr-plugin/input-method-keeper
```

Herdr accepts `owner/repo[/subdir...]`, so this repository can keep the plugin
under `input-method-keeper/`.

## Manifest readiness

The current manifest already declares the fields Herdr requires:

- `id = "ppggff.input-method-keeper"`
- `name = "Input Method Keeper"`
- `version = "0.4.0"`
- `min_herdr_version = "0.7.4"`
- `platforms = ["macos"]`
- a macOS build preflight that verifies Python and prepares a usable backend
- actions, pane entrypoint, and event hooks as argv command arrays

The public plugin id is `ppggff.input-method-keeper`. Renaming it later changes
the Herdr config and state directories users will see.

## Marketplace readiness

The [Herdr plugin marketplace](https://herdr.dev/plugins/) automatically indexes
public GitHub repositories with the `herdr-plugin` topic. To remain
discoverable:

- Add the GitHub repository topic `herdr-plugin`.
- Keep manifest metadata accurate, especially `id`, `name`, `description`, and
  `platforms`.
- Declare `platforms = ["macos"]` honestly because this plugin depends on macOS
  input-source APIs.

## Repository readiness

- License is MIT.
- Add a GitHub repository description such as:
  `Keep macOS input sources stable per Herdr pane.`
- Add topics such as `herdr-plugin`, `macos`, `input-method`, and `ime`.
- Run the unit tests before tagging:

```sh
python3 -m unittest discover -s input-method-keeper/tests
```

- For live verification, run the smoke checks from
  [input-method-keeper/README.md](input-method-keeper/README.md#testing).

## Post-release checklist

Immediately after publishing a tag:

1. Confirm the GitHub release is public and points at the tagged commit.
2. Install the exact tag through Herdr rather than relying on a local link:

   ```sh
   herdr plugin install ppggff/herdr-plugin/input-method-keeper \
     --ref vX.Y.Z --yes
   ```

3. Confirm the managed plugin reports the expected version, requested ref,
   resolved commit, plugin root, and backend executable:

   ```sh
   herdr plugin list --plugin ppggff.input-method-keeper --json
   herdr plugin action invoke status --plugin ppggff.input-method-keeper
   herdr plugin action invoke doctor --plugin ppggff.input-method-keeper
   ```

4. Confirm recent event hooks exit successfully and do not emit stderr:

   ```sh
   herdr plugin log list --plugin ppggff.input-method-keeper --limit 20
   ```

5. Verify an actual two-pane cycle in both directions with two installed input
   sources. Restore the user's original host input source afterward.

Record the tag, resolved commit, Herdr version, backend executable, test result,
and test date in the release notes or the version's release record.

During the first week of normal use, sample these signals without forcing
cleanup first:

- After ordinary focus activity beyond the first due maintenance interval,
  `status` shows an advancing reconciliation timestamp and sensible
  live/stored/unmatched/missing counts.
- Ordinary pane, tab, workspace, and pane-move activity does not leave confirmed
  stale current-session records. Ambiguous records may remain until Herdr can
  confirm absence.
- Focus events continue to exit successfully. Investigate repeated slow events,
  lock contention, selection failures, or a sustained latency regression rather
  than reacting to one isolated sample.
- If logs reach their thresholds, `focus.log` rotates at 5 MiB and retains two
  historical segments, while debug logs rotate at 10 MiB and retain three.
- The dashboard and `status` remain read-only; routine current-session cleanup
  happens automatically. `doctor --gc-all` is a forced recovery and old-session
  maintenance path, not a daily-use requirement.
