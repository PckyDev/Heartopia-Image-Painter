from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

from PIL import Image


RGB = Tuple[int, int, int]


@dataclass
class PixelGrid:
    w: int
    h: int
    pixels: List[RGB]  # row-major

    def get(self, x: int, y: int) -> RGB:
        return self.pixels[y * self.w + x]


def _dither_to_palette(img: Image.Image, palette: Sequence[RGB]) -> Image.Image:
    # Pillow expects exactly 256 colors * 3 channels in the palette table.
    pal_img = Image.new("P", (1, 1))
    pal_data: List[int] = []
    for (r, g, b) in palette:
        pal_data.extend([int(r), int(g), int(b)])
    # Pad remaining entries with black.
    pal_data.extend([0] * (768 - len(pal_data)))
    pal_img.putpalette(pal_data)

    q = img.quantize(
        palette=pal_img,
        dither=Image.Dither.FLOYDSTEINBERG,
    )
    return q.convert("RGB")


def load_and_resize_to_grid(
    path: str,
    w: int,
    h: int,
    *,
    dither: bool = False,
    palette: Optional[Sequence[RGB]] = None,
) -> PixelGrid:
    img = Image.open(path).convert("RGBA")

    # Composite alpha over white, so transparent areas become white.
    bg = Image.new("RGBA", img.size, (255, 255, 255, 255))
    img = Image.alpha_composite(bg, img)

    img = img.convert("RGB")
    img = img.resize((w, h), resample=Image.Resampling.LANCZOS)

    if dither and palette:
        unique_palette = list(dict.fromkeys((int(r), int(g), int(b)) for (r, g, b) in palette))
        # Pillow palette mode supports max 256 colors.
        if unique_palette:
            img = _dither_to_palette(img, unique_palette[:256])

    pixels = list(img.getdata())
    return PixelGrid(w=w, h=h, pixels=[(int(r), int(g), int(b)) for (r, g, b) in pixels])
