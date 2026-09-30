#!/usr/bin/env python3
"""one/ursa.py against tools/ursa-mock.py: reading, read-only refusal,
settings checked against what the camera supports, looks, record, tally.

    python3 tools/test-ursa.py
"""

import importlib.util
import os
import sys
import threading
import time
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
S = srv.state
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

# read-only is the default: a plain Camera cannot write
try:
    ursa.Camera(host).set_iso(800)
    check("a Camera is read-only by default", False)
except ursa.ReadOnly:
    check("a Camera is read-only by default", True)

# settings are snapped or clamped to what the camera takes; each write audited
audit = []
cam = ursa.Camera(host, read_only=False, audit=lambda ev, d: audit.append((ev, d)))
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

check("every write audited (%d)" % len(audit), len(audit) == S.get("sent", 0))
check("audit line says who, what and the answer",
      all(ev == "CAMERA" for ev, _ in audit) and any("cam1 (" in d and "PUT /video/iso {\"iso\": 800} -> 204" in d for _, d in audit))
check("the refused iris write is in the audit too", any("/lens/iris" in d and "-> 403" in d for _, d in audit))

# record needs its own opt-in, even with writes on
try:
    cam.record(True)
    check("record refused without allow_record", False)
except ursa.RecordOff:
    check("record refused without allow_record", True)
check("... and nothing was sent", S["recording"] is False)
rec = ursa.Camera(host, read_only=False, allow_record=True)
check("record on", rec.record(True)["ok"] and cam.status()["recording"] is True)
check("record off", rec.record(False)["ok"] and cam.status()["recording"] is False)
urllib.request.urlopen(urllib.request.Request(f"http://{host}/mock/tally", data=b'{"status": "Program"}', method="POST"))
check("tally reads Program", cam.status()["tally"] == "Program")

# a camera that is not there: no answer, no exception
gone = ursa.Camera("127.0.0.1:9", timeout=0.5)
st = gone.status()
check("no camera: offline, all None", st["online"] is False and st["iso"] is None)

# the pool: two cameras and one that is not there; answers never wait on a camera
srv2 = mock.serve(0, name="cam2")
threading.Thread(target=srv2.serve_forever, daemon=True).start()
host2 = f"127.0.0.1:{srv2.server_address[1]}"
pool = ursa.Pool([host, host2, "10.255.255.1"], every=0.2, timeout=0.5)
time.sleep(1.0)
t = time.time(); snap = pool.snapshot(); took = time.time() - t
keys = [r["key"] for r in snap]
check("pool: a snapshot never waits (%.0f ms)" % (took * 1000), took < 0.05)
check("pool: keyed by address, labelled by the name each reports",
      {(r["key"], r["label"]) for r in snap if r["online"]} == {(host, "cam1"), (host2, "cam2")})
check("pool: the missing camera is listed offline by its address",
      any(r["key"] == "10.255.255.1" and r["online"] in (False, None) for r in snap))
check("pool: a unique name finds its camera too", pool.camera("cam2") is pool.camera(host2))
check("pool: read-only unless the config says writes", pool.camera(host2).read_only is True)
try:
    pool.camera(host2).set_iso(800); check("pool: read-only camera refuses", False)
except ursa.ReadOnly:
    check("pool: read-only camera refuses", True)
srv2.state["name"] = "cam1"                   # both on one name, as cameras ship
time.sleep(0.6)
snap = pool.snapshot()
pair = [r for r in snap if r.get("name") == "cam1"]
check("pool: two cameras on one name: both flagged, with what to fix",
      len(pair) == 2 and all(len(r["duplicate"]) == 2 and "Blackmagic Camera Setup" in r["clash"] for r in pair))
check("pool: ... and both still reachable by address",
      pool.camera(host) is not None and pool.camera(host2) is not None and pool.camera(host) is not pool.camera(host2))
check("pool: ... the shared name alone is ambiguous", pool.camera("cam1") is None)
srv2.state["name"] = "cam2-renamed"           # renamed in Setup while the pool runs
srv2.state["iso"] = 1600
time.sleep(0.6)
snap = pool.snapshot()
r2 = next(r for r in snap if r["key"] == host2)
check("pool: renamed mid-run: same key, new label, state carried on",
      r2["label"] == "cam2-renamed" and r2["iso"] == 1600 and r2["online"])
check("pool: renamed mid-run: the clash clears on both",
      all(not r["clash"] and not r["duplicate"] for r in snap if r["online"]))
pool.set_hosts([host])
check("pool: a camera taken out of the config goes", [r["host"] for r in pool.snapshot()] == [host])
wpool = ursa.Pool([host], writes=True, audit=lambda ev, d: None, every=0.2)
time.sleep(0.5)
check("pool: writes when the config says so; record still off",
      wpool.camera(host).read_only is False and wpool.camera(host).allow_record is False)
pool.stop(); wpool.stop()

srv.shutdown(); srv2.shutdown()
print(f"\n{'ALL PASS' if not fails else str(len(fails)) + ' FAILED'}")
sys.exit(1 if fails else 0)
