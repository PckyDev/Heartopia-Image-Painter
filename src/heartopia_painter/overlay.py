from __future__ import annotations

import os
from dataclasses import dataclass
from typing import List, Optional, Tuple

from PySide6 import QtCore, QtGui, QtWidgets


@dataclass
class RectResult:
    x: int
    y: int
    w: int
    h: int


@dataclass
class PointResult:
    x: int
    y: int


class RectSelectOverlay(QtWidgets.QWidget):
    """Fullscreen overlay that lets the user drag out a rectangle.

    Optionally draws a translucent preview pixmap inside the current selection
    rectangle to help alignment.
    """

    rectSelected = QtCore.Signal(RectResult)
    cancelled = QtCore.Signal()

    def __init__(self, preview_pixmap: Optional[QtGui.QPixmap] = None, parent=None):
        super().__init__(parent)
        self.setWindowFlags(
            QtCore.Qt.WindowType.FramelessWindowHint
            | QtCore.Qt.WindowType.WindowStaysOnTopHint
            | QtCore.Qt.WindowType.Tool
        )
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setCursor(QtCore.Qt.CursorShape.CrossCursor)
        self.setMouseTracking(True)
        self.setFocusPolicy(QtCore.Qt.FocusPolicy.StrongFocus)

        self._preview = preview_pixmap
        self._drag_start: Optional[QtCore.QPoint] = None
        self._drag_end: Optional[QtCore.QPoint] = None

        self._mouse_pos: Optional[QtCore.QPoint] = None

        # Magnifier / zoom assist (mouse wheel to change zoom)
        self._magnifier_zoom: int = 1  # 1 disables
        self._magnifier_src_px: int = 18  # half-size in pixels of the sampled region
        self._magnifier_box_px: int = 170  # rendered box size

        # Cover all screens
        geom = QtCore.QRect()
        for screen in QtWidgets.QApplication.screens():
            geom = geom.united(screen.geometry())
        self.setGeometry(geom)

        # For mapping local <-> global coordinates
        self._global_origin = geom.topLeft()

    def start(self):
        self._drag_start = None
        self._drag_end = None
        self._mouse_pos = None
        self.show()
        self.raise_()
        self.activateWindow()
        self.setFocus()

    def keyPressEvent(self, event: QtGui.QKeyEvent):
        if event.key() == QtCore.Qt.Key.Key_Escape:
            self.hide()
            self.cancelled.emit()
            return
        super().keyPressEvent(event)

    def mousePressEvent(self, event: QtGui.QMouseEvent):
        if event.button() == QtCore.Qt.MouseButton.LeftButton:
            # Use widget-local coordinates; global coords can be negative on multi-monitor.
            self._drag_start = event.position().toPoint()
            self._drag_end = self._drag_start
            self._mouse_pos = self._drag_end
            self.update()

    def mouseMoveEvent(self, event: QtGui.QMouseEvent):
        self._mouse_pos = event.position().toPoint()
        if self._drag_start is not None:
            self._drag_end = event.position().toPoint()
            self.update()
        else:
            # Still repaint to update magnifier position.
            if self._magnifier_zoom > 1:
                self.update()

    def wheelEvent(self, event: QtGui.QWheelEvent):
        # Mouse wheel adjusts magnifier zoom for precise alignment.
        # Typical delta is 120 per notch.
        delta = event.angleDelta().y()
        if delta == 0:
            return
        step = 1 if delta > 0 else -1
        self._magnifier_zoom = max(1, min(12, self._magnifier_zoom + step))
        self.update()

    def mouseReleaseEvent(self, event: QtGui.QMouseEvent):
        if event.button() == QtCore.Qt.MouseButton.LeftButton and self._drag_start is not None:
            self._drag_end = event.position().toPoint()
            rect = self._current_rect()
            if rect is not None and rect.width() > 5 and rect.height() > 5:
                self.hide()
                # Convert back to global screen coordinates for downstream clicking.
                global_rect = rect.translated(self._global_origin)
                self.rectSelected.emit(
                    RectResult(
                        x=global_rect.x(),
                        y=global_rect.y(),
                        w=global_rect.width(),
                        h=global_rect.height(),
                    )
                )
            else:
                self.update()

    def _current_rect(self) -> Optional[QtCore.QRect]:
        if self._drag_start is None or self._drag_end is None:
            return None
        x1, y1 = self._drag_start.x(), self._drag_start.y()
        x2, y2 = self._drag_end.x(), self._drag_end.y()
        x = min(x1, x2)
        y = min(y1, y2)
        w = abs(x2 - x1)
        h = abs(y2 - y1)
        return QtCore.QRect(x, y, w, h)

    def paintEvent(self, _event: QtGui.QPaintEvent):
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)

        # Dim the whole screen
        dim_alpha = 90 if self._magnifier_zoom <= 1 else 70
        dim = QtGui.QColor(0, 0, 0, dim_alpha)
        painter.fillRect(self.rect(), dim)

        # Magnifier works even before dragging
        if self._magnifier_zoom > 1 and self._mouse_pos is not None:
            local_pt = self._mouse_pos
            global_pt = local_pt + self._global_origin
            screen = QtGui.QGuiApplication.screenAt(global_pt) or QtGui.QGuiApplication.primaryScreen()
            if screen is not None:
                sgeo = screen.geometry()
                sx = int(global_pt.x() - sgeo.x())
                sy = int(global_pt.y() - sgeo.y())
                half = int(self._magnifier_src_px)
                grab = screen.grabWindow(0, sx - half, sy - half, half * 2, half * 2)

                target = QtCore.QSize(self._magnifier_box_px, self._magnifier_box_px)
                zoomed = grab.scaled(
                    target,
                    QtCore.Qt.AspectRatioMode.IgnoreAspectRatio,
                    QtCore.Qt.TransformationMode.FastTransformation,
                )

                offset = 22
                bx = local_pt.x() + offset
                by = local_pt.y() + offset
                if bx + self._magnifier_box_px + 6 > self.width():
                    bx = local_pt.x() - offset - self._magnifier_box_px
                if by + self._magnifier_box_px + 26 > self.height():
                    by = local_pt.y() - offset - self._magnifier_box_px
                box = QtCore.QRect(bx, by, self._magnifier_box_px, self._magnifier_box_px)

                painter.setPen(QtCore.Qt.PenStyle.NoPen)
                painter.setBrush(QtGui.QColor(0, 0, 0, 170))
                painter.drawRoundedRect(box.adjusted(-6, -22, 6, 6), 8, 8)

                painter.drawPixmap(box.topLeft(), zoomed)
                pen = QtGui.QPen(QtGui.QColor(255, 255, 255, 220))
                pen.setWidth(2)
                painter.setPen(pen)
                painter.setBrush(QtCore.Qt.BrushStyle.NoBrush)
                painter.drawRect(box)

                cx = box.center().x()
                cy = box.center().y()
                pen2 = QtGui.QPen(QtGui.QColor(0, 200, 255, 230))
                pen2.setWidth(2)
                painter.setPen(pen2)
                painter.drawLine(cx - 10, cy, cx + 10, cy)
                painter.drawLine(cx, cy - 10, cx, cy + 10)

                painter.setPen(QtGui.QColor(255, 255, 255, 235))
                painter.drawText(
                    QtCore.QRect(box.x() - 6, box.y() - 22, box.width() + 12, 18),
                    QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignVCenter,
                    f"Zoom {self._magnifier_zoom}x",
                )

        rect = self._current_rect()
        if rect is None:
            # Helper text when not dragging yet
            painter.setPen(QtGui.QColor(255, 255, 255, 235))
            painter.drawText(
                QtCore.QRect(20, 20, 680, 40),
                QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignVCenter,
                f"Drag to select canvas (ESC to cancel, scroll to zoom: {self._magnifier_zoom}x)",
            )
            return

        # Clear selection area a bit
        painter.setCompositionMode(QtGui.QPainter.CompositionMode.CompositionMode_Clear)
        painter.fillRect(rect, QtGui.QColor(0, 0, 0, 0))
        painter.setCompositionMode(QtGui.QPainter.CompositionMode.CompositionMode_SourceOver)

        # Very subtle selection tint (keep edges clean for alignment)
        painter.fillRect(rect, QtGui.QColor(0, 200, 255, 12))

        # Preview image inside selection
        if self._preview is not None and not self._preview.isNull():
            scaled = self._preview.scaled(
                rect.size(),
                QtCore.Qt.AspectRatioMode.IgnoreAspectRatio,
                QtCore.Qt.TransformationMode.SmoothTransformation,
            )
            painter.setOpacity(0.40)
            painter.drawPixmap(rect.topLeft(), scaled)
            painter.setOpacity(1.0)

        # Helper text
        painter.setPen(QtGui.QColor(255, 255, 255, 230))
        painter.drawText(
            rect.adjusted(6, 6, -6, -6),
            QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignTop,
            f"{rect.width()}x{rect.height()}  (ESC to cancel, scroll to zoom: {self._magnifier_zoom}x)",
        )

        # Magnifier is drawn above (also when not dragging)


