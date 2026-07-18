#!/usr/bin/env python3
"""Render the One/View marks as 180x180 apple-touch-icon PNGs.

iOS ignores SVG favicons for home-screen icons; it wants a PNG. The marks are
simple geometry, so redraw them with PIL (4x supersampled) on House Black —
iOS rounds the corners itself. Re-run after any mark change.
"""
from PIL import Image, ImageDraw

S = 4                     # supersample
SIZE = 180
CANVAS = SIZE * S
BLACK, TEXT, GREEN = "#0B0D10", "#e8ecef", "#2fd97b"

# map the 96x96 viewBox into the icon with breathing room
ART = CANVAS * 0.68
K = ART / 96
OX = (CANVAS - ART) / 2
OY = (CANVAS - ART) / 2

def rr(d, x, y, w, h, rx, fill=None, outline=None, width=0):
    d.rounded_rectangle([OX + x*K, OY + y*K, OX + (x+w)*K, OY + (y+h)*K],
                        radius=rx*K, fill=fill, outline=outline, width=round(width*K))

def dot(d, cx, cy, r, fill):
    d.ellipse([OX + (cx-r)*K, OY + (cy-r)*K, OX + (cx+r)*K, OY + (cy+r)*K], fill=fill)

def arc(d, cx, cy, r, a0, a1, colour, sw):
    # PIL strokes inward from the bbox, so pad it by half the stroke width
    # to keep the centerline on radius r (matching SVG stroke behavior)
    ro = r + sw / 2
    bbox = [OX + (cx-ro)*K, OY + (cy-ro)*K, OX + (cx+ro)*K, OY + (cy+ro)*K]
    d.arc(bbox, a0, a1, fill=colour, width=round(sw*K))
    # round linecaps
    import math
    for a in (a0, a1):
        ex = cx + r * math.cos(math.radians(a))
        ey = cy + r * math.sin(math.radians(a))
        dot(d, ex, ey, sw/2, colour)

def frame(d):
    rr(d, 6, 10, 84, 66, 10, outline=TEXT, width=7)

def make(path, draw_fn):
    img = Image.new("RGB", (CANVAS, CANVAS), BLACK)
    d = ImageDraw.Draw(img)
    draw_fn(d)
    img.resize((SIZE, SIZE), Image.LANCZOS).save(path, optimize=True)
    print("wrote", path)

DIM28 = (232, 236, 239, 71)   # TEXT at .28 — flatten onto black
def blend(hexc, alpha):
    r, g, b = (int(hexc[i:i+2], 16) for i in (1, 3, 5))
    br, bg, bb = (int("0B0D10"[i:i+2], 16) for i in (0, 2, 4))
    return tuple(round(c*alpha + bc*(1-alpha)) for c, bc in ((r,br),(g,bg),(b,bb)))

def one(d):
    frame(d)
    rr(d, 20, 54, 40, 9, 4.5, fill=GREEN)
    rr(d, 64, 54, 12, 9, 4.5, fill=blend("#e8ecef", 0.28))
    rr(d, 20, 83, 26, 7, 3.5, fill=GREEN)
    rr(d, 50, 83, 26, 7, 3.5, fill=GREEN)

def view(d):
    frame(d)
    rr(d, 20, 54, 30, 9, 4.5, fill=GREEN)
    dot(d, 64, 58, 4, GREEN)
    arc(d, 64, 58, 12, 270, 360, GREEN, 5.5)
    arc(d, 64, 58, 25, 270, 315, blend("#e8ecef", 0.4), 5.5)
    rr(d, 20, 83, 56, 7, 3.5, fill=GREEN)

import os
base = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
make(os.path.join(base, "one/static/apple-touch-icon.png"), one)
make(os.path.join(base, "view/static/apple-touch-icon.png"), view)
