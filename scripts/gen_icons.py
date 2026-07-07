"""Generate the Aeolus PWA raster icons with Pillow.

Mirrors static/icons/aeolus.svg: a rounded-square night-sky tile with three
wind gusts (the long middle one in prairie amber), each gust a horizontal
stroke ending in a curl. Drawn supersampled, then downscaled with Lanczos.

Run from the repo root: python scripts/gen_icons.py
Outputs: static/icons/icon-192.png, icon-512.png, icon-maskable-512.png,
apple-touch-icon.png (180).
"""

import os

from PIL import Image, ImageDraw

BG = (22, 35, 58, 255)        # night sky
LIGHT = (220, 233, 247, 255)  # gust strokes
AMBER = (237, 161, 0, 255)    # prairie amber, the brand accent

SS = 4  # supersample factor
OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "static", "icons")


def _cap(draw, xy, r, color):
    x, y = xy
    draw.ellipse([x - r, y - r, x + r, y + r], fill=color)


def _gust(draw, u, x0, x1, y, curl_r, curl_up, width, color):
    """One wind stroke: horizontal line ending in a curl at the right end.

    The curl is a 270-degree arc of radius curl_r tangent to the line end,
    above the line when curl_up else below. u = design-unit scale.
    """
    lw = int(width * u)
    r = curl_r * u
    draw.line([x0 * u, y * u, x1 * u, y * u], fill=color, width=lw)
    _cap(draw, (x0 * u, y * u), lw / 2, color)

    # circle tangent to the line end, centered directly above/below it
    cy = y * u - r if curl_up else y * u + r
    cx = x1 * u
    bbox = [cx - r, cy - r, cx + r, cy + r]
    # Pillow arc angles: 0 = 3 o'clock, increasing clockwise.
    # Start at the tangent point (bottom of circle when curl_up, top when
    # not) and sweep 270 degrees away from the line.
    if curl_up:
        draw.arc(bbox, start=90, end=0, fill=color, width=lw)
    else:
        draw.arc(bbox, start=270, end=180, fill=color, width=lw)
    # round the free end of the arc (at 0 deg / 180 deg on the circle)
    end = (cx + r, cy) if curl_up else (cx - r, cy)
    _cap(draw, end, lw / 2, color)
    _cap(draw, (cx, y * u), lw / 2, color)


def draw_tile(size: int, glyph_scale: float = 1.0, rounded: bool = True) -> Image.Image:
    """The icon at `size` px. glyph_scale < 1 shrinks the glyph toward the
    center (maskable safe zone); rounded=False keeps the tile full-bleed."""
    big = size * SS
    u = big / 24.0  # design grid: 24 units, matching the SVG
    img = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    if rounded:
        d.rounded_rectangle([0, 0, big - 1, big - 1], radius=int(5.4 * u), fill=BG)
    else:
        d.rectangle([0, 0, big, big], fill=BG)

    glyph = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glyph)
    _gust(gd, u, 4.0, 11.4, 8.0, 2.3, True, 1.3, LIGHT)
    _gust(gd, u, 4.0, 16.4, 12.2, 2.7, True, 1.3, AMBER)
    _gust(gd, u, 4.0, 13.2, 16.4, 2.3, False, 1.3, LIGHT)

    if glyph_scale != 1.0:
        gs = int(big * glyph_scale)
        glyph = glyph.resize((gs, gs), Image.LANCZOS)
        pad = (big - gs) // 2
        img.alpha_composite(glyph, (pad, pad))
    else:
        img.alpha_composite(glyph)

    return img.resize((size, size), Image.LANCZOS)


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    outputs = {
        "icon-192.png": draw_tile(192),
        "icon-512.png": draw_tile(512),
        # maskable: full-bleed background, glyph inside the 80% safe zone
        "icon-maskable-512.png": draw_tile(512, glyph_scale=0.72, rounded=False),
        "apple-touch-icon.png": draw_tile(180, rounded=False),
    }
    for name, img in outputs.items():
        path = os.path.join(OUT_DIR, name)
        img.save(path, "PNG")
        print(f"wrote {path} ({img.size[0]}x{img.size[1]})")


if __name__ == "__main__":
    main()
