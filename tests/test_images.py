"""Image sizing. On a 640x480 panel, oversized art costs space, bandwidth and
theme load time while being invisible."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest

from pyskraper.core.images import PILLOW_AVAILABLE, apply_server_resize, resize_if_needed

pytestmark = pytest.mark.skipif(not PILLOW_AVAILABLE, reason="Pillow not installed (optional [images] extra)")


def _image(path: Path, size: tuple[int, int], mode: str = "RGB") -> Path:
    from PIL import Image

    Image.new(mode, size, "red").save(path)
    return path


class TestServerSideHint:
    def test_adds_both_dimensions(self) -> None:
        url = apply_server_resize("https://media.screenscraper.fr/x.png?media=ss", 640, 480)
        params = parse_qs(urlparse(url).query)
        assert params["maxwidth"] == ["640"]
        assert params["maxheight"] == ["480"]
        assert params["media"] == ["ss"], "existing parameters must survive"

    def test_no_caps_leaves_the_url_alone(self) -> None:
        url = "https://media.screenscraper.fr/x.png"
        assert apply_server_resize(url, None, None) == url


class TestLocalResize:
    def test_oversized_image_is_shrunk_preserving_aspect(self, tmp_path: Path) -> None:
        from PIL import Image

        path = _image(tmp_path / "box.png", (1500, 1000))
        assert resize_if_needed(path, 640, 480)

        with Image.open(path) as result:
            assert result.size[0] <= 640 and result.size[1] <= 480
            assert abs(result.size[0] / result.size[1] - 1.5) < 0.02

    def test_image_within_the_cap_is_untouched(self, tmp_path: Path) -> None:
        """Re-encoding a small image loses quality for no benefit."""
        path = _image(tmp_path / "small.png", (320, 240))
        before = path.read_bytes()
        assert not resize_if_needed(path, 640, 480)
        assert path.read_bytes() == before

    def test_never_upscales(self, tmp_path: Path) -> None:
        from PIL import Image

        path = _image(tmp_path / "tiny.png", (100, 100))
        resize_if_needed(path, 640, 480)
        with Image.open(path) as result:
            assert result.size == (100, 100)

    def test_height_only_cap(self, tmp_path: Path) -> None:
        from PIL import Image

        path = _image(tmp_path / "tall.png", (400, 1200))
        assert resize_if_needed(path, None, 480)
        with Image.open(path) as result:
            assert result.size[1] == 480

    def test_format_conversion(self, tmp_path: Path) -> None:
        from PIL import Image

        path = _image(tmp_path / "box.png", (800, 600), mode="RGBA")
        assert resize_if_needed(path, 640, 480, convert_to="jpg")

        converted = tmp_path / "box.jpg"
        assert converted.exists()
        assert not path.exists(), "the original should not be left behind"
        with Image.open(converted) as result:
            assert result.mode == "RGB", "RGBA must be flattened for JPEG"

    def test_non_image_is_skipped(self, tmp_path: Path) -> None:
        pdf = tmp_path / "manual.pdf"
        pdf.write_bytes(b"%PDF-1.4 not really")
        assert not resize_if_needed(pdf, 640, 480)

    def test_corrupt_image_is_not_fatal(self, tmp_path: Path) -> None:
        broken = tmp_path / "broken.png"
        broken.write_bytes(b"not a png")
        assert not resize_if_needed(broken, 640, 480)

    def test_leaves_no_part_file(self, tmp_path: Path) -> None:
        path = _image(tmp_path / "box.png", (1500, 1000))
        resize_if_needed(path, 640, 480)
        assert not path.with_name(path.name + ".part").exists()

    def test_failed_save_leaves_no_part_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A save that raises must not strand a .part on the card.

        Nothing downstream can reclaim one: the scanner skips the .part suffix,
        and the writer's media index cannot map the name back to a ROM stem, so
        `verify --clean-orphans` never sees it either. On removable media it is
        litter with no command that finds it.
        """
        from PIL import Image

        path = _image(tmp_path / "box.png", (1500, 1000))

        def exploding_save(self: object, fp: Any, **kwargs: object) -> None:
            # Pillow opens the destination and starts encoding before it can
            # discover the disk is full, so a failure here has already put
            # bytes on the card. Simulate that, not a failure before the open.
            if hasattr(fp, "write"):
                fp.write(b"\x89PNG\r\n\x1a\n partial")
            else:
                Path(fp).write_bytes(b"\x89PNG\r\n\x1a\n partial")
            raise OSError("disk full")

        monkeypatch.setattr(Image.Image, "save", exploding_save)

        assert not resize_if_needed(path, 640, 480), "a failed resize reports failure"
        assert not path.with_name(path.name + ".part").exists()
        assert path.exists(), "the original must survive a failed resize"
