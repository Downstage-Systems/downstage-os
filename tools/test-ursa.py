#!/usr/bin/env python3
"""one/ursa.py against tools/ursa-mock.py: reading, read-only refusal,
settings checked against what the camera supports, looks, record, tally.

    python3 tools/test-ursa.py
"""

import importlib.util
import os
import sys
import threading
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "one"))
import ursa  # noqa: E402

spec = importlib.util.spec_from_file_location("ursa_mock", os.path.join(HERE, "ursa-mock.py"))
mock = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mock)

srv = mock.serve(0)
port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()
host = f"127.0.0.1:{port}"
S = mock.Handler.state
fails = []


def check(what, cond):
    print(("pass  " if cond else "FAIL  ") + what)
    if not cond:
        fails.append(what)


# reading, and finding the scheme (https refused by the mock: falls back to http)
ro = ursa.Camera(host, read_only=True)
st = ro.status()
check("status: online, model and name", st["online"] and st["model"] == "URSA Broadcast G2" and st["name"] == "cam1")
check("status: exposure fields", (st["iso"], st["gain"], st["nd"], st["iris"], st["wb"], st["shutter_speed"]) == (400, 0, 0.0, 4.0, 5600, 50))
check("status: tally None, not recording", st["tally"] == "None" and st["recording"] is False)
check("scheme found: http", ro.scheme == "http")
caps = ro.capabilities()
check("capabilities: ISO list, ND stops, WB range", 800 in caps["isos"] and caps["nd_stops"] == [0.0, 2.0, 4.0, 6.0] and caps["wb"]["max"] == 10000)

# read-only never reaches the camera
try:
    ro.set_iso(800)
    check("read-only refuses a write", False)
except ursa.ReadOnly:
    check("read-only refuses a write", True)
check("read-only: nothing reached the camera", S["calls"] == [])

# settings are snapped or clamped to what the camera takes
cam = ursa.Camera(host)
check("ISO 750 goes as 800", cam.set_iso(750)["ok"] and S["iso"] == 800)
check("ND 5 goes as the nearest stop", cam.set_nd(5)["ok"] and S["nd"] in (4.0, 6.0))
check("WB 12000 K clamps to 10000", cam.set_wb(12000)["ok"] and S["wb"] == 10000)
check("shutter angle 170 goes as 172.8", cam.set_shutter(angle=170)["ok"] and S["shutter"] == {"shutterAngle": 172.8})
check("iris f/2.8", cam.set_iris(2.8)["ok"] and S["iris"] == 2.8)
S["ae"] = "Continuous"
r = cam.set_iris(5.6)
check("iris refused (403) under auto exposure", not r["ok"] and r["status"] == 403 and S["iris"] == 2.8)
S["ae"] = "Off"

# looks: capture, change everything, apply, back where it was
look = cam.capture_look()
check("look captured", look == {"nd": S["nd"], "iso": 800, "gain": 0, "iris": 2.8, "wb": 10000, "tint": 0, "shutter": {"angle": 172.8}})
cam.set_iso(3200); cam.set_nd(0); cam.set_wb(3200); cam.set_iris(8); cam.set_shutter(speed=100)
del S["calls"][:]
res = cam.apply_look(look)
check("look applied: every field took", all(x["ok"] for x in res) and len(res) == 7)
check("look applied: camera matches", (S["iso"], S["iris"], S["wb"], S["shutter"]) == (800, 2.8, 10000, {"shutterAngle": 172.8}))
order = [c[1] for c in S["calls"]]
check("look order: ND and ISO before the iris, colour last",
      order.index("/video/ndFilter") < order.index("/lens/iris") and order.index("/video/iso") < order.index("/lens/iris")
      and order[-2:] == ["/video/whiteBalance", "/video/whiteBalanceTint"])

# record, and tally from the switcher
check("record on", cam.record(True)["ok"] and cam.status()["recording"] is True)
check("record off", cam.record(False)["ok"] and cam.status()["recording"] is False)
urllib.request.urlopen(urllib.request.Request(f"http://{host}/mock/tally", data=b'{"status": "Program"}', method="POST"))
check("tally reads Program", cam.status()["tally"] == "Program")

# a camera that is not there: no answer, no exception
gone = ursa.Camera("127.0.0.1:9", timeout=0.5)
st = gone.status()
check("no camera: offline, all None", st["online"] is False and st["iso"] is None)

srv.shutdown()
print(f"\n{'ALL PASS' if not fails else str(len(fails)) + ' FAILED'}")
sys.exit(1 if fails else 0)