class PointSelectOverlay(QtWidgets.QWidget):
    """Fullscreen overlay to pick a single point on screen.

    This is used instead of global mouse hooks (which can be flaky on some setups).
    """

    pointSelected = QtCore.Signal(PointResult)
    cancelled = QtCore.Signal()

    def __init__(self, instruction: str = "Click to select (ESC to cancel)", parent=None):
        super().__init__(parent)
        self.setWindowFlags(
            QtCore.Qt.WindowType.FramelessWindowHint
            | QtCore.Qt.WindowType.WindowStaysOnTopHint
            | QtCore.Qt.WindowType.Tool
        )
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setCursor(QtCore.Qt.CursorShape.CrossCursor)

        self._instruction = instruction
        self._mouse_pos: Optional[QtCore.QPoint] = None

        geom = QtCore.QRect()
        for screen in QtWidgets.QApplication.screens():
            geom = geom.united(screen.geometry())
        self.setGeometry(geom)
        self._global_origin = geom.topLeft()

    def start(self):
        self._mouse_pos = None
        self.show()
        self.raise_()
        self.activateWindow()

    def keyPressEvent(self, event: QtGui.QKeyEvent):
        if event.key() == QtCore.Qt.Key.Key_Escape:
            self.hide()
            self.cancelled.emit()
            return
        super().keyPressEvent(event)

    def mouseMoveEvent(self, event: QtGui.QMouseEvent):
        self._mouse_pos = event.position().toPoint()
        self.update()

    def mousePressEvent(self, event: QtGui.QMouseEvent):
        if event.button() == QtCore.Qt.MouseButton.LeftButton:
            local = event.position().toPoint()
            global_pt = local + self._global_origin
            self.hide()
            self.pointSelected.emit(PointResult(x=global_pt.x(), y=global_pt.y()))
            return
        if event.button() == QtCore.Qt.MouseButton.RightButton:
            self.hide()
            self.cancelled.emit()
            return

    def paintEvent(self, _event: QtGui.QPaintEvent):
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)

        painter.fillRect(self.rect(), QtGui.QColor(0, 0, 0, 70))

        # Instruction box
        box = QtCore.QRect(20, 20, 520, 64)
        painter.setPen(QtCore.Qt.PenStyle.NoPen)
        painter.setBrush(QtGui.QColor(0, 0, 0, 160))
        painter.drawRoundedRect(box, 8, 8)
        painter.setPen(QtGui.QColor(255, 255, 255, 235))
        painter.drawText(
            box.adjusted(12, 10, -12, -10),
            QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignVCenter,
            self._instruction,
        )

        # Crosshair
        if self._mouse_pos is not None:
            p = self._mouse_pos
            pen = QtGui.QPen(QtGui.QColor(0, 200, 255, 230))
            pen.setWidth(2)
            painter.setPen(pen)
            painter.drawLine(p.x() - 15, p.y(), p.x() + 15, p.y())
            painter.drawLine(p.x(), p.y() - 15, p.x(), p.y() + 15)


