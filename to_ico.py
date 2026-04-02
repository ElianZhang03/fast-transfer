#!/usr/bin/env python3
"""PNG/JPG 等图片 → logo.ico（最大 256×256，透明底保留）。依赖: pip install Pillow"""
from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image

# Pillow 9+: Image.Resampling.LANCZOS；旧版用 Image.LANCZOS
try:
    _LANCZOS = Image.Resampling.LANCZOS
except AttributeError:
    _LANCZOS = Image.LANCZOS


def main() -> None:
    base = Path(__file__).resolve().parent
    in_path = Path(sys.argv[1]) if len(sys.argv) > 1 else base / "icon.png"
    out_path = Path(sys.argv[2]) if len(sys.argv) > 2 else base / "logo.ico"

    if not in_path.is_file():
        raise SystemExit(f"找不到输入文件: {in_path}")

    im = Image.open(in_path).convert("RGBA")
    im.thumbnail((256, 256), _LANCZOS)

    canvas = Image.new("RGBA", (256, 256), (0, 0, 0, 0))
    x = (256 - im.width) // 2
    y = (256 - im.height) // 2
    canvas.paste(im, (x, y), im)

    canvas.save(
        out_path,
        format="ICO",
        sizes=[(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (16, 16)],
    )
    print(f"已生成: {out_path.resolve()}")


if __name__ == "__main__":
    main()
