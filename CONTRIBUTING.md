# Contributing to pySkraper

Thanks for considering a contribution! Here is everything you need to get a working development
environment and understand how to submit changes.

**The easiest useful contribution is adding a handheld** that isn't in the list yet. It is two short
entries, it needs no understanding of the scraper, and new devices ship faster than the list keeps
up with — see [DEVICES.md](DEVICES.md#adding-your-device).

---

## Environment Setup

pySkraper requires **Python 3.12 or later**. We recommend managing Python versions with [pyenv](https://github.com/pyenv/pyenv).

```bash
git clone https://github.com/Gnzlt/pySkraper.git
cd pySkraper

# Create and activate a virtualenv
python3 -m venv .venv
source .venv/bin/activate

# Install in editable mode with dev extras
pip install -e ".[dev,images]"
```

Install the `images` extra even though it is optional at runtime. Without Pillow,
`tests/test_images.py` skips its whole module -- ten tests covering the local resize path --
and reports success while having tested none of it. CI installs `.[dev,images]` for the
same reason.

To match the shipped runtime dependencies exactly instead:

```bash
pip install -e ".[dev]"
```

Always run through the venv rather than a bare `python3`:

```bash
source .venv/bin/activate     # or call .venv/bin/<tool> directly
```

---

## Project Structure

```
pySkraper/
├── README.md
├── DEVICES.md                     # supported handhelds + how to add one
├── CONTRIBUTING.md                # this file
├── AGENTS.md                      # orientation for AI coding agents
├── LICENSE
├── .github/workflows/ci.yml       # lint, format, types, tests on 3.12 and 3.13
├── pyproject.toml                 # deps, ruff, mypy, pytest config
├── .python-version                # Python series for pyenv (3.12)
├── .gitignore
├── .venv/                         # project virtualenv (git-ignored)
├── pyskraper.yaml                 # your config (git-ignored, created by the wizard)
├── data/                          # cache db, journals, log (git-ignored)
├── quarantine/                    # dedupe quarantine (git-ignored)
├── pyskraper/
│   ├── __init__.py
│   ├── __main__.py                # python -m pyskraper
│   ├── cli.py                     # Typer app: setup, scrape, systems, quota, doctor,
│   │                              #   hash, devices, dedupe, verify, cache
│   ├── wizard.py                  # guided setup: sequential prompts, no TUI
│   ├── banner.py                  # the splash logo, with a plain-line fallback
│   ├── theme.py                   # the logo's palette: rich styles + the sweep bar
│   ├── paths.py                   # base dir resolution + platform mount roots
│   ├── devices.py                 # device profiles (rg35xx-2024, …)
│   ├── detect.py                  # board name off the card -> profile
│   ├── config.py                  # YAML + env loading, validation
│   ├── exits.py                   # process exit codes (the CLI's contract)
│   ├── ui.py                      # shared byte/free-space formatting
│   ├── logging_setup.py           # console + rotating file log, redaction
│   ├── api/
│   │   ├── client.py              # async httpx client, auth, retry, backoff
│   │   ├── errors.py              # HTTP + French-message error taxonomy
│   │   ├── parser.py              # response payload -> metadata and media
│   │   ├── quota.py               # ssuser parsing, adaptive semaphore
│   │   └── selectors.py           # region/language fallback resolution
│   ├── core/
│   │   ├── scanner.py             # ROM tree walker, per-system extensions
│   │   ├── hasher.py              # streaming CRC32/MD5/SHA1, archive-aware, cached
│   │   ├── serials.py             # disc serial extraction (chd/cue/bin/iso)
│   │   ├── archives.py            # zip-aware hash candidates
│   │   ├── atomic.py              # every write to the card goes through here
│   │   ├── images.py              # local resizing (needs the images extra)
│   │   ├── journal.py             # resume state for interrupted runs
│   │   ├── models.py              # RomFile, ScrapeResult, MediaAsset
│   │   ├── identify.py            # hash → serial → filename → search strategy
│   │   ├── media.py               # asset planning + atomic downloads
│   │   ├── scraper.py             # orchestrator, resume journal
│   │   ├── naming.py              # filename cleaning + cataloguing tags
│   │   ├── dedupe.py              # duplicate detection, keep rules, quarantine
│   │   ├── verify.py              # drift and orphan detection
│   │   └── cache.py               # SQLite cache
│   ├── output/
│   │   ├── base.py                # Writer protocol
│   │   └── batocera.py            # KNULLI/Batocera gamelist.xml (the only writer)
│   └── systems.py                 # folder name <-> systemeid <-> extensions
└── tests/                         # one module per unit, all API calls mocked
```

---

## Running Tests

```bash
pytest -v                    # unit tests -- all API calls mocked with respx
```

The `integration` marker is registered and deselected by default via `addopts` in
`pyproject.toml`, which is what guarantees a plain `pytest` can never spend quota. **No test
currently carries the mark** -- the machinery exists ahead of the first live test, deliberately,
because that guarantee is worth having before one is written rather than after.

---

## Code Style

```bash
ruff check .
ruff format --check .
mypy pyskraper/
pytest
```

The project targets `ruff` with `line-length = 120` and `select = ["E","F","W","I","N","UP","B","A","C4","SIM","RUF"]`, plus `mypy --strict`. All four run in CI on every push and pull request
([`.github/workflows/ci.yml`](.github/workflows/ci.yml), Python 3.12 and 3.13); all commits should be clean.

---

## Key Design Rules

Please read these before submitting changes:

1. **Never delete or overwrite ROM files.** Metadata and media are regenerable; ROMs are not.
2. **Nothing deletes anything unless the user asked twice.** `dedupe` reports by default,
   quarantines before deleting, and refuses `--non-interactive --delete`. `verify --clean-orphans`
   removes media and metadata entries only, and must never gain the ability to remove a ROM.
3. **No front-end-specific conditionals outside `pyskraper/output/`.** If a change needs one, the
   `Writer` abstraction is wrong -- fix the protocol instead.
4. **Hash-first identification -- always.** Don't reintroduce a hashing size cap or make filename
   matching the primary path.
5. **All API interactions in tests are `respx`-mocked.** Anything touching the live API is marked
   `integration` and deselected by default. Never run a test suite against real ScreenScraper
   credentials or a real SD card.

---

## Submitting a Pull Request

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/my-feature`)
3. Commit your changes (`git commit -m 'Add my feature'`)
4. Push to the branch (`git push origin feature/my-feature`)
5. Open a Pull Request against `main`

Please ensure `ruff check`, `ruff format`, and `mypy pyskraper/` all pass, and that `pytest -v` is green, before opening a PR.

By contributing, you agree that your contributions are licensed under GPL-3.0-or-later, the same license as the project.
