from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple

from PySide6 import QtCore, QtGui, QtWidgets

from .capture import ClickCaptureResult, capture_next_left_click_with_color
from .config import AppConfig, MainColor, ShadeButton, default_config_path, load_config, save_config
from .image_processing import PixelGrid, load_and_resize_to_grid
from .overlay import RectResult, RectSelectOverlay
from .paint import PainterOptions, paint_grid


CANVAS_PRESETS = {
    "1:1 (30x30)": (30, 30),
}


@dataclass
class LoadedImage:
    path: str
    grid: PixelGrid


class WorkerSignals(QtCore.QObject):
    progress = QtCore.Signal(int, int)
    finished = QtCore.Signal()
    error = QtCore.Signal(str)


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Heartopia Image Painter")

        self._config_path = default_config_path()
        self._cfg = load_config(self._config_path)

        self._loaded: Optional[LoadedImage] = None
        self._canvas_rect: Optional[Tuple[int, int, int, int]] = None

        self._overlay: Optional[RectSelectOverlay] = None

        self._stop_flag = False

        self._build_ui()
        self._refresh_config_view()

    def _build_ui(self):
        root = QtWidgets.QWidget()
        self.setCentralWidget(root)
        layout = QtWidgets.QVBoxLayout(root)

        # Image load
        row1 = QtWidgets.QHBoxLayout()
        self.btn_load = QtWidgets.QPushButton("Import image…")
        self.lbl_image = QtWidgets.QLabel("No image loaded")
        self.lbl_image.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
        row1.addWidget(self.btn_load)
        row1.addWidget(self.lbl_image, 1)
        layout.addLayout(row1)

        # Preset
        row2 = QtWidgets.QHBoxLayout()
        row2.addWidget(QtWidgets.QLabel("Canvas preset:"))
        self.cbo_preset = QtWidgets.QComboBox()
        self.cbo_preset.addItems(list(CANVAS_PRESETS.keys()))
        row2.addWidget(self.cbo_preset, 1)
        self.btn_select_canvas = QtWidgets.QPushButton("Select canvas area…")
        row2.addWidget(self.btn_select_canvas)
        layout.addLayout(row2)

        self.lbl_canvas = QtWidgets.QLabel("Canvas: not selected")
        layout.addWidget(self.lbl_canvas)

        # Config
        cfg_group = QtWidgets.QGroupBox("Color configuration")
        cfg_layout = QtWidgets.QVBoxLayout(cfg_group)

        row_cfg1 = QtWidgets.QHBoxLayout()
        self.btn_set_shades_button = QtWidgets.QPushButton("Set shades-panel button")
        self.btn_set_back_button = QtWidgets.QPushButton("Set back button")
        row_cfg1.addWidget(self.btn_set_shades_button)
        row_cfg1.addWidget(self.btn_set_back_button)
        cfg_layout.addLayout(row_cfg1)

        row_cfg2 = QtWidgets.QHBoxLayout()
        self.btn_add_color = QtWidgets.QPushButton("Setup new color…")
        self.btn_remove_color = QtWidgets.QPushButton("Remove selected")
        row_cfg2.addWidget(self.btn_add_color)
        row_cfg2.addWidget(self.btn_remove_color)
        cfg_layout.addLayout(row_cfg2)

        self.lst_colors = QtWidgets.QListWidget()
        cfg_layout.addWidget(self.lst_colors)

        self.lbl_cfg_hint = QtWidgets.QLabel(
            "Tip: Move mouse to top-left to abort painting (PyAutoGUI failsafe)."
        )
        self.lbl_cfg_hint.setWordWrap(True)
        cfg_layout.addWidget(self.lbl_cfg_hint)

        layout.addWidget(cfg_group)

        # Paint
        paint_group = QtWidgets.QGroupBox("Paint")
        paint_layout = QtWidgets.QVBoxLayout(paint_group)

        rowp = QtWidgets.QHBoxLayout()
        self.btn_paint = QtWidgets.QPushButton("Paint now")
        self.btn_stop = QtWidgets.QPushButton("Stop")
        self.btn_stop.setEnabled(False)
        rowp.addWidget(self.btn_paint)
        rowp.addWidget(self.btn_stop)
        paint_layout.addLayout(rowp)

        self.progress = QtWidgets.QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        paint_layout.addWidget(self.progress)

        layout.addWidget(paint_group)

        # Wiring
        self.btn_load.clicked.connect(self._on_load)
        self.btn_select_canvas.clicked.connect(self._on_select_canvas)
        self.btn_set_shades_button.clicked.connect(lambda: self._capture_global_button("shades"))
        self.btn_set_back_button.clicked.connect(lambda: self._capture_global_button("back"))
        self.btn_add_color.clicked.connect(self._on_setup_new_color)
        self.btn_remove_color.clicked.connect(self._on_remove_selected_color)
        self.btn_paint.clicked.connect(self._on_paint)
        self.btn_stop.clicked.connect(self._on_stop)

    def _refresh_config_view(self):
        self.lst_colors.clear()
        for mc in self._cfg.main_colors:
            self.lst_colors.addItem(f"{mc.name}  ({len(mc.shades)} shades)")

        if self._canvas_rect is None:
            self.lbl_canvas.setText("Canvas: not selected")
        else:
            x, y, w, h = self._canvas_rect
            self.lbl_canvas.setText(f"Canvas: x={x}, y={y}, w={w}, h={h}")

    def _save_cfg(self):
        save_config(self._config_path, self._cfg)

    def _capture_click_async(self, title: str, message: str, apply_capture):
        """Shows a prompt, then captures the next left-click + sampled RGB."""
        QtWidgets.QMessageBox.information(self, title, message)

        def on_result(res: ClickCaptureResult):
            QtCore.QMetaObject.invokeMethod(
                self,
                lambda: apply_capture(res),
                QtCore.Qt.ConnectionType.QueuedConnection,
            )

        capture_next_left_click_with_color(on_result=on_result)

    def _selected_preset_wh(self) -> Tuple[int, int]:
        key = self.cbo_preset.currentText()
        return CANVAS_PRESETS.get(key, (30, 30))

    def _on_load(self):
        w, h = self._selected_preset_wh()
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self,
            "Select image",
            str(Path.home()),
            "Images (*.png *.jpg *.jpeg *.bmp *.webp);;All Files (*.*)",
        )
        if not path:
            return
        try:
            grid = load_and_resize_to_grid(path, w=w, h=h)
        except Exception as e:
            QtWidgets.QMessageBox.critical(self, "Import failed", str(e))
            return

        self._loaded = LoadedImage(path=path, grid=grid)
        self.lbl_image.setText(f"Loaded: {path} ({w}x{h})")

    def _on_select_canvas(self):
        if self._loaded is None:
            QtWidgets.QMessageBox.information(self, "Select image", "Import an image first.")
            return

        # Build translucent preview pixmap from the resized image
        img = QtGui.QImage(self._loaded.path)
        if img.isNull():
            # fallback: no preview
            pix = None
        else:
            pix = QtGui.QPixmap.fromImage(img)

        self._overlay = RectSelectOverlay(preview_pixmap=pix)
        self._overlay.rectSelected.connect(self._on_canvas_rect_selected)
        self._overlay.cancelled.connect(lambda: None)
        self._overlay.start()

    def _on_canvas_rect_selected(self, r: RectResult):
        # Use selection as canvas rect (we'll refine snapping later)
        self._canvas_rect = (r.x, r.y, r.w, r.h)
        self._refresh_config_view()

    def _capture_global_button(self, which: str):
        QtWidgets.QMessageBox.information(
            self,
            "Capture",
            "After closing this dialog, click the button location in-game.",
        )

        def on_result(res: ClickCaptureResult):
            QtCore.QMetaObject.invokeMethod(
                self,
                lambda: self._apply_global_button_capture(which, res),
                QtCore.Qt.ConnectionType.QueuedConnection,
            )

        capture_next_left_click_with_color(on_result=on_result)

    def _apply_global_button_capture(self, which: str, res: ClickCaptureResult):
        if which == "shades":
            self._cfg.shades_panel_button_pos = res.pos
        elif which == "back":
            self._cfg.back_button_pos = res.pos
        self._save_cfg()

    def _on_setup_new_color(self):
        name, ok = QtWidgets.QInputDialog.getText(self, "New color", "Color name:")
        if not ok or not name.strip():
            return
        name = name.strip()

        # Ensure global buttons exist (shades panel + back). Capture them as part of the wizard.
        self._wizard_ensure_globals_then_continue(name)

    def _wizard_ensure_globals_then_continue(self, color_name: str):
        if self._cfg.shades_panel_button_pos is None:
            self._capture_click_async(
                "Setup new color",
                "Before adding colors, we need the SHADES-PANEL button location.\n\n"
                "After closing this dialog, click the button that opens the shades panel.",
                lambda res: self._wizard_set_global_then_continue(color_name, "shades", res),
            )
            return
        if self._cfg.back_button_pos is None:
            self._capture_click_async(
                "Setup new color",
                "Before adding colors, we need the BACK button location.\n\n"
                "After closing this dialog, click the back button (returns to main colors).",
                lambda res: self._wizard_set_global_then_continue(color_name, "back", res),
            )
            return

        self._wizard_capture_main_color(color_name)

    def _wizard_set_global_then_continue(self, color_name: str, which: str, res: ClickCaptureResult):
        if which == "shades":
            self._cfg.shades_panel_button_pos = res.pos
        elif which == "back":
            self._cfg.back_button_pos = res.pos
        self._save_cfg()
        # Continue capturing any remaining globals, then proceed.
        self._wizard_ensure_globals_then_continue(color_name)

    def _wizard_capture_main_color(self, name: str):
        self._capture_click_async(
            "Setup new color",
            "Step 1: Click the MAIN color button in the main palette.",
            lambda res: self._wizard_after_main_capture(name, res),
        )

    def _wizard_after_main_capture(self, name: str, res: ClickCaptureResult):
        main = MainColor(name=name, pos=res.pos, rgb=res.rgb, shades=[])
        self._cfg.main_colors.append(main)
        self._save_cfg()
        self._refresh_config_view()

        QtWidgets.QMessageBox.information(
            self,
            "Setup new color",
            "Step 2: Open the shades panel in-game.\n"
            "Then click each shade button one-by-one (left click).\n"
            "When you are done, click 'Finish'.",
        )

        # Collect shade clicks until user clicks Finish
        shades: list[ShadeButton] = []
        self._shade_capture_active = True

        dlg = QtWidgets.QDialog(self)
        dlg.setWindowTitle("Capture shades")
        v = QtWidgets.QVBoxLayout(dlg)
        lbl = QtWidgets.QLabel("Click shade buttons in-game. Captured:")
        v.addWidget(lbl)
        lst = QtWidgets.QListWidget()
        v.addWidget(lst)
        btn_finish = QtWidgets.QPushButton("Finish")
        v.addWidget(btn_finish)

        def add_shade_capture(res2: ClickCaptureResult):
            def _add():
                if not getattr(self, "_shade_capture_active", False):
                    return
                shade_name = f"shade-{len(shades)+1}"
                sh = ShadeButton(name=shade_name, pos=res2.pos, rgb=res2.rgb)
                shades.append(sh)
                lst.addItem(f"{shade_name} @ {res2.pos} rgb={res2.rgb}")
            QtCore.QMetaObject.invokeMethod(
                self, _add, QtCore.Qt.ConnectionType.QueuedConnection
            )

        def arm_next():
            if not self._shade_capture_active:
                return
            capture_next_left_click_with_color(
                on_result=lambda r: (add_shade_capture(r), arm_next()),
            )

        def finish():
            self._shade_capture_active = False
            # Save shades into matching main color
            for mc in self._cfg.main_colors:
                if mc.name == name and mc.pos == main.pos:
                    mc.shades = shades
                    break
            self._save_cfg()
            self._refresh_config_view()
            dlg.accept()

        btn_finish.clicked.connect(finish)

        arm_next()
        dlg.exec()

    def _on_remove_selected_color(self):
        idx = self.lst_colors.currentRow()
        if idx < 0:
            return
        if idx >= len(self._cfg.main_colors):
            return
        name = self._cfg.main_colors[idx].name
        if (
            QtWidgets.QMessageBox.question(
                self,
                "Remove",
                f"Remove color '{name}'?",
            )
            != QtWidgets.QMessageBox.StandardButton.Yes
        ):
            return
        self._cfg.main_colors.pop(idx)
        self._save_cfg()
        self._refresh_config_view()

    def _on_paint(self):
        if self._loaded is None:
            QtWidgets.QMessageBox.information(self, "Missing", "Import an image first.")
            return
        if self._canvas_rect is None:
            QtWidgets.QMessageBox.information(self, "Missing", "Select canvas area first.")
            return

        if (
            not self._cfg.main_colors
            or self._cfg.shades_panel_button_pos is None
            or self._cfg.back_button_pos is None
        ):
            QtWidgets.QMessageBox.information(
                self,
                "Missing configuration",
                "Set up your colors and the global buttons first.\n\n"
                "Required: at least one main color with shades, plus the shades-panel and back buttons.",
            )
            return

        # Safety prompt
        if (
            QtWidgets.QMessageBox.warning(
                self,
                "About to paint",
                "This will control your mouse and click in-game.\n"
                "Make sure the game is focused and your palette/canvas is visible.\n\n"
                "PyAutoGUI failsafe: move mouse to top-left to abort.",
                QtWidgets.QMessageBox.StandardButton.Ok
                | QtWidgets.QMessageBox.StandardButton.Cancel,
            )
            != QtWidgets.QMessageBox.StandardButton.Ok
        ):
            return

        if not self._paint_countdown(seconds=3):
            return

        self.btn_paint.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self._stop_flag = False

        total = self._loaded.grid.w * self._loaded.grid.h

        signals = WorkerSignals()
        signals.progress.connect(lambda x, y: self._on_progress(x, y, total))
        signals.finished.connect(self._on_paint_done)
        signals.error.connect(self._on_paint_error)

        def work():
            try:
                opts = PainterOptions(click_delay_s=0.01)

                def get_pixel(x: int, y: int):
                    return self._loaded.grid.get(x, y)

                paint_grid(
                    cfg=self._cfg,
                    canvas_rect=self._canvas_rect,
                    grid_w=self._loaded.grid.w,
                    grid_h=self._loaded.grid.h,
                    get_pixel=get_pixel,
                    options=opts,
                    progress_cb=lambda x, y: signals.progress.emit(x, y),
                    should_stop=lambda: self._stop_flag,
                )
                signals.finished.emit()
            except Exception as e:
                signals.error.emit(str(e))

        threading.Thread(target=work, daemon=True).start()

    def _paint_countdown(self, seconds: int = 3) -> bool:
        """Modal countdown before starting automation. Returns False if cancelled."""
        dlg = QtWidgets.QDialog(self)
        dlg.setWindowTitle("Starting")
        dlg.setModal(True)

        v = QtWidgets.QVBoxLayout(dlg)
        lbl = QtWidgets.QLabel()
        lbl.setWordWrap(True)
        v.addWidget(lbl)
        btn_cancel = QtWidgets.QPushButton("Cancel")
        v.addWidget(btn_cancel)

        remaining = {"n": max(0, int(seconds))}

        def update_text():
            n = remaining["n"]
            if n <= 0:
                lbl.setText("Starting now…")
            else:
                lbl.setText(
                    "Switch to the game window now.\n\n"
                    f"Starting in {n}…\n\n"
                    "Failsafe: move mouse to top-left to abort."
                )

        timer = QtCore.QTimer(dlg)

        def tick():
            remaining["n"] -= 1
            update_text()
            if remaining["n"] <= 0:
                timer.stop()
                dlg.accept()

        def cancel():
            timer.stop()
            dlg.reject()

        btn_cancel.clicked.connect(cancel)

        update_text()
        timer.timeout.connect(tick)
        timer.start(1000)

        return dlg.exec() == QtWidgets.QDialog.DialogCode.Accepted

    def _on_stop(self):
        self._stop_flag = True

    def _on_progress(self, x: int, y: int, total: int):
        idx = y * self._loaded.grid.w + x + 1
        pct = int((idx / total) * 100)
        self.progress.setValue(max(0, min(100, pct)))

    def _on_paint_done(self):
        self.btn_paint.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self.progress.setValue(100)

    def _on_paint_error(self, msg: str):
        self.btn_paint.setEnabled(True)
        self.btn_stop.setEnabled(False)
        QtWidgets.QMessageBox.critical(self, "Paint error", msg)


def run():
    # Better DPI behavior on Windows
    os.environ.setdefault("QT_ENABLE_HIGHDPI_SCALING", "1")

    app = QtWidgets.QApplication([])
    w = MainWindow()
    w.resize(900, 650)
    w.show()
    app.exec()
