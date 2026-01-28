from __future__ import annotations

from typing import Tuple

import mss


def get_screen_pixel_rgb(x: int, y: int) -> Tuple[int, int, int]:
    """Fast 1x1 pixel sample from the screen at absolute coordinates."""
    with mss.mss() as sct:
        monitor = {"left": x, "top": y, "width": 1, "height": 1}
        img = sct.grab(monitor)
        # mss commonly returns BGRA, but some backends can return BGR.
        px = img.pixel(0, 0)
        if len(px) == 4:
            b, g, r, _a = px
        elif len(px) == 3:
            b, g, r = px
        else:
            raise ValueError(f"Unexpected pixel format length: {len(px)}")
        return (int(r), int(g), int(b))
