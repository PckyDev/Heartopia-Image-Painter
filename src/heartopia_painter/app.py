from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple

from PySide6 import QtCore, QtGui, QtWidgets

from .screen import get_screen_pixel_rgb
from .config import AppConfig, MainColor, ShadeButton, default_config_path, load_config, save_config
from .image_processing import PixelGrid, load_and_resize_to_grid
from .overlay import Marker, MarkersOverlay, PointResult, PointSelectOverlay, RectResult, RectSelectOverlay
from .paint import PainterOptions, paint_grid


ONE_TO_ONE_PRESET_NAME = "1:1"
TSHIRT_PRESET_NAME = "T-Shirt"

ONE_TO_ONE_PRECISIONS: dict[str, Tuple[int, int]] = {
    "Small": (30, 30),
    "Medium": (50, 50),
    "Big": (100, 100),
    "Super Large": (150, 150),
}

TSHIRT_PARTS: dict[str, Tuple[int, int]] = {
    "Front": (64, 80),
    "Back": (64, 80),
    "Left Sleeve": (64, 48),
    "Right Sleeve": (64, 48),
}


def selection_key(preset: str, variant: Optional[str]) -> str:
    if preset == ONE_TO_ONE_PRESET_NAME:
        precision = variant or "Small"
        return f"{preset}::{precision}"
    if preset == TSHIRT_PRESET_NAME:
        return f"{preset}::{variant or 'Front'}"
    return preset


@dataclass
class LoadedImage:
    path: str
    grid: PixelGrid


@dataclass
class ClickCaptureResult:
    pos: Tuple[int, int]
    rgb: Tuple[int, int, int]


