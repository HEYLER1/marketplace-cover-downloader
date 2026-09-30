#!/usr/bin/env python3
"""Arma una hoja de contactos con portadas ya descargadas, para el README.

Uso:
    venv/bin/python muestras.py [carpeta] [salida.png]
"""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_SOURCE = BASE_DIR / "marketplace_portadas" / "callao"
DEFAULT_TARGET = BASE_DIR / "docs" / "capturas" / "05-portadas-guardadas.png"

COLUMNS = 6
ROWS = 4
CELL = 300
CAPTION = 26
MARGIN = 28
TITLE_HEIGHT = 96
BACKGROUND = "#0d1117"
CELL_BACKGROUND = "#161b22"
FOREGROUND = "#e6edf3"
MUTED = "#8b949e"

FONT_CANDIDATES = (
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
)


def load_font(size: int) -> ImageFont.ImageFont:
    for candidate in FONT_CANDIDATES:
        path = Path(candidate)
        if path.exists():
            try:
                return ImageFont.truetype(str(path), size)
            except OSError:
                continue
    return ImageFont.load_default()


def short_name(name: str, font: ImageFont.ImageFont, max_width: int) -> str:
    if font.getlength(name) <= max_width:
        return name
    cut = name
    while cut and font.getlength(cut + "…") > max_width:
        cut = cut[:-1]
    return cut + "…"


def build_contact_sheet(source: Path, target: Path) -> Path:
    files = sorted(source.glob("*.jpg"))
    if not files:
        raise SystemExit(f"No hay imágenes JPEG en {source}")
    step = max(len(files) // (COLUMNS * ROWS), 1)
    chosen = files[::step][: COLUMNS * ROWS]

    width = MARGIN * 2 + COLUMNS * CELL
    height = TITLE_HEIGHT + MARGIN + ROWS * (CELL + CAPTION)
    sheet = Image.new("RGB", (width, height), BACKGROUND)
    draw = ImageDraw.Draw(sheet)
    title_font = load_font(34)
    caption_font = load_font(17)

    draw.text(
        (MARGIN, 26),
        f"{len(files)} portadas guardadas en marketplace_portadas/{source.name}/",
        font=title_font,
        fill=FOREGROUND,
    )
    draw.text(
        (MARGIN, 64),
        "filtro de texto + YOLO11n (detección) + YOLO11n-cls (clasificación)",
        font=caption_font,
        fill=MUTED,
    )

    for index, path in enumerate(chosen):
        column = index % COLUMNS
        row = index // COLUMNS
        x = MARGIN + column * CELL
        y = TITLE_HEIGHT + MARGIN + row * (CELL + CAPTION)
        with Image.open(path) as raw:
            thumb = ImageOps.exif_transpose(raw).convert("RGB")
            thumb.thumbnail((CELL - 16, CELL - 16), Image.LANCZOS)
        box = Image.new("RGB", (CELL - 8, CELL - 8), CELL_BACKGROUND)
        box.paste(thumb, ((box.width - thumb.width) // 2, (box.height - thumb.height) // 2))
        sheet.paste(box, (x, y))
        draw.text(
            (x + 4, y + CELL - 2),
            short_name(path.name, caption_font, CELL - 12),
            font=caption_font,
            fill=MUTED,
        )

    target.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(target, optimize=True)
    print(f"{target.relative_to(BASE_DIR)}  ({sheet.width}x{sheet.height}, {len(chosen)} de {len(files)})")
    return target


if __name__ == "__main__":
    source = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_SOURCE
    target = Path(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_TARGET
    build_contact_sheet(source, target)