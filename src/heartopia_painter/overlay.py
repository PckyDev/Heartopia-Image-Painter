from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

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

        rect = self._current_rect()
        if rect is None:
            return

        # Clear selection area a bit
        painter.setCompositionMode(QtGui.QPainter.CompositionMode.CompositionMode_Clear)
        painter.fillRect(rect, QtGui.QColor(0, 0, 0, 0))
        painter.setCompositionMode(QtGui.QPainter.CompositionMode.CompositionMode_SourceOver)

        # Visible selection fill (helps UX even if transparency behaves oddly)
        painter.fillRect(rect, QtGui.QColor(0, 200, 255, 40))

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

        # Selection border
        pen = QtGui.QPen(QtGui.QColor(0, 200, 255, 230))
        pen.setWidth(2)
        painter.setPen(pen)
        painter.setBrush(QtCore.Qt.BrushStyle.NoBrush)
        painter.drawRect(rect)

        # Helper text
        painter.setPen(QtGui.QColor(255, 255, 255, 230))
        painter.drawText(
            rect.adjusted(6, 6, -6, -6),
            QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignTop,
            f"{rect.width()}x{rect.height()}  (ESC to cancel, scroll to zoom: {self._magnifier_zoom}x)",
        )

        # Magnifier (if enabled)
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

                # Scale up with fast/nearest transform for crisp pixel edges
                target = QtCore.QSize(self._magnifier_box_px, self._magnifier_box_px)
                zoomed = grab.scaled(
                    target,
                    QtCore.Qt.AspectRatioMode.IgnoreAspectRatio,
                    QtCore.Qt.TransformationMode.FastTransformation,
                )

                # Place box near cursor but keep inside overlay bounds
                offset = 22
                bx = local_pt.x() + offset
                by = local_pt.y() + offset
                if bx + self._magnifier_box_px + 6 > self.width():
                    bx = local_pt.x() - offset - self._magnifier_box_px
                if by + self._magnifier_box_px + 26 > self.height():
                    by = local_pt.y() - offset - self._magnifier_box_px
                box = QtCore.QRect(bx, by, self._magnifier_box_px, self._magnifier_box_px)

                # Background + border
                painter.setPen(QtCore.Qt.PenStyle.NoPen)
                painter.setBrush(QtGui.QColor(0, 0, 0, 170))
                painter.drawRoundedRect(box.adjusted(-6, -22, 6, 6), 8, 8)

                painter.drawPixmap(box.topLeft(), zoomed)
                pen = QtGui.QPen(QtGui.QColor(255, 255, 255, 220))
                pen.setWidth(2)
                painter.setPen(pen)
                painter.setBrush(QtCore.Qt.BrushStyle.NoBrush)
                painter.drawRect(box)

                # Crosshair at center
                cx = box.center().x()
                cy = box.center().y()
                pen2 = QtGui.QPen(QtGui.QColor(0, 200, 255, 230))
                pen2.setWidth(2)
                painter.setPen(pen2)
                painter.drawLine(cx - 10, cy, cx + 10, cy)
                painter.drawLine(cx, cy - 10, cx, cy + 10)

                # Title
                painter.setPen(QtGui.QColor(255, 255, 255, 235))
                painter.drawText(
                    QtCore.QRect(box.x() - 6, box.y() - 22, box.width() + 12, 18),
                    QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignVCenter,
                    f"Zoom {self._magnifier_zoom}x",
                )


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

