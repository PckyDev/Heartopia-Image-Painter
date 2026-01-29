from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Optional, Tuple

import pyautogui

from .config import AppConfig, MainColor, ShadeButton


Point = Tuple[int, int]
RGB = Tuple[int, int, int]


@dataclass
class PainterOptions:
    move_duration_s: float = 0.03
    mouse_down_s: float = 0.02
    after_click_delay_s: float = 0.06
    panel_open_delay_s: float = 0.12
    shade_select_delay_s: float = 0.06
    row_delay_s: float = 0.10


def _tap(pos: Point, opts: PainterOptions, extra_delay_s: float = 0.0):
    # Move + mouseDown/mouseUp is more reliable for some games than pyautogui.click().
    pyautogui.moveTo(pos[0], pos[1], duration=max(0.0, float(opts.move_duration_s)))
    pyautogui.mouseDown()
    time.sleep(max(0.0, float(opts.mouse_down_s)))
    pyautogui.mouseUp()
    time.sleep(max(0.0, float(opts.after_click_delay_s) + float(extra_delay_s)))


def _find_best_match(rgb: RGB, cfg: AppConfig) -> Optional[Tuple[MainColor, ShadeButton]]:
    # Naive: choose closest shade across all colors.
    # Later: speed up with caching / KD-tree.
    best = None
    best_dist = None

    def dist2(a: RGB, b: RGB) -> int:
        return (a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2

    for mc in cfg.main_colors:
        for sh in mc.shades:
            d = dist2(rgb, sh.rgb)
            if best_dist is None or d < best_dist:
                best_dist = d
                best = (mc, sh)
    return best


def paint_grid(
    cfg: AppConfig,
    canvas_rect: Tuple[int, int, int, int],
    grid_w: int,
    grid_h: int,
    get_pixel: Callable[[int, int], RGB],
    options: Optional[PainterOptions] = None,
    progress_cb: Optional[Callable[[int, int], None]] = None,
    should_stop: Optional[Callable[[], bool]] = None,
) -> None:
    """Paints a WxH pixel grid into a canvas rectangle.

    This assumes the game's canvas pixels map evenly into the selected rectangle.
    The actual mapping may need per-game tweaking; this is the first-pass.
    """

    if options is None:
        options = PainterOptions()

    x0, y0, w, h = canvas_rect
    if grid_w <= 0 or grid_h <= 0:
        return

    if not cfg.main_colors or cfg.shades_panel_button_pos is None or cfg.back_button_pos is None:
        raise RuntimeError("Color configuration incomplete. Set up colors and global buttons first.")

    # Compute cell centers
    cell_w = w / grid_w
    cell_h = h / grid_h

    pyautogui.PAUSE = 0
    pyautogui.FAILSAFE = True  # moving mouse to top-left aborts

    last_main: Optional[MainColor] = None
    last_shade: Optional[ShadeButton] = None
    in_shades_panel = False

    for y in range(grid_h):
        for x in range(grid_w):
            if should_stop and should_stop():
                return

            rgb = get_pixel(x, y)
            match = _find_best_match(rgb, cfg)
            if match is None:
                continue
            main, shade = match

            # Select main color if changed
            if last_main is None or main.name != last_main.name:
                # Ensure we're on the main palette before selecting a new main color.
                if in_shades_panel:
                    _tap(cfg.back_button_pos, options)
                    in_shades_panel = False

                _tap(main.pos, options)
                # Open shades panel
                _tap(cfg.shades_panel_button_pos, options, extra_delay_s=options.panel_open_delay_s)
                in_shades_panel = True
                last_main = main
                last_shade = None

            # If something put us back on main palette, re-open shades panel.
            if not in_shades_panel:
                _tap(cfg.shades_panel_button_pos, options, extra_delay_s=options.panel_open_delay_s)
                in_shades_panel = True

            # Select shade
            if last_shade is None or shade.pos != last_shade.pos:
                _tap(shade.pos, options, extra_delay_s=options.shade_select_delay_s)
                last_shade = shade

            # Paint cell
            cx = int(x0 + (x + 0.5) * cell_w)
            cy = int(y0 + (y + 0.5) * cell_h)
            _tap((cx, cy), options)

            if progress_cb:
                progress_cb(x, y)

        if options.row_delay_s > 0:
            time.sleep(options.row_delay_s)

    # Leave the game UI in a predictable state.
    if in_shades_panel:
        _tap(cfg.back_button_pos, options)
