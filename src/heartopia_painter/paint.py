from __future__ import annotations

from collections import deque
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
    on_point: Optional[Callable[[int], None]] = None,
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

    for idx, (px, py) in enumerate(points):
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
        if on_point:
            try:
                on_point(idx)
            except Exception:
                pass

    if after_stroke_delay > 0:
        time.sleep(after_stroke_delay)


def _interruptible_sleep(duration_s: float, should_stop: Optional[Callable[[], bool]] = None) -> bool:
    """Sleep in small increments so ESC/pause can interrupt quickly.

    Returns False if interrupted by should_stop(), True if the full sleep elapsed.
    """

    try:
        dur = max(0.0, float(duration_s))
    except Exception:
        dur = 0.0

    end = time.time() + dur
    while True:
        if should_stop and should_stop():
            return False
        now = time.time()
        if now >= end:
            return True
        time.sleep(min(0.02, max(0.0, end - now)))


# Back-compat: some older call sites used the misspelling.
_interruptable_sleep = _interruptible_sleep


def erase_canvas(
    cfg: AppConfig,
    canvas_rect: Tuple[int, int, int, int],
    grid_w: int,
    grid_h: int,
    options: PainterOptions,
    should_stop: Optional[Callable[[], bool]] = None,
    status_cb: Optional[Callable[[str], None]] = None,
) -> None:
    """Erase the whole canvas quickly.

    Workflow:
    - Select eraser tool
    - Click thickness-up 5x (assumes that reaches 10x10)
    - Sweep horizontal bands across the canvas
    """

    if cfg.eraser_tool_button_pos is None or cfg.eraser_thickness_up_button_pos is None:
        raise RuntimeError(
            "Eraser configuration missing. Set 'eraser tool button' and 'eraser thickness + button' first."
        )

    if grid_w <= 0 or grid_h <= 0:
        raise RuntimeError("Invalid grid size.")

    def stop() -> bool:
        return bool(should_stop and should_stop())

    if status_cb:
        try:
            status_cb("Selecting eraser tool…")
        except Exception:
            pass
    _tap(cfg.eraser_tool_button_pos, options)
    if stop():
        return

    if status_cb:
        try:
            status_cb("Setting eraser size (5x)…")
        except Exception:
            pass
    for _ in range(5):
        if stop():
            return
        _tap(cfg.eraser_thickness_up_button_pos, options)

    if status_cb:
        try:
            status_cb("Erasing canvas…")
        except Exception:
            pass

    # Erase-specific click timing: much faster than paint, but keep a tiny
    # non-zero hold so games register the click.
    erase_click_opts = PainterOptions(
        move_duration_s=0.0,
        mouse_down_s=max(0.006, min(0.02, float(options.mouse_down_s))),
        after_click_delay_s=max(0.0, min(0.01, float(options.after_click_delay_s))),
        panel_open_delay_s=float(options.panel_open_delay_s),
        shade_select_delay_s=float(options.shade_select_delay_s),
        row_delay_s=float(options.row_delay_s),
        enable_drag_strokes=False,
        drag_step_duration_s=float(options.drag_step_duration_s),
        after_drag_delay_s=float(options.after_drag_delay_s),
    )

    # Periodic micro-pauses help avoid the game/UI dropping fast click bursts.
    taps = 0
    burst_pause_every = 30
    burst_pause_s = 0.02

    # Erase should always be click-based (no stroke-neighbors/drag strokes),
    # since drag can fail to register in some UI states.
    step = 10
    y_step = 10

    for band_idx, y0 in enumerate(range(0, grid_h, y_step)):
        if stop():
            return

        # Aim for the middle of the vertical band.
        y = min(grid_h - 1, y0 + y_step // 2)

        if status_cb and (y0 % max(1, y_step * 4) == 0):
            try:
                status_cb(f"Erasing… row {y0+1}/{grid_h}")
            except Exception:
                pass

        # Click-based erase, fast but reliable: step by 10 cells.
        # Use a snake pattern so the cursor doesn't waste time jumping back.
        xs = list(range(0, grid_w, step))
        if (grid_w - 1) not in xs:
            xs.append(grid_w - 1)
        if (band_idx % 2) == 1:
            xs = list(reversed(xs))

        for x in xs:
            if stop():
                return
            pt = _cell_center(canvas_rect, grid_w, grid_h, int(x), int(y))
            _tap(pt, erase_click_opts)
            taps += 1
            if (taps % burst_pause_every) == 0:
                _interruptible_sleep(burst_pause_s, should_stop)


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


def _sleep_with_stop(duration_s: float, should_stop: Optional[Callable[[], bool]] = None) -> bool:
    """Sleep in small chunks so stop/pause can interrupt quickly.

    Returns False if interrupted by should_stop.
    """

    d = max(0.0, float(duration_s))
    if d <= 0:
        return True
    end = time.perf_counter() + d
    while True:
        if should_stop and should_stop():
            return False
        now = time.perf_counter()
        if now >= end:
            return True
        time.sleep(min(0.02, max(0.0, end - now)))


def _maybe_emit_verify(
    verify_cb: Optional[Callable[[Optional[Tuple[int, int]]], None]],
    pt: Optional[Tuple[int, int]],
    idx: int,
    every: int = 10,
) -> None:
    if verify_cb is None:
        return
    if every <= 1 or (idx % every) == 0:
        try:
            verify_cb(pt)
        except Exception:
            pass


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
        # Defensive: ALWAYS attempt to return to the main palette before
        # selecting a main color. Our in_shades_panel flag can get out of sync
        # if a Back click didn't register, which causes main-color clicks to hit
        # the wrong UI and can create verification loops.
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
            if progress_cb:
                def _on_point(idx: int) -> None:
                    try:
                        rx, ry = run[idx]
                    except Exception:
                        return
                    progress_cb(int(rx), int(ry))

                _rapid_click_stroke(pts, options, should_stop=should_stop, on_point=_on_point)
            else:
                _rapid_click_stroke(pts, options, should_stop=should_stop)
        else:
            for (rx, ry), p in zip(run, pts):
                if should_stop and should_stop():
                    return
                _tap(p, options)
                if progress_cb:
                    progress_cb(int(rx), int(ry))

        i = j


def _verify_outline_then_repair(
    cfg: AppConfig,
    canvas_rect: Tuple[int, int, int, int],
    grid_w: int,
    grid_h: int,
    outline_coords: List[Tuple[int, int]],
    expected_rgb: Optional[RGB],
    avoid_rgb: Optional[RGB],
    options: PainterOptions,
    should_stop: Optional[Callable[[], bool]] = None,
    status_cb: Optional[Callable[[str], None]] = None,
    verify_cb: Optional[Callable[[Optional[Tuple[int, int]]], None]] = None,
    max_passes_override: Optional[int] = None,
    local_base_rgb: Optional[Callable[[int, int], Optional[RGB]]] = None,
) -> bool:
    """Verify outline pixels are painted correctly; repaint misses if needed.

    Returns True when the outline verifies within max passes.
    Returns False if it never converges (caller should skip bucket-fill).
    """

    if not outline_coords:
        return True

    # For region bucket-fill spill safety, it's often more reliable to verify that
    # outline pixels are NOT the base fill color, rather than requiring an exact
    # match to the target shade (games can alter displayed RGB).
    if expected_rgb is None and avoid_rgb is None:
        return True

    tol = int(getattr(cfg, "verify_tolerance", 35))
    tol2 = max(0, tol) ** 2
    # When verifying that outline pixels are NOT the base fill color, we want
    # a stricter "base similarity" threshold; otherwise a large verify tolerance
    # can cause false failures if the outline shade is somewhat close to the base.
    base_tol = max(8, int(tol * 0.5))
    base_tol2 = max(0, base_tol) ** 2
    settle_s = max(0.0, float(getattr(cfg, "verify_settle_s", 0.05)))
    # Outline verification is just for spill safety; keep it bounded.
    max_passes = max(1, min(5, int(getattr(cfg, "verify_max_passes", 10))))
    if max_passes_override is not None:
        try:
            max_passes = max(1, int(max_passes_override))
        except Exception:
            pass

    coords = list(outline_coords)
    coords.sort(key=lambda xy: (xy[1], xy[0]))

    for _pass in range(max_passes):
        if should_stop and should_stop():
            return False
        if settle_s > 0:
            if not _sleep_with_stop(settle_s, should_stop=should_stop):
                return False

        if status_cb is not None:
            try:
                if avoid_rgb is not None:
                    status_cb(f"Verifying outline vs base… pass {_pass+1}/{max_passes}")
                else:
                    status_cb(f"Verifying outline… pass {_pass+1}/{max_passes}")
            except Exception:
                pass

        mism: List[Tuple[int, int]] = []
        for i, (x, y) in enumerate(coords):
            if should_stop and should_stop():
                return False
            cx, cy = _cell_center(canvas_rect, grid_w, grid_h, x, y)
            _maybe_emit_verify(verify_cb, (x, y), i, every=8)
            actual = get_screen_pixel_rgb(cx, cy)

            if avoid_rgb is not None:
                base_ref = None
                if local_base_rgb is not None:
                    try:
                        base_ref = local_base_rgb(int(x), int(y))
                    except Exception:
                        base_ref = None
                if base_ref is None:
                    base_ref = avoid_rgb
                # Primary goal: outline should not look like base.
                # If expected_rgb is provided too, use it to disambiguate when the
                # outline shade is close to the base shade (avoid false mismatches).
                # Hole detection must always consider the global base.
                # Local base sampling can be polluted if the nearest "outside"
                # neighbor is already painted, which would otherwise mask holes.
                looks_like_base = False
                try:
                    looks_like_base = looks_like_base or (_dist2(actual, avoid_rgb) <= base_tol2)
                except Exception:
                    pass
                try:
                    if base_ref is not None:
                        looks_like_base = looks_like_base or (_dist2(actual, base_ref) <= base_tol2)
                except Exception:
                    pass
                if expected_rgb is not None:
                    looks_like_expected = _dist2(actual, expected_rgb) <= tol2
                    if looks_like_base and (not looks_like_expected):
                        mism.append((x, y))
                else:
                    if looks_like_base:
                        mism.append((x, y))
            else:
                # Fallback: mismatch if pixel doesn't match expected.
                if expected_rgb is None:
                    continue
                if _dist2(actual, expected_rgb) > tol2:
                    mism.append((x, y))

        if not mism:
            _maybe_emit_verify(verify_cb, None, 0, every=1)
            return True

        # Repaint just the mismatched outline pixels.
        # Use reliable click taps (double-tap) rather than strokes.
        try:
            if cfg.paint_tool_button_pos is not None:
                _tap(cfg.paint_tool_button_pos, options)
        except Exception:
            pass
        for x, y in mism:
            if should_stop and should_stop():
                return False
            cx, cy = _cell_center(canvas_rect, grid_w, grid_h, int(x), int(y))
            _tap((cx, cy), options)
            _tap((cx, cy), options, extra_delay_s=0.01)

    _maybe_emit_verify(verify_cb, None, 0, every=1)
    return False


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
    status_cb: Optional[Callable[[str], None]] = None,
    verify_cb: Optional[Callable[[Optional[Tuple[int, int]]], None]] = None,
) -> None:
    if not bool(getattr(cfg, "verify_rows", True)):
        _maybe_emit_verify(verify_cb, None, 0, every=1)
        return

    tol = int(getattr(cfg, "verify_tolerance", 35))
    tol2 = max(0, tol) ** 2
    max_passes = max(1, int(getattr(cfg, "verify_max_passes", 10)))
    settle_s = max(0.0, float(getattr(cfg, "verify_settle_s", 0.05)))

    prev_mismatch_n: Optional[int] = None
    stagnant_passes = 0

    for _pass in range(max_passes):
        if should_stop and should_stop():
            return
        if settle_s > 0:
            if not _sleep_with_stop(settle_s, should_stop=should_stop):
                return

        if status_cb is not None:
            try:
                status_cb(f"Verifying row {y+1}/{grid_h}… pass {_pass+1}/{max_passes}")
            except Exception:
                pass

        # Collect mismatches grouped by shade
        groups: Dict[Tuple[str, Point], Tuple[MainColor, ShadeButton, List[int]]] = {}
        for x in range(grid_w):
            if should_stop and should_stop():
                return
            exp = row_expected[x] if x < len(row_expected) else None
            if exp is None:
                continue
            main, shade = exp

            cx, cy = _cell_center(canvas_rect, grid_w, grid_h, x, y)
            _maybe_emit_verify(verify_cb, (x, y), x, every=6)
            actual = get_screen_pixel_rgb(cx, cy)
            if _dist2(actual, shade.rgb) <= tol2:
                continue
            key = (main.name, shade.pos)
            if key not in groups:
                groups[key] = (main, shade, [])
            groups[key][2].append(x)

        if not groups:
            _maybe_emit_verify(verify_cb, None, 0, every=1)
            return

        mismatch_n = sum(len(t[2]) for t in groups.values())
        if prev_mismatch_n is not None:
            if mismatch_n >= prev_mismatch_n:
                stagnant_passes += 1
            else:
                stagnant_passes = 0
        prev_mismatch_n = mismatch_n

        if stagnant_passes >= max(1, int(getattr(cfg, "verify_auto_recover_after_passes", 2))) and bool(
            getattr(cfg, "verify_auto_recover_loops", False)
        ):
            if status_cb is not None:
                try:
                    status_cb("Verify loop detected; resyncing UI and continuing…")
                except Exception:
                    pass
            # Resync palette state.
            try:
                _tap(cfg.back_button_pos, options)
                _tap(cfg.back_button_pos, options)
            except Exception:
                pass
            _maybe_emit_verify(verify_cb, None, 0, every=1)
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

    _maybe_emit_verify(verify_cb, None, 0, every=1)

    # If we get here, verification never converged.
    if bool(getattr(cfg, "verify_auto_recover_loops", False)):
        if status_cb is not None:
            try:
                status_cb("Verify did not converge; skipping and continuing…")
            except Exception:
                pass
        _maybe_emit_verify(verify_cb, None, 0, every=1)
        try:
            _tap(cfg.back_button_pos, options)
        except Exception:
            pass
        return

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
    status_cb: Optional[Callable[[str], None]] = None,
    verify_cb: Optional[Callable[[Optional[Tuple[int, int]]], None]] = None,
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
            if not _sleep_with_stop(settle_s, should_stop=should_stop):
                return

        if status_cb is not None:
            try:
                status_cb(
                    f"Verifying color '{main.name}/{shade.name}'… pass {_pass+1}/{max_passes}"
                )
            except Exception:
                pass

        mismatches: List[Tuple[int, int]] = []
        for i, (x, y) in enumerate(coords_sorted):
            if should_stop and should_stop():
                return
            cx, cy = _cell_center(canvas_rect, grid_w, grid_h, x, y)
            _maybe_emit_verify(verify_cb, (x, y), i, every=10)
            actual = get_screen_pixel_rgb(cx, cy)
            if _dist2(actual, shade.rgb) > tol2:
                mismatches.append((x, y))

        if not mismatches:
            _maybe_emit_verify(verify_cb, None, 0, every=1)
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

    if bool(getattr(cfg, "verify_auto_recover_loops", False)):
        if status_cb is not None:
            try:
                status_cb("Verify did not converge for a color; skipping and continuing…")
            except Exception:
                pass
        _maybe_emit_verify(verify_cb, None, 0, every=1)
        try:
            _tap(cfg.back_button_pos, options)
        except Exception:
            pass
        return

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
    allow_region_bucket_fill: bool = True,
    resume_base_bucket_key: Optional[Tuple[str, Point]] = None,
    resume_base_bucket_rgb: Optional[RGB] = None,
    bucket_base_cb: Optional[Callable[[str, int, int, int, int, int], None]] = None,
    progress_cb: Optional[Callable[[int, int], None]] = None,
    should_stop: Optional[Callable[[], bool]] = None,
    status_cb: Optional[Callable[[str], None]] = None,
    verify_cb: Optional[Callable[[Optional[Tuple[int, int]]], None]] = None,
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
        if status_cb is not None:
            try:
                status_cb("Painting by color…")
            except Exception:
                pass
        _paint_grid_by_color(
            cfg=cfg,
            canvas_rect=canvas_rect,
            grid_w=grid_w,
            grid_h=grid_h,
            get_pixel=get_pixel,
            options=options,
            skip=skip,
            allow_bucket_fill=allow_bucket_fill,
            allow_region_bucket_fill=allow_region_bucket_fill,
            resume_base_bucket_key=resume_base_bucket_key,
            resume_base_bucket_rgb=resume_base_bucket_rgb,
            bucket_base_cb=bucket_base_cb,
            progress_cb=progress_cb,
            should_stop=should_stop,
            status_cb=status_cb,
            verify_cb=verify_cb,
        )
        return

    last_main: Optional[MainColor] = None
    last_shade: Optional[ShadeButton] = None
    in_shades_panel = False

    # Optional streaming verification (verify a few cells behind while painting).
    streaming = bool(getattr(cfg, "verify_streaming_enabled", False)) and bool(getattr(cfg, "verify_rows", True))
    lag = max(0, int(getattr(cfg, "verify_streaming_lag", 10)))
    # Clamp lag to something sensible so it doesn't appear "stuck".
    lag = min(lag, 200)
    verify_tol = int(getattr(cfg, "verify_tolerance", 35))
    verify_tol2 = max(0, verify_tol) ** 2
    verify_queue = deque()  # (x, y, main, shade)
    verify_i = 0

    def _stream_verify_flush(force: bool = False) -> None:
        nonlocal last_main, last_shade, in_shades_panel, verify_i
        if not streaming:
            return
        while verify_queue and (force or len(verify_queue) > lag):
            if should_stop and should_stop():
                return
            x, y, main, shade = verify_queue.popleft()
            cx, cy = _cell_center(canvas_rect, grid_w, grid_h, int(x), int(y))
            verify_i += 1
            # Always update the cursor so streaming verify is visible.
            _maybe_emit_verify(verify_cb, (int(x), int(y)), verify_i, every=1)
            try:
                actual = get_screen_pixel_rgb(cx, cy)
            except Exception:
                continue
            if _dist2(actual, shade.rgb) <= verify_tol2:
                continue

            # Mismatch: select the expected shade and repaint this cell.
            last_main, last_shade, in_shades_panel = _select_shade(
                cfg=cfg,
                options=options,
                main=main,
                shade=shade,
                last_main=last_main,
                last_shade=last_shade,
                in_shades_panel=in_shades_panel,
            )
            _tap((cx, cy), options)
            if progress_cb:
                progress_cb(int(x), int(y))

        _maybe_emit_verify(verify_cb, None, 0, every=1)

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

                if status_cb is not None:
                    try:
                        status_cb(f"Bucket-filled base color: {bucket_main.name}/{bucket_shade.name}")
                    except Exception:
                        pass

    for y in range(grid_h):
        if status_cb is not None:
            try:
                status_cb(f"Painting row {y+1}/{grid_h}…")
            except Exception:
                pass
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
            last_main, last_shade, in_shades_panel = _select_shade(
                cfg=cfg,
                options=options,
                main=main,
                shade=shade,
                last_main=last_main,
                last_shade=last_shade,
                in_shades_panel=in_shades_panel,
            )

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
                if streaming:
                    for xx in range(run_start, run_end + 1):
                        verify_queue.append((int(xx), int(y), main, shade))
                    _stream_verify_flush(force=False)
            else:
                for xx in range(run_start, run_end + 1):
                    cx = int(x0 + (xx + 0.5) * cell_w)
                    cy = int(y0 + (y + 0.5) * cell_h)
                    _tap((cx, cy), options)
                    if progress_cb:
                        progress_cb(xx, y)
                    if streaming:
                        verify_queue.append((int(xx), int(y), main, shade))
                        _stream_verify_flush(force=False)

            x = run_end + 1

        if streaming:
            # Flush remaining lagging checks for this row.
            _stream_verify_flush(force=True)
        else:
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
                status_cb=status_cb,
                verify_cb=verify_cb,
            )

        if options.row_delay_s > 0:
            if not _sleep_with_stop(options.row_delay_s, should_stop=should_stop):
                return

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
    allow_region_bucket_fill: bool = True,
    resume_base_bucket_key: Optional[Tuple[str, Point]] = None,
    resume_base_bucket_rgb: Optional[RGB] = None,
    bucket_base_cb: Optional[Callable[[str, int, int, int, int, int], None]] = None,
    progress_cb: Optional[Callable[[int, int], None]] = None,
    should_stop: Optional[Callable[[], bool]] = None,
    status_cb: Optional[Callable[[str], None]] = None,
    verify_cb: Optional[Callable[[Optional[Tuple[int, int]]], None]] = None,
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

    def _sample_base_rgb() -> Optional[RGB]:
        # Sample a few points inside the canvas and take per-channel median.
        try:
            x0, y0, w, h = canvas_rect
            pts = [
                (int(x0 + w * 0.50), int(y0 + h * 0.50)),
                (int(x0 + w * 0.35), int(y0 + h * 0.35)),
                (int(x0 + w * 0.65), int(y0 + h * 0.35)),
                (int(x0 + w * 0.35), int(y0 + h * 0.65)),
                (int(x0 + w * 0.65), int(y0 + h * 0.65)),
            ]
            rgbs: List[RGB] = []
            for px, py in pts:
                try:
                    rgbs.append(get_screen_pixel_rgb(int(px), int(py)))
                except Exception:
                    continue
            if not rgbs:
                return None
            rs = sorted(c[0] for c in rgbs)
            gs = sorted(c[1] for c in rgbs)
            bs = sorted(c[2] for c in rgbs)
            mid = len(rgbs) // 2
            return (int(rs[mid]), int(gs[mid]), int(bs[mid]))
        except Exception:
            return None

    # Optional bucket-fill: fill entire canvas with the most-used shade and then
    # skip painting that shade.
    bucket_key: Optional[Tuple[str, Point]] = None
    base_rgb: Optional[RGB] = None

    # Resume path: allow region fill without redoing the base bucket-fill.
    if resume_base_bucket_key is not None and resume_base_bucket_rgb is not None:
        bucket_key = resume_base_bucket_key
        base_rgb = resume_base_bucket_rgb
    if allow_bucket_fill and bool(getattr(cfg, "bucket_fill_enabled", False)) and ordered:
        main0, shade0, coords0 = ordered[0]
        min_cells = max(0, int(getattr(cfg, "bucket_fill_min_cells", 50)))
        if len(coords0) >= min_cells:
            if status_cb is not None:
                try:
                    status_cb(f"Bucket-filling base canvas: {main0.name}/{shade0.name}…")
                except Exception:
                    pass
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
            # Use the actual on-screen base color for region-fill outline verification.
            # Palette RGBs can differ from rendered RGBs in-game.
            base_rgb = _sample_base_rgb() or shade0.rgb
            if bucket_base_cb is not None:
                try:
                    bucket_base_cb(
                        str(main0.name),
                        int(shade0.pos[0]),
                        int(shade0.pos[1]),
                        int(base_rgb[0]),
                        int(base_rgb[1]),
                        int(base_rgb[2]),
                    )
                except Exception:
                    pass
            # Mark these pixels as complete for progress purposes.
            if progress_cb:
                for xx, yy in coords0:
                    progress_cb(xx, yy)

    if allow_region_bucket_fill and bool(getattr(cfg, "bucket_fill_regions_enabled", False)) and bucket_key is None:
        if status_cb is not None:
            try:
                status_cb("Region fill disabled (needs base bucket-fill). Lower Bucket min cells or disable region fill.")
            except Exception:
                pass

    # Optional region bucket fill (outline then fill). Only meaningful if we have a
    # base fill; otherwise bucket fill can leak into other unpainted base-colored areas.
    regions_enabled = (
        allow_region_bucket_fill
        and bucket_key is not None
        and bool(getattr(cfg, "bucket_fill_regions_enabled", False))
        and cfg.paint_tool_button_pos is not None
        and cfg.bucket_tool_button_pos is not None
    )
    regions_min_cells = max(0, int(getattr(cfg, "bucket_fill_regions_min_cells", 200)))
    disable_regions_for_rest = False

    if allow_region_bucket_fill and bool(getattr(cfg, "bucket_fill_regions_enabled", False)) and bucket_key is not None:
        if cfg.paint_tool_button_pos is None or cfg.bucket_tool_button_pos is None:
            if status_cb is not None:
                try:
                    status_cb("Region fill disabled (capture paint tool + bucket tool buttons first).")
                except Exception:
                    pass

    last_main: Optional[MainColor] = None
    last_shade: Optional[ShadeButton] = None
    in_shades_panel = False

    # Optional streaming verification for Paint-by-Color.
    streaming = bool(getattr(cfg, "verify_streaming_enabled", False)) and bool(getattr(cfg, "verify_rows", True))
    lag = max(0, int(getattr(cfg, "verify_streaming_lag", 10)))
    lag = min(lag, 200)
    verify_tol = int(getattr(cfg, "verify_tolerance", 35))
    verify_tol2 = max(0, verify_tol) ** 2
    verify_i = 0
    verify_settle_s = max(0.0, float(getattr(cfg, "verify_settle_s", 0.05)))
    verify_settle_s = min(0.10, verify_settle_s)

    for main, shade, coords in ordered:
        if should_stop and should_stop():
            return

        if bucket_key is not None and (main.name, shade.pos) == bucket_key:
            continue

        # Use the unified selection logic (includes retries + UI sanity check).
        if status_cb is not None:
            try:
                status_cb(f"Selecting shade: {main.name}/{shade.name}…")
            except Exception:
                pass
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
        if regions_enabled and (not disable_regions_for_rest) and regions_min_cells > 0 and len(coords) >= regions_min_cells:
            coord_set = set(coords)

            bucketed: set[Tuple[int, int]] = set()

            comps_total = 0
            comps_small = 0
            comps_no_interior = 0
            comps_outline_fail = 0
            comps_filled = 0
            regions_total = 0
            regions_filled = 0

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

                comps_total += 1

                if len(comp) < regions_min_cells:
                    comps_small += 1
                    continue
                # Edge-touching components are allowed; the game canvas boundary
                # acts as a hard stop, and we also verify the outline before
                # bucket-filling to reduce spill risk.

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
                    comps_no_interior += 1
                    continue

                # Outline boundary pixels with the target shade (paint tool).
                if status_cb is not None:
                    try:
                        status_cb(f"Region fill: outlining {len(boundary)} px, filling {len(comp)} px…")
                    except Exception:
                        pass
                # Close the shades panel before painting/verifying on the canvas.
                # Some UI layouts can overlay the canvas and cause clicks to miss.
                if in_shades_panel and cfg.back_button_pos is not None:
                    try:
                        _tap(cfg.back_button_pos, options)
                        in_shades_panel = False
                    except Exception:
                        pass

                # For outline safety clicks, prefer slightly slower, click-only timing.
                outline_opts = PainterOptions(
                    move_duration_s=float(options.move_duration_s),
                    mouse_down_s=max(float(options.mouse_down_s), 0.02),
                    after_click_delay_s=max(float(options.after_click_delay_s), 0.02),
                    panel_open_delay_s=float(options.panel_open_delay_s),
                    shade_select_delay_s=float(options.shade_select_delay_s),
                    row_delay_s=float(options.row_delay_s),
                    enable_drag_strokes=False,
                    drag_step_duration_s=float(options.drag_step_duration_s),
                    after_drag_delay_s=float(options.after_drag_delay_s),
                )

                _tap(cfg.paint_tool_button_pos, outline_opts)

                # Optional: stream-verify the outline while painting it, to reduce
                # expensive post-pass outline verification.
                outline_streaming = bool(getattr(cfg, "verify_streaming_enabled", False))
                outline_lag = max(0, int(getattr(cfg, "verify_streaming_lag", 10)))
                outline_lag = min(outline_lag, 60)
                outline_queue = deque()  # (x, y, t_painted)
                outline_verify_settle_s = max(0.0, float(getattr(cfg, "verify_settle_s", 0.05)))
                outline_verify_settle_s = min(0.10, outline_verify_settle_s)

                # Local base sampler: sample a neighbor just outside the component.
                # This is much more reliable than a single global base_rgb.
                local_base_cache: Dict[Tuple[int, int], RGB] = {}

                def _local_base_rgb(px: int, py: int) -> Optional[RGB]:
                    key = (int(px), int(py))
                    if key in local_base_cache:
                        return local_base_cache[key]
                    try:
                        for nx, ny in ((px - 1, py), (px + 1, py), (px, py - 1), (px, py + 1)):
                            if (nx, ny) not in comp_set:
                                cx2, cy2 = _cell_center(canvas_rect, grid_w, grid_h, int(nx), int(ny))
                                rgb2 = get_screen_pixel_rgb(cx2, cy2)
                                if base_rgb is not None:
                                    tol = int(getattr(cfg, "verify_tolerance", 35))
                                    base_tol = max(8, int(tol * 0.5))
                                    base_tol2 = max(0, base_tol) ** 2
                                    if _dist2(rgb2, base_rgb) <= base_tol2:
                                        local_base_cache[key] = rgb2
                                        return rgb2
                                break
                    except Exception:
                        return base_rgb
                    return base_rgb

                def _outline_flush(force: bool = False, max_steps: int = 2) -> None:
                    if not outline_streaming:
                        return
                    steps = 0
                    while outline_queue and (force or len(outline_queue) > outline_lag):
                        if not force and steps >= max(1, int(max_steps)):
                            break
                        if should_stop and should_stop():
                            return
                        ox, oy, t_painted = outline_queue[0]
                        if outline_verify_settle_s > 0:
                            now = time.time()
                            ready_in = (float(t_painted) + outline_verify_settle_s) - now
                            if ready_in > 0:
                                if not force:
                                    break
                                if not _sleep_with_stop(min(0.02, ready_in), should_stop=should_stop):
                                    return
                                continue
                        ox, oy, _t = outline_queue.popleft()
                        cx, cy = _cell_center(canvas_rect, grid_w, grid_h, int(ox), int(oy))
                        try:
                            actual = get_screen_pixel_rgb(cx, cy)
                        except Exception:
                            steps += 1
                            continue
                        # Consider it bad only if it still looks like local base AND not like expected shade.
                        tol = int(getattr(cfg, "verify_tolerance", 35))
                        base_tol = max(8, int(tol * 0.5))
                        base_tol2 = max(0, base_tol) ** 2
                        tol2 = max(0, int(tol)) ** 2
                        base_ref = _local_base_rgb(int(ox), int(oy))
                        looks_like_base = False
                        if base_rgb is not None:
                            looks_like_base = looks_like_base or (_dist2(actual, base_rgb) <= base_tol2)
                        if base_ref is not None:
                            looks_like_base = looks_like_base or (_dist2(actual, base_ref) <= base_tol2)
                        looks_like_expected = _dist2(actual, shade.rgb) <= tol2
                        if looks_like_base and (not looks_like_expected):
                            _tap((cx, cy), outline_opts)
                            _tap((cx, cy), outline_opts, extra_delay_s=0.01)
                        steps += 1

                def _outline_progress(x: int, y: int) -> None:
                    if not outline_streaming:
                        return
                    outline_queue.append((int(x), int(y), time.time()))
                    backlog = len(outline_queue) - outline_lag
                    if backlog > 40:
                        step_n = 6
                    elif backlog > 20:
                        step_n = 3
                    else:
                        step_n = 1
                    _outline_flush(force=False, max_steps=step_n)

                _paint_coord_runs(
                    cfg=cfg,
                    canvas_rect=canvas_rect,
                    grid_w=grid_w,
                    grid_h=grid_h,
                    coords=boundary,
                    options=outline_opts,
                    progress_cb=_outline_progress if outline_streaming else None,
                    should_stop=should_stop,
                )

                # Large regions can develop tiny holes. A second outline pass helps, but costs time.
                # Only do it for large regions whose shade is close to base (low contrast), since those
                # are more likely to miss/verify poorly and leak.
                do_second_outline_pass = False
                if len(comp) >= (max(1, int(regions_min_cells)) * 2):
                    if base_rgb is None:
                        do_second_outline_pass = True
                    else:
                        tol = int(getattr(cfg, "verify_tolerance", 35))
                        close_thresh = max(60, int(tol * 2))
                        try:
                            do_second_outline_pass = _dist2(shade.rgb, base_rgb) <= (close_thresh * close_thresh)
                        except Exception:
                            do_second_outline_pass = True

                if do_second_outline_pass:
                    _paint_coord_runs(
                        cfg=cfg,
                        canvas_rect=canvas_rect,
                        grid_w=grid_w,
                        grid_h=grid_h,
                        coords=boundary,
                        options=outline_opts,
                        progress_cb=_outline_progress if outline_streaming else None,
                        should_stop=should_stop,
                    )

                if outline_streaming:
                    _outline_flush(force=True, max_steps=999999)

                # Verify the outline before bucket-fill to reduce spill risk.
                outline_ok = _verify_outline_then_repair(
                    cfg=cfg,
                    canvas_rect=canvas_rect,
                    grid_w=grid_w,
                    grid_h=grid_h,
                    outline_coords=boundary,
                    expected_rgb=shade.rgb,
                    avoid_rgb=base_rgb,
                    options=outline_opts,
                    should_stop=should_stop,
                    status_cb=status_cb,
                    verify_cb=verify_cb,
                    max_passes_override=1 if outline_streaming else None,
                    local_base_rgb=_local_base_rgb,
                )

                # If outline didn't verify, we can still attempt a cautious fill
                # for large, high-contrast regions. We'll run quick spill checks
                # afterwards; if spill is detected, we disable further region fills.
                try:
                    contrast2 = _dist2(shade.rgb, base_rgb) if base_rgb is not None else 0
                except Exception:
                    contrast2 = 0
                allow_cautious_fill = (
                    (not outline_ok)
                    and (base_rgb is not None)
                    and (len(comp) >= max(regions_min_cells, 600))
                    and (contrast2 >= 120 * 120)
                )
                if (not outline_ok) and (not allow_cautious_fill):
                    # Can't guarantee a sealed boundary; skip bucket-fill for safety.
                    comps_outline_fail += 1
                    if status_cb is not None:
                        try:
                            status_cb("Region fill skipped (outline didn't verify)")
                        except Exception:
                            pass
                    continue
                if (not outline_ok) and allow_cautious_fill and status_cb is not None:
                    try:
                        status_cb("Outline didn't verify; attempting cautious region fill with spill checks…")
                    except Exception:
                        pass

                if should_stop and should_stop():
                    return

                boundary_set = set(boundary)
                interior_set = set(comp_set) - boundary_set
                if not interior_set:
                    comps_no_interior += 1
                    continue

                # Find interior connected components (tight outlines can split interior
                # into multiple enclosed regions that need multiple bucket clicks).
                interior_components: List[List[Tuple[int, int]]] = []
                while interior_set:
                    seed = next(iter(interior_set))
                    stack2 = [seed]
                    interior_set.remove(seed)
                    sub: List[Tuple[int, int]] = []
                    while stack2:
                        qx, qy = stack2.pop()
                        sub.append((qx, qy))
                        for nx, ny in ((qx - 1, qy), (qx + 1, qy), (qx, qy - 1), (qx, qy + 1)):
                            if (nx, ny) in interior_set:
                                interior_set.remove((nx, ny))
                                stack2.append((nx, ny))
                    interior_components.append(sub)

                # Bucket-fill each enclosed interior subregion.
                tol = int(getattr(cfg, "verify_tolerance", 35))
                tol2 = max(0, tol) ** 2
                # Base similarity should be stricter than general verify tolerance.
                # If verify_tolerance is high, using it here can cause false "no effect"
                # results even when the bucket fill succeeded.
                base_tol = max(8, int(tol * 0.5))
                base_tol2 = max(0, base_tol) ** 2
                settle_s = max(0.0, float(getattr(cfg, "verify_settle_s", 0.05)))

                _tap(cfg.bucket_tool_button_pos, options)
                filled_cells: set[Tuple[int, int]] = set(boundary)

                regions_total += len(interior_components)
                filled_any = False
                spill_detected = False
                for sub in interior_components:
                    if should_stop and should_stop():
                        return
                    if not sub:
                        continue
                    fx, fy = sub[0]
                    _tap(_cell_center(canvas_rect, grid_w, grid_h, fx, fy), options)
                    if settle_s > 0:
                        if not _sleep_with_stop(settle_s, should_stop=should_stop):
                            return

                    ok = True
                    # Spot-check that the click actually filled (cell should not remain base).
                    if base_rgb is not None:
                        cx, cy = _cell_center(canvas_rect, grid_w, grid_h, fx, fy)
                        actual = get_screen_pixel_rgb(cx, cy)
                        if _dist2(actual, base_rgb) <= base_tol2:
                            ok = False

                    if ok:
                        filled_any = True
                        regions_filled += 1
                        filled_cells |= set(sub)

                        if allow_cautious_fill:
                            # Spill check: sample a few neighbor cells just outside the component.
                            # If any are no longer close to base, consider it spill.
                            samples = 0
                            changed = 0
                            # Deterministic sampling: a few boundary pixels spread out.
                            stride = max(1, len(boundary) // 7)
                            for bi in range(0, len(boundary), stride):
                                if samples >= 6:
                                    break
                                bx, by = boundary[bi]
                                # Find an outside neighbor.
                                out_pt = None
                                for nx, ny in ((bx - 1, by), (bx + 1, by), (bx, by - 1), (bx, by + 1)):
                                    if (nx, ny) not in comp_set:
                                        out_pt = (nx, ny)
                                        break
                                if out_pt is None:
                                    continue
                                ox, oy = out_pt
                                cx2, cy2 = _cell_center(canvas_rect, grid_w, grid_h, int(ox), int(oy))
                                try:
                                    a2 = get_screen_pixel_rgb(cx2, cy2)
                                except Exception:
                                    continue
                                samples += 1
                                if base_rgb is not None and _dist2(a2, base_rgb) > base_tol2:
                                    changed += 1
                            if samples > 0 and changed > 0:
                                spill_detected = True
                                if status_cb is not None:
                                    try:
                                        status_cb(
                                            f"Region fill spill detected (outside changed {changed}/{samples}); disabling further region fills"
                                        )
                                    except Exception:
                                        pass
                                break

                _tap(cfg.paint_tool_button_pos, options)

                if filled_any:
                    comps_filled += 1
                    bucketed |= filled_cells
                    if progress_cb:
                        for xx, yy in filled_cells:
                            progress_cb(xx, yy)
                else:
                    # Nothing filled; leave these cells for normal painting.
                    if status_cb is not None:
                        try:
                            status_cb("Region fill warning: fill click(s) had no effect; painting region normally")
                        except Exception:
                            pass
                    # Defensive: ensure we are back on the paint tool before the
                    # subsequent normal-paint pass (avoid accidentally painting
                    # with the bucket tool if the tool switch didn't register).
                    try:
                        _tap(cfg.paint_tool_button_pos, options, extra_delay_s=0.02)
                    except Exception:
                        pass

                if spill_detected:
                    disable_regions_for_rest = True
                    # Don't trust subsequent region fills; leave remaining for normal painting.
                    # We also stop processing more components of this shade.
                    break

            if status_cb is not None:
                try:
                    status_cb(
                        f"Region fill summary: comps={comps_total}, filled={comps_filled}, "
                        f"skipped_small={comps_small}, skipped_thin={comps_no_interior}, skipped_outline={comps_outline_fail}"
                    )
                except Exception:
                    pass
            if status_cb is not None and regions_total > 0:
                try:
                    status_cb(f"Region fill subregions: filled={regions_filled}/{regions_total}")
                except Exception:
                    pass

            if bucketed:
                remaining = [xy for xy in coords if xy not in bucketed]
        elif regions_enabled and regions_min_cells > 0 and len(coords) < regions_min_cells:
            if status_cb is not None:
                try:
                    status_cb(f"Region fill not attempted for {main.name}/{shade.name} ({len(coords)} < {regions_min_cells})")
                except Exception:
                    pass

        # Paint remaining cells for this shade.
        # Prefer horizontal strokes across adjacent pixels (same shade).
        if status_cb is not None:
            try:
                if streaming:
                    status_cb(
                        f"Painting shade: {main.name}/{shade.name} ({len(remaining)} px) … "
                        f"[stream verify lag={lag}]"
                    )
                else:
                    status_cb(f"Painting shade: {main.name}/{shade.name} ({len(remaining)} px) …")
            except Exception:
                pass

        # Defensive: ensure the paint tool is active before doing normal pixel
        # painting. Region bucket-fill uses the bucket tool and, very rarely,
        # the switch back can fail to register, which would cause catastrophic
        # bucket-painting during the normal pass.
        try:
            if cfg.paint_tool_button_pos is not None:
                _tap(cfg.paint_tool_button_pos, options, extra_delay_s=0.02)
        except Exception:
            pass

        # Streaming verify for this shade: verify a few cells behind as we paint,
        # then flush at the end of the shade.
        verify_queue = deque()  # (x, y, t_painted)

        def flush_verify(force: bool = False, max_steps: int = 1) -> None:
            nonlocal verify_i
            if not streaming:
                return
            steps = 0
            while verify_queue and (force or len(verify_queue) > lag):
                if not force and steps >= max(1, int(max_steps)):
                    break
                if should_stop and should_stop():
                    return
                x, y, t_painted = verify_queue[0]
                # Don't verify too early; let the game render the paint.
                if verify_settle_s > 0:
                    now = time.time()
                    ready_in = (float(t_painted) + verify_settle_s) - now
                    if ready_in > 0:
                        if not force:
                            break
                        # Force-flush: wait a tiny amount and keep going.
                        if not _sleep_with_stop(min(0.02, ready_in), should_stop=should_stop):
                            return
                        continue
                x, y, _t = verify_queue.popleft()
                cx, cy = _cell_center(canvas_rect, grid_w, grid_h, int(x), int(y))
                verify_i += 1
                _maybe_emit_verify(verify_cb, (int(x), int(y)), verify_i, every=1)
                try:
                    actual = get_screen_pixel_rgb(cx, cy)
                except Exception:
                    steps += 1
                    continue
                if _dist2(actual, shade.rgb) <= verify_tol2:
                    steps += 1
                    continue
                # Mismatch: we expect the currently-selected shade, so just tap again.
                _tap((cx, cy), options)
                _tap((cx, cy), options, extra_delay_s=0.01)
                if progress_cb:
                    progress_cb(int(x), int(y))
                steps += 1
            if force:
                # If this takes a while, keep the user informed in the status overlay.
                if status_cb is not None:
                    try:
                        status_cb(
                            f"Streaming verify flush: {main.name}/{shade.name} … remaining={len(verify_queue)}"
                        )
                    except Exception:
                        pass
                _maybe_emit_verify(verify_cb, None, 0, every=1)

        def progress_and_stream(x: int, y: int) -> None:
            if progress_cb:
                progress_cb(int(x), int(y))
            if streaming:
                verify_queue.append((int(x), int(y), time.time()))
                backlog = len(verify_queue) - lag
                # Adaptive: if verification is falling behind, verify a bit more per paint click
                # to avoid large end-of-shade flush pauses.
                if backlog > 60:
                    steps = 6
                elif backlog > 30:
                    steps = 3
                else:
                    steps = 1
                flush_verify(force=False, max_steps=steps)

        _paint_coord_runs(
            cfg=cfg,
            canvas_rect=canvas_rect,
            grid_w=grid_w,
            grid_h=grid_h,
            coords=list(remaining),
            options=options,
            progress_cb=progress_and_stream if streaming else progress_cb,
            should_stop=should_stop,
        )

        if streaming:
            flush_verify(force=True)

        # If streaming is enabled, we've already been verifying/repainting as we
        # go. Skip the heavier post-pass verification.
        if not streaming:
            _verify_and_repair_color_group(
                cfg=cfg,
                canvas_rect=canvas_rect,
                grid_w=grid_w,
                grid_h=grid_h,
                main=main,
                shade=shade,
                coords=list(remaining),
                options=options,
                progress_cb=progress_cb,
                should_stop=should_stop,
                status_cb=status_cb,
                verify_cb=verify_cb,
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
