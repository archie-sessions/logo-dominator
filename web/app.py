#!/usr/bin/env python3
"""
The Logo Dominator — web app
Equalize visual weight of SVG/EPS logos and export as a combined SVG.
"""

import os
import sys

# Ensure Homebrew cairo is on the dynamic linker path (macOS)
_brew_lib = "/opt/homebrew/lib"
if os.path.isdir(_brew_lib) and _brew_lib not in os.environ.get("DYLD_LIBRARY_PATH", ""):
    os.environ["DYLD_LIBRARY_PATH"] = _brew_lib + ":" + os.environ.get("DYLD_LIBRARY_PATH", "")
    os.execve(sys.executable, [sys.executable] + sys.argv, os.environ)

import io
import math
import re
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path

import cairosvg
import numpy as np
from flask import Flask, jsonify, render_template, request, send_file
from lxml import etree
from PIL import Image

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024  # 50 MB

TEMP_DIR = Path(tempfile.mkdtemp(prefix="logodom_"))
ALLOWED_EXT = {"svg", "eps"}

# ── SVG namespace registration so lxml doesn't emit ns0: prefixes ─────────────
SVG_NS = "http://www.w3.org/2000/svg"
XLINK_NS = "http://www.w3.org/1999/xlink"
etree.register_namespace = getattr(etree, "register_namespace", lambda *a: None)
for _prefix, _uri in [
    ("svg", SVG_NS),
    ("xlink", XLINK_NS),
    ("dc", "http://purl.org/dc/elements/1.1/"),
    ("cc", "http://creativecommons.org/ns#"),
    ("rdf", "http://www.w3.org/1999/02/22-rdf-syntax-ns#"),
    ("inkscape", "http://www.inkscape.org/namespaces/inkscape"),
    ("sodipodi", "http://sodipodi.sourceforge.net/DTD/sodipodi-0.0.dtd"),
]:
    try:
        etree._Element.__module__  # lxml check
    except Exception:
        pass


# ── helpers ───────────────────────────────────────────────────────────────────

def allowed(filename: str) -> bool:
    return Path(filename).suffix.lower().lstrip(".") in ALLOWED_EXT


def _find_tool(*names: str) -> str | None:
    """Find first available binary, checking Homebrew prefix explicitly."""
    extra = ["/opt/homebrew/bin", "/usr/local/bin"]
    for name in names:
        found = shutil.which(name)
        if found:
            return found
        for prefix in extra:
            candidate = os.path.join(prefix, name)
            if os.path.isfile(candidate):
                return candidate
    return None


def eps_to_svg(eps_path: str) -> str | None:
    """Convert EPS → SVG via Ghostscript (EPS→PDF) + pdf2svg (PDF→SVG)."""
    gs = _find_tool("gs")
    p2s = _find_tool("pdf2svg")
    if not gs or not p2s:
        missing = []
        if not gs:  missing.append("Ghostscript (brew install ghostscript)")
        if not p2s: missing.append("pdf2svg (brew install pdf2svg)")
        raise RuntimeError("Missing tools: " + ", ".join(missing))

    pdf_path = eps_path.replace(".eps", "_tmp.pdf")
    svg_path = eps_path.replace(".eps", "_converted.svg")

    # Step 1: EPS → PDF
    r1 = subprocess.run(
        [gs, "-dBATCH", "-dNOPAUSE", "-dNOSAFER", "-dEPSCrop",
         "-sDEVICE=pdfwrite", f"-sOutputFile={pdf_path}", eps_path],
        capture_output=True,
    )
    if r1.returncode != 0 or not Path(pdf_path).exists():
        raise RuntimeError(f"Ghostscript failed: {r1.stderr.decode(errors='replace').strip()}")

    # Step 2: PDF → SVG
    r2 = subprocess.run([p2s, pdf_path, svg_path], capture_output=True)
    if r2.returncode != 0 or not Path(svg_path).exists():
        raise RuntimeError(f"pdf2svg failed: {r2.stderr.decode(errors='replace').strip()}")

    Path(pdf_path).unlink(missing_ok=True)
    return svg_path


