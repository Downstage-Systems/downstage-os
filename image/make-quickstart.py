#!/usr/bin/env python3
"""Generate a per-unit quick-start card PDF from the build log.

Usage: ./make-quickstart.py DS1-A-2607-0002 [out-dir]
Env:   DOWNSTAGE_BUILD_LOG (default ~/Downloads/downstage-build-log.csv)
       DOWNSTAGE_TTF_DIR   (dir containing Rajdhani-700/600, ShareTechMono-400,
                            Inter-400 TTFs)

Brand: print palette (Ink #12161A / Green #12A95C), Rajdhani display,
Share Tech Mono for machine data. 6x4in landscape, print at 100%.
"""
import csv, os, sys
from reportlab.pdfgen import canvas
from reportlab.lib.colors import HexColor
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

SERIAL = sys.argv[1] if len(sys.argv) > 1 else sys.exit(__doc__)
OUT    = sys.argv[2] if len(sys.argv) > 2 else "."
LOG    = os.environ.get("DOWNSTAGE_BUILD_LOG", os.path.expanduser("~/Downloads/downstage-build-log.csv"))
TTF    = os.environ.get("DOWNSTAGE_TTF_DIR", os.path.dirname(__file__) + "/ttf")

row = next((r for r in csv.DictReader(open(LOG)) if r["serial"] == SERIAL), None) or sys.exit(f"{SERIAL} not in build log")
ssid, pw = row["hotspot_ssid"], row["hotspot_pass"]
num = SERIAL.split("-")[-1]
if SERIAL.startswith("DS1-"):
    product, hostname = "One", f"downstage-{num}"
elif SERIAL.startswith("DSV-"):
    product, hostname = "View", f"downstage-v{num.lstrip('0') if len(num.lstrip('0'))>=3 else num.lstrip('0').zfill(3)}"
else:
    sys.exit("unknown serial prefix")

for name, f in [("Rajdhani-Bold", "Rajdhani-700.ttf"), ("Rajdhani-Semi", "Rajdhani-600.ttf"),
                ("STMono", "ShareTechMono-400.ttf"), ("Inter", "Inter-400.ttf")]:
    pdfmetrics.registerFont(TTFont(name, f"{TTF}/{f}"))

INK, GREEN, DIM = HexColor("#12161A"), HexColor("#12A95C"), HexColor("#5E6A72")
W, H = 6*72, 4*72
m = 22

if product == "One":
    LINES = [
        ("step", "1", "Connect power (and wired network if you have it). Both HDMI outputs are live."),
        ("gap", "", ""),
        ("step", "2", "On any device on the same network, open the setup page:"),
        ("mono", f"http://{hostname}.local:8080", "(the front panel shows the address too)"),
        ("gap", "", ""),
        ("step", "3", "No network at the venue? After ~90 seconds the unit makes its own WiFi:"),
        ("mono", f"WiFi: {ssid}", f"password: {pw}"),
        ("mono", "http://10.42.0.1:8080", "setup page while on the hotspot"),
        ("gap", "", ""),
        ("step", "4", "Timer views for other screens (tablets, TVs — just a browser):"),
        ("mono", f"http://{hostname}.local:4001", "Stream Deck via Companion :8000"),
    ]
else:
    LINES = [
        ("step", "1", "Connect power and the display. The e-ink panel on the front shows its address."),
        ("gap", "", ""),
        ("step", "2", "On any device on the same network, open the setup page:"),
        ("mono", f"http://{hostname}.local:8080", ""),
        ("gap", "", ""),
        ("step", "3", "Point it at your Downstage One (or any OnTime server) and pick a view."),
        ("gap", "", ""),
        ("step", "4", "No network at the venue? After ~90 seconds the unit makes its own WiFi:"),
        ("mono", f"WiFi: {ssid}", f"password: {pw}"),
        ("mono", "http://10.42.0.1:8080", "setup page while on the hotspot"),
    ]

path = f"{OUT}/quick-start-{SERIAL}.pdf"
c = canvas.Canvas(path, pagesize=(W, H))
c.setFont("Rajdhani-Bold", 21)
c.setFillColor(INK); c.drawString(m, H-38, "DOWNSTAGE ")
w = c.stringWidth("DOWNSTAGE ", "Rajdhani-Bold", 21)
c.setFillColor(GREEN); c.drawString(m+w, H-38, product.upper())
c.setFont("STMono", 8.5); c.setFillColor(DIM); c.drawRightString(W-m, H-36, f"S/N {SERIAL}")
c.setStrokeColor(GREEN); c.setLineWidth(2.5); c.line(m, H-48, W-m, H-48)
c.setFont("Rajdhani-Semi", 12); c.setFillColor(INK)
c.drawString(m, H-66, "Power it on. Point a browser at it. Doors.")
y = H-92
for kind, a, b in LINES:
    if kind == "step":
        c.setFont("Rajdhani-Bold", 11); c.setFillColor(GREEN); c.drawString(m, y, a)
        c.setFont("Inter", 9.5); c.setFillColor(INK); c.drawString(m+16, y, b); y -= 15
    elif kind == "mono":
        c.setFont("STMono", 10.5); c.setFillColor(GREEN); c.drawString(m+16, y, a)
        if b:
            c.setFont("Inter", 8.5); c.setFillColor(DIM)
            c.drawString(m+16+c.stringWidth(a, "STMono", 10.5)+10, y, b)
        y -= 15
    else:
        y -= 5
c.setFont("Inter", 7.5); c.setFillColor(DIM)
c.drawString(m, 30, f"Advanced access: ssh pi@{hostname}.local · password: downstage")
c.setFont("STMono", 7); c.drawRightString(W-m, 30, "support: hello@downstage.systems")
c.setFont("Inter", 7.5)
c.drawString(m, 19, "Built on Ontime, free open-source software (GPL v3) · source: github.com/cpvalente/ontime")
c.save()
print("wrote", path)
