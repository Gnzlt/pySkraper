"""Image sizing for small screens.

The RG35XX's panel is 640x480. ScreenScraper routinely serves box art at 1500px
or wider, and on a 3.5" screen every one of those extra pixels is invisible --
it only costs SD space, download allowance, and theme load time on a device with
1 GB of RAM. Capping typically cuts media size by 70-85%.

Two mechanisms, in order of preference:

1. **Server-side**: ask ScreenScraper to send it already resized, which saves
   the bandwidth as well as the space.
2. **Local**: resize after download with Pillow.

Pillow is an optional extra, so local resizing degrades to "leave it alone"
rather than failing. And because server-side resizing is documented but not
something we can verify without credentials, the local pass runs regardless and
simply finds nothing to do when the server already obliged.
"""

from __future__ import annotations

import logging
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from .atomic import atomic_binary

__all__ = ["PILLOW_AVAILABLE", "apply_server_resize", "resize_if_needed"]

log = logging.getLogger(__name__)

try:  # pragma: no cover - exercised by whichever extras are installed
    from PIL import Image

    PILLOW_AVAILABLE = True
except ImportError:  # pragma: no cover
    PILLOW_AVAILABLE = False

# Formats worth rewriting. Video and PDFs are passed through untouched.
_RESIZABLE = frozenset({".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"})


def apply_server_resize(url: str, max_width: int | None, max_height: int | None) -> str:
    """Add ``maxwidth``/``maxheight`` to a media URL.

    Harmless if the server ignores them: an unknown query parameter costs one
    URL's worth of bytes and changes nothing else.
    """
    if not max_width and not max_height:
        return url

    parsed = urlparse(url)
    params = dict(parse_qsl(parsed.query, keep_blank_values=True))
    if max_width:
        params["maxwidth"] = str(max_width)
    if max_height:
        params["maxheight"] = str(max_height)
    return urlunparse(parsed._replace(query=urlencode(params)))


def resize_if_needed(
    path: Path,
    max_width: int | None,
    max_height: int | None,
    *,
    convert_to: str | None = None,
) -> bool:
    """Shrink an image in place if it exceeds the cap. Returns True if rewritten.

    Aspect ratio is always preserved, and images already within the cap are left
    completely untouched -- re-encoding them would lose quality for no benefit.
    """
    if not PILLOW_AVAILABLE:
        return False
    if path.suffix.lower() not in _RESIZABLE:
        return False
    if not max_width and not max_height:
        return False

    try:
        with Image.open(path) as image:
            width, height = image.size
            target = _fit(width, height, max_width, max_height)
            if target == (width, height) and not convert_to:
                return False

            resized = image.resize(target, Image.Resampling.LANCZOS) if target != (width, height) else image.copy()

            destination = path
            if convert_to:
                destination = path.with_suffix(f".{convert_to.lstrip('.')}")
            if destination.suffix.lower() in (".jpg", ".jpeg") and resized.mode in ("RGBA", "P", "LA"):
                resized = resized.convert("RGB")

            # Write beside the target then replace, so an interrupted resize
            # cannot leave a truncated image on the card. atomic_binary unlinks
            # its part file on any exception -- doing this by hand left the
            # part behind on a failed save, where nothing could ever reclaim it:
            # the scanner skips the .part suffix and the writer's media index
            # cannot map one back to a ROM stem.
            #
            # The temp name has no usable extension, so the format has to be
            # stated explicitly.
            fmt = Image.registered_extensions().get(destination.suffix.lower())
            with atomic_binary(destination) as handle:
                resized.save(handle, format=fmt)

        if destination != path:
            path.unlink(missing_ok=True)
    except (OSError, ValueError) as exc:
        log.debug("Could not resize %s: %s", path.name, exc)
        return False

    return True


def _fit(width: int, height: int, max_width: int | None, max_height: int | None) -> tuple[int, int]:
    """Largest size within the caps that keeps the aspect ratio. Never upscales."""
    scale = 1.0
    if max_width and width > max_width:
        scale = min(scale, max_width / width)
    if max_height and height > max_height:
        scale = min(scale, max_height / height)
    if scale >= 1.0:
        return width, height
    return max(1, int(width * scale)), max(1, int(height * scale))
