from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Callable, Optional, Tuple

from pynput import mouse

from .screen import get_screen_pixel_rgb


Point = Tuple[int, int]
RGB = Tuple[int, int, int]


@dataclass
class ClickCaptureResult:
    pos: Point
    rgb: RGB


def capture_next_left_click_with_color(
    on_result: Callable[[ClickCaptureResult], None],
    on_cancel: Optional[Callable[[], None]] = None,
) -> None:
    """Capture the next global left mouse click and sample RGB at that point.

    Runs listener in a background thread and calls callbacks on that same thread.
    The GUI should re-enter the Qt thread via signals/queued calls.
    """

    stop_event = threading.Event()

    def _on_click(x, y, button, pressed):
        if stop_event.is_set():
            return False
        if button == mouse.Button.left and pressed:
            rgb = get_screen_pixel_rgb(int(x), int(y))
            stop_event.set()
            on_result(ClickCaptureResult(pos=(int(x), int(y)), rgb=rgb))
            return False
        return True

    def _on_scroll(x, y, dx, dy):
        return not stop_event.is_set()

    def _on_move(x, y):
        return not stop_event.is_set()

    def _on_error(_e):
        if not stop_event.is_set() and on_cancel:
            stop_event.set()
            on_cancel()

    def _run():
        try:
            with mouse.Listener(
                on_click=_on_click,
                on_move=_on_move,
                on_scroll=_on_scroll,
            ) as listener:
                listener.join()
        except Exception:
            _on_error(None)

    t = threading.Thread(target=_run, daemon=True)
    t.start()
