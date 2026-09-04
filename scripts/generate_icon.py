"""Generate square app-icon artifacts from the committed logo source."""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageChops

ICON_SIZES = [16, 24, 32, 48, 64, 128, 256]
CROP_PADDING_RATIO = 0.05


def _content_box(image: Image.Image) -> tuple[int, int, int, int] | None:
    """Return the bounding box of non-background pixels, if any.

    AI-generated backgrounds carry near-white noise, so anything at or above
    245 counts as background.
    """

    mask = image.convert("L").point(lambda value: 0 if value >= 245 else 255)
    return mask.getbbox()


def _square_logo(source: Path) -> Image.Image:
    """Crop the logo to its content and center it on a square canvas."""

    with Image.open(source) as image:
        image.load()
        canvas = Image.new("RGBA", image.size, (255, 255, 255, 0))
        merged = Image.alpha_composite(canvas, image.convert("RGBA"))
    # The line-art mark reads best with a transparent background: near-white
    # pixels (background noise plus the anti-aliased halo) become transparent
    # while the dark strokes are untouched.
    opaque = merged.convert("L").point(lambda value: 0 if value >= 245 else 255)
    merged.putalpha(ImageChops.darker(merged.getchannel("A"), opaque))
    box = _content_box(merged)
    if box is None:
        return merged
    padding = int(max(merged.size) * CROP_PADDING_RATIO)
    left = max(box[0] - padding, 0)
    upper = max(box[1] - padding, 0)
    right = min(box[2] + padding, merged.width)
    lower = min(box[3] + padding, merged.height)
    cropped = merged.crop((left, upper, right, lower))
    side = max(cropped.size)
    square = Image.new("RGBA", (side, side), (255, 255, 255, 0))
    square.alpha_composite(cropped, ((side - cropped.width) // 2, (side - cropped.height) // 2))
    return square


def main(argv: list[str] | None = None) -> int:
    """Write assets/icon-square.png and assets/icon.ico from assets/icon.png."""

    del argv
    root = Path(__file__).resolve().parent.parent
    source = root / "assets" / "icon.png"
    square_path = root / "assets" / "icon-square.png"
    target = root / "assets" / "icon.ico"
    if not source.is_file():
        print(f"Missing logo source: {source}", file=sys.stderr)
        return 1
    square = _square_logo(source)
    print(f"Cropped logo bounds: {square.size}")
    square.save(square_path, format="PNG")
    square.save(target, format="ICO", sizes=[(size, size) for size in ICON_SIZES])
    print(f"Wrote {square_path} and {target} ({target.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
