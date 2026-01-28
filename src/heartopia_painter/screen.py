from __future__ import annotations

from typing import Tuple

import mss


def get_screen_pixel_rgb(x: int, y: int) -> Tuple[int, int, int]:
    """Fast 1x1 pixel sample from the screen at absolute coordinates."""
    with mss.mss() as sct:
        monitor = {"left": x, "top": y, "width": 1, "height": 1}
        img = sct.grab(monitor)
        # Use the raw RGB bytes from mss to avoid backend-dependent channel order.
        # For a 1x1 grab, this is exactly 3 bytes: R, G, B.
        rgb_bytes = getattr(img, "rgb", None)
        if rgb_bytes is not None and len(rgb_bytes) >= 3:
            r = rgb_bytes[0]
            g = rgb_bytes[1]
            b = rgb_bytes[2]
            return (int(r), int(g), int(b))

        # Fallback: try pixel(), assuming BGRA/BGR ordering.
        px = img.pixel(0, 0)
        if len(px) == 4:
            b, g, r, _a = px
            return (int(r), int(g), int(b))
        if len(px) == 3:
            b, g, r = px
            return (int(r), int(g), int(b))
        raise ValueError(f"Unexpected pixel format length: {len(px)}")
