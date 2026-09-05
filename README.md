# pySkraper

<p align="center">
  <img src="logo.jpeg" alt="pySkraper logo" width="400">
</p>

<p align="center">
  <a href="https://github.com/Gnzlt/pySkraper/releases/latest"><img src="https://img.shields.io/github/v/release/Gnzlt/pySkraper?color=blue" alt="Latest release"></a>
  <img src="https://img.shields.io/badge/python-3.12%2B-blue.svg" alt="Python 3.12+">
  <img src="https://img.shields.io/badge/platform-macOS%20%7C%20Linux-lightgrey.svg" alt="Platform: macOS | Linux">
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-GPL--3.0--or--later-blue.svg" alt="License: GPL-3.0-or-later"></a>
</p>

> A lightweight, self-contained Python tool that downloads game metadata, box art, logos, screenshots, manuals, and gameplay videos from [ScreenScraper.fr](https://www.screenscraper.fr) and writes them straight into your handheld's SD card. Runs on macOS and Linux.

---

## What it does

pySkraper looks up each of your games on [ScreenScraper.fr](https://www.screenscraper.fr) — a big
community database of retro game information — downloads the artwork and descriptions, and writes
them onto the card in the format your handheld already understands.

You get box art (front, back and 3D), screenshots and title screens, logos, wheels and marquees,
cartridge and disc art, fan art, maps and bezels, plus titles, descriptions, genre, developer,
publisher, release date, number of players and rating, in your chosen language. Manuals and videos
too, if you ask for them.

You do this from your computer, in three steps:

1. Take the games card out of the handheld and put it in a card reader.
2. Run `pyskraper`.
3. Put the card back.

It is built for **KNULLI**, and works the same on Batocera, RetroBat and EmuELEC, which read the
same files.

**Why not use the scraper built into the handheld?** It is slower, drains the battery, and starts
over from scratch if it stops. Some handhelds have no Wi-Fi at all — the RG35XX 2024 and RG28XX
among them — so there it either can't run, or wants the single USB-C port for a dongle instead of
power.

## Will it work with my handheld?

Almost certainly. KNULLI writes the model name onto the card, so pySkraper reads it and offers your
handheld by name — you press Enter. It uses the screen size to avoid downloading artwork far bigger
than your screen, and that is the only thing it needs the model for.

If your handheld isn't recognised, pick **Other / custom resolution** and type your screen size,
such as `800x600`. Nothing else changes.

**[See the full list of supported handhelds →](DEVICES.md)**

## What you'll need

- **A ScreenScraper account and developer credentials.** Both free — see below.
- **An SD card reader**, and the handheld's games card.
- **Python 3.12 or later** on a Mac or Linux machine. (Windows isn't tested.)
- An internet connection.

### Getting your ScreenScraper credentials

You need **two** sets of credentials, and both are free:

1. **Developer credentials** (`devid` + `devpassword`) identify the *software*. Request them on the
   [ScreenScraper forum](https://www.screenscraper.fr/forumsujets.php?frub=12&numpage=0), saying
   what you'll use them for. Without these, nothing works at all.
2. **A member account** (`ssid` + `sspassword`) identifies *you*. It sets how fast you can download
   and how many games you can look up per day. Register free at
   [screenscraper.fr](https://www.screenscraper.fr/membreinscription.php).

A plain registered account is typically good for around 20,000 lookups a day, which is plenty for a
large collection.

> [!WARNING]
> Don't create extra accounts to get around the daily limit. It gets your IP permanently banned, and
> it gets pySkraper blocked for everyone else using it.

## Installing

Open a terminal and run:

```bash
git clone https://github.com/Gnzlt/pySkraper.git
cd pySkraper

python3 -m venv .venv
source .venv/bin/activate

pip install -e .
```

Check it worked:

```bash
pyskraper --version
```

Every time you come back to use it, `cd` into the folder and run `source .venv/bin/activate` again
first.

Optionally, `pip install -e ".[images]"` adds the ability to shrink oversized artwork for small
screens.

## Using it

Plug in the card reader and run:

```bash
pyskraper
```

That's it. pySkraper asks you seven short questions — everything has a sensible default, so you can
usually just press Enter — and then starts working:

```
3  Device
   Detected from the card: Anbernic RG35XX 2024 — 640x480
   Same KNULLI image as the Anbernic RG35XX Plus, and the same screen.

   Use this device? [y/n] (y):

...

snes — 1,510 ROMs
  - Rom Hack v1.2                          no match
Scraping  snes: Super Metroid [md5] ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ 1204/1510  612 req  0:06:12 eta 0:01:34
```

**Nothing is written to your card until you answer the last question**, so you can back out at any
point with `Ctrl-C`.

Your answers are saved. From then on, you can just run:

```bash
pyskraper scrape
```

<details>
<summary><b>See the full walkthrough</b></summary>

```
             ████ █
████  █   █ █     █  ██ █ ███  ████ ████   ███  █ ███
█   █ █   █  ███  █ ██  ██        █ █   █ █████ ██
█   █ █   █     █ ███   █     █   █ █   █ █     █
████   ████ ████  █  ██ █      ████ ████   ███  █
█     ████                          █

ScreenScraper for KNULLI/Batocera handhelds · v1.0.0
Ctrl-C at any point exits without saving.

1  ScreenScraper credentials
   Developer ID: yourname
   Developer password:
   Do you have a ScreenScraper member account? [y/n] (n): y
   Username (yourname):
   Password:
   Checking with ScreenScraper...
   Verified as yourname — 2 thread(s)
   19,412 of 20,000 requests left today

2  ROM location
   Found 2 possible location(s):

   > 1  /Volumes/SHARE/roms
        24 known system(s) · 119 unrecognised · boot partition KNULLI
     2  /Volumes/Backup/roms
        1 known system(s)
     3  Type a path instead

   Which one? (1):

3  Device
   Detected from the card: Anbernic RG35XX 2024 — 640x480
   Same KNULLI image as the Anbernic RG35XX Plus, and the same screen.

   Use this device? [y/n] (y):
   Allwinner H700, 1 GB RAM, 3.5in 640x480 4:3 IPS, no built-in Wi-Fi.

4  Systems
   24 system(s) with ROMs

        System         ROMs
   x  1 gb            1,040
   x  2 gba           3,144
   x  3 nes           2,083
   x  4 snes            305
        ...

   Enter to accept · numbers to toggle (e.g. 1,4,7) · a all · n none
   Toggle ():

5  Media
   > 1  Recommended (boxart, image, marquee, thumbnail, titleshot, wheel)
     2  Everything (every media type ScreenScraper offers)
     3  Custom

   Which? (1):

6  Language and region
   Preferred language (en):
   Preferred region (us):

7  Ready to scrape
   Card         /Volumes/SHARE/roms
   Systems      21 selected
   ROMs         9,752
   Free space   18.3 GB

   Enter save and start scraping · s save and exit · q quit without saving
```

</details>

### A few things worth knowing

- **It's safe to interrupt.** Progress is saved as it goes. Run it again and it picks up where it
  stopped rather than starting over.
- **It won't wipe your data.** Favourites, play counts and play times already on the card are kept.
- **Running it twice is cheap.** Results are saved on your computer, so a second run over an
  unchanged collection costs nothing.
- **Videos and manuals are off by default.** They're enormous — a full collection with videos can
  run to tens of gigabytes and take days. Turn them on at the "Media" question if you want them.
- **Games it can't identify are listed as `no match`.** Everything else stays quiet.

## Where things end up

Artwork goes into folders inside each system's ROM folder, next to a `gamelist.xml` that your
handheld reads:

```
/Volumes/SHARE/roms/snes/
├── gamelist.xml
├── Super Mario World.sfc
├── images/
│   ├── Super Mario World-image.png
│   ├── Super Mario World-thumb.png
│   └── Super Mario World-marquee.png
├── videos/
│   └── Super Mario World-video.mp4
└── manuals/
    └── Super Mario World-manual.pdf
```

The paths above are macOS. On Linux the card mounts under `/run/media/<you>/SHARE/roms` or
`/media/<you>/SHARE/roms`. pySkraper finds it either way, and `--roms` takes it directly:

```bash
pyskraper scrape --roms /run/media/$USER/SHARE/roms
```

pySkraper keeps its own files — your saved answers in `pyskraper.yaml`, plus `data/` — inside its
own folder. Nothing is installed elsewhere on your system, so you can
copy the whole folder to a USB stick and run it from any machine the card reader is plugged into.
To delete it, delete the folder.

> [!IMPORTANT]
> `pyskraper.yaml` contains your ScreenScraper passwords. It's readable only by you and is excluded
> from git, but don't share or upload that file. You can keep passwords out of it entirely by
> setting the `PYSKRAPER_DEVPASSWORD` and `PYSKRAPER_SSPASSWORD` environment variables instead.

## Commands

Run these after `source .venv/bin/activate`:

| Command | What it does |
|---|---|
| `pyskraper` | Guided setup, then scrape |
| `pyskraper scrape` | Scrape using your saved answers |
| `pyskraper scrape --system snes` | Just one system |
| `pyskraper scrape --dry-run` | Show what *would* happen, download nothing |
| `pyskraper doctor` | Check credentials, card and free space |
| `pyskraper verify` | Check for missing games or leftover artwork |
| `pyskraper dedupe` | Find duplicate games (only reports; changes nothing) |
| `pyskraper --help` | Everything else |

Two commands can change files, and both do nothing until you add `--apply`:

- `pyskraper dedupe --apply` deletes duplicate games. It first shows you exactly what would go and
  how much space it frees, then asks you to type the word `delete` — anything else cancels. Every
  deleted path is written to a journal in `data/dedupe/` so you can see afterwards what went.
- `pyskraper verify --clean-orphans --apply` removes artwork for games no longer on the card. It
  asks first, and never touches a game file — everything it removes comes back from a re-scrape.

## If something goes wrong

- **Start with `pyskraper doctor`.** It checks your credentials, finds the card, and reports free
  space — most problems show up there.
- **A few `no match` games is normal.** Hacks, homebrew and unusual regions often aren't in the
  database. `pyskraper hash <file>` shows exactly what was sent for that game, and is the output
  worth pasting into a bug report.
- **If a run stops**, for any reason, just run it again. It resumes.
- **Anything else:** [open an issue](https://github.com/Gnzlt/pySkraper/issues).

## Contributing

Contributions are welcome. Adding a handheld that isn't on the list yet is the easiest useful
change to make, and needs no understanding of the scraper — see
**[DEVICES.md](DEVICES.md#adding-your-device)**. For everything else, see
**[CONTRIBUTING.md](CONTRIBUTING.md)**.

## License

Copyright (C) 2026 Gnzlt. pySkraper is licensed under the **GNU General Public License v3.0 or
later** — see [`LICENSE`](LICENSE) for the full text.

In short: use it, modify it, share it, for any purpose. If you distribute a modified version,
it has to stay open under the same license — so this work, and anything built on top of it,
stays available to everyone. That is the whole point of the choice.

## Acknowledgements

- [ScreenScraper.fr](https://www.screenscraper.fr) — for maintaining an extraordinary community database of retro game metadata and media
- [KNULLI](https://knulli.org) and [Batocera](https://batocera.org) — for the firmware, and for `gamelist.xml` semantics that are actually documented in source
- [Skyscraper](https://github.com/muldjord/skyscraper) — for showing what a well-architected scraper looks like
- The retro gaming community ❤️

---

> **Disclaimer**: This project is not affiliated with or endorsed by ScreenScraper.fr, KNULLI, or Batocera. Please respect ScreenScraper's Terms of Service and API usage policies. If you find the service valuable, [support it](https://www.tipeee.com/screenscraper) — the database is community-funded, and scrapers like this one are pure consumption.