def svg_to_pil(svg_path: str, height: int) -> Image.Image:
    png = cairosvg.svg2png(url=svg_path, output_height=height)
    return Image.open(io.BytesIO(png)).convert("RGBA")


def measure_logo(img: Image.Image) -> dict:
    """
    Analyze a rasterized logo. Returns:
      pixels      — inked pixel count (opaque, non-white)
      content_w/h — tight bounding box around actual content (strips SVG whitespace)
      density     — coverage ratio: pixels / content area (0–1)
      darkness    — mean darkness of inked pixels (0=white, 1=black)
    """
    arr = np.array(img)
    h, w = arr.shape[:2]

    opaque = arr[:, :, 3] > 32
    near_white = (arr[:, :, 0] > 240) & (arr[:, :, 1] > 240) & (arr[:, :, 2] > 240)
    inked = opaque & ~near_white
    pixel_count = int(np.sum(inked))

    if pixel_count == 0:
        return {"pixels": 0, "content_w": w, "content_h": h, "density": 0.0, "darkness": 0.0}

    # Tight content bounding box — strips built-in SVG whitespace padding
    rows_with_ink = np.any(inked, axis=1)
    cols_with_ink = np.any(inked, axis=0)
    row_min = int(np.argmax(rows_with_ink))
    row_max = int(h - 1 - np.argmax(rows_with_ink[::-1]))
    col_min = int(np.argmax(cols_with_ink))
    col_max = int(w - 1 - np.argmax(cols_with_ink[::-1]))
    content_h = max(1, row_max - row_min + 1)
    content_w = max(1, col_max - col_min + 1)

    # Density: how much of the content bounding box is actually inked
    density = pixel_count / (content_w * content_h)

    # Darkness: mean darkness of inked pixels (0=light, 1=dark)
    inked_px = arr[inked]  # shape (N, 4)
    lightness = (inked_px[:, 0].astype(np.float32) +
                 inked_px[:, 1].astype(np.float32) +
                 inked_px[:, 2].astype(np.float32)) / 3.0
    darkness = float(1.0 - np.mean(lightness) / 255.0)

    # Vertical centre of mass, weighted by darkness of each inked pixel
    lum_weights = 1.0 - (arr[inked, :3].astype(np.float32).mean(axis=1) / 255.0)
    ys = np.where(inked)[0]  # row index of every inked pixel
    vc_y = float(np.average(ys, weights=lum_weights) / h)

    return {
        "pixels": pixel_count,
        "content_w": content_w,
        "content_h": content_h,
        "density": density,
        "darkness": darkness,
        "vc_y": vc_y,
    }


def _parse_dim(val: str, default: float = 100.0) -> float:
    """Strip CSS units and return float."""
    m = re.match(r"[\d.]+", str(val or default).strip())
    return float(m.group()) if m else default


def get_svg_viewbox(svg_path: str) -> tuple[float, float, float, float]:
    """Return (min_x, min_y, vw, vh). Falls back to width/height attrs."""
    try:
        parser = etree.XMLParser(recover=True, remove_comments=False)
        root = etree.parse(svg_path, parser).getroot()
        vb = root.get("viewBox") or root.get("viewbox", "")
        if vb:
            parts = re.split(r"[\s,]+", vb.strip())
            return tuple(map(float, parts[:4]))
        w = _parse_dim(root.get("width", "100"))
        h = _parse_dim(root.get("height", "100"))
        return 0.0, 0.0, w, h
    except Exception:
        return 0.0, 0.0, 100.0, 100.0


def make_id_prefix(name: str, index: int) -> str:
    """Sanitized ID prefix to avoid collisions when embedding multiple SVGs."""
    safe = re.sub(r"[^a-zA-Z0-9]", "_", Path(name).stem)[:20]
    return f"ld{index}_{safe}"


