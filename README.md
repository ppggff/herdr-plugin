# Herdr Input Method Keeper

This repository contains `input-method-keeper`, a Herdr plugin for macOS input
source memory.

## What It Does

- Remembers the macOS input source used by each Herdr pane.
- Restores a pane's remembered input source when focus returns to that pane.
- Supports one global fallback input source and one global default action.
- Provides a compact settings popup, status, diagnostics, and an optional live dashboard pane.

## Get Started

Install the current release:

```sh
herdr plugin install ppggff/herdr-plugin/input-method-keeper \
  --ref v0.4.0 --yes
```

See [input-method-keeper/README.md](input-method-keeper/README.md) for install,
upgrade, quick start, configuration, testing, and troubleshooting.

Version 0.4 adds a compact keyboard-driven settings popup backed by the same
mutation semantics as the existing actions. See the
[v0.4 design and release evidence](input-method-keeper/V0.4.md).

See the [v0.4.0 release](https://github.com/ppggff/herdr-plugin/releases/tag/v0.4.0)
for highlights and validation details.

For the GitHub publishing and post-release verification checklist, see
[PUBLISHING.md](PUBLISHING.md).
