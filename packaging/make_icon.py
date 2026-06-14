#!/usr/bin/env python3
"""
Generate the TUBE-RIPPER app icon + web icons, themed to match the wordmark:
a glossy red play button with a teal glow on a deep navy squircle.

Outputs:
  packaging/app.icns                (macOS app icon, via iconutil)
  packaging/icon_master.png         (1024 master)
  apple-touch-icon.png (repo root)  (180px, iOS home-screen bookmark)
"""
import os
import subprocess
import sys

from PIL import Image, ImageDraw, ImageFilter

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
S = 1024


def vgrad(size, top, bot):
    img = Image.new("RGB", (size, size))
    d = ImageDraw.Draw(img)
    for y in range(size):
        t = y / (size - 1)
        d.line([(0, y), (size, y)],
            fill=tuple(int(top[i] + (bot[i] - top[i]) * t) for i in range(3)))
    return img


def rounded_mask(size, radius, pad=0):
    m = Image.new("L", (size, size), 0)
    ImageDraw.Draw(m).rounded_rectangle([pad, pad, size - 1 - pad, size - 1 - pad],
                                        radius=radius, fill=255)
    return m


def build_master():
    radius = 232
    mask = rounded_mask(S, radius)

    # deep navy → indigo background (matches the wordmark's dark field)
    bg = vgrad(S, (34, 22, 66), (8, 5, 22))
    base = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    base.paste(bg, (0, 0), mask)

    # teal glow pooled behind the button
    glowt = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    ImageDraw.Draw(glowt).ellipse([170, 250, S - 170, S - 250], fill=(0, 255, 213, 130))
    base = Image.alpha_composite(base, glowt.filter(ImageFilter.GaussianBlur(90)))

    # red play button geometry
    bw, bh, br = 600, 430, 116
    bx0, by0 = (S - bw) // 2, (S - bh) // 2
    bx1, by1 = bx0 + bw, by0 + bh

    # red glow halo
    halo = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    ImageDraw.Draw(halo).rounded_rectangle([bx0, by0, bx1, by1], radius=br, fill=(255, 40, 70, 220))
    base = Image.alpha_composite(base, halo.filter(ImageFilter.GaussianBlur(40)))

    # the button: red vertical gradient clipped to a rounded rect, dark outline
    btn = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    bmask = rounded_mask(S, br)  # not used; build per-rect below
    bmask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(bmask).rounded_rectangle([bx0, by0, bx1, by1], radius=br, fill=255)
    red = vgrad(S, (255, 74, 92), (190, 12, 40))
    btn.paste(red, (0, 0), bmask)

    bd = ImageDraw.Draw(btn)
    # glossy top highlight
    gloss = Image.new("L", (S, S), 0)
    ImageDraw.Draw(gloss).rounded_rectangle(
        [bx0 + 26, by0 + 22, bx1 - 26, by0 + bh // 2], radius=br - 30, fill=70)
    sheen = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    sheen.putdata([(255, 255, 255, a) for a in gloss.getdata()])
    btn = Image.alpha_composite(btn, sheen)
    bd = ImageDraw.Draw(btn)
    # dark outline for definition
    bd.rounded_rectangle([bx0, by0, bx1, by1], radius=br, outline=(0, 0, 0, 235), width=10)
    # white play triangle
    cx, cy = S // 2, S // 2
    bd.polygon([(cx - 70, cy - 112), (cx - 70, cy + 112), (cx + 132, cy)], fill=(255, 255, 255, 255))

    out = Image.alpha_composite(base, btn)

    # gentle teal rim glow on the squircle edge
    rim = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    ImageDraw.Draw(rim).rounded_rectangle(
        [8, 8, S - 9, S - 9], radius=radius - 6, outline=(0, 255, 213, 150), width=8)
    out = Image.alpha_composite(out, rim.filter(ImageFilter.GaussianBlur(12)))

    final = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    final.paste(out, (0, 0), mask)
    return final


def main():
    master = build_master()
    master.save(os.path.join(HERE, "icon_master.png"))

    # iOS home-screen bookmark icon
    master.resize((180, 180), Image.LANCZOS).save(os.path.join(ROOT, "apple-touch-icon.png"))

    # crisp vector favicon for the browser tab + bookmarks
    favicon = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
        '<rect width="64" height="64" rx="14" fill="#0a0518"/>'
        '<ellipse cx="32" cy="33" rx="22" ry="14" fill="#00ffd5" opacity="0.28"/>'
        '<rect x="12" y="20" width="40" height="26" rx="8" fill="#ff2b46" '
        'stroke="#000" stroke-width="2.5"/>'
        '<path d="M28 27 L28 41 L41 34 Z" fill="#fff"/></svg>'
    )
    with open(os.path.join(ROOT, "favicon.svg"), "w") as fh:
        fh.write(favicon)

    # macOS .icns
    iconset = os.path.join(HERE, "app.iconset")
    os.makedirs(iconset, exist_ok=True)
    for size, scale in [(16, 1), (16, 2), (32, 1), (32, 2), (128, 1),
                        (128, 2), (256, 1), (256, 2), (512, 1), (512, 2)]:
        px = size * scale
        suffix = "" if scale == 1 else "@2x"
        master.resize((px, px), Image.LANCZOS).save(
            os.path.join(iconset, f"icon_{size}x{size}{suffix}.png"))
    try:
        subprocess.run(["iconutil", "-c", "icns", iconset,
                        "-o", os.path.join(HERE, "app.icns")], check=True)
        print("wrote app.icns + apple-touch-icon.png")
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        print(f"iconutil failed ({e})", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
