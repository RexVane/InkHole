"""Generate desktop icon assets from the shared InkHole brand renderer."""

from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PIL import Image
from PySide6.QtGui import QGuiApplication


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from inkhole.branding import app_icon_pixmap


def main() -> None:
    app = QGuiApplication.instance() or QGuiApplication([])
    output = ROOT / "assets"
    output.mkdir(parents=True, exist_ok=True)

    png_path = output / "inkhole.png"
    if not app_icon_pixmap(1024).save(str(png_path), "PNG"):
        raise RuntimeError(f"failed to write {png_path}")

    with Image.open(png_path) as source:
        image = source.convert("RGBA")
        image.save(
            output / "inkhole.ico",
            format="ICO",
            sizes=[(16, 16), (24, 24), (32, 32), (48, 48),
                   (64, 64), (128, 128), (256, 256)],
        )
        image.save(output / "inkhole.icns", format="ICNS")

    print(f"Generated icons in {output}")


if __name__ == "__main__":
    main()
