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

        self._preview = preview_pixmap
        self._drag_start: Optional[QtCore.QPoint] = None
        self._drag_end: Optional[QtCore.QPoint] = None

        # Cover all screens
        geom = QtCore.QRect()
        for screen in QtWidgets.QApplication.screens():
            geom = geom.united(screen.geometry())
        self.setGeometry(geom)

    def start(self):
        self._drag_start = None
        self._drag_end = None
        self.show()
        self.raise_()
        self.activateWindow()

    def keyPressEvent(self, event: QtGui.QKeyEvent):
        if event.key() == QtCore.Qt.Key.Key_Escape:
            self.hide()
            self.cancelled.emit()
            return
        super().keyPressEvent(event)

    def mousePressEvent(self, event: QtGui.QMouseEvent):
        if event.button() == QtCore.Qt.MouseButton.LeftButton:
            self._drag_start = event.globalPosition().toPoint()
            self._drag_end = self._drag_start
            self.update()

    def mouseMoveEvent(self, event: QtGui.QMouseEvent):
        if self._drag_start is not None:
            self._drag_end = event.globalPosition().toPoint()
            self.update()

    def mouseReleaseEvent(self, event: QtGui.QMouseEvent):
        if event.button() == QtCore.Qt.MouseButton.LeftButton and self._drag_start is not None:
            self._drag_end = event.globalPosition().toPoint()
            rect = self._current_rect()
            if rect is not None and rect.width() > 5 and rect.height() > 5:
                self.hide()
                self.rectSelected.emit(
                    RectResult(x=rect.x(), y=rect.y(), w=rect.width(), h=rect.height())
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
        dim = QtGui.QColor(0, 0, 0, 90)
        painter.fillRect(self.rect(), dim)

        rect = self._current_rect()
        if rect is None:
            return

        # Clear selection area a bit
        painter.setCompositionMode(QtGui.QPainter.CompositionMode.CompositionMode_Clear)
        painter.fillRect(rect, QtGui.QColor(0, 0, 0, 0))
        painter.setCompositionMode(QtGui.QPainter.CompositionMode.CompositionMode_SourceOver)

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
            f"{rect.width()}x{rect.height()}  (ESC to cancel)",
        )
