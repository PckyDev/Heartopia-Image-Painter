from __future__ import annotations

from typing import Tuple

import mss


def get_screen_pixel_rgb(x: int, y: int) -> Tuple[int, int, int]:
    """Fast 1x1 pixel sample from the screen at absolute coordinates."""
    with mss.mss() as sct:
        monitor = {"left": x, "top": y, "width": 1, "height": 1}
        img = sct.grab(monitor)
        # mss returns BGRA
        b, g, r, _a = img.pixel(0, 0)
        return (r, g, b)
