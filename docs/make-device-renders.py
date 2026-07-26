#!/usr/bin/env python3
"""Brand renders for the Downstage units.

Outputs:
  docs/renders/view-ship-screen.png       exact 250x122 e-ink ship frame, x3
  docs/renders/downstage-view-mockup.png  product mockup with the ship screen live
  {one,view}/static/unit-one.png          fleet-list thumbnails (both apps)
  {one,view}/static/unit-view.png

The ship-screen drawing here mirrors EPaperDisplay._page_ship in view/app.py —
keep the two in sync when either changes.
"""
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

ROOT   = Path(__file__).resolve().parent.parent
OUT    = Path(__file__).resolve().parent / "renders"
TTF    = ROOT.parent / "downstage-factory" / "ttf"
OUT.mkdir(exist_ok=True)

BG     = (11, 13, 16)       # brand near-black
CASE   = (23, 26, 30)
CASE2  = (16, 18, 21)
GREEN  = (47, 217, 123)
TEXT   = (232, 236, 239)
MUTED  = (140, 150, 158)

raj700 = lambda s: ImageFont.truetype(str(TTF / "Rajdhani-700.ttf"), s)
raj600 = lambda s: ImageFont.truetype(str(TTF / "Rajdhani-600.ttf"), s)
mono   = lambda s: ImageFont.truetype(str(TTF / "ShareTechMono-400.ttf"), s)


# ── the View brand mark (from downstage-view-mark.svg, 96-unit box) ──────────
def draw_view_mark(draw, x, y, size, ink, width_scale=1.0):
    s = size / 96.0
    w = max(2, round(7 * s * width_scale))
    def pt(a, b): return (x + a * s, y + b * s)
    draw.rounded_rectangle([pt(6, 10), pt(90, 76)], radius=10 * s, outline=ink, width=w)
    draw.rounded_rectangle([pt(20, 54), pt(50, 63)], radius=4.5 * s, fill=ink)
    draw.rounded_rectangle([pt(20, 83), pt(76, 90)], radius=3.5 * s, fill=ink)
    r = 4 * s
    cx, cy = x + 64 * s, y + 58 * s
    draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=ink)
    aw = max(2, round(5.5 * s * width_scale))
    r2 = 12 * s
    draw.arc([cx - r2, cy - r2, cx + r2, cy + r2], -90, 0, fill=ink, width=aw)
    r3 = 25 * s
    draw.arc([cx - r3, cy - r3, cx + r3, cy + r3], -90, -35, fill=ink, width=max(1, aw - 2))


def draw_one_mark(draw, x, y, size, ink, accent=None):
    accent = accent or ink
    s = size / 96.0
    w = max(2, round(7 * s))
    def pt(a, b): return (x + a * s, y + b * s)
    draw.rounded_rectangle([pt(6, 10), pt(90, 76)], radius=10 * s, outline=ink, width=w)
    draw.rounded_rectangle([pt(20, 54), pt(60, 63)], radius=4.5 * s, fill=accent)
    draw.rounded_rectangle([pt(64, 54), pt(76, 63)], radius=4.5 * s, fill=ink)
    draw.rounded_rectangle([pt(20, 83), pt(46, 90)], radius=3.5 * s, fill=accent)
    draw.rounded_rectangle([pt(50, 83), pt(76, 90)], radius=3.5 * s, fill=accent)


# ── ship screen: exact 250x122 1-bit frame (mirrors view/app.py) ─────────────
def view_mark_bitmap(size):
    """Supersampled 1-bit mark — crisp at e-ink scale."""
    big = Image.new("L", (size * 4, size * 4), 255)
    draw_view_mark(ImageDraw.Draw(big), 0, 0, size * 4, 0)
    return big.resize((size, size), Image.LANCZOS).point(lambda v: 0 if v < 150 else 255, "1")


