# /// script
# requires-python = ">=3.12"
# dependencies = ["numpy", "pillow"]
# ///
"""Regenerate the Dracula homepage tile icons from kubernetes/apps/default/homepage/app/icons/sources.tsv.

Each source logo is rasterised, reduced to a mask and traced with potrace, then filled with the
Dracula purple-to-pink gradient. Modes: `alpha` keeps the silhouette, `knock` keeps the logo's
dominant colour and cuts the rest out as see-through detail, `invert` keeps only that detail.

Needs rsvg-convert and potrace on PATH. Run: uv run scripts/homepage_icons.py [slug ...]
"""

import pathlib
import re
import subprocess
import sys
import tempfile
import urllib.request

import numpy as np  # pylint: disable=import-error
from PIL import Image  # pylint: disable=import-error

SIZE = 512
ICONS = pathlib.Path(__file__).resolve().parent.parent / "kubernetes/apps/default/homepage/app/icons"
SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {s} {s}"><defs>'
    '<linearGradient id="g" x1="0" y1="0" x2="1" y2="1">'
    '<stop offset="0" stop-color="#bd93f9"/><stop offset="1" stop-color="#ff79c6"/>'
    "</linearGradient></defs>"
    '<g transform="{tf}" fill="url(#g)" fill-rule="evenodd">{paths}</g></svg>\n'
)


def load(url, tmp):
    """Fetch a source logo and return it as a centred SIZE x SIZE RGBA float array."""
    raw = tmp / ("src" + pathlib.Path(url).suffix)
    with urllib.request.urlopen(url, timeout=30) as resp:  # nosec B310 - fixed https sources
        raw.write_bytes(resp.read())
    if raw.suffix == ".svg":
        png = tmp / "src.png"
        subprocess.run(
            ["rsvg-convert", "-w", str(SIZE), "-h", str(SIZE), "-a", "-b", "none", str(raw), "-o", str(png)],
            check=True,
        )
        img = Image.open(png).convert("RGBA")
    else:
        img = Image.open(raw).convert("RGBA")
        img.thumbnail((SIZE, SIZE), Image.LANCZOS)
    canvas = Image.new("RGBA", (SIZE, SIZE))
    canvas.paste(img, ((SIZE - img.width) // 2, (SIZE - img.height) // 2))
    return np.asarray(canvas).astype(float) / 255


def mask(rgba, mode, thr):
    """Boolean glyph mask for one logo."""
    alpha = rgba[..., 3] > 0.5
    if mode == "alpha":
        return alpha
    rgb = rgba[..., :3]
    quant = (rgb[alpha] * 7).round().astype(int)
    dom = np.bincount(quant[:, 0] * 64 + quant[:, 1] * 8 + quant[:, 2]).argmax()
    near = np.linalg.norm(rgb - np.array([dom // 64, (dom // 8) % 8, dom % 8]) / 7, axis=-1) < thr
    return alpha & (~near if mode == "invert" else near)


def fit(glyph):
    """Crop to the glyph's bounding square with a small margin so every tile fills alike."""
    ys, xs = np.nonzero(glyph)
    side = max(ys.max() - ys.min(), xs.max() - xs.min()) + 1
    half = side // 2 + int(side * 0.06)
    cy, cx = (ys.min() + ys.max()) // 2 + SIZE, (xs.min() + xs.max()) // 2 + SIZE
    big = np.zeros((SIZE * 3, SIZE * 3), bool)
    big[SIZE : 2 * SIZE, SIZE : 2 * SIZE] = glyph
    crop = big[cy - half : cy + half, cx - half : cx + half]
    return np.asarray(Image.fromarray(crop.astype(np.uint8) * 255).resize((SIZE, SIZE), Image.NEAREST)) > 127


def trace(glyph, tmp):
    """Vectorise a mask with potrace; returns the group transform and path data."""
    pbm = tmp / "m.pbm"
    Image.fromarray((~glyph * 255).astype(np.uint8)).convert("1").save(pbm)
    out = subprocess.run(
        ["potrace", "-s", "--flat", "-t", "8", "-a", "1", "-O", "0.6", str(pbm), "-o", "-"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    group = re.search(r'<g transform="([^"]+)"[^>]*>(.*?)</g>', out, re.S)
    if group is None:
        raise RuntimeError("potrace produced no group")
    return group.group(1), re.findall(r'd="([^"]+)"', group.group(2))


def main():
    """Rebuild every icon listed in sources.tsv, or only the slugs given as arguments."""
    only = set(sys.argv[1:])
    for line in (ICONS / "sources.tsv").read_text().splitlines():
        if not line or line.startswith("#"):
            continue
        slug, mode, thr, url = line.split("\t")
        if only and slug not in only:
            continue
        with tempfile.TemporaryDirectory() as tmp:
            glyph = fit(mask(load(url, pathlib.Path(tmp)), mode, float(thr)))
            transform, paths = trace(glyph, pathlib.Path(tmp))
        body = "".join(f'<path d="{d}"/>' for d in paths)
        (ICONS / f"{slug}.svg").write_text(SVG.format(s=SIZE, tf=transform, paths=body))
        print(slug)


if __name__ == "__main__":
    main()
