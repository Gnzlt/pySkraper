# AGENTS.md

Orientation for AI coding agents working on pySkraper. Human contributors want
[CONTRIBUTING.md](CONTRIBUTING.md) — this file assumes it and adds the constraints that are easy to
violate without knowing the history behind them.

## What this is

A Python CLI that downloads game metadata and artwork from ScreenScraper.fr and writes it onto a
KNULLI/Batocera handheld's SD card, from a desktop machine with a card reader. It runs on the
desktop rather than on the device because many of the target handhelds have no Wi-Fi at all, and the
ones that do scrape slowly and drain the battery doing it.

## Commands

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev,images]"

pytest                                        # full suite, ~5s, no network
pytest tests/test_scraper.py -v               # one module
pytest tests/test_scraper.py::test_name       # one test
ruff check . && ruff format --check .
mypy pyskraper/
```

Install the `images` extra. Without Pillow, `tests/test_images.py` skips its entire module and the
suite still reports success while having tested none of the local resize path. CI installs
`.[dev,images]` for that reason.

## Layout

| Path | What lives there |
|---|---|
| `pyskraper/api/` | ScreenScraper HTTP client, error taxonomy, response parsing, quota, region/language selection |
| `pyskraper/core/` | Scanning, hashing, identification, media download, dedupe, verify, cache, journal, atomic writes |
| `pyskraper/output/` | The `Writer` protocol and the gamelist.xml implementation. The **only** place that knows a front-end's file format |
| `pyskraper/cli.py` | Typer app — `setup`, `scrape`, `systems`, `quota`, `doctor`, `hash`, `devices`, `dedupe`, `verify`, `cache` |
| `pyskraper/wizard.py` | Guided setup: sequential prompts, no TUI |
| `pyskraper/paths.py` | Base-dir resolution and platform mount roots. The only module allowed to know about platforms |

`CONTRIBUTING.md` has the full file-by-file tree.

## The safety model

This is the part to get right. The tool operates on removable media holding files a user may not be
able to replace, and there is no Trash on an SD card.

- **Never delete or overwrite a ROM file.** Metadata and media are regenerable and the tool rewrites
  them freely; ROMs are not. This applies to your own shell commands during development as much as
  to the code.
- **Nothing deletes anything unless asked twice.** `dedupe` reports and changes nothing by default;
  `--apply` quarantines (moves to a local folder on the computer, not the card); `--delete` requires
  `--apply` *and* an interactive confirmation; `--non-interactive --delete` is refused outright,
  with no override flag. Any new destructive capability takes this same shape.
- **`verify --clean-orphans` removes media and gamelist entries only.** It must never gain the
  ability to remove a ROM.
- **Atomic writes for anything touching the card**, via `core/atomic.py`: write `<target>.part`,
  `fsync`, then `os.replace`. The card can be pulled mid-write.
- **Quarantine moves are copy-verify-unlink, never `shutil.move`.** The card and the quarantine
  directory are different filesystems. The copy is fsynced and size-checked before the original is
  unlinked, so a failure at any point leaves the source intact.

## Architecture invariants

- **No front-end-specific conditionals outside `pyskraper/output/`.** If a change seems to need one,
  the `Writer` abstraction is wrong — fix the protocol. `Writer` (`output/base.py:25`) has five
  methods, not two: `plan_paths`/`write` are the scrape path, and `list_entries`/`media_index`/
  `remove_entries` are the hygiene path that `dedupe` and `verify` read the card back through. A new
  front-end implements all five.
- **Identification is hash-first.** Every lookup sends `crc32` + `md5` + `sha1` together, with no
  size cap. The chain is cache → hashes → disc serial → `romnom`+size → search
  (`core/identify.py:5`). Do not make filename matching primary and do not reintroduce a hashing
  size cap; both are deliberate, and both have been proposed as "simplifications" before.
- **All state lives under `paths.base_dir()`** — config, cache, journals, logs, quarantine. Never
  `~/.config`, `~/Library`, `~/.cache` or any other system location, so the whole folder can be
  copied to a USB stick and run from any machine. `tests/test_paths.py` and `tests/test_wizard.py`
  guard this; the wizard test runs with `$HOME` pointed at an empty directory and asserts it stays
  empty.
- **macOS and Linux are both first-class.** Platform knowledge belongs in `paths.py` and nowhere
  else. `candidate_mount_roots(platform)` takes the platform as an argument precisely so the Linux
  branch is testable from a Mac. Windows is not claimed.
- **Exit codes are a caller contract**, defined once in `exits.py` — 0 ok, 1 partial, 2 config,
  3 quota, 130 interrupted. Don't renumber them, and don't add one a caller can't act on.
- **Config resolution order**: CLI flag → `PYSKRAPER_*` env var → config file → device profile →
  built-in default. A device profile supplies defaults only; it never overrides an explicit value.

## API behaviour that was measured, not assumed

ScreenScraper's live behaviour contradicts its documentation in several places. These were
established against the real service and are expensive to rediscover.

- **Body markers are checked before status codes** in `api/errors.py`, and this is load-bearing
  rather than stylistic: bad credentials return **403**, which the status table maps to
  "blacklisted", and a bad *developer* password returns **HTTP 200** with a French error sentence in
  the body. `classify()` runs on every response before anything parses it. Never reduce it to a
  status-code lookup.
- **Never marker-scan a JSON body.** Game synopses are free text and will eventually contain an
  error phrase, turning a good response into a spurious failure. Marker scanning applies to
  non-JSON bodies only.
- **The KO quota is a separate, much smaller budget** (`maxrequestskoperday`) from the main daily
  request quota, and it is spent by lookups that match nothing. Track and report it distinctly, and
  cache misses so a re-run never re-burns it on the same unmatched ROM.
- **`softname` must be space-free.** Validated at config load, before any request, because a space
  in it silently corrupts the media URLs that come back.
- **Credentials are redacted in the log formatter, not at call sites**, so there is exactly one
  place to get it right. A test asserts no credential string appears in log output at any verbosity,
  including exception tracebacks.
- **Dedupe keep rules come in two kinds, and the distinction is load-bearing.** A *substantive* rule
  (`region:`, `verified`, `latest-revision`, `in-gamelist`, `good-dump`, `original`) may abstain,
  and only substantive rules can conflict. A *tiebreak* (`shortest-name`, `largest`) has an opinion
  about every pair and is consulted only after every substantive rule has abstained. Collapsing the
  two would flag an entire library as undecidable: `shortest-name` contradicts `latest-revision` on
  every group containing a revision, because `(Rev 1)` always makes the name longer. See
  `core/dedupe.py:220`.

## Testing and style

- **Unit tests never touch the network.** API interactions are `respx`-mocked. Anything hitting the
  live API is marked `integration` and deselected by `addopts` in `pyproject.toml`, which is what
  guarantees a bare `pytest` can never spend real quota. No test currently carries the mark; the
  machinery exists ahead of the first one, deliberately.
- **Never run the scraper against real credentials or a real SD card** unless explicitly asked. It
  spends quota and writes to removable media.
- `ruff` (line-length 120, `select = ["E","F","W","I","N","UP","B","A","C4","SIM","RUF"]`) and
  `mypy --strict` stay clean at all times. Both run in CI on Python 3.12 and 3.13.
- **No new dependency** without first checking whether `httpx`, `typer`, `rich`, `pydantic` or
  `pyyaml` already covers it.

## It is a script, not an application

The wizard is sequential prompts printed one after another; the scrape is a progress bar plus a
printed line when something notable happens. No full-screen layouts, no cursor-addressed panes, no
space-to-toggle grids. This is a deliberate design position, not an unfinished one — it works the
same over ssh and in a terminal that has been resized. A change that wants a TUI is a design
discussion to open first, not a detail to implement.
