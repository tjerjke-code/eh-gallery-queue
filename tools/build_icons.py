"""Build Windows multi-size ``.ico`` for EH Gallery Queue desktop shortcut.

Generates a square PNG source (procedural artwork) then packs PNG-in-ICO
frames (Vista+). Run from repo root:

    python -m tools.build_icons

Launcher branding only — not gallery thumbnails.
"""

from __future__ import annotations

import struct
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
ICONS = ROOT / "assets" / "icons"
SIZES = (16, 24, 32, 40, 48, 64, 128, 256)
MASTER = 1024


def _lerp(a: float, b: float, t: float) -> float:
    """Linear interpolate ``a`` → ``b`` by ``t``."""
    return a + (b - a) * t


def _gradient_bg(
    size: int,
    top: tuple[int, int, int],
    bottom: tuple[int, int, int],
) -> Image.Image:
    """Vertical RGB gradient background."""
    img = Image.new("RGB", (size, size))
    pixels = img.load()
    assert pixels is not None
    for y in range(size):
        t = y / max(size - 1, 1)
        color = (
            int(_lerp(top[0], bottom[0], t)),
            int(_lerp(top[1], bottom[1], t)),
            int(_lerp(top[2], bottom[2], t)),
        )
        for x in range(size):
            pixels[x, y] = color
    return img.convert("RGBA")


def render_prod_icon(size: int = MASTER) -> Image.Image:
    """Daily / pythonw launcher: stacked gallery frames + accent bar."""
    img = _gradient_bg(size, (28, 24, 36), (12, 10, 16))
    draw = ImageDraw.Draw(img)

    # Three stacked “pages” (back → front)
    layers = (
        (0.22, 0.18, 0.86, 0.78, (64, 56, 80, 255), (120, 108, 148, 255)),
        (0.18, 0.20, 0.82, 0.82, (88, 78, 108, 255), (148, 132, 176, 255)),
        (0.14, 0.24, 0.78, 0.86, (236, 232, 240, 255), (96, 168, 132, 255)),
    )
    for left_f, top_f, right_f, bottom_f, fill, outline in layers:
        box = (
            int(size * left_f),
            int(size * top_f),
            int(size * right_f),
            int(size * bottom_f),
        )
        draw.rounded_rectangle(
            box,
            radius=int(size * 0.06),
            fill=fill,
            outline=outline,
            width=max(3, size // 80),
        )

    # Front “image” window
    inset_l = int(size * 0.22)
    inset_t = int(size * 0.34)
    inset_r = int(size * 0.70)
    inset_b = int(size * 0.72)
    draw.rounded_rectangle(
        (inset_l, inset_t, inset_r, inset_b),
        radius=int(size * 0.04),
        fill=(40, 52, 48, 255),
        outline=(72, 148, 112, 255),
        width=max(2, size // 96),
    )
    # Simple mountain + sun silhouette inside the window
    sun_r = size * 0.05
    sx = size * 0.58
    sy = size * 0.44
    draw.ellipse(
        (sx - sun_r, sy - sun_r, sx + sun_r, sy + sun_r),
        fill=(232, 196, 96, 255),
    )
    draw.polygon(
        [
            (inset_l + size * 0.02, inset_b - size * 0.02),
            (size * 0.36, size * 0.48),
            (size * 0.48, size * 0.58),
            (size * 0.62, size * 0.46),
            (inset_r - size * 0.02, inset_b - size * 0.02),
        ],
        fill=(72, 148, 112, 255),
    )
    return img


def write_png_ico(png_path: Path, ico_path: Path, sizes: tuple[int, ...] = SIZES) -> None:
    """Write a multi-resolution ICO using PNG payloads (Vista+).

    Args:
        png_path: Source artwork (preferably 1024+ square).
        ico_path: Destination ``.ico`` path.
        sizes: Pixel sizes to embed (largest should be 256).
    """
    base = Image.open(png_path).convert("RGBA")
    width, height = base.size
    side = min(width, height)
    left = (width - side) // 2
    top = (height - side) // 2
    base = base.crop((left, top, left + side, top + side))

    frames: list[tuple[int, bytes]] = []
    for size in sizes:
        frame = base.resize((size, size), Image.Resampling.LANCZOS)
        buffer = BytesIO()
        frame.save(buffer, format="PNG", optimize=True)
        frames.append((size, buffer.getvalue()))

    count = len(frames)
    offset = 6 + 16 * count
    header = struct.pack("<HHH", 0, 1, count)
    entries = bytearray()
    blobs = bytearray()
    for size, payload in frames:
        dim = 0 if size >= 256 else size
        entries += struct.pack(
            "<BBBBHHII",
            dim,
            dim,
            0,
            0,
            1,
            32,
            len(payload),
            offset,
        )
        blobs += payload
        offset += len(payload)

    ico_path.parent.mkdir(parents=True, exist_ok=True)
    ico_path.write_bytes(header + entries + blobs)


def run() -> None:
    """Write PNG source and desktop ICO under ``assets/icons``."""
    ICONS.mkdir(parents=True, exist_ok=True)
    png_path = ICONS / "eh-gallery-queue.png"
    ico_path = ICONS / "eh-prod.ico"
    render_prod_icon().save(png_path, format="PNG", optimize=True)
    write_png_ico(png_path, ico_path)
    print(f"Wrote {png_path.name} + {ico_path.name} ({ico_path.stat().st_size} bytes)")


if __name__ == "__main__":
    run()
