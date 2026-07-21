# Herdr Input Method Keeper

This repository contains `input-method-keeper`, a Herdr plugin for macOS input
source memory.

## What It Does

- Remembers the macOS input source used by each Herdr pane.
- Restores a pane's remembered input source when focus returns to that pane.
- Supports one global fallback input source and one global default action.
- Provides status, diagnostics, and an optional live dashboard pane.

## Get Started

Install the current release:

```sh
herdr plugin install ppggff/herdr-plugin/input-method-keeper \
  --ref v0.2.0 --yes
```

See [input-method-keeper/README.md](input-method-keeper/README.md) for install,
upgrade, quick start, configuration, testing, and troubleshooting.

See the [v0.2.0 release](https://github.com/ppggff/herdr-plugin/releases/tag/v0.2.0)
for highlights and validation details.

For the GitHub publishing checklist, see [PUBLISHING.md](PUBLISHING.md).
