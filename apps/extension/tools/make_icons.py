"""Draw the toolbar icons (standard library only): a deep-purple rounded square with the
vivid-green ring of the web app's brand mark.

Run from apps/extension: ``python3 tools/make_icons.py``."""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

PURPLE = (0x37, 0x00, 0x3C)
GREEN = (0x00, 0xFF, 0x87)


def png(width: int, height: int, rgba: bytes) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        c = kind + data
        return struct.pack(">I", len(data)) + c + struct.pack(">I", zlib.crc32(c) & 0xFFFFFFFF)

    stride = width * 4
    raw = b"".join(b"\x00" + rgba[y * stride : (y + 1) * stride] for y in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )


def icon(size: int) -> bytes:
    px = bytearray()
    c = (size - 1) / 2
    radius = size * 0.22  # corner radius of the square
    ring_outer, ring_inner = size * 0.33, size * 0.20
    for y in range(size):
        for x in range(size):
            # inside the rounded square?
            dx = max(abs(x - c) - (c - radius), 0)
            dy = max(abs(y - c) - (c - radius), 0)
            inside = (dx * dx + dy * dy) <= radius * radius
            if not inside:
                px += bytes((0, 0, 0, 0))
                continue
            d = ((x - c) ** 2 + (y - c) ** 2) ** 0.5
            colour = GREEN if ring_inner <= d <= ring_outer else PURPLE
            px += bytes((*colour, 255))
    return png(size, size, bytes(px))


def main() -> None:
    out = Path(__file__).resolve().parent.parent / "src" / "icons"
    out.mkdir(parents=True, exist_ok=True)
    for size in (16, 32, 48, 128):
        (out / f"icon-{size}.png").write_bytes(icon(size))
        print(out / f"icon-{size}.png")


if __name__ == "__main__":
    main()
