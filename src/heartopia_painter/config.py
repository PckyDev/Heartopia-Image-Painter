from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple


Point = Tuple[int, int]
RGB = Tuple[int, int, int]
Rect = Tuple[int, int, int, int]


@dataclass
class ShadeButton:
    name: str
    pos: Point
    rgb: RGB


@dataclass
class MainColor:
    name: str
    pos: Point
    rgb: RGB
    shades: List[ShadeButton] = field(default_factory=list)


@dataclass
class AppConfig:
    # Store the UI preset key (e.g. "1:1 (30x30)") so it can be restored on startup.
    canvas_preset: str = "1:1 (30x30)"

    # Convenience: restore last session state
    last_image_path: Optional[str] = None
    last_canvas_rect: Optional[Rect] = None

    # Buttons that are global (same regardless of which color is selected)
    shades_panel_button_pos: Optional[Point] = None
    back_button_pos: Optional[Point] = None

    main_colors: List[MainColor] = field(default_factory=list)

    def to_json_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_json_dict(data: dict) -> "AppConfig":
        def to_tuple2(v):
            if v is None:
                return None
            return (int(v[0]), int(v[1]))

        def to_rgb(v):
            return (int(v[0]), int(v[1]), int(v[2]))

        def to_tuple4(v):
            if v is None:
                return None
            return (int(v[0]), int(v[1]), int(v[2]), int(v[3]))

        cfg = AppConfig()
        preset = data.get("canvas_preset", "1:1 (30x30)")
        # Backward compatibility with early configs that stored "30x30".
        if preset == "30x30":
            preset = "1:1 (30x30)"
        cfg.canvas_preset = str(preset)

        cfg.last_image_path = data.get("last_image_path")
        if cfg.last_image_path is not None:
            cfg.last_image_path = str(cfg.last_image_path)
        cfg.last_canvas_rect = to_tuple4(data.get("last_canvas_rect"))
        cfg.shades_panel_button_pos = to_tuple2(data.get("shades_panel_button_pos"))
        cfg.back_button_pos = to_tuple2(data.get("back_button_pos"))

        cfg.main_colors = []
        for mc in data.get("main_colors", []):
            main = MainColor(
                name=str(mc.get("name", "Unnamed")),
                pos=to_tuple2(mc.get("pos")) or (0, 0),
                rgb=to_rgb(mc.get("rgb", (0, 0, 0))),
                shades=[],
            )
            for sh in mc.get("shades", []):
                main.shades.append(
                    ShadeButton(
                        name=str(sh.get("name", "Shade")),
                        pos=to_tuple2(sh.get("pos")) or (0, 0),
                        rgb=to_rgb(sh.get("rgb", (0, 0, 0))),
                    )
                )
            cfg.main_colors.append(main)
        return cfg


def default_config_path() -> Path:
    # Keep config next to the repo for now
    return Path.cwd() / "config.json"


def load_config(path: Path) -> AppConfig:
    if not path.exists():
        return AppConfig()
    data = json.loads(path.read_text(encoding="utf-8"))
    return AppConfig.from_json_dict(data)


def save_config(path: Path, cfg: AppConfig) -> None:
    path.write_text(json.dumps(cfg.to_json_dict(), indent=2), encoding="utf-8")