def prefix_ids(root: etree._Element, prefix: str) -> None:
    """Prefix all id= and href/xlink:href references to avoid collisions."""
    id_map: dict[str, str] = {}

    # First pass: collect and rename all ids
    for el in root.iter():
        old_id = el.get("id")
        if old_id:
            new_id = f"{prefix}_{old_id}"
            id_map[old_id] = new_id
            el.set("id", new_id)

    if not id_map:
        return

    # Build regex for url(#id) and href="#id" patterns
    pattern = re.compile(r"(url\(#|#)(" + "|".join(re.escape(k) for k in id_map) + r")\b")

    def _rewrite(val: str) -> str:
        def _sub(m):
            return m.group(1) + id_map[m.group(2)]
        return pattern.sub(_sub, val)

    # Second pass: rewrite references
    xlink_href = f"{{{XLINK_NS}}}href"
    for el in root.iter():
        for attr in ("href", xlink_href, "style", "clip-path", "mask", "filter", "fill", "stroke"):
            v = el.get(attr)
            if v and ("#" in v or "url(" in v):
                el.set(attr, _rewrite(v))
        # Also rewrite text content (rare but possible in <style> blocks)
        if el.text and ("#" in el.text or "url(" in el.text):
            el.text = _rewrite(el.text)


def embed_svg(svg_path: str, x: float, y: float, w: float, h: float,
              vx: float, vy: float, vw: float, vh: float,
              id_prefix: str) -> str:
    """
    Return an SVG string fragment: the logo's root <svg> repositioned and
    sized, with IDs prefixed to avoid collisions.
    """
    parser = etree.XMLParser(recover=True, remove_comments=False)
    try:
        root = etree.parse(svg_path, parser).getroot()
    except Exception:
        return ""

    # Prefix IDs
    prefix_ids(root, id_prefix)

    # Remove any fixed width/height that would override our sizing
    root.attrib.pop("width", None)
    root.attrib.pop("height", None)

    # Set position/size/viewBox
    root.set("x", f"{x:.4f}")
    root.set("y", f"{y:.4f}")
    root.set("width", f"{w:.4f}")
    root.set("height", f"{h:.4f}")
    root.set("viewBox", f"{vx} {vy} {vw} {vh}")
    root.set("preserveAspectRatio", "xMidYMid meet")
    root.set("overflow", "hidden")

    etree.cleanup_namespaces(root)
    return etree.tostring(root, encoding="unicode", pretty_print=False)


def compose_svg(logo_items: list[dict], gap: int = 60, row_gap: int = 80,
                padding: int = 50, cols: int = 4) -> str:
    """Build and return the composite SVG string, wrapping into rows of `cols`."""
    REF_H = 200.0  # SVG coordinate units for reference height

    logos = []
    for i, item in enumerate(logo_items):
        vx, vy, vw, vh = get_svg_viewbox(item["svg_path"])
        h = REF_H * item["scale"]
        w = (vw / vh * h) if vh > 0 else REF_H
        logos.append({**item, "vx": vx, "vy": vy, "vw": vw, "vh": vh,
                      "w": w, "h": h, "idx": i})

    # Split into rows of `cols`
    rows = [logos[i:i + cols] for i in range(0, len(logos), cols)]
    n_cols = min(cols, len(logos))

    # Column widths: max logo width at each column index across all rows
    col_widths = [0.0] * n_cols
    for row in rows:
        for j, logo in enumerate(row):
            col_widths[j] = max(col_widths[j], logo["w"])

    # Row heights: tallest logo in each row
    row_heights = [max(l["h"] for l in row) for row in rows]

    canvas_w = sum(col_widths) + gap * (n_cols - 1) + 2 * padding
    canvas_h = sum(row_heights) + row_gap * (len(rows) - 1) + 2 * padding

    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'xmlns:xlink="http://www.w3.org/1999/xlink" '
        f'viewBox="0 0 {canvas_w:.3f} {canvas_h:.3f}" '
        f'width="{canvas_w:.3f}" height="{canvas_h:.3f}">',
        '<rect width="100%" height="100%" fill="white"/>',
    ]

    # Precompute left edge of each column
    col_x = [padding + sum(col_widths[:j]) + gap * j for j in range(n_cols)]

    y = float(padding)
    for r, row in enumerate(rows):
        row_h = row_heights[r]
        for j, logo in enumerate(row):
            # Center logo within its column cell; align vertically by visual centre of mass
            lx = col_x[j] + (col_widths[j] - logo["w"]) / 2
            vc_y = logo.get("vc_y", 0.5)
            ly = y + row_h / 2 - logo["h"] * vc_y
            ly = max(y, min(ly, y + row_h - logo["h"]))  # clamp within row bounds
            prefix = make_id_prefix(logo["name"], logo["idx"])
            parts.append(embed_svg(
                logo["svg_path"], lx, ly, logo["w"], logo["h"],
                logo["vx"], logo["vy"], logo["vw"], logo["vh"],
                prefix,
            ))
        y += row_h + row_gap

    parts.append("</svg>")
    return "\n".join(parts)


# ── routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/process", methods=["POST"])
def process():
    files = request.files.getlist("files")
    if not files or all(f.filename == "" for f in files):
        return jsonify(error="No files received."), 400

    session_dir = TEMP_DIR / uuid.uuid4().hex
    session_dir.mkdir()

    logo_items: list[dict] = []
    errors: list[str] = []

    for f in files:
        if not f.filename:
            continue
        name = Path(f.filename).name
        if not allowed(name):
            errors.append(f"{name}: unsupported format (use .svg or .eps)")
            continue

        save_path = str(session_dir / name)
        f.save(save_path)

        svg_path = save_path
        if name.lower().endswith(".eps"):
            try:
                svg_path = eps_to_svg(save_path)
            except RuntimeError as e:
                errors.append(f"{name}: {e}")
                continue

        logo_items.append({"name": name, "svg_path": svg_path})

    if not logo_items:
        return jsonify(error="No valid files. " + " | ".join(errors)), 400

    # Rasterize and measure logos
    RENDER_H = 400
    # Density correction reference: logos at ~35% coverage need no adjustment.
    # Sparse logos (outlines) scale up; dense logos (solid fills) scale down.
    DENSITY_REF = 0.35
    DENSITY_FACTOR = 0.5  # exponent — 0=no correction, 1=full correction

    for item in logo_items:
        try:
            img = svg_to_pil(item["svg_path"], RENDER_H)
            m = measure_logo(img)
            item.update(m)
            item["rendered_w"], item["rendered_h"] = img.size
        except Exception as e:
            item.update({"pixels": 0, "content_w": 0, "content_h": 0,
                         "density": 0.0, "darkness": 0.0})
            item["rendered_w"] = item["rendered_h"] = 0
            errors.append(f'{item["name"]}: render error — {e}')

    valid = [it for it in logo_items if it["pixels"] > 0]
    if not valid:
        return jsonify(error="Could not rasterize any logo."), 500

    target = int(np.median([it["pixels"] for it in valid]))

    for item in logo_items:
        c = item["pixels"]
        base_scale = math.sqrt(target / c) if c > 0 else 1.0

        # Density correction: sparse logos (outlines) need more room to look
        # as heavy as solid-filled logos with the same raw pixel count.
        d = item.get("density", DENSITY_REF)
        if d > 0:
            density_correction = (DENSITY_REF / d) ** DENSITY_FACTOR
            density_correction = max(0.5, min(2.0, density_correction))
        else:
            density_correction = 1.0

        item["scale"] = base_scale * density_correction

    svg_string = compose_svg(logo_items)

    # Persist for download
    session_id = session_dir.name
    (session_dir / "output.svg").write_text(svg_string, encoding="utf-8")

    stats = [
        {
            "name": it["name"],
            "pixels": it["pixels"],
            "scale": round(it["scale"], 4),
            "density": round(it.get("density", 0.0), 3),
        }
        for it in logo_items
    ]

    return jsonify(
        session_id=session_id,
        svg=svg_string,
        stats=stats,
        target_pixels=target,
        errors=errors,
    )


@app.route("/download/<session_id>")
def download(session_id: str):
    if not re.fullmatch(r"[a-f0-9]{32}", session_id):
        return "Invalid session", 400
    path = TEMP_DIR / session_id / "output.svg"
    if not path.exists():
        return "Not found — did you process files first?", 404
    return send_file(str(path), as_attachment=True,
                     download_name="equalized_logos.svg",
                     mimetype="image/svg+xml")


if __name__ == "__main__":
    print(f"Temp dir: {TEMP_DIR}")
    app.run(debug=True, port=5050, use_reloader=False)