class WorkerSignals(QtCore.QObject):
    progress = QtCore.Signal(int, int)
    finished = QtCore.Signal()
    error = QtCore.Signal(str)
    paused = QtCore.Signal(str)
    stopped = QtCore.Signal(str)


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Heartopia Image Painter")

        self.statusBar().showMessage("Ready")

        self._config_path = default_config_path()
        self._cfg = load_config(self._config_path)

        self._loaded: Optional[LoadedImage] = None
        self._canvas_rect: Optional[Tuple[int, int, int, int]] = None

        self._overlay: Optional[RectSelectOverlay] = None

        self._markers_overlay: Optional[MarkersOverlay] = None

        self._esc_listener = None

        self._stop_flag = False
        self._stop_reason: Optional[str] = None  # "pause" | "stop" | None
        # Paint session state (used for pause/resume)
        self._paint_total: int = 0
        self._paint_done: set[tuple[int, int]] = set()
        self._paint_paused: bool = False
        self._paint_session_sig: Optional[tuple] = None

        self._build_ui()
        self._apply_persisted_state()
        self._refresh_config_view()

    def _start_esc_listener(self) -> None:
        # Global hotkey so it works even when the game window is focused.
        try:
            from pynput import keyboard  # type: ignore
        except Exception:
            self.statusBar().showMessage("ESC stop hotkey unavailable (pynput import failed)", 5000)
            return

        # Stop any previous listener.
        self._stop_esc_listener()

        def on_press(key):
            try:
                if key == keyboard.Key.esc:
                    # Pause painting immediately (worker thread checks should_stop).
                    self._stop_reason = "pause"
                    self._stop_flag = True

                    # Stop listening so we don't re-trigger.
                    try:
                        self._run_on_ui_thread(lambda: self.statusBar().showMessage("Pausing…", 1500))
                    except Exception:
                        pass
                    return False  # stop listener
            except Exception:
                return None
            return None

        # On some Windows setups, suppress=True can fail (or require elevated privileges).
        # Prefer suppression to avoid ESC closing dialogs, but fall back if needed.
        try:
            self._esc_listener = keyboard.Listener(on_press=on_press, suppress=True)
            self._esc_listener.daemon = True
            self._esc_listener.start()
            self.statusBar().showMessage("ESC hotkey armed (suppressed)", 2500)
        except Exception:
            try:
                self._esc_listener = keyboard.Listener(on_press=on_press, suppress=False)
                self._esc_listener.daemon = True
                self._esc_listener.start()
                self.statusBar().showMessage("ESC hotkey armed", 2500)
            except Exception:
                self._esc_listener = None
                self.statusBar().showMessage("ESC stop hotkey unavailable (listener failed)", 5000)

    def _stop_esc_listener(self) -> None:
        try:
            if self._esc_listener is not None:
                self._esc_listener.stop()
        except Exception:
            pass
        finally:
            self._esc_listener = None

    def _build_ui(self):
        root = QtWidgets.QWidget()
        self.setCentralWidget(root)
        layout = QtWidgets.QVBoxLayout(root)

        tabs = QtWidgets.QTabWidget()
        layout.addWidget(tabs)

        tab_main = QtWidgets.QWidget()
        tab_main_layout = QtWidgets.QVBoxLayout(tab_main)

        tab_cfg = QtWidgets.QWidget()
        tab_cfg_layout = QtWidgets.QVBoxLayout(tab_cfg)

        tab_timing = QtWidgets.QWidget()
        tab_timing_layout = QtWidgets.QVBoxLayout(tab_timing)

        tabs.addTab(tab_main, "Main")
        tabs.addTab(tab_cfg, "Color configuration")
        tabs.addTab(tab_timing, "Timing / reliability")

        # Image load
        row1 = QtWidgets.QHBoxLayout()
        self.btn_load = QtWidgets.QPushButton("Import image…")
        self.lbl_image = QtWidgets.QLabel("No image loaded")
        self.lbl_image.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
        row1.addWidget(self.btn_load)
        row1.addWidget(self.lbl_image, 1)
        tab_main_layout.addLayout(row1)

        # Preset / Precision / Part
        row2 = QtWidgets.QHBoxLayout()
        row2.addWidget(QtWidgets.QLabel("Canvas preset:"))
        self.cbo_preset = QtWidgets.QComboBox()
        self.cbo_preset.addItems([ONE_TO_ONE_PRESET_NAME, TSHIRT_PRESET_NAME])
        row2.addWidget(self.cbo_preset, 1)

        self.lbl_precision = QtWidgets.QLabel("Precision:")
        self.cbo_precision = QtWidgets.QComboBox()
        self.cbo_precision.addItems(list(ONE_TO_ONE_PRECISIONS.keys()))
        row2.addWidget(self.lbl_precision)
        row2.addWidget(self.cbo_precision)

        self.lbl_part = QtWidgets.QLabel("Part:")
        self.cbo_part = QtWidgets.QComboBox()
        self.cbo_part.addItems(list(TSHIRT_PARTS.keys()))
        row2.addWidget(self.lbl_part)
        row2.addWidget(self.cbo_part)

        self.btn_select_canvas = QtWidgets.QPushButton("Select canvas area…")
        row2.addWidget(self.btn_select_canvas)
        tab_main_layout.addLayout(row2)

        self.lbl_canvas = QtWidgets.QLabel("Canvas: not selected")
        tab_main_layout.addWidget(self.lbl_canvas)

        self.lbl_global_buttons = QtWidgets.QLabel("Palette buttons: not set")
        self.lbl_global_buttons.setWordWrap(True)
        tab_main_layout.addWidget(self.lbl_global_buttons)

        # Config
        cfg_group = QtWidgets.QGroupBox("Color configuration")
        cfg_layout = QtWidgets.QVBoxLayout(cfg_group)

        row_cfg1 = QtWidgets.QHBoxLayout()
        self.btn_set_shades_button = QtWidgets.QPushButton("Set shades-panel button")
        self.btn_set_back_button = QtWidgets.QPushButton("Set back button")
        self.btn_show_main_overlay = QtWidgets.QPushButton("Show main-color overlay")
        row_cfg1.addWidget(self.btn_set_shades_button)
        row_cfg1.addWidget(self.btn_set_back_button)
        row_cfg1.addWidget(self.btn_show_main_overlay)
        cfg_layout.addLayout(row_cfg1)

        row_cfg_tools = QtWidgets.QHBoxLayout()
        self.btn_set_paint_tool = QtWidgets.QPushButton("Set paint tool button")
        self.btn_set_bucket_tool = QtWidgets.QPushButton("Set bucket tool button")
        row_cfg_tools.addWidget(self.btn_set_paint_tool)
        row_cfg_tools.addWidget(self.btn_set_bucket_tool)
        cfg_layout.addLayout(row_cfg_tools)

        row_cfg2 = QtWidgets.QHBoxLayout()
        self.btn_add_color = QtWidgets.QPushButton("Setup new color…")
        self.btn_remove_color = QtWidgets.QPushButton("Remove selected")
        self.btn_fix_swap_rb = QtWidgets.QPushButton("Fix colors: swap R/B")
        row_cfg2.addWidget(self.btn_add_color)
        row_cfg2.addWidget(self.btn_remove_color)
        row_cfg2.addWidget(self.btn_fix_swap_rb)
        cfg_layout.addLayout(row_cfg2)

        self.lst_colors = QtWidgets.QListWidget()
        cfg_layout.addWidget(self.lst_colors)

        self.lbl_cfg_hint = QtWidgets.QLabel(
            "Tip: Move mouse to top-left to abort painting (PyAutoGUI failsafe)."
        )
        self.lbl_cfg_hint.setWordWrap(True)
        cfg_layout.addWidget(self.lbl_cfg_hint)

        tab_cfg_layout.addWidget(cfg_group)

        tab_cfg_layout.addStretch(1)

        # Timing / reliability (tab)
        timing = QtWidgets.QGroupBox("Timing / reliability")
        tlay = QtWidgets.QGridLayout(timing)

        def ms_spin(min_ms: int, max_ms: int, step_ms: int):
            s = QtWidgets.QSpinBox()
            s.setRange(min_ms, max_ms)
            s.setSingleStep(step_ms)
            s.setSuffix(" ms")
            return s

        self.spin_move = ms_spin(0, 500, 5)
        self.spin_down = ms_spin(0, 500, 5)
        self.spin_after = ms_spin(0, 2000, 10)
        self.spin_panel = ms_spin(0, 3000, 10)
        self.spin_shade = ms_spin(0, 2000, 10)
        self.spin_row = ms_spin(0, 5000, 10)

        self.chk_drag = QtWidgets.QCheckBox("Stroke neighbors (rapid clicks)")
        self.spin_drag_step = ms_spin(0, 200, 1)
        self.spin_after_drag = ms_spin(0, 2000, 10)

        self.chk_verify = QtWidgets.QCheckBox("Verify (repaint misses)")
        self.spin_verify_tol = QtWidgets.QSpinBox()
        self.spin_verify_tol.setRange(0, 255)
        self.spin_verify_tol.setSingleStep(1)
        self.spin_verify_tol.setSuffix(" tol")
        self.spin_verify_passes = QtWidgets.QSpinBox()
        self.spin_verify_passes.setRange(1, 50)
        self.spin_verify_passes.setSingleStep(1)
        self.spin_verify_passes.setSuffix(" passes")

        tlay.addWidget(QtWidgets.QLabel("Mouse move duration:"), 0, 0)
        tlay.addWidget(self.spin_move, 0, 1)
        tlay.addWidget(QtWidgets.QLabel("Mouse down hold:"), 1, 0)
        tlay.addWidget(self.spin_down, 1, 1)
        tlay.addWidget(QtWidgets.QLabel("After each click delay:"), 2, 0)
        tlay.addWidget(self.spin_after, 2, 1)
        tlay.addWidget(QtWidgets.QLabel("After opening shades panel:"), 0, 2)
        tlay.addWidget(self.spin_panel, 0, 3)
        tlay.addWidget(QtWidgets.QLabel("After selecting shade:"), 1, 2)
        tlay.addWidget(self.spin_shade, 1, 3)
        tlay.addWidget(QtWidgets.QLabel("Row delay:"), 2, 2)
        tlay.addWidget(self.spin_row, 2, 3)

        tlay.addWidget(self.chk_drag, 3, 0, 1, 2)
        tlay.addWidget(QtWidgets.QLabel("Stroke step delay:"), 3, 2)
        tlay.addWidget(self.spin_drag_step, 3, 3)
        tlay.addWidget(QtWidgets.QLabel("After stroke delay:"), 4, 2)
        tlay.addWidget(self.spin_after_drag, 4, 3)

        tlay.addWidget(self.chk_verify, 4, 0, 1, 2)
        tlay.addWidget(QtWidgets.QLabel("Verify tolerance:"), 5, 2)
        tlay.addWidget(self.spin_verify_tol, 5, 3)
        tlay.addWidget(QtWidgets.QLabel("Verify max passes:"), 6, 2)
        tlay.addWidget(self.spin_verify_passes, 6, 3)

        tab_timing_layout.addWidget(timing)

        tab_timing_layout.addStretch(1)

        # Paint (main tab)
        paint_group = QtWidgets.QGroupBox("Paint")
        paint_layout = QtWidgets.QVBoxLayout(paint_group)

        rowm = QtWidgets.QHBoxLayout()
        rowm.addWidget(QtWidgets.QLabel("Method:"))
        self.cbo_paint_mode = QtWidgets.QComboBox()
        self.cbo_paint_mode.addItems(["Paint by Row", "Paint by Color"])
        rowm.addWidget(self.cbo_paint_mode)
        rowm.addStretch(1)
        paint_layout.addLayout(rowm)

        row_bucket = QtWidgets.QHBoxLayout()
        self.chk_bucket_fill = QtWidgets.QCheckBox("Bucket-fill most-used color first")
        self.spin_bucket_min = QtWidgets.QSpinBox()
        self.spin_bucket_min.setRange(0, 100000)
        self.spin_bucket_min.setSingleStep(10)
        self.spin_bucket_min.setSuffix(" min cells")
        row_bucket.addWidget(self.chk_bucket_fill)
        row_bucket.addStretch(1)
        row_bucket.addWidget(QtWidgets.QLabel("Threshold:"))
        row_bucket.addWidget(self.spin_bucket_min)
        paint_layout.addLayout(row_bucket)

        rowp = QtWidgets.QHBoxLayout()
        self.btn_paint = QtWidgets.QPushButton("Paint now")
        self.btn_resume = QtWidgets.QPushButton("Resume")
        self.btn_resume.setEnabled(False)
        self.btn_stop = QtWidgets.QPushButton("Stop")
        self.btn_stop.setEnabled(False)
        rowp.addWidget(self.btn_paint)
        rowp.addWidget(self.btn_resume)
        rowp.addWidget(self.btn_stop)
        paint_layout.addLayout(rowp)

        self.progress = QtWidgets.QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        paint_layout.addWidget(self.progress)

        tab_main_layout.addWidget(paint_group)

        tab_main_layout.addStretch(1)

        # Wiring
        self.btn_load.clicked.connect(self._on_load)
        self.btn_select_canvas.clicked.connect(self._on_select_canvas)
        self.btn_set_shades_button.clicked.connect(lambda: self._capture_global_button("shades"))
        self.btn_set_back_button.clicked.connect(lambda: self._capture_global_button("back"))
        self.btn_show_main_overlay.clicked.connect(self._on_toggle_main_color_overlay)
        self.btn_set_paint_tool.clicked.connect(lambda: self._capture_global_button("paint_tool"))
        self.btn_set_bucket_tool.clicked.connect(lambda: self._capture_global_button("bucket_tool"))
        self.btn_add_color.clicked.connect(self._on_setup_new_color)
        self.btn_remove_color.clicked.connect(self._on_remove_selected_color)
        self.btn_fix_swap_rb.clicked.connect(self._on_fix_swap_rb)
        self.btn_paint.clicked.connect(self._on_paint)
        self.btn_resume.clicked.connect(self._on_resume)
        self.btn_stop.clicked.connect(self._on_stop)

        self.cbo_preset.currentTextChanged.connect(self._on_preset_changed)
        self.cbo_precision.currentTextChanged.connect(self._on_precision_changed)
        self.cbo_part.currentTextChanged.connect(self._on_part_changed)

        self.cbo_paint_mode.currentTextChanged.connect(self._on_paint_mode_changed)

        self.chk_bucket_fill.stateChanged.connect(lambda _v: self._on_bucket_fill_changed())
        self.spin_bucket_min.valueChanged.connect(lambda _v: self._on_bucket_fill_changed())

        self.spin_move.valueChanged.connect(self._on_timing_changed)
        self.spin_down.valueChanged.connect(self._on_timing_changed)
        self.spin_after.valueChanged.connect(self._on_timing_changed)
        self.spin_panel.valueChanged.connect(self._on_timing_changed)
        self.spin_shade.valueChanged.connect(self._on_timing_changed)
        self.spin_row.valueChanged.connect(self._on_timing_changed)
        self.chk_drag.stateChanged.connect(lambda _v: self._on_timing_changed(0))
        self.spin_drag_step.valueChanged.connect(self._on_timing_changed)
        self.spin_after_drag.valueChanged.connect(self._on_timing_changed)

        self.chk_verify.stateChanged.connect(lambda _v: self._on_verify_changed())
        self.spin_verify_tol.valueChanged.connect(lambda _v: self._on_verify_changed())
        self.spin_verify_passes.valueChanged.connect(lambda _v: self._on_verify_changed())

    def _on_toggle_main_color_overlay(self) -> None:
        # Toggle if already visible.
        try:
            if self._markers_overlay is not None and self._markers_overlay.isVisible():
                self._markers_overlay.hide()
                return
        except Exception:
            pass

        markers: list[Marker] = []
        for mc in getattr(self._cfg, "main_colors", []) or []:
            pos = getattr(mc, "pos", None)
            if not pos or tuple(pos) == (0, 0):
                continue
            rgb = getattr(mc, "rgb", (0, 200, 255))
            markers.append(Marker(label=str(getattr(mc, "name", "Color")), pos=(int(pos[0]), int(pos[1])), color=rgb))

        if not markers:
            QtWidgets.QMessageBox.information(
                self,
                "No main colors",
                "No main color button positions are saved yet.\n\nUse 'Setup new color…' first.",
            )
            return

        self._markers_overlay = MarkersOverlay(
            markers=markers,
            title="Main color button positions",
            duration_ms=15000,
        )
        self._markers_overlay.start()

    def _apply_persisted_state(self):
        # Restore preset
        if self._cfg.canvas_preset and self.cbo_preset.findText(self._cfg.canvas_preset) >= 0:
            self.cbo_preset.setCurrentText(self._cfg.canvas_preset)

        # Restore 1:1 precision
        if (
            getattr(self._cfg, "one_to_one_precision", None)
            and self.cbo_precision.findText(self._cfg.one_to_one_precision) >= 0
        ):
            self.cbo_precision.setCurrentText(self._cfg.one_to_one_precision)

        # Restore T-Shirt part
        if self._cfg.tshirt_part and self.cbo_part.findText(self._cfg.tshirt_part) >= 0:
            self.cbo_part.setCurrentText(self._cfg.tshirt_part)

        self._update_variant_ui_visibility()

        # Restore per-selection state (image + canvas)
        self._restore_selection_state()

        # Restore timing controls
        self._sync_timing_ui_from_cfg()

        # Restore paint mode
        self._sync_paint_mode_ui_from_cfg()

    def _sync_paint_mode_ui_from_cfg(self) -> None:
        # Block signals so we don't save during startup.
        self.cbo_paint_mode.blockSignals(True)
        try:
            pm = (getattr(self._cfg, "paint_mode", "row") or "row").strip().lower()
            if pm == "color":
                self.cbo_paint_mode.setCurrentText("Paint by Color")
            else:
                self.cbo_paint_mode.setCurrentText("Paint by Row")
        finally:
            self.cbo_paint_mode.blockSignals(False)

    def _on_paint_mode_changed(self, _text: str) -> None:
        txt = self.cbo_paint_mode.currentText().strip().lower()
        self._cfg.paint_mode = "color" if "color" in txt else "row"
        self._save_cfg()

    def _sync_timing_ui_from_cfg(self):
        def to_ms(v: float) -> int:
            return int(round(max(0.0, float(v)) * 1000.0))

        # Block signals to avoid saving on startup repeatedly
        widgets = [
            self.spin_move,
            self.spin_down,
            self.spin_after,
            self.spin_panel,
            self.spin_shade,
            self.spin_row,
            self.spin_drag_step,
            self.spin_after_drag,
        ]
        for w in widgets:
            w.blockSignals(True)
        self.chk_drag.blockSignals(True)
        self.chk_verify.blockSignals(True)
        self.spin_verify_tol.blockSignals(True)
        self.spin_verify_passes.blockSignals(True)
        self.chk_bucket_fill.blockSignals(True)
        self.spin_bucket_min.blockSignals(True)

        self.spin_move.setValue(to_ms(self._cfg.move_duration_s))
        self.spin_down.setValue(to_ms(self._cfg.mouse_down_s))
        self.spin_after.setValue(to_ms(self._cfg.after_click_delay_s))
        self.spin_panel.setValue(to_ms(self._cfg.panel_open_delay_s))
        self.spin_shade.setValue(to_ms(self._cfg.shade_select_delay_s))
        self.spin_row.setValue(to_ms(self._cfg.row_delay_s))

        self.chk_drag.setChecked(bool(getattr(self._cfg, "enable_drag_strokes", False)))
        self.spin_drag_step.setValue(to_ms(getattr(self._cfg, "drag_step_duration_s", 0.01)))
        self.spin_after_drag.setValue(to_ms(getattr(self._cfg, "after_drag_delay_s", 0.02)))

        self.chk_verify.setChecked(bool(getattr(self._cfg, "verify_rows", True)))
        self.spin_verify_tol.setValue(int(getattr(self._cfg, "verify_tolerance", 35)))
        self.spin_verify_passes.setValue(int(getattr(self._cfg, "verify_max_passes", 10)))

        self.chk_bucket_fill.setChecked(bool(getattr(self._cfg, "bucket_fill_enabled", False)))
        self.spin_bucket_min.setValue(int(getattr(self._cfg, "bucket_fill_min_cells", 50)))

        for w in widgets:
            w.blockSignals(False)
        self.chk_drag.blockSignals(False)
        self.chk_verify.blockSignals(False)
        self.spin_verify_tol.blockSignals(False)
        self.spin_verify_passes.blockSignals(False)
        self.chk_bucket_fill.blockSignals(False)
        self.spin_bucket_min.blockSignals(False)

    def _on_timing_changed(self, _value: int):
        # Persist timing settings immediately
        def to_s(ms: int) -> float:
            return max(0.0, float(ms) / 1000.0)

        self._cfg.move_duration_s = to_s(self.spin_move.value())
        self._cfg.mouse_down_s = to_s(self.spin_down.value())
        self._cfg.after_click_delay_s = to_s(self.spin_after.value())
        self._cfg.panel_open_delay_s = to_s(self.spin_panel.value())
        self._cfg.shade_select_delay_s = to_s(self.spin_shade.value())
        self._cfg.row_delay_s = to_s(self.spin_row.value())

        self._cfg.enable_drag_strokes = bool(self.chk_drag.isChecked())
        self._cfg.drag_step_duration_s = to_s(self.spin_drag_step.value())
        self._cfg.after_drag_delay_s = to_s(self.spin_after_drag.value())
        self._save_cfg()

    def _on_verify_changed(self) -> None:
        self._cfg.verify_rows = bool(self.chk_verify.isChecked())
        self._cfg.verify_tolerance = int(self.spin_verify_tol.value())
        self._cfg.verify_max_passes = int(self.spin_verify_passes.value())
        self._save_cfg()

    def _on_bucket_fill_changed(self) -> None:
        self._cfg.bucket_fill_enabled = bool(self.chk_bucket_fill.isChecked())
        self._cfg.bucket_fill_min_cells = int(self.spin_bucket_min.value())
        self._save_cfg()

    def _refresh_config_view(self):
        self.lst_colors.clear()
        for mc in self._cfg.main_colors:
            self.lst_colors.addItem(f"{mc.name}  ({len(mc.shades)} shades)")

        preset = self.cbo_preset.currentText()
        part_txt = ""
        if preset == ONE_TO_ONE_PRESET_NAME:
            part_txt = f" — {self.cbo_precision.currentText()}"
        elif preset == TSHIRT_PRESET_NAME:
            part_txt = f" — {self.cbo_part.currentText()}"

        if self._canvas_rect is None:
            self.lbl_canvas.setText(f"Canvas{part_txt}: not selected")
        else:
            x, y, w, h = self._canvas_rect
            self.lbl_canvas.setText(f"Canvas{part_txt}: x={x}, y={y}, w={w}, h={h}")

        sp = self._cfg.shades_panel_button_pos
        bp = self._cfg.back_button_pos
        pp = getattr(self._cfg, "paint_tool_button_pos", None)
        bk = getattr(self._cfg, "bucket_tool_button_pos", None)
        sp_txt = f"{sp}" if sp is not None else "(not set)"
        bp_txt = f"{bp}" if bp is not None else "(not set)"
        pp_txt = f"{pp}" if pp is not None else "(not set)"
        bk_txt = f"{bk}" if bk is not None else "(not set)"
        self.lbl_global_buttons.setText(
            f"Palette buttons — Shades panel: {sp_txt} | Back: {bp_txt} | Paint tool: {pp_txt} | Bucket: {bk_txt}"
        )

    def _save_cfg(self):
        save_config(self._config_path, self._cfg)

    def _run_on_ui_thread(self, fn) -> None:
        # QTimer.singleShot reliably queues work onto the Qt event loop.
        QtCore.QTimer.singleShot(0, fn)

    def _confirm_capture(self, label: str, res: ClickCaptureResult):
        self.statusBar().showMessage(f"Captured {label} at {res.pos} rgb={res.rgb}", 5000)
        QtWidgets.QMessageBox.information(
            self,
            "Captured",
            f"Captured {label}.\n\nPosition: {res.pos}\nRGB: {res.rgb}",
        )

    def _capture_click_async(self, title: str, message: str, apply_capture):
        """Shows a prompt, then uses a fullscreen overlay to pick a point + sample RGB."""
        QtWidgets.QMessageBox.information(self, title, message)

        ov = PointSelectOverlay(instruction="Click the location on screen (ESC/right-click to cancel)")
        self._point_overlay = ov  # keep alive

        def on_sel(p: PointResult):
            rgb = get_screen_pixel_rgb(int(p.x), int(p.y))
            res = ClickCaptureResult(pos=(int(p.x), int(p.y)), rgb=rgb)
            self._run_on_ui_thread(lambda: apply_capture(res))

        ov.pointSelected.connect(on_sel)
        ov.cancelled.connect(lambda: None)
        ov.start()

    def _selected_preset_wh(self) -> Tuple[int, int]:
        preset = self.cbo_preset.currentText()
        if preset == ONE_TO_ONE_PRESET_NAME:
            precision = self.cbo_precision.currentText() or self._cfg.one_to_one_precision or "Small"
            return ONE_TO_ONE_PRECISIONS.get(precision, ONE_TO_ONE_PRECISIONS["Small"])
        if preset == TSHIRT_PRESET_NAME:
            part = self.cbo_part.currentText() or self._cfg.tshirt_part or "Front"
            return TSHIRT_PARTS.get(part, TSHIRT_PARTS["Front"])
        return (30, 30)

    def _current_selection_key(self) -> str:
        preset = self.cbo_preset.currentText()
        if preset == ONE_TO_ONE_PRESET_NAME:
            precision = self.cbo_precision.currentText() or self._cfg.one_to_one_precision or "Small"
            return selection_key(preset, precision)
        if preset == TSHIRT_PRESET_NAME:
            part = self.cbo_part.currentText() if preset == TSHIRT_PRESET_NAME else None
            return selection_key(preset, part)
        return selection_key(preset, None)

    def _update_variant_ui_visibility(self) -> None:
        preset = self.cbo_preset.currentText()
        is_one_to_one = preset == ONE_TO_ONE_PRESET_NAME
        is_tshirt = preset == TSHIRT_PRESET_NAME

        self.lbl_precision.setVisible(is_one_to_one)
        self.cbo_precision.setVisible(is_one_to_one)
        self.lbl_part.setVisible(is_tshirt)
        self.cbo_part.setVisible(is_tshirt)

    def _restore_selection_state(self) -> None:
        sel_key = self._current_selection_key()

        rect = self._cfg.last_canvas_rect_by_key.get(sel_key)
        if rect is None and self._cfg.last_canvas_rect is not None:
            rect = tuple(self._cfg.last_canvas_rect)
        self._canvas_rect = tuple(rect) if rect is not None else None

        img_path = self._cfg.last_image_path_by_key.get(sel_key)
        if not img_path and self._cfg.last_image_path:
            img_path = self._cfg.last_image_path

        self._loaded = None
        if img_path:
            p = Path(img_path)
            if p.exists():
                try:
                    w, h = self._selected_preset_wh()
                    grid = load_and_resize_to_grid(str(p), w=w, h=h)
                    self._loaded = LoadedImage(path=str(p), grid=grid)
                    self.lbl_image.setText(f"Loaded: {p} ({w}x{h})")
                except Exception:
                    # If the image can't be loaded anymore, just ignore it.
                    self._loaded = None
                    self.lbl_image.setText("No image loaded")
            else:
                self.lbl_image.setText("No image loaded")
        else:
            self.lbl_image.setText("No image loaded")

        self._refresh_config_view()

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

        sel_key = self._current_selection_key()
        self._cfg.last_image_path_by_key[sel_key] = path
        self._cfg.last_image_path = path
        self._cfg.canvas_preset = self.cbo_preset.currentText()
        if self.cbo_preset.currentText() == ONE_TO_ONE_PRESET_NAME:
            self._cfg.one_to_one_precision = self.cbo_precision.currentText() or self._cfg.one_to_one_precision
        if self.cbo_preset.currentText() == TSHIRT_PRESET_NAME:
            self._cfg.tshirt_part = self.cbo_part.currentText() or self._cfg.tshirt_part
        self._save_cfg()

    def _on_select_canvas(self):
        if self._loaded is None:
            QtWidgets.QMessageBox.information(self, "Select image", "Import an image first.")
            return

        # Build preview pixmap from the resized grid (matches the preset exactly)
        grid = self._loaded.grid
        qimg = QtGui.QImage(grid.w, grid.h, QtGui.QImage.Format.Format_RGB888)
        for y in range(grid.h):
            for x in range(grid.w):
                r, g, b = grid.get(x, y)
                qimg.setPixel(x, y, QtGui.qRgb(r, g, b))
        pix = QtGui.QPixmap.fromImage(qimg)

        self._overlay = RectSelectOverlay(preview_pixmap=pix)
        self._overlay.rectSelected.connect(self._on_canvas_rect_selected)
        self._overlay.cancelled.connect(lambda: None)
        self._overlay.start()

    def _on_canvas_rect_selected(self, r: RectResult):
        # Use selection as canvas rect (we'll refine snapping later)
        self._canvas_rect = (r.x, r.y, r.w, r.h)

        sel_key = self._current_selection_key()
        self._cfg.last_canvas_rect_by_key[sel_key] = self._canvas_rect
        self._cfg.last_canvas_rect = self._canvas_rect
        self._save_cfg()
        self._refresh_config_view()

        QtWidgets.QMessageBox.information(
            self,
            "Canvas selected",
            f"Canvas area saved.\n\nPosition: ({r.x}, {r.y})\nSize: {r.w}x{r.h}",
        )

    def _capture_global_button(self, which: str):
        self._capture_click_async(
            "Capture",
            "After closing this dialog, click the button location in-game (the click will NOT press it).",
            lambda res: self._apply_global_button_capture(which, res),
        )

    def _apply_global_button_capture(self, which: str, res: ClickCaptureResult):
        if which == "shades":
            self._cfg.shades_panel_button_pos = res.pos
            self._confirm_capture("shades-panel button", res)
        elif which == "back":
            self._cfg.back_button_pos = res.pos
            self._confirm_capture("back button", res)
        elif which == "paint_tool":
            self._cfg.paint_tool_button_pos = res.pos
            self._confirm_capture("paint tool button", res)
        elif which == "bucket_tool":
            self._cfg.bucket_tool_button_pos = res.pos
            self._confirm_capture("bucket tool button", res)
        self._save_cfg()
        self._refresh_config_view()

    def _on_preset_changed(self, _text: str):
        self._cfg.canvas_preset = self.cbo_preset.currentText()
        self._update_variant_ui_visibility()
        if self.cbo_preset.currentText() == ONE_TO_ONE_PRESET_NAME:
            self._cfg.one_to_one_precision = self.cbo_precision.currentText() or self._cfg.one_to_one_precision
        if self.cbo_preset.currentText() == TSHIRT_PRESET_NAME:
            self._cfg.tshirt_part = self.cbo_part.currentText() or self._cfg.tshirt_part
        self._save_cfg()
        self._restore_selection_state()

    def _on_precision_changed(self, _text: str):
        if self.cbo_preset.currentText() != ONE_TO_ONE_PRESET_NAME:
            return
        self._cfg.one_to_one_precision = self.cbo_precision.currentText() or self._cfg.one_to_one_precision
        self._save_cfg()
        self._restore_selection_state()

    def _on_part_changed(self, _text: str):
        if self.cbo_preset.currentText() != TSHIRT_PRESET_NAME:
            return
        self._cfg.tshirt_part = self.cbo_part.currentText() or self._cfg.tshirt_part
        self._save_cfg()
        self._restore_selection_state()

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
            self._confirm_capture("shades-panel button", res)
        elif which == "back":
            self._cfg.back_button_pos = res.pos
            self._confirm_capture("back button", res)
        self._save_cfg()
        self._refresh_config_view()
        # Continue capturing any remaining globals, then proceed.
        self._wizard_ensure_globals_then_continue(color_name)

    def _wizard_capture_main_color(self, name: str):
        self._capture_click_async(
            "Setup new color",
            "Step 1: Click the MAIN color button in the main palette.",
            lambda res: self._wizard_after_main_capture(name, res),
        )

    def _wizard_after_main_capture(self, name: str, res: ClickCaptureResult):
        self._confirm_capture(f"main color '{name}'", res)
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

        # Collect shade picks until user clicks Finish.
        # Important: keep this NON-MODAL so you can freely interact with the game.
        shades: list[ShadeButton] = []
        self._shade_capture_active = True

        dlg = QtWidgets.QDialog(self)
        dlg.setWindowTitle(f"Capture shades — {name}")
        dlg.setModal(False)
        dlg.setWindowFlags(dlg.windowFlags() | QtCore.Qt.WindowType.WindowStaysOnTopHint)

        v = QtWidgets.QVBoxLayout(dlg)
        lbl = QtWidgets.QLabel(
            "Click 'Capture next shade', then click the shade button location in the game.\n"
            "Repeat until done, then click Finish." 
        )
        lbl.setWordWrap(True)
        v.addWidget(lbl)

        lst = QtWidgets.QListWidget()
        v.addWidget(lst)

        row = QtWidgets.QHBoxLayout()
        btn_capture = QtWidgets.QPushButton("Capture next shade")
        btn_finish = QtWidgets.QPushButton("Finish")
        row.addWidget(btn_capture)
        row.addWidget(btn_finish)
        v.addLayout(row)

        def add_shade_capture(res2: ClickCaptureResult):
            if not getattr(self, "_shade_capture_active", False):
                return
            shade_name = f"shade-{len(shades)+1}"
            sh = ShadeButton(name=shade_name, pos=res2.pos, rgb=res2.rgb)
            shades.append(sh)
            lst.addItem(f"{shade_name} @ {res2.pos} rgb={res2.rgb}")
            self.statusBar().showMessage(f"Captured {shade_name} at {res2.pos} rgb={res2.rgb}", 4000)

        def capture_one():
            if not getattr(self, "_shade_capture_active", False):
                return

            ov = PointSelectOverlay(instruction="Click the SHADE button location (ESC/right-click to cancel)")
            self._point_overlay = ov

            def on_sel(p: PointResult):
                rgb = get_screen_pixel_rgb(int(p.x), int(p.y))
                r = ClickCaptureResult(pos=(int(p.x), int(p.y)), rgb=rgb)
                self._run_on_ui_thread(lambda: add_shade_capture(r))

            def on_cancel():
                self.statusBar().showMessage("Shade capture cancelled", 3000)

            ov.pointSelected.connect(on_sel)
            ov.cancelled.connect(on_cancel)
            ov.start()

        def finish():
            self._shade_capture_active = False
            # Save shades into matching main color
            for mc in self._cfg.main_colors:
                if mc.name == name and mc.pos == main.pos:
                    mc.shades = shades
                    break
            self._save_cfg()
            self._refresh_config_view()
            dlg.close()

            QtWidgets.QMessageBox.information(
                self,
                "Shades saved",
                f"Saved {len(shades)} shades for '{name}'.",
            )

        def on_close(_event):
            # If user closes the window, stop capturing to avoid orphan overlays.
            self._shade_capture_active = False

        dlg.closeEvent = on_close  # type: ignore[method-assign]

        btn_capture.clicked.connect(capture_one)
        btn_finish.clicked.connect(finish)

        dlg.show()
        dlg.raise_()
        dlg.activateWindow()

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

    def _on_fix_swap_rb(self):
        if (
            QtWidgets.QMessageBox.question(
                self,
                "Swap R/B channels?",
                "If your captured palette colors look wrong (e.g. yellows behave like blues),\n"
                "you may have captured colors when the sampler had swapped channels.\n\n"
                "This will swap the Red and Blue channels for ALL saved main/shade colors.\n\n"
                "Proceed?",
            )
            != QtWidgets.QMessageBox.StandardButton.Yes
        ):
            return

        def swap(rgb):
            r, g, b = rgb
            return (b, g, r)

        for mc in self._cfg.main_colors:
            mc.rgb = swap(mc.rgb)
            for sh in mc.shades:
                sh.rgb = swap(sh.rgb)

        self._save_cfg()
        self._refresh_config_view()
        QtWidgets.QMessageBox.information(self, "Done", "Swapped R/B for saved colors.")

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

        self._start_paint_worker(resume=False)

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
        # Manual stop is a cancel.
        self._stop_reason = "stop"
        self._stop_flag = True
        self._stop_esc_listener()
    def _current_paint_session_sig(self) -> Optional[tuple]:
        if self._loaded is None or self._canvas_rect is None:
            return None
        return (
            self._loaded.path,
            self._loaded.grid.w,
            self._loaded.grid.h,
            tuple(self._canvas_rect),
            self._current_selection_key(),
            str(getattr(self._cfg, "paint_mode", "row")),
        )

    def _reset_paint_session(self) -> None:
        self._paint_total = 0
        self._paint_done.clear()
        self._paint_paused = False
        self._paint_session_sig = None
        self.btn_resume.setEnabled(False)

    def _on_progress(self, x: int, y: int, total: int):
        # Progress callbacks can arrive out of order (Paint-by-Color) and can
        # repeat due to verification repaints. Track unique completed cells.
        if self._loaded is None:
            return
        if total > 0:
            self._paint_total = int(total)
        key = (int(x), int(y))
        if key not in self._paint_done:
            self._paint_done.add(key)

        denom = max(1, int(self._paint_total) or int(total) or 1)
        pct = int((len(self._paint_done) / denom) * 100)
        self.progress.setValue(max(0, min(100, pct)))

    def _on_paint_done(self):
        self.btn_paint.setEnabled(True)
        self.btn_resume.setEnabled(False)
        self.btn_stop.setEnabled(False)
        self.progress.setValue(100)
        self._stop_esc_listener()
        self._reset_paint_session()

    def _on_paint_paused(self, msg: str) -> None:
        self.btn_paint.setEnabled(True)
        self.btn_resume.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self._stop_esc_listener()
        self._paint_paused = True
        self.statusBar().showMessage(msg or "Paused", 4000)

    def _on_paint_stopped(self, msg: str) -> None:
        self.btn_paint.setEnabled(True)
        self.btn_resume.setEnabled(False)
        self.btn_stop.setEnabled(False)
        self._stop_esc_listener()
        self._reset_paint_session()
        self.statusBar().showMessage(msg or "Stopped", 4000)

    def _on_paint_error(self, msg: str):
        self.btn_paint.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self._stop_esc_listener()

        # Keep the session state so the user can tweak settings and resume.
        self._paint_paused = True
        self.btn_resume.setEnabled(True)
        QtWidgets.QMessageBox.warning(
            self,
            "Paint paused",
            "Painting hit an error and has been paused.\n\n"
            "You can tweak timing/verification settings and press Resume to continue from the last completed step.\n\n"
            f"Error: {msg}",
        )

    def _start_paint_worker(self, resume: bool) -> None:
        if self._loaded is None or self._canvas_rect is None:
            return

        total = self._loaded.grid.w * self._loaded.grid.h
        if not resume:
            self._reset_paint_session()
            self._paint_total = total
            self._paint_session_sig = self._current_paint_session_sig()
        else:
            # Validate that the session hasn't changed.
            cur_sig = self._current_paint_session_sig()
            if self._paint_session_sig is None or cur_sig != self._paint_session_sig:
                QtWidgets.QMessageBox.information(
                    self,
                    "Can't resume",
                    "The image/canvas/preset changed since the last run.\n\nStart a new paint instead.",
                )
                self._reset_paint_session()
                return

        self.btn_paint.setEnabled(False)
        self.btn_resume.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self._stop_flag = False
        self._stop_reason = None
        self._start_esc_listener()

        signals = WorkerSignals()
        signals.progress.connect(lambda x, y: self._on_progress(x, y, total))
        signals.finished.connect(self._on_paint_done)
        signals.error.connect(self._on_paint_error)
        signals.paused.connect(self._on_paint_paused)
        signals.stopped.connect(self._on_paint_stopped)

        def work():
            try:
                opts = PainterOptions(
                    move_duration_s=self._cfg.move_duration_s,
                    mouse_down_s=self._cfg.mouse_down_s,
                    after_click_delay_s=self._cfg.after_click_delay_s,
                    panel_open_delay_s=self._cfg.panel_open_delay_s,
                    shade_select_delay_s=self._cfg.shade_select_delay_s,
                    row_delay_s=self._cfg.row_delay_s,
                    enable_drag_strokes=bool(getattr(self._cfg, "enable_drag_strokes", False)),
                    drag_step_duration_s=float(getattr(self._cfg, "drag_step_duration_s", 0.01)),
                    after_drag_delay_s=float(getattr(self._cfg, "after_drag_delay_s", 0.02)),
                )

                def get_pixel(x: int, y: int):
                    return self._loaded.grid.get(x, y)

                skip_fn = (lambda x, y: (int(x), int(y)) in self._paint_done) if resume else None

                paint_grid(
                    cfg=self._cfg,
                    canvas_rect=self._canvas_rect,
                    grid_w=self._loaded.grid.w,
                    grid_h=self._loaded.grid.h,
                    get_pixel=get_pixel,
                    options=opts,
                    paint_mode=self._cfg.paint_mode,
                    skip=skip_fn,
                    allow_bucket_fill=(not resume),
                    progress_cb=lambda x, y: signals.progress.emit(x, y),
                    should_stop=lambda: self._stop_flag,
                )

                if self._stop_flag:
                    if self._stop_reason == "pause":
                        signals.paused.emit("Paused (ESC)")
                        return
                    if self._stop_reason == "stop":
                        signals.stopped.emit("Stopped")
                        return

                signals.finished.emit()
            except Exception as e:
                signals.error.emit(str(e))

        threading.Thread(target=work, daemon=True).start()

    def _on_resume(self) -> None:
        if not self._paint_paused:
            return
        if self._loaded is None or self._canvas_rect is None:
            return
        if not self._paint_done:
            return

        # Short countdown to refocus the game window.
        if not self._paint_countdown(seconds=2):
            return

        self._paint_paused = False
        self._start_paint_worker(resume=True)


def run():
    # Qt on Windows can emit a scary-but-harmless DPI awareness warning on some setups.
    # Suppress that specific category to keep console output clean.
    rules = os.environ.get("QT_LOGGING_RULES", "")
    if "qt.qpa.window=false" not in rules:
        os.environ["QT_LOGGING_RULES"] = (rules + (";" if rules else "") + "qt.qpa.window=false").strip(";")

    app = QtWidgets.QApplication([])
    w = MainWindow()
    w.resize(900, 650)
    w.show()
    app.exec()