@dataclass
class Marker:
    label: str
    pos: Tuple[int, int]
    color: Tuple[int, int, int] = (0, 200, 255)


class MarkersOverlay(QtWidgets.QWidget):
    """Fullscreen overlay that draws labeled markers at global screen coords.

    Intended as a quick visual guide for aligning the game window to previously
    captured UI button positions.

    Defaults to click-through so you can drag the game window while it is shown.
    """

    def __init__(
        self,
        markers: List[Marker],
        title: str = "Markers",
        duration_ms: int = 15000,
        parent=None,
    ):
        super().__init__(parent)
        self.setWindowFlags(
            QtCore.Qt.WindowType.FramelessWindowHint
            | QtCore.Qt.WindowType.WindowStaysOnTopHint
            | QtCore.Qt.WindowType.Tool
        )
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_NoSystemBackground, True)
        # Allow dragging the underlying game window while this is visible.
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setFocusPolicy(QtCore.Qt.FocusPolicy.StrongFocus)

        self._markers = list(markers)
        self._title = str(title)
        self._duration_ms = int(duration_ms)
        self._hide_timer: Optional[QtCore.QTimer] = None

        geom = QtCore.QRect()
        for screen in QtWidgets.QApplication.screens():
            geom = geom.united(screen.geometry())
        self.setGeometry(geom)
        self._global_origin = geom.topLeft()

    def start(self) -> None:
        if self._hide_timer is None:
            self._hide_timer = QtCore.QTimer(self)
            self._hide_timer.setSingleShot(True)
            self._hide_timer.timeout.connect(self.hide)
        self._hide_timer.start(max(250, self._duration_ms))

        self.show()
        self.raise_()
        self.activateWindow()
        self.setFocus()
        self.update()

    def keyPressEvent(self, event: QtGui.QKeyEvent):
        if event.key() == QtCore.Qt.Key.Key_Escape:
            self.hide()
            return
        super().keyPressEvent(event)

    def paintEvent(self, _event: QtGui.QPaintEvent):
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)

        # Slight dim for visibility without hiding the game.
        painter.fillRect(self.rect(), QtGui.QColor(0, 0, 0, 40))

        # Header
        header = QtCore.QRect(20, 20, 720, 70)
        painter.setPen(QtCore.Qt.PenStyle.NoPen)
        painter.setBrush(QtGui.QColor(0, 0, 0, 165))
        painter.drawRoundedRect(header, 10, 10)
        painter.setPen(QtGui.QColor(255, 255, 255, 235))
        painter.drawText(
            header.adjusted(14, 10, -14, -10),
            QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignVCenter,
            f"{self._title}\nAuto-hides in {max(1, int(round(self._duration_ms / 1000.0)))}s (overlay is click-through)",
        )

        # Markers
        radius = 12
        for m in self._markers:
            gx, gy = int(m.pos[0]), int(m.pos[1])
            local = QtCore.QPoint(gx, gy) - self._global_origin

            col = QtGui.QColor(int(m.color[0]), int(m.color[1]), int(m.color[2]), 255)
            pen = QtGui.QPen(col)
            pen.setWidth(3)
            painter.setPen(pen)
            painter.setBrush(QtGui.QColor(col.red(), col.green(), col.blue(), 30))
            painter.drawEllipse(local, radius, radius)

            # Crosshair
            painter.drawLine(local.x() - 18, local.y(), local.x() + 18, local.y())
            painter.drawLine(local.x(), local.y() - 18, local.x(), local.y() + 18)

            # Label background + text
            label = str(m.label)
            fm = painter.fontMetrics()
            tw = fm.horizontalAdvance(label)
            th = fm.height()
            pad = 6
            box = QtCore.QRect(local.x() + 18, local.y() - th, tw + pad * 2, th + pad)
            painter.setPen(QtCore.Qt.PenStyle.NoPen)
            painter.setBrush(QtGui.QColor(0, 0, 0, 175))
            painter.drawRoundedRect(box, 6, 6)
            painter.setPen(QtGui.QColor(255, 255, 255, 235))
            painter.drawText(box.adjusted(pad, 0, -pad, 0), QtCore.Qt.AlignmentFlag.AlignVCenter, label)


