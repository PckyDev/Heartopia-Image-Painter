from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple

import pyautogui

from .config import AppConfig, MainColor, ShadeButton
from .screen import get_screen_pixel_rgb


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

    enable_drag_strokes: bool = False
    drag_step_duration_s: float = 0.01
    after_drag_delay_s: float = 0.02


def _tap(pos: Point, opts: PainterOptions, extra_delay_s: float = 0.0):
    # Move + mouseDown/mouseUp is more reliable for some games than pyautogui.click().
    pyautogui.moveTo(pos[0], pos[1], duration=max(0.0, float(opts.move_duration_s)))
    pyautogui.mouseDown(button="left")
    time.sleep(max(0.0, float(opts.mouse_down_s)))
    pyautogui.mouseUp(button="left")
    time.sleep(max(0.0, float(opts.after_click_delay_s) + float(extra_delay_s)))


def _stroke(points: List[Point], opts: PainterOptions, should_stop: Optional[Callable[[], bool]] = None) -> None:
    if not points:
        return
    # Some games respond better to a lower-level mouse controller than PyAutoGUI.
    try:
        from pynput.mouse import Button, Controller  # type: ignore

        mouse = Controller()
        mouse.position = points[0]
        mouse.press(Button.left)
        time.sleep(max(0.0, float(opts.mouse_down_s)))

        step = max(0.0, float(opts.drag_step_duration_s))
        substeps_per_cell = 6

        for target in points[1:]:
            if should_stop and should_stop():
                break
            x0, y0 = mouse.position
            x1, y1 = target
            dx = x1 - x0
            dy = y1 - y0

            # Interpolate a few micro-moves per cell so the game receives
            # continuous mouse-move events while the button is held.
            n = max(1, int(substeps_per_cell))
            for i in range(1, n + 1):
                if should_stop and should_stop():
                    break
                mx = int(round(x0 + dx * (i / n)))
                my = int(round(y0 + dy * (i / n)))
                mouse.position = (mx, my)
                if step > 0:
                    time.sleep(step / n)

        mouse.release(Button.left)
        time.sleep(max(0.0, float(opts.after_drag_delay_s)))
        return
    except Exception:
        # Fallback: PyAutoGUI drag
        pass

    pyautogui.moveTo(points[0][0], points[0][1], duration=max(0.0, float(opts.move_duration_s)))
    pyautogui.mouseDown(button="left")
    time.sleep(max(0.0, float(opts.mouse_down_s)))
    try:
        step = max(0.0, float(opts.drag_step_duration_s))
        substeps_per_cell = 6
        curx, cury = points[0]
        for px, py in points[1:]:
            if should_stop and should_stop():
                return
            dx = px - curx
            dy = py - cury
            n = max(1, int(substeps_per_cell))
            for i in range(1, n + 1):
                if should_stop and should_stop():
                    return
                mx = int(round(curx + dx * (i / n)))
                my = int(round(cury + dy * (i / n)))
                pyautogui.moveTo(mx, my, duration=0)
                if step > 0:
                    time.sleep(step / n)
            curx, cury = px, py
    finally:
        pyautogui.mouseUp(button="left")
    time.sleep(max(0.0, float(opts.after_drag_delay_s)))


def _rapid_click_stroke(
    points: List[Point],
    opts: PainterOptions,
    should_stop: Optional[Callable[[], bool]] = None,
) -> None:
    """Fast, reliable stroke: click every point in a run with reduced delays.

    This is a fallback when true drag-painting doesn't register in-game.
    We reuse the drag timing knobs as stroke timing:
    - drag_step_duration_s: delay between clicks within the stroke
    - after_drag_delay_s: delay after the stroke finishes
    """

    if not points:
        return

    per_click_delay = max(0.0, float(opts.drag_step_duration_s))
    after_stroke_delay = max(0.0, float(opts.after_drag_delay_s))

    for (px, py) in points:
        if should_stop and should_stop():
            return
        # Move as fast as possible; rely on per-click delay for stability.
        pyautogui.moveTo(px, py, duration=0)
        pyautogui.mouseDown(button="left")
        if opts.mouse_down_s > 0:
            time.sleep(max(0.0, float(opts.mouse_down_s)))
        pyautogui.mouseUp(button="left")
        if per_click_delay > 0:
            time.sleep(per_click_delay)

    if after_stroke_delay > 0:
        time.sleep(after_stroke_delay)


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


