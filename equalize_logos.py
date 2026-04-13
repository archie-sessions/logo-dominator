#!/usr/bin/env python3
# Ensure Homebrew cairo is findable on macOS
import os, sys
_brew_lib = "/opt/homebrew/lib"
if os.path.isdir(_brew_lib):
    os.environ.setdefault("DYLD_LIBRARY_PATH", _brew_lib)
    # If the library wasn't loaded yet, re-exec with the env set
    if "cairosvg" not in sys.modules and _brew_lib not in os.environ.get("DYLD_LIBRARY_PATH", ""):
        os.execve(sys.executable, [sys.executable] + sys.argv, os.environ)

"""
equalize_logos.py — align SVG logos on a horizontal axis with equal visual weight.

Visual weight is equalized by scaling each logo so it contains the same number
of dark pixels when rasterized.

Usage:
    python3 equalize_logos.py <svg1> <svg2> ... [options]
    python3 equalize_logos.py SVGs/*.svg -o output.png

Options:
    -o, --output PATH     Output file (PNG). Default: output.png
    --gap PIXELS          Gap between logos. Default: 80
    --padding PIXELS      Padding around the whole image. Default: 60
    --bg COLOR            Background color (white/transparent). Default: white
    --render-size PIXELS  Height to render each logo at for pixel counting. Default: 400
    --target-pixels N     Target dark pixel count. Default: median of all logos.
"""

import argparse
import math
import sys
from pathlib import Path

import cairosvg
import numpy as np
from PIL import Image
import io


def svg_to_image(svg_path: str, height: int) -> Image.Image:
    """Render an SVG to a PIL Image at the given height (width auto-scaled)."""
    png_bytes = cairosvg.svg2png(url=svg_path, output_height=height)
    return Image.open(io.BytesIO(png_bytes)).convert("RGBA")


def count_inked_pixels(img: Image.Image) -> int:
    """Count opaque pixels that aren't near-white — works for any logo color."""
    arr = np.array(img)
    opaque = arr[:, :, 3] > 32
    near_white = (arr[:, :, 0] > 240) & (arr[:, :, 1] > 240) & (arr[:, :, 2] > 240)
    return int(np.sum(opaque & ~near_white))


def get_svg_natural_size(svg_path: str, render_height: int) -> tuple[float, float]:
    """Return the natural (width, height) of an SVG by rendering and checking."""
    png_bytes = cairosvg.svg2png(url=svg_path, output_height=render_height)
    img = Image.open(io.BytesIO(png_bytes))
    return img.size  # (width, height)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("svgs", nargs="+", help="SVG files to process")
    parser.add_argument("-o", "--output", default="output.png", help="Output PNG path")
    parser.add_argument("--gap", type=int, default=80, help="Gap between logos in pixels")
    parser.add_argument("--padding", type=int, default=60, help="Padding around image in pixels")
    parser.add_argument("--bg", default="white", choices=["white", "transparent"], help="Background color")
    parser.add_argument("--render-size", type=int, default=400, help="Reference render height for pixel counting")
    parser.add_argument("--target-pixels", type=int, default=None, help="Target dark pixel count (default: median)")
    args = parser.parse_args()

    svg_paths = [str(Path(p).resolve()) for p in args.svgs]
    for p in svg_paths:
        if not Path(p).exists():
            print(f"Error: file not found: {p}", file=sys.stderr)
            sys.exit(1)

    print(f"Processing {len(svg_paths)} logos...")

    # Step 1: render each at reference size and count dark pixels
    render_h = args.render_size
    pixel_counts = []
    natural_sizes = []

    for svg in svg_paths:
        print(f"  Counting pixels: {Path(svg).name}")
        img = svg_to_image(svg, render_h)
        count = count_inked_pixels(img)
        pixel_counts.append(count)
        natural_sizes.append(img.size)  # (w, h) at render_h
        print(f"    → {count:,} inked pixels  ({img.size[0]}×{img.size[1]} rendered)")

    # Step 2: determine target pixel count
    target = args.target_pixels or int(np.median(pixel_counts))
    print(f"\nTarget inked pixels: {target:,}")

    # Step 3: compute scale factors
    # inked_pixels ∝ scale², so scale = sqrt(target / count)
    scale_factors = []
    for i, count in enumerate(pixel_counts):
        if count == 0:
            print(f"Warning: {Path(svg_paths[i]).name} has 0 inked pixels — skipping scale", file=sys.stderr)
            scale_factors.append(1.0)
        else:
            scale_factors.append(math.sqrt(target / count))

    # Step 4: compute final rendered heights for each logo
    # We render at render_h; the final height = render_h * scale_factor
    final_heights = [int(round(render_h * s)) for s in scale_factors]
    final_widths = [int(round(natural_sizes[i][0] * scale_factors[i])) for i in range(len(svg_paths))]

    print("\nScaled sizes:")
    for i, svg in enumerate(svg_paths):
        print(f"  {Path(svg).name}: scale={scale_factors[i]:.3f}  →  {final_widths[i]}×{final_heights[i]}")

    # Step 5: render each logo at its final size
    logo_images = []
    for i, svg in enumerate(svg_paths):
        print(f"  Rendering final: {Path(svg).name}")
        img = svg_to_image(svg, final_heights[i])
        logo_images.append(img)

    # Step 6: compose horizontally, vertically centered
    canvas_h = max(final_heights) + 2 * args.padding
    total_w = sum(img.width for img in logo_images) + args.gap * (len(logo_images) - 1) + 2 * args.padding

    if args.bg == "white":
        canvas = Image.new("RGBA", (total_w, canvas_h), (255, 255, 255, 255))
    else:
        canvas = Image.new("RGBA", (total_w, canvas_h), (0, 0, 0, 0))

    x = args.padding
    for img in logo_images:
        y = (canvas_h - img.height) // 2  # vertically center
        canvas.paste(img, (x, y), mask=img)
        x += img.width + args.gap

    # Step 7: save
    output_path = Path(args.output)
    if args.bg == "white":
        canvas = canvas.convert("RGB")
    canvas.save(str(output_path))
    print(f"\nSaved → {output_path.resolve()}")
    print(f"Canvas size: {canvas.width}×{canvas.height}px")


if __name__ == "__main__":
    main()