def ship_frame(serial="DSV-A-2607-0001"):
    img = Image.new("1", (250, 122), 255)
    d = ImageDraw.Draw(img)
    mark_s = 44
    mark = view_mark_bitmap(mark_s)
    img.paste(mark, ((250 - mark_s) // 2, 4))
    wordmark = raj700(26)
    text = "DOWNSTAGE VIEW"
    tw = d.textlength(text, font=wordmark)
    d.text(((250 - tw) / 2, 50), text, font=wordmark, fill=0)
    d.line([78, 92, 172, 92], fill=0, width=1)
    sub = mono(12)
    sw = d.textlength(serial, font=sub)
    d.text(((250 - sw) / 2, 100), serial, font=sub, fill=0)
    return img


# ── fleet thumbnails: tiny landscape cases, transparent bg ───────────────────
def thumb_view():
    W, H = 260, 150
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([10, 14, W - 10, H - 14], radius=26, fill=CASE,
                        outline=(60, 66, 74), width=3)
    d.rounded_rectangle([22, 26, W - 22, H - 26], radius=18, fill=CASE2)
    # e-ink window, off-white like the real panel
    d.rounded_rectangle([44, 42, 196, 110], radius=6, fill=(238, 238, 234))
    md = ImageDraw.Draw(img)
    draw_view_mark(md, 58, 52, 48, (25, 28, 32))
    md.rounded_rectangle([118, 66, 182, 74], radius=4, fill=(25, 28, 32))
    md.rounded_rectangle([118, 82, 164, 88], radius=3, fill=(120, 124, 128))
    # power LED
    d.ellipse([216, 68, 228, 80], fill=GREEN)
    return img


def thumb_one():
    W, H = 260, 150
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([10, 14, W - 10, H - 14], radius=22, fill=CASE,
                        outline=(60, 66, 74), width=3)
    d.rounded_rectangle([22, 26, W - 22, H - 26], radius=14, fill=CASE2)
    # OLED slot with a green status line
    d.rounded_rectangle([44, 52, 186, 98], radius=6, fill=(6, 7, 8))
    d.rounded_rectangle([56, 62, 132, 70], radius=3, fill=GREEN)
    d.rounded_rectangle([56, 78, 168, 84], radius=3, fill=(90, 190, 130))
    d.rounded_rectangle([56, 88, 108, 93], radius=2, fill=(70, 80, 88))
    d.ellipse([216, 68, 228, 80], fill=GREEN)
    return img


# ── product mockup ────────────────────────────────────────────────────────────
def mockup():
    W, H = 1600, 1000
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    # faint grid, same feel as the web UI backdrop
    for gx in range(0, W, 80):
        d.line([gx, 0, gx, H], fill=(15, 18, 22))
    for gy in range(0, H, 80):
        d.line([0, gy, W, gy], fill=(15, 18, 22))

    # drop shadow
    sh = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(sh).ellipse([260, 660, 1340, 760], fill=(0, 0, 0, 140))
    img.paste(Image.composite(Image.new("RGB", (W, H), (0, 0, 0)), img, sh.split()[3]),
              (0, 0))

    # case: top face, rounded like the enclosure photo
    cx0, cy0, cx1, cy1 = 300, 130, 1300, 700
    d.rounded_rectangle([cx0, cy0, cx1, cy1], radius=95, fill=(28, 31, 36))
    d.rounded_rectangle([cx0 + 10, cy0 + 10, cx1 - 10, cy1 - 10], radius=85,
                        fill=(18, 20, 24))
    # glossy top panel
    d.rounded_rectangle([cx0 + 34, cy0 + 34, cx1 - 34, cy1 - 34], radius=62,
                        fill=(10, 11, 13), outline=(42, 46, 52), width=2)

    # e-ink window: ship frame scaled x3, crisp pixels
    frame = ship_frame().convert("RGB").resize((750, 366), Image.NEAREST)
    fx, fy = (W - 750) // 2, cy0 + (cy1 - cy0 - 366) // 2
    d.rounded_rectangle([fx - 14, fy - 14, fx + 764, fy + 380], radius=10,
                        fill=(5, 6, 7))
    img.paste(frame, (fx, fy))

    # caption
    t1 = "DOWNSTAGE VIEW"
    f1 = raj700(64)
    tw = d.textlength(t1, font=f1)
    d.text(((W - tw) / 2, 790), t1, font=f1, fill=TEXT)
    t2 = "Wireless stage display · e-ink status panel · DSV series"
    f2 = raj600(30)
    tw = d.textlength(t2, font=f2)
    d.text(((W - tw) / 2, 878), t2, font=f2, fill=MUTED)
    d.rounded_rectangle([(W - 60) / 2, 950, (W + 60) / 2, 956], radius=3, fill=GREEN)
    return img


if __name__ == "__main__":
    ship = ship_frame()
    ship.resize((750, 366), Image.NEAREST).save(OUT / "view-ship-screen.png")
    mockup().save(OUT / "downstage-view-mockup.png")
    tv, to = thumb_view(), thumb_one()
    for app in ("one", "view"):
        tv.save(ROOT / app / "static" / "unit-view.png")
        to.save(ROOT / app / "static" / "unit-one.png")
    print("renders written:", *[p.name for p in OUT.iterdir()])