def _dist2(a: RGB, b: RGB) -> int:
    return (a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2


def _ui_sanity_check_at(
    pos: Point,
    expected_rgb: RGB,
    tol: int,
) -> bool:
    """Return True if the screen pixel at pos is close to expected_rgb.

    Used to detect when the game window/UI has moved (captured button coords no
    longer line up), which otherwise causes endless repaint attempts.
    """

    try:
        actual = get_screen_pixel_rgb(int(pos[0]), int(pos[1]))
    except Exception:
        return False
    tol2 = max(0, int(tol)) ** 2
    return _dist2(actual, expected_rgb) <= tol2


def _cell_center(canvas_rect: Tuple[int, int, int, int], grid_w: int, grid_h: int, x: int, y: int) -> Point:
    x0, y0, w, h = canvas_rect
    cell_w = w / grid_w
    cell_h = h / grid_h
    cx = int(x0 + (x + 0.5) * cell_w)
    cy = int(y0 + (y + 0.5) * cell_h)
    return (cx, cy)


def _select_shade(
    cfg: AppConfig,
    options: PainterOptions,
    main: MainColor,
    shade: ShadeButton,
    last_main: Optional[MainColor],
    last_shade: Optional[ShadeButton],
    in_shades_panel: bool,
) -> Tuple[Optional[MainColor], Optional[ShadeButton], bool]:
    if cfg.shades_panel_button_pos is None or cfg.back_button_pos is None:
        raise RuntimeError("Color configuration incomplete. Set shades panel + back button positions first.")

    # Sanity tolerance for UI sampling (button pixel colors can vary slightly).
    ui_tol = max(60, int(getattr(cfg, "verify_tolerance", 35)))

    # Select main if needed
    if last_main is None or main.name != last_main.name:
        # Defensive: when switching main colors, ALWAYS try to return to the main
        # palette first. If a previous Back click failed to register, our
        # in_shades_panel flag may be False while the UI is still in the shades
        # panel (which causes main-color clicks to hit the wrong UI).
        if last_main is not None:
            _tap(cfg.back_button_pos, options)
            in_shades_panel = False
        elif in_shades_panel:
            _tap(cfg.back_button_pos, options)
            in_shades_panel = False
        _tap(main.pos, options)
        _tap(cfg.shades_panel_button_pos, options, extra_delay_s=options.panel_open_delay_s)
        in_shades_panel = True
        last_main = main
        last_shade = None

    if not in_shades_panel:
        _tap(cfg.shades_panel_button_pos, options, extra_delay_s=options.panel_open_delay_s)
        in_shades_panel = True

    # NOTE: We intentionally do NOT hard-fail based on sampling shade.pos.
    # Shade button pixels can vary due to hover/selection highlights and UI
    # effects, which can produce false positives even when the window is aligned.
    # We'll rely on repaint verification to correct missed clicks.

    if last_shade is None or shade.pos != last_shade.pos:
        _tap(shade.pos, options, extra_delay_s=options.shade_select_delay_s)
        # Extra tap helps when the first click doesn't register.
        _tap(shade.pos, options, extra_delay_s=0.0)
        last_shade = shade

    return last_main, last_shade, in_shades_panel


def _bucket_fill_canvas_with_shade(
    cfg: AppConfig,
    canvas_rect: Tuple[int, int, int, int],
    grid_w: int,
    grid_h: int,
    main: MainColor,
    shade: ShadeButton,
    options: PainterOptions,
    should_stop: Optional[Callable[[], bool]] = None,
) -> None:
    """Bucket fill the entire canvas with the given shade.

    Requires captured tool buttons: paint tool + bucket tool.
    """

    if cfg.paint_tool_button_pos is None or cfg.bucket_tool_button_pos is None:
        raise RuntimeError(
            "Bucket fill is enabled but tool button positions are not set. "
            "Capture 'paint tool button' and 'bucket tool button' first."
        )

    # Ensure we're in a consistent UI state while picking the shade.
    _tap(cfg.paint_tool_button_pos, options)

    last_main: Optional[MainColor] = None
    last_shade: Optional[ShadeButton] = None
    in_shades_panel = False
    last_main, last_shade, in_shades_panel = _select_shade(
        cfg=cfg,
        options=options,
        main=main,
        shade=shade,
        last_main=last_main,
        last_shade=last_shade,
        in_shades_panel=in_shades_panel,
    )
    if in_shades_panel:
        _tap(cfg.back_button_pos, options)

    if should_stop and should_stop():
        return

    # Switch to bucket tool and fill inside the canvas.
    _tap(cfg.bucket_tool_button_pos, options)

    x0, y0, w, h = canvas_rect
    # Click near the center of the canvas to fill it.
    fill_pt = (int(x0 + w * 0.5), int(y0 + h * 0.5))
    _tap(fill_pt, options)

    # Switch back to paint tool so subsequent pixel painting works as expected.
    _tap(cfg.paint_tool_button_pos, options)

    # Tiny settle helps some games register the fill.
    settle_s = max(0.0, float(getattr(cfg, "verify_settle_s", 0.05)))
    if settle_s > 0:
        time.sleep(settle_s)


def _paint_coord_runs(
    cfg: AppConfig,
    canvas_rect: Tuple[int, int, int, int],
    grid_w: int,
    grid_h: int,
    coords: List[Tuple[int, int]],
    options: PainterOptions,
    progress_cb: Optional[Callable[[int, int], None]] = None,
    should_stop: Optional[Callable[[], bool]] = None,
) -> None:
    """Paint an arbitrary set of coords (assumes correct shade already selected)."""

    if not coords:
        return

    x0, y0, w, h = canvas_rect
    cell_w = w / grid_w
    cell_h = h / grid_h

    coords.sort(key=lambda xy: (xy[1], xy[0]))
    i = 0
    while i < len(coords):
        if should_stop and should_stop():
            return
        x, y = coords[i]
        run = [(x, y)]
        j = i + 1
        while j < len(coords):
            nx, ny = coords[j]
            if ny != y or nx != run[-1][0] + 1:
                break
            run.append((nx, ny))
            j += 1

        pts: List[Point] = []
        for rx, ry in run:
            cx = int(x0 + (rx + 0.5) * cell_w)
            cy = int(y0 + (ry + 0.5) * cell_h)
            pts.append((cx, cy))

        if options.enable_drag_strokes and len(pts) >= 2:
            _rapid_click_stroke(pts, options, should_stop=should_stop)
        else:
            for p in pts:
                if should_stop and should_stop():
                    return
                _tap(p, options)

        if progress_cb:
            for rx, ry in run:
                progress_cb(rx, ry)

        i = j


def _verify_and_repair_row(
    cfg: AppConfig,
    canvas_rect: Tuple[int, int, int, int],
    grid_w: int,
    grid_h: int,
    y: int,
    row_expected: List[Optional[Tuple[MainColor, ShadeButton]]],
    options: PainterOptions,
    progress_cb: Optional[Callable[[int, int], None]] = None,
    should_stop: Optional[Callable[[], bool]] = None,
) -> None:
    if not bool(getattr(cfg, "verify_rows", True)):
        return

    tol = int(getattr(cfg, "verify_tolerance", 35))
    tol2 = max(0, tol) ** 2
    max_passes = max(1, int(getattr(cfg, "verify_max_passes", 10)))
    settle_s = max(0.0, float(getattr(cfg, "verify_settle_s", 0.05)))

    for _pass in range(max_passes):
        if should_stop and should_stop():
            return
        if settle_s > 0:
            time.sleep(settle_s)

        # Collect mismatches grouped by shade
        groups: Dict[Tuple[str, Point], Tuple[MainColor, ShadeButton, List[int]]] = {}
        for x in range(grid_w):
            exp = row_expected[x] if x < len(row_expected) else None
            if exp is None:
                continue
            main, shade = exp

            cx, cy = _cell_center(canvas_rect, grid_w, grid_h, x, y)
            actual = get_screen_pixel_rgb(cx, cy)
            if _dist2(actual, shade.rgb) <= tol2:
                continue
            key = (main.name, shade.pos)
            if key not in groups:
                groups[key] = (main, shade, [])
            groups[key][2].append(x)

        if not groups:
            return

        # Repaint mismatches, minimizing palette switches.
        last_main: Optional[MainColor] = None
        last_shade: Optional[ShadeButton] = None
        in_shades_panel = False

        ordered = sorted(groups.values(), key=lambda t: (-len(t[2]), t[0].name, t[1].pos[0], t[1].pos[1]))
        for main, shade, xs in ordered:
            if should_stop and should_stop():
                return

            last_main, last_shade, in_shades_panel = _select_shade(
                cfg,
                options,
                main,
                shade,
                last_main,
                last_shade,
                in_shades_panel,
            )

            xs.sort()
            # Break into contiguous runs so we can use the fast stroke option.
            run: List[int] = []
            for x in xs:
                if not run or x == run[-1] + 1:
                    run.append(x)
                    continue
                pts = [_cell_center(canvas_rect, grid_w, grid_h, rx, y) for rx in run]
                if options.enable_drag_strokes and len(pts) >= 2:
                    _rapid_click_stroke(pts, options, should_stop=should_stop)
                else:
                    for p in pts:
                        if should_stop and should_stop():
                            return
                        _tap(p, options)
                if progress_cb:
                    for rx in run:
                        progress_cb(rx, y)
                run = [x]

            if run:
                pts = [_cell_center(canvas_rect, grid_w, grid_h, rx, y) for rx in run]
                if options.enable_drag_strokes and len(pts) >= 2:
                    _rapid_click_stroke(pts, options, should_stop=should_stop)
                else:
                    for p in pts:
                        if should_stop and should_stop():
                            return
                        _tap(p, options)
                if progress_cb:
                    for rx in run:
                        progress_cb(rx, y)

        if in_shades_panel:
            _tap(cfg.back_button_pos, options)

    # If we get here, verification never converged.
    raise RuntimeError(
        f"Row verification failed (row {y+1}/{grid_h}). "
        f"Try increasing Verify tolerance or timing, or disable verification."
    )


def _verify_and_repair_color_group(
    cfg: AppConfig,
    canvas_rect: Tuple[int, int, int, int],
    grid_w: int,
    grid_h: int,
    main: MainColor,
    shade: ShadeButton,
    coords: List[Tuple[int, int]],
    options: PainterOptions,
    progress_cb: Optional[Callable[[int, int], None]] = None,
    should_stop: Optional[Callable[[], bool]] = None,
) -> None:
    """Verify/repaint a single shade group after painting it.

    This is used by Paint-by-Color to keep the initial pass fast and then
    correct any missed pixels per color.
    """

    if not bool(getattr(cfg, "verify_rows", True)):
        return

    tol = int(getattr(cfg, "verify_tolerance", 35))
    tol2 = max(0, tol) ** 2
    max_passes = max(1, int(getattr(cfg, "verify_max_passes", 10)))
    settle_s = max(0.0, float(getattr(cfg, "verify_settle_s", 0.05)))

    coords_sorted = sorted(coords, key=lambda xy: (xy[1], xy[0]))

    for _pass in range(max_passes):
        if should_stop and should_stop():
            return
        if settle_s > 0:
            time.sleep(settle_s)

        mismatches: List[Tuple[int, int]] = []
        for x, y in coords_sorted:
            cx, cy = _cell_center(canvas_rect, grid_w, grid_h, x, y)
            actual = get_screen_pixel_rgb(cx, cy)
            if _dist2(actual, shade.rgb) > tol2:
                mismatches.append((x, y))

        if not mismatches:
            return

        # Force a full reselect each pass; if a click failed earlier, relying on
        # cached state can keep repainting with the wrong shade.
        last_main: Optional[MainColor] = None
        last_shade: Optional[ShadeButton] = None
        in_shades_panel = False

        last_main, last_shade, in_shades_panel = _select_shade(
            cfg,
            options,
            main,
            shade,
            last_main,
            last_shade,
            in_shades_panel,
        )

        # Repaint mismatches, using contiguous horizontal runs for speed.
        mismatches.sort(key=lambda xy: (xy[1], xy[0]))
        i = 0
        while i < len(mismatches):
            if should_stop and should_stop():
                return
            x, y = mismatches[i]
            run = [(x, y)]
            j = i + 1
            while j < len(mismatches):
                nx, ny = mismatches[j]
                if ny != y or nx != run[-1][0] + 1:
                    break
                run.append((nx, ny))
                j += 1

            pts = [_cell_center(canvas_rect, grid_w, grid_h, rx, ry) for rx, ry in run]
            if options.enable_drag_strokes and len(pts) >= 2:
                _rapid_click_stroke(pts, options, should_stop=should_stop)
            else:
                for p in pts:
                    if should_stop and should_stop():
                        return
                    _tap(p, options)

            if progress_cb:
                for rx, ry in run:
                    progress_cb(rx, ry)

            i = j

    raise RuntimeError(
        "Color verification failed for a shade group. "
        "Try increasing Verify tolerance or timing, or disable verification."
    )


def paint_grid(
    cfg: AppConfig,
    canvas_rect: Tuple[int, int, int, int],
    grid_w: int,
    grid_h: int,
    get_pixel: Callable[[int, int], RGB],
    options: Optional[PainterOptions] = None,
    paint_mode: str = "row",
    skip: Optional[Callable[[int, int], bool]] = None,
    allow_bucket_fill: bool = True,
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

    mode = (paint_mode or "row").strip().lower()
    if mode in {"color", "colour", "paint by color"}:
        _paint_grid_by_color(
            cfg=cfg,
            canvas_rect=canvas_rect,
            grid_w=grid_w,
            grid_h=grid_h,
            get_pixel=get_pixel,
            options=options,
            skip=skip,
            allow_bucket_fill=allow_bucket_fill,
            progress_cb=progress_cb,
            should_stop=should_stop,
        )
        return

    last_main: Optional[MainColor] = None
    last_shade: Optional[ShadeButton] = None
    in_shades_panel = False

    # Cache best-match results for repeated RGBs.
    match_cache: Dict[RGB, Optional[Tuple[MainColor, ShadeButton]]] = {}

    def get_match(rgb: RGB) -> Optional[Tuple[MainColor, ShadeButton]]:
        if rgb in match_cache:
            return match_cache[rgb]
        m = _find_best_match(rgb, cfg)
        match_cache[rgb] = m
        return m

    # Optional bucket-fill pre-pass: fill the entire canvas with the most-used shade,
    # then skip painting that shade in the per-pixel pass.
    bucket_key: Optional[Tuple[str, Point]] = None
    if allow_bucket_fill and bool(getattr(cfg, "bucket_fill_enabled", False)):
        # Build usage counts.
        counts: Dict[Tuple[str, Point], Tuple[int, MainColor, ShadeButton]] = {}
        for yy in range(grid_h):
            for xx in range(grid_w):
                if should_stop and should_stop():
                    return
                if skip is not None and skip(xx, yy):
                    continue
                m = get_match(get_pixel(xx, yy))
                if m is None:
                    continue
                mc, sh = m
                k = (mc.name, sh.pos)
                if k not in counts:
                    counts[k] = (0, mc, sh)
                counts[k] = (counts[k][0] + 1, counts[k][1], counts[k][2])

        if counts:
            bucket_key, (bucket_n, bucket_main, bucket_shade) = max(
                ((k, v) for (k, v) in counts.items()),
                key=lambda kv: kv[1][0],
            )
            min_cells = max(0, int(getattr(cfg, "bucket_fill_min_cells", 50)))
            if bucket_n < min_cells:
                bucket_key = None
            else:
                _bucket_fill_canvas_with_shade(
                    cfg=cfg,
                    canvas_rect=canvas_rect,
                    grid_w=grid_w,
                    grid_h=grid_h,
                    main=bucket_main,
                    shade=bucket_shade,
                    options=options,
                    should_stop=should_stop,
                )

    for y in range(grid_h):
        x = 0
        while x < grid_w:
            if should_stop and should_stop():
                return

            if skip is not None and skip(x, y):
                if progress_cb:
                    progress_cb(x, y)
                x += 1
                continue

            rgb = get_pixel(x, y)
            match = get_match(rgb)
            if match is None:
                x += 1
                continue
            main, shade = match

            if bucket_key is not None and (main.name, shade.pos) == bucket_key:
                # Already bucket-filled.
                if progress_cb:
                    progress_cb(x, y)
                x += 1
                continue

            # Find run of adjacent same-shade pixels to potentially stroke.
            run_start = x
            run_end = x
            while run_end + 1 < grid_w:
                if skip is not None and skip(run_end + 1, y):
                    break
                nxt = get_match(get_pixel(run_end + 1, y))
                if nxt is None:
                    break
                nmain, nshade = nxt
                if nmain.name != main.name or nshade.pos != shade.pos:
                    break
                run_end += 1

            # Select main color if changed
            if last_main is None or main.name != last_main.name:
                if in_shades_panel:
                    _tap(cfg.back_button_pos, options)
                    in_shades_panel = False
                _tap(main.pos, options)
                _tap(cfg.shades_panel_button_pos, options, extra_delay_s=options.panel_open_delay_s)
                in_shades_panel = True
                last_main = main
                last_shade = None

            if not in_shades_panel:
                _tap(cfg.shades_panel_button_pos, options, extra_delay_s=options.panel_open_delay_s)
                in_shades_panel = True

            if last_shade is None or shade.pos != last_shade.pos:
                _tap(shade.pos, options, extra_delay_s=options.shade_select_delay_s)
                last_shade = shade

            # Paint run
            run_len = run_end - run_start + 1
            if options.enable_drag_strokes and run_len >= 2:
                pts: List[Point] = []
                for xx in range(run_start, run_end + 1):
                    cx = int(x0 + (xx + 0.5) * cell_w)
                    cy = int(y0 + (y + 0.5) * cell_h)
                    pts.append((cx, cy))
                _rapid_click_stroke(pts, options, should_stop=should_stop)
                if progress_cb:
                    for xx in range(run_start, run_end + 1):
                        progress_cb(xx, y)
            else:
                for xx in range(run_start, run_end + 1):
                    cx = int(x0 + (xx + 0.5) * cell_w)
                    cy = int(y0 + (y + 0.5) * cell_h)
                    _tap((cx, cy), options)
                    if progress_cb:
                        progress_cb(xx, y)

            x = run_end + 1

        # Verify the row after it's been attempted once.
        row_expected: List[Optional[Tuple[MainColor, ShadeButton]]] = [None] * grid_w
        for xx in range(grid_w):
            if skip is not None and skip(xx, y):
                row_expected[xx] = None
                continue
            m = get_match(get_pixel(xx, y))
            row_expected[xx] = m
        _verify_and_repair_row(
            cfg=cfg,
            canvas_rect=canvas_rect,
            grid_w=grid_w,
            grid_h=grid_h,
            y=y,
            row_expected=row_expected,
            options=options,
            progress_cb=progress_cb,
            should_stop=should_stop,
        )

        if options.row_delay_s > 0:
            time.sleep(options.row_delay_s)

    # Leave the game UI in a predictable state.
    if in_shades_panel:
        _tap(cfg.back_button_pos, options)


def _paint_grid_by_color(
    cfg: AppConfig,
    canvas_rect: Tuple[int, int, int, int],
    grid_w: int,
    grid_h: int,
    get_pixel: Callable[[int, int], RGB],
    options: PainterOptions,
    skip: Optional[Callable[[int, int], bool]] = None,
    allow_bucket_fill: bool = True,
    progress_cb: Optional[Callable[[int, int], None]] = None,
    should_stop: Optional[Callable[[], bool]] = None,
) -> None:
    """Paint all pixels grouped by shade.

    This minimizes palette switching by selecting a shade once and painting all
    cells that need that shade before moving to the next.
    """

    x0, y0, w, h = canvas_rect
    if grid_w <= 0 or grid_h <= 0:
        return

    cell_w = w / grid_w
    cell_h = h / grid_h

    # Cache best-match results for repeated RGBs.
    match_cache: Dict[RGB, Optional[Tuple[MainColor, ShadeButton]]] = {}

    def get_match(rgb: RGB) -> Optional[Tuple[MainColor, ShadeButton]]:
        if rgb in match_cache:
            return match_cache[rgb]
        m = _find_best_match(rgb, cfg)
        match_cache[rgb] = m
        return m

    # Group: (main_name, shade_pos) -> (main, shade, [(x,y), ...])
    groups: Dict[Tuple[str, Point], Tuple[MainColor, ShadeButton, List[Tuple[int, int]]]] = {}

    # Preprocess all pixels first so we know what to paint per shade.
    for y in range(grid_h):
        for x in range(grid_w):
            if should_stop and should_stop():
                return
            if skip is not None and skip(x, y):
                continue
            rgb = get_pixel(x, y)
            match = get_match(rgb)
            if match is None:
                continue
            main, shade = match
            key = (main.name, shade.pos)
            if key not in groups:
                groups[key] = (main, shade, [])
            groups[key][2].append((x, y))

    # Stable order: most-used shades first, then name/pos as tie-breaker.
    ordered = sorted(
        groups.values(),
        key=lambda t: (-len(t[2]), t[0].name, t[1].pos[0], t[1].pos[1]),
    )

    # Optional bucket-fill: fill entire canvas with the most-used shade and then
    # skip painting that shade.
    bucket_key: Optional[Tuple[str, Point]] = None
    if allow_bucket_fill and bool(getattr(cfg, "bucket_fill_enabled", False)) and ordered:
        main0, shade0, coords0 = ordered[0]
        min_cells = max(0, int(getattr(cfg, "bucket_fill_min_cells", 50)))
        if len(coords0) >= min_cells:
            _bucket_fill_canvas_with_shade(
                cfg=cfg,
                canvas_rect=canvas_rect,
                grid_w=grid_w,
                grid_h=grid_h,
                main=main0,
                shade=shade0,
                options=options,
                should_stop=should_stop,
            )
            bucket_key = (main0.name, shade0.pos)
            # Mark these pixels as complete for progress purposes.
            if progress_cb:
                for xx, yy in coords0:
                    progress_cb(xx, yy)

    # Optional region bucket fill (outline then fill). Only meaningful if we have a
    # base fill; otherwise bucket fill can leak into other unpainted base-colored areas.
    regions_enabled = (
        allow_bucket_fill
        and bucket_key is not None
        and bool(getattr(cfg, "bucket_fill_regions_enabled", False))
        and cfg.paint_tool_button_pos is not None
        and cfg.bucket_tool_button_pos is not None
    )
    regions_min_cells = max(0, int(getattr(cfg, "bucket_fill_regions_min_cells", 200)))

    last_main: Optional[MainColor] = None
    last_shade: Optional[ShadeButton] = None
    in_shades_panel = False

    for main, shade, coords in ordered:
        if should_stop and should_stop():
            return

        if bucket_key is not None and (main.name, shade.pos) == bucket_key:
            continue

        # Use the unified selection logic (includes retries + UI sanity check).
        last_main, last_shade, in_shades_panel = _select_shade(
            cfg=cfg,
            options=options,
            main=main,
            shade=shade,
            last_main=last_main,
            last_shade=last_shade,
            in_shades_panel=in_shades_panel,
        )

        # If enabled, bucket-fill large connected regions by outlining first.
        # This is very fast when the canvas currently has a uniform base color.
        remaining = coords
        if regions_enabled and regions_min_cells > 0 and len(coords) >= regions_min_cells:
            coord_set = set(coords)

            def touches_edge(comp: List[Tuple[int, int]]) -> bool:
                for cx, cy in comp:
                    if cx <= 0 or cy <= 0 or cx >= grid_w - 1 or cy >= grid_h - 1:
                        return True
                return False

            bucketed: set[Tuple[int, int]] = set()

            while coord_set:
                if should_stop and should_stop():
                    return
                start = next(iter(coord_set))
                stack = [start]
                comp: List[Tuple[int, int]] = []
                coord_set.remove(start)
                while stack:
                    px, py = stack.pop()
                    comp.append((px, py))
                    for nx, ny in ((px - 1, py), (px + 1, py), (px, py - 1), (px, py + 1)):
                        if (nx, ny) in coord_set:
                            coord_set.remove((nx, ny))
                            stack.append((nx, ny))

                if len(comp) < regions_min_cells:
                    continue
                if touches_edge(comp):
                    # Not safe to bucket-fill; leave for normal per-pixel paint.
                    continue

                comp_set = set(comp)
                boundary: List[Tuple[int, int]] = []
                interior: Optional[Tuple[int, int]] = None
                for px, py in comp:
                    is_boundary = False
                    for nx, ny in ((px - 1, py), (px + 1, py), (px, py - 1), (px, py + 1)):
                        if (nx, ny) not in comp_set:
                            is_boundary = True
                            break
                    if is_boundary:
                        boundary.append((px, py))
                    elif interior is None:
                        interior = (px, py)

                if interior is None:
                    # No interior (thin shape) -> not worth bucket filling.
                    continue

                # Outline boundary pixels with the target shade (paint tool).
                _tap(cfg.paint_tool_button_pos, options)
                _paint_coord_runs(
                    cfg=cfg,
                    canvas_rect=canvas_rect,
                    grid_w=grid_w,
                    grid_h=grid_h,
                    coords=boundary,
                    options=options,
                    progress_cb=None,
                    should_stop=should_stop,
                )

                if should_stop and should_stop():
                    return

                # Bucket-fill inside the outlined region.
                _tap(cfg.bucket_tool_button_pos, options)
                fx, fy = interior
                _tap(_cell_center(canvas_rect, grid_w, grid_h, fx, fy), options)
                _tap(cfg.paint_tool_button_pos, options)

                bucketed |= comp_set

                if progress_cb:
                    for xx, yy in comp:
                        progress_cb(xx, yy)

            if bucketed:
                remaining = [xy for xy in coords if xy not in bucketed]

        # Paint remaining cells for this shade.
        # Prefer horizontal strokes across adjacent pixels (same shade).
        _paint_coord_runs(
            cfg=cfg,
            canvas_rect=canvas_rect,
            grid_w=grid_w,
            grid_h=grid_h,
            coords=list(remaining),
            options=options,
            progress_cb=progress_cb,
            should_stop=should_stop,
        )

        # Verify this color batch after painting it (faster than waiting for row completion).
        _verify_and_repair_color_group(
            cfg=cfg,
            canvas_rect=canvas_rect,
            grid_w=grid_w,
            grid_h=grid_h,
            main=main,
            shade=shade,
            coords=coords,
            options=options,
            progress_cb=progress_cb,
            should_stop=should_stop,
        )

        # Keep UI state and our state in sync. The shades panel is typically left
        # open after selecting a shade; close it between groups so the next main
        # selection is reliable.
        if in_shades_panel:
            _tap(cfg.back_button_pos, options)
            in_shades_panel = False
        last_main = None
        last_shade = None

        if options.row_delay_s > 0:
            time.sleep(options.row_delay_s)

    if in_shades_panel:
        _tap(cfg.back_button_pos, options)
