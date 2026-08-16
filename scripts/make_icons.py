"""Write minimal solid-color PNGs and a tiny ICO for Tauri icons (stdlib only)."""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "src-tauri" / "icons"
OUT.mkdir(parents=True, exist_ok=True)

# Warm violet matching the UI accent
R, G, B, A = 124, 88, 196, 255


def png(size: int) -> bytes:
    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    raw = bytearray()
    for y in range(size):
        raw.append(0)
        for x in range(size):
            m = size / 2
            r = size * 0.42
            dx, dy = x + 0.5 - m, y + 0.5 - m
            inside = (abs(dx) ** 4 + abs(dy) ** 4) ** 0.25 <= r
            if inside:
                raw.extend((R, G, B, A))
            else:
                raw.extend((0, 0, 0, 0))

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
        + chunk(b"IEND", b"")
    )


def ico(sizes: list[int]) -> bytes:
    images = [(s, png(s)) for s in sizes]
    header = struct.pack("<HHH", 0, 1, len(images))
    offset = 6 + 16 * len(images)
    entries = b""
    data = b""
    for s, blob in images:
        entries += struct.pack(
            "<BBBBHHII",
            s if s < 256 else 0,
            s if s < 256 else 0,
            0,
            0,
            1,
            32,
            len(blob),
            offset,
        )
        data += blob
        offset += len(blob)
    return header + entries + data


def main() -> None:
    for name, size in (
        ("32x32.png", 32),
        ("128x128.png", 128),
        ("128x128@2x.png", 256),
        ("icon.png", 128),
    ):
        (OUT / name).write_bytes(png(size))
    (OUT / "icon.ico").write_bytes(ico([16, 32, 48, 256]))
    # Placeholder so the path exists; replace with a real .icns for macOS shipping.
    (OUT / "icon.icns").write_bytes(png(256))
    print(f"Wrote icons to {OUT}")


if __name__ == "__main__":
    main()
