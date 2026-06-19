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
- `version = "0.1.0"`
- `min_herdr_version = "0.7.0"`
- `platforms = ["macos"]`
- actions, pane entrypoint, and event hooks as argv command arrays

The public plugin id is `ppggff.input-method-keeper`. Renaming it later changes
the Herdr config and state directories users will see.

## Marketplace readiness

The Herdr marketplace is not live yet. To be discoverable when it launches:

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
