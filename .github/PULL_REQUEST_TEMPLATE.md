<!--
Thanks for contributing. Keep this short -- a couple of sentences is usually plenty.
Delete any section that doesn't apply.
-->

## What this changes

<!-- What does it do, and why? If it fixes an open issue, write "Fixes #123". -->

## Checks

- [ ] `ruff check .` and `ruff format --check .` pass
- [ ] `mypy pyskraper/` passes
- [ ] `pytest` passes

<!--
These four are exactly what CI runs, on Python 3.12 and 3.13. Running them locally
first is faster than waiting for the workflow. Setup is in CONTRIBUTING.md; install
the `images` extra (`pip install -e ".[dev,images]"`) or ten image tests skip silently.
-->

## Adding a handheld?

<!-- Delete this section if your PR isn't a new device. -->

- [ ] Entry added to `pyskraper/devices.py`, next to the same brand's other devices
- [ ] Board name mapped in `pyskraper/detect.py`
- [ ] Table in `DEVICES.md` updated to match
- [ ] Screen resolution is the **displayed** geometry (watch for rotated panels — see [DEVICES.md](https://github.com/Gnzlt/pySkraper/blob/main/DEVICES.md#adding-your-device))

**Where the resolution came from:** <!-- spec sheet, KNULLI build tree, bootlogo header, a device you own -->

## Anything that touches deletion or the card

<!-- Delete this section if your PR doesn't go near dedupe, verify --clean-orphans, or writing to the card. -->

The project's standing rules, for reference — a PR that changes any of these needs to say so
explicitly and explain why:

- ROM files are never deleted or overwritten. Metadata and media are regenerable; ROMs are not.
- Nothing deletes anything unless asked twice: `dedupe` reports by default, quarantines before it
  deletes, and refuses `--non-interactive --delete`.
- `verify --clean-orphans` removes media and gamelist entries only, never a ROM.
- Writes to the card go through `core/atomic.py` (`.part` → `fsync` → `os.replace`).

## Notes for the reviewer

<!-- Anything surprising, anything you're unsure about, anything you couldn't test. -->
