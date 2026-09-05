# Supported handhelds

**Usually you don't need this page.** KNULLI writes its board name — the model name of the handheld
— onto the card, so the wizard reads it and offers your handheld by name. You just press Enter. The
screen size is then used to keep artwork from being needlessly large, and that is the only thing
pySkraper uses the model for.

`pyskraper devices` prints this same list in the terminal.

| Brand | Models | Screen |
|---|---|---|
| Anbernic | RG35XX 2024, RG35XX Plus, RG35XX H, RG35XX SP, RG35XX Pro, RG35XX (original), RG28XX, RG40XX H, RG40XX V, RG Arc S | 640×480 |
| Anbernic | RG34XX, RG34XX SP | 720×480 |
| Anbernic | RGCubeXX | 720×720 |
| TrimUI | Brick | 1024×768 |
| TrimUI | Smart Pro, Smart Pro S | 1280×720 |
| Miyoo | Flip | 640×480 |
| Powkiddy | RGB30 | 720×720 |
| Powkiddy | X55 | 1280×720 |
| Powkiddy | V20, V90S | 640×480 |
| Powkiddy | A13 | 1024×600 |
| GoRetroid | Retroid Pocket 5, Retroid Pocket Flip 2 | 1920×1080 |
| GoRetroid | Retroid Pocket Mini, Retroid Pocket Mini V2 | 1280×960 |
| MagicX | XU Mini M | 640×480 |
| GKD | Pixel 2 | 640×480 |
| BatleXP | G350 | 640×480 |
| Unbranded | R36S | 640×480 |
| Unbranded | PS5000 | 960×544 |
| Unbranded | PS7000 | 1024×600 |
| Orange Pi | Zero 2w | HDMI out |

That is every board KNULLI currently ships an image for, which is a slightly wider list than the one
on [knulli.org](https://knulli.org/devices/) — deliberately, so that a card is never recognised as a
device pySkraper has no entry for.

**Something else?** Pick **Other / custom resolution** in the wizard and type your screen size —
`800x600`, say — or press Enter to download artwork at full size. Nothing else about the scrape
depends on knowing the device.

New handhelds appear constantly, and this list will fall behind. If yours is missing, adding it is a
few lines. Pull requests very welcome.

## Adding your device

This is the easiest useful contribution to make, and the one most likely to be needed: new handhelds
ship faster than any list keeps up with. You do not need to understand the scraper to do it.

A device is two entries. The first is in [`pyskraper/devices.py`](pyskraper/devices.py) — copy a
nearby one and change the values:

```python
"powkiddy-rgb20sx": DeviceProfile(
    id="powkiddy-rgb20sx",          # <brand>-<model>, lower case, no spaces
    name="RGB20SX",                 # model only — the brand is a heading above it
    vendor="Powkiddy",              # a new vendor becomes a new group in the wizard
    screen=(640, 480),              # the panel's real resolution
    notes="Rockchip RK3566, 1 GB RAM, 3.5in 640x480 4:3 IPS.",
    defaults=_screen(640, 480),     # same numbers as `screen`
),
```

The second is in [`pyskraper/detect.py`](pyskraper/detect.py), so a card can be recognised as your
device rather than just chosen from the list:

```python
"rgb20sx": ("powkiddy-rgb20sx",),   # KNULLI's board name -> your profile id
```

The board name is whatever is in `knulli.board` on the card — look in
`SHARE/system/configs/batteryplus/knulli.board`, or run `pyskraper doctor` with the card plugged in
and it will tell you. It is also the directory name under `board/<soc>/` in
[knulli-linux](https://github.com/knulli-cfw/knulli-linux/tree/knulli-main/board).

The wizard and `pyskraper devices` are both driven from these, and the list order on screen is the
order in the file.

Four things worth getting right:

- **Put it next to the same brand's other devices.** The entries for one vendor have to sit together
  in the dict; a test enforces it, because splitting them up makes the wizard list them in an order
  the source doesn't show.
- **The resolution should come from a spec sheet, not a guess.** A wrong number silently produces
  artwork that is the wrong size on someone else's card, which is the kind of bug nobody thinks to
  report. The vendor's product page is good; KNULLI's own build tree is better, because it is what
  the device actually runs. Many boards ship a full-screen `bootlogo.bmp` whose header carries the
  panel size, and the rest have panel timings in their `.dtb`. If both a spec sheet and KNULLI agree,
  you're done. Put the SoC and RAM in `notes` so the next person can check your work without hunting.
- **Watch for rotated panels.** A few devices have a physically portrait screen turned sideways in
  software — the RG28XX and the GKD Pixel 2 both report 480×640 in KNULLI's files but display
  640×480. Use the displayed geometry, since that is what the artwork has to fit.
- **Run `pytest`.** `tests/test_devices.py` and `tests/test_detect.py` check that `screen` and
  `defaults` agree, that the entry is reachable from the wizard, and that every device has a board
  name and vice versa. They pick up your device automatically — there is no test to add.

Please update the table on this page too, so the docs and the code agree.

Then open a pull request with a link to where you got the specs. If you own the device and can say
the artwork looks right on it, mention that too; it is worth more than the spec sheet.

See [CONTRIBUTING.md](CONTRIBUTING.md) for how to get a development environment set up.