class StatusOverlay(QtWidgets.QWidget):
    # Small, click-through overlay for live painting status.
    # Not fullscreen to avoid spanning monitors.

    def __init__(self, title: str = "Painter status", parent=None):
        super().__init__(parent)
        self.setWindowFlags(
            QtCore.Qt.WindowType.FramelessWindowHint
            | QtCore.Qt.WindowType.WindowStaysOnTopHint
            | QtCore.Qt.WindowType.Tool
            | QtCore.Qt.WindowType.WindowDoesNotAcceptFocus
        )
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setFocusPolicy(QtCore.Qt.FocusPolicy.NoFocus)

        self._title = str(title)
        self._status: str = "Idle"
        self._anchor_rect: Optional[QtCore.QRect] = None

        # Replica canvas state (grid coords)
        self._grid_w: int = 0
        self._grid_h: int = 0
        self._base_img: Optional[QtGui.QImage] = None
        self._painted_img: Optional[QtGui.QImage] = None
        self._painted_mask: Optional[bytearray] = None
        self._painted_count: int = 0
        self._paint_cursor: Optional[Tuple[int, int]] = None
        self._verify_cursor: Optional[Tuple[int, int]] = None

        self._update_timer: Optional[QtCore.QTimer] = None

        # Default size; includes a small replica canvas.
        self.resize(860, 280)

        self.hide()

    def start(self) -> None:
        self.show()
        self.raise_()
        self._apply_platform_clickthrough()
        self.update()

    def stop(self) -> None:
        self.hide()

    def set_status(self, status: str) -> None:
        self._status = str(status)
        self._request_update()

    def _request_update(self) -> None:
        if not self.isVisible():
            return
        if self._update_timer is None:
            self._update_timer = QtCore.QTimer(self)
            self._update_timer.setSingleShot(True)
            self._update_timer.timeout.connect(self.update)
        if not self._update_timer.isActive():
            self._update_timer.start(16)

    def set_grid(self, w: int, h: int, pixels: List[Tuple[int, int, int]]) -> None:
        self._grid_w = max(0, int(w))
        self._grid_h = max(0, int(h))
        if self._grid_w <= 0 or self._grid_h <= 0:
            self._base_img = None
            self._painted_img = None
            self._painted_mask = None
            self._painted_count = 0
            return

        base = QtGui.QImage(self._grid_w, self._grid_h, QtGui.QImage.Format.Format_RGB32)
        # Fill base from the resized image pixels (row-major)
        i = 0
        for yy in range(self._grid_h):
            for xx in range(self._grid_w):
                if i < len(pixels):
                    r, g, b = pixels[i]
                else:
                    r, g, b = (255, 255, 255)
                base.setPixel(xx, yy, QtGui.QColor(int(r), int(g), int(b)).rgb())
                i += 1

        painted = QtGui.QImage(self._grid_w, self._grid_h, QtGui.QImage.Format.Format_ARGB32)
        painted.fill(QtCore.Qt.GlobalColor.transparent)

        self._base_img = base
        self._painted_img = painted
        self._painted_mask = bytearray(self._grid_w * self._grid_h)
        self._painted_count = 0
        self._paint_cursor = None
        self._verify_cursor = None
        self._request_update()

    def mark_painted(self, x: int, y: int) -> None:
        if self._base_img is None or self._painted_img is None or self._painted_mask is None:
            return
        xx, yy = int(x), int(y)
        if xx < 0 or yy < 0 or xx >= self._grid_w or yy >= self._grid_h:
            return
        idx = yy * self._grid_w + xx
        if self._painted_mask[idx]:
            return
        self._painted_mask[idx] = 1
        self._painted_count += 1
        self._painted_img.setPixel(xx, yy, self._base_img.pixel(xx, yy))
        self._paint_cursor = (xx, yy)
        self._request_update()

    def set_verify_cursor(self, x: int, y: int) -> None:
        xx, yy = int(x), int(y)
        if xx < 0 or yy < 0:
            self._verify_cursor = None
        else:
            self._verify_cursor = (xx, yy)
        self._request_update()

    def set_anchor_rect(self, rect: Optional[Tuple[int, int, int, int]]) -> None:
        if rect is None:
            self._anchor_rect = None
            return
        l, t, r, b = (int(rect[0]), int(rect[1]), int(rect[2]), int(rect[3]))
        self._anchor_rect = QtCore.QRect(l, t, max(0, r - l), max(0, b - t))
        self._reposition_to_anchor()

    def _reposition_to_anchor(self) -> None:
        if self._anchor_rect is None:
            return
        self.move(self._anchor_rect.left() + 16, self._anchor_rect.top() + 16)

    def _apply_platform_clickthrough(self) -> None:
        if os.name != "nt":
            return
        try:
            import ctypes

            GWL_EXSTYLE = -20
            WS_EX_TRANSPARENT = 0x00000020
            WS_EX_LAYERED = 0x00080000
            WS_EX_NOACTIVATE = 0x08000000

            user32 = ctypes.windll.user32
            hwnd = int(self.winId())
            if hwnd == 0:
                return

            get_long = getattr(user32, "GetWindowLongPtrW", None) or user32.GetWindowLongW
            set_long = getattr(user32, "SetWindowLongPtrW", None) or user32.SetWindowLongW
            ex = int(get_long(hwnd, GWL_EXSTYLE))
            ex |= WS_EX_TRANSPARENT | WS_EX_LAYERED | WS_EX_NOACTIVATE
            set_long(hwnd, GWL_EXSTYLE, ex)
        except Exception:
            pass

    def paintEvent(self, _event: QtGui.QPaintEvent):
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)

        self._reposition_to_anchor()

        panel = self.rect()
        painter.setPen(QtCore.Qt.PenStyle.NoPen)
        painter.setBrush(QtGui.QColor(0, 0, 0, 185))
        painter.drawRoundedRect(panel, 10, 10)

        # Layout: left text, right replica canvas
        left = panel.adjusted(14, 10, -14, -10)
        canvas_box = QtCore.QRect(panel.right() - 248, panel.top() + 14, 234, 234)
        text_box = QtCore.QRect(left.left(), left.top(), canvas_box.left() - left.left() - 12, left.height())

        painter.setPen(QtGui.QColor(255, 255, 255, 240))
        painted_txt = ""
        if self._grid_w > 0 and self._grid_h > 0:
            painted_txt = f"\nPainted: {self._painted_count}/{self._grid_w * self._grid_h}"
        painter.drawText(
            text_box,
            QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignTop,
            f"{self._title}\n{self._status}{painted_txt}",
        )

        # Replica canvas
        painter.setPen(QtCore.Qt.PenStyle.NoPen)
        painter.setBrush(QtGui.QColor(20, 20, 20, 220))
        painter.drawRoundedRect(canvas_box, 8, 8)

        if self._base_img is not None and self._painted_img is not None and self._grid_w > 0 and self._grid_h > 0:
            inner = canvas_box.adjusted(8, 8, -8, -8)
            # Keep square aspect
            size = min(inner.width(), inner.height())
            inner = QtCore.QRect(inner.left(), inner.top(), size, size)

            base_scaled = self._base_img.scaled(inner.size(), QtCore.Qt.AspectRatioMode.IgnoreAspectRatio, QtCore.Qt.TransformationMode.FastTransformation)
            painted_scaled = self._painted_img.scaled(inner.size(), QtCore.Qt.AspectRatioMode.IgnoreAspectRatio, QtCore.Qt.TransformationMode.FastTransformation)

            painter.save()
            painter.setOpacity(0.22)
            painter.drawImage(inner, base_scaled)
            painter.restore()

            painter.drawImage(inner, painted_scaled)

            # Draw paint/verify cursors in grid space
            cw = inner.width() / float(self._grid_w)
            ch = inner.height() / float(self._grid_h)

            def cell_rect(gx: int, gy: int) -> QtCore.QRectF:
                return QtCore.QRectF(
                    inner.left() + gx * cw,
                    inner.top() + gy * ch,
                    cw,
                    ch,
                )

            if self._paint_cursor is not None:
                gx, gy = self._paint_cursor
                pen = QtGui.QPen(QtGui.QColor(80, 200, 255, 255))
                pen.setWidth(2)
                painter.setPen(pen)
                painter.setBrush(QtCore.Qt.BrushStyle.NoBrush)
                painter.drawRect(cell_rect(gx, gy))

            if self._verify_cursor is not None:
                gx, gy = self._verify_cursor
                pen = QtGui.QPen(QtGui.QColor(255, 230, 80, 255))
                pen.setWidth(2)
                painter.setPen(pen)
                painter.setBrush(QtCore.Qt.BrushStyle.NoBrush)
                painter.drawRect(cell_rect(gx, gy))




