#!/usr/bin/env python3
"""
Generate the TUBE-RIPPER app icon: a glowing red YouTube-style play badge on a
dark neon keygen-grid squircle. Renders a 1024px master, builds a full .iconset,
and runs `iconutil` to emit packaging/app.icns.
"""
import os
import subprocess
import sys

from PIL import Image, ImageChops, ImageDraw, ImageFilter

HERE = os.path.dirname(os.path.abspath(__file__))
S = 1024


def vgrad(size, top, bot):
    """Vertical gradient RGB image."""
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

    # background gradient (deep purple -> near black)
    bg = vgrad(S, (60, 14, 102), (9, 2, 22))
    base = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    base.paste(bg, (0, 0), mask)

    # faint keygen grid, clipped to the squircle
    grid = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    gd = ImageDraw.Draw(grid)
    step = 64
    for x in range(0, S + 1, step):
        gd.line([(x, 0), (x, S)], fill=(0, 255, 213, 20))
    for y in range(0, S + 1, step):
        gd.line([(0, y), (S, y)], fill=(0, 255, 213, 20))
    grid.putalpha(ImageChops.multiply(grid.split()[3], mask))
    base = Image.alpha_composite(base, grid)

    # red play badge (rounded rect) with a glow
    bw, bh, br = 620, 430, 120
    bx0, by0 = (S - bw) // 2, (S - bh) // 2
    badge_mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(badge_mask).rounded_rectangle(
        [bx0, by0, bx0 + bw, by0 + bh], radius=br, fill=255)
    redgrad = vgrad(S, (255, 64, 96), (188, 0, 32))
    badge = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    badge.paste(redgrad, (0, 0), badge_mask)

    # white play triangle
    cx, cy = S // 2, S // 2
    tri = [(cx - 78, cy - 120), (cx - 78, cy + 120), (cx + 138, cy)]
    ImageDraw.Draw(badge).polygon(tri, fill=(255, 255, 255, 255))

    # red glow behind the badge
    glow_src = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    gmask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(gmask).rounded_rectangle(
        [bx0, by0, bx0 + bw, by0 + bh], radius=br, fill=255)
    glow_src.paste(Image.new("RGBA", (S, S), (255, 30, 70, 255)), (0, 0), gmask)
    glow = glow_src.filter(ImageFilter.GaussianBlur(46))

    out = Image.alpha_composite(base, glow)
    out = Image.alpha_composite(out, badge)

    # neon border with glow
    bord = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    ImageDraw.Draw(bord).rounded_rectangle(
        [7, 7, S - 8, S - 8], radius=radius - 6, outline=(0, 255, 213, 200), width=9)
    out = Image.alpha_composite(out, bord.filter(ImageFilter.GaussianBlur(11)))
    out = Image.alpha_composite(out, bord)

    # clip everything to the squircle
    final = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    final.paste(out, (0, 0), mask)
    return final


def main():
    master = build_master()
    master.save(os.path.join(HERE, "icon_master.png"))

    iconset = os.path.join(HERE, "app.iconset")
    os.makedirs(iconset, exist_ok=True)
    specs = [(16, 1), (16, 2), (32, 1), (32, 2), (128, 1),
            (128, 2), (256, 1), (256, 2), (512, 1), (512, 2)]
    for size, scale in specs:
        px = size * scale
        img = master.resize((px, px), Image.LANCZOS)
        suffix = "" if scale == 1 else "@2x"
        img.save(os.path.join(iconset, f"icon_{size}x{size}{suffix}.png"))

    icns = os.path.join(HERE, "app.icns")
    try:
        subprocess.run(["iconutil", "-c", "icns", iconset, "-o", icns], check=True)
        print(f"wrote {icns}")
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        print(f"iconutil failed ({e}); leaving PNGs in {iconset}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
