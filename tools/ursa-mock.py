#!/usr/bin/env python3
"""A pretend Blackmagic URSA Broadcast G2 for building the One's camera control
without a camera on the bench (or one in a show).

Serves the slice of the Camera Control REST API that one/ursa.py uses, over
plain HTTP, with state in memory and the answers Blackmagic's developer manual
gives: 204 for a set, 400 for a value the camera does not support, 403 for
the iris while auto exposure has it, 501 for what the camera does not have.
Tally is read-only in the real API; here POST /mock/tally {"status": ...}
sets it, standing in for the switcher.

    python3 tools/ursa-mock.py [port]        (default 8811)
    python3 one/ursa.py 127.0.0.1:8811 status

Values are plausible for an URSA Broadcast G2 with a B4 zoom, not measured:
check them against a real camera's /control/api/v1 before trusting any.
Standard library only.
"""

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

API = "/control/api/v1"
CAPS = {
    "isos": [100, 200, 400, 800, 1600, 3200, 6400, 12800],
    "gains": [-12, -6, 0, 6, 12, 18, 24, 30, 36],
    "shutter_speeds": [50, 60, 100, 120, 250, 500, 1000, 2000],
    "shutter_angles": [45.0, 90.0, 172.8, 180.0, 270.0, 360.0],
    "nd_stops": [0.0, 2.0, 4.0, 6.0],          # clear, 1/4, 1/16, 1/64
    "wb": {"min": 2500, "max": 10000},
    "tint": {"min": -50, "max": 50},
    "iris": {"min": 1.8, "max": 16.0},
    "zoom_mm": {"min": 8, "max": 136},
}


def new_state():
    return {"iso": 400, "gain": 0, "wb": 5600, "tint": 0, "nd": 0.0, "shutter": {"shutterSpeed": 50},
            "measure": "ShutterSpeed", "iris": 4.0, "zoom": 0.2, "focus": 0.5, "ae": "Off",
            "recording": False, "tally": "None", "preset": "default", "presets": ["default.cset", "Studio.cset"],
            "name": "cam1", "calls": []}


class Handler(BaseHTTPRequestHandler):
    state = new_state()
    lock = threading.Lock()

    def log_message(self, *a):   # quiet: the test reads state["calls"] instead
        pass

    def _send(self, code, body=None):
        raw = b"" if body is None else json.dumps(body).encode()
        self.send_response(code)
        if raw:
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        try:
            return json.loads(self.rfile.read(n)) if n else {}
        except ValueError:
            return None

    def do_GET(self):
        p, s = self.path, self.state
        if not p.startswith(API):
            return self._send(404)
        p = p[len(API):]
        iris_norm = (s["iris"] - CAPS["iris"]["min"]) / (CAPS["iris"]["max"] - CAPS["iris"]["min"])
        zmm = CAPS["zoom_mm"]
        shutter = dict(s["shutter"], continuousShutterAutoExposure=s["ae"] == "Continuous")
        table = {
            "/system/product": {"deviceName": s["name"], "productName": "URSA Broadcast G2", "softwareVersion": "9.1 (mock)"},
            "/system/videoFormat": {"name": "1080p50", "frameRate": "50", "width": 1920, "height": 1080, "interlaced": False},
            "/lens/iris": {"apertureStop": s["iris"], "normalised": round(iris_norm, 3), "apertureNumber": 0,
                           "continuousApertureAutoExposure": s["ae"] == "Continuous"},
            "/lens/iris/description": {"controllable": True, "apertureStop": CAPS["iris"]},
            "/lens/zoom": {"focalLength": round(zmm["min"] + s["zoom"] * (zmm["max"] - zmm["min"])), "normalised": s["zoom"]},
            "/lens/zoom/description": {"controllable": True, "focalLength": dict(zmm, adjustable=True)},
            "/lens/focus": {"normalised": s["focus"]},
            "/lens/focus/description": {"controllable": True, "focusDistance": {"adjustable": False}},
            "/video/iso": {"iso": s["iso"]}, "/video/supportedISOs": {"supportedISOs": CAPS["isos"]},
            "/video/gain": {"gain": s["gain"]}, "/video/supportedGains": {"supportedGains": CAPS["gains"]},
            "/video/whiteBalance": {"whiteBalance": s["wb"]}, "/video/whiteBalance/description": {"whiteBalance": CAPS["wb"]},
            "/video/whiteBalanceTint": {"whiteBalanceTint": s["tint"]},
            "/video/whiteBalanceTint/description": {"whiteBalanceTint": CAPS["tint"]},
            "/video/ndFilter": {"stop": s["nd"]}, "/video/supportedNDFilters": {"supportedStops": CAPS["nd_stops"]},
            "/video/shutter": shutter, "/video/shutter/measurement": {"measurement": s["measure"]},
            "/video/supportedShutters": {"shutterSpeeds": CAPS["shutter_speeds"], "shutterAngles": CAPS["shutter_angles"]},
            "/video/autoExposure": {"mode": s["ae"], "type": ""},
            "/transports/0/record": {"recording": s["recording"]},
            "/transports/0/timecode": {"display": "10:00:00:00", "timeline": "00:00:00:00"},
            "/camera/tallyStatus": {"status": s["tally"]},
            "/camera/power": {"source": "AC", "milliVolt": 12100, "batteries": []},
            "/presets": {"presets": s["presets"]}, "/presets/active": {"preset": s["preset"]},
        }
        if p in table:
            return self._send(200, table[p])
        return self._send(501)

    def _set(self, method):
        p, s = self.path, self.state
        body = self._body()
        if body is None:
            return self._send(400)
        if p == "/mock/tally":           # the switcher, not the API
            s["tally"] = body.get("status", "None")
            return self._send(204)
        if not p.startswith(API):
            return self._send(404)
        p = p[len(API):]
        s["calls"].append([method, p, body])
        s["sent"] = s.get("sent", 0) + 1
        ok = lambda: self._send(204)
        if method == "PUT" and p == "/video/iso":
            if body.get("iso") not in CAPS["isos"]:
                return self._send(400)
            s["iso"] = body["iso"]; return ok()
        if method == "PUT" and p == "/video/gain":
            if body.get("gain") not in CAPS["gains"]:
                return self._send(400)
            s["gain"] = body["gain"]; return ok()
        if method == "PUT" and p == "/video/whiteBalance":
            v = body.get("whiteBalance")
            if not isinstance(v, int) or not CAPS["wb"]["min"] <= v <= CAPS["wb"]["max"]:
                return self._send(400)
            s["wb"] = v; return ok()
        if method == "PUT" and p == "/video/whiteBalance/doAuto":
            s["wb"] = 5200; return ok()
        if method == "PUT" and p == "/video/whiteBalanceTint":
            v = body.get("whiteBalanceTint")
            if not isinstance(v, int) or not CAPS["tint"]["min"] <= v <= CAPS["tint"]["max"]:
                return self._send(400)
            s["tint"] = v; return ok()
        if method == "PUT" and p == "/video/ndFilter":
            if body.get("stop") not in CAPS["nd_stops"]:
                return self._send(400)
            s["nd"] = body["stop"]; return ok()
        if method == "PUT" and p == "/video/shutter":
            if "shutterAngle" in body and body["shutterAngle"] in CAPS["shutter_angles"]:
                s["shutter"] = {"shutterAngle": body["shutterAngle"]}; s["measure"] = "ShutterAngle"; return ok()
            if "shutterSpeed" in body and body["shutterSpeed"] in CAPS["shutter_speeds"]:
                s["shutter"] = {"shutterSpeed": body["shutterSpeed"]}; s["measure"] = "ShutterSpeed"; return ok()
            return self._send(400)
        if method == "PUT" and p == "/video/autoExposure":
            s["ae"] = body.get("mode", "Off"); return ok()
        if method == "PUT" and p == "/lens/iris":
            if s["ae"] == "Continuous":
                return self._send(403)
            lo, hi = CAPS["iris"]["min"], CAPS["iris"]["max"]
            if "apertureStop" in body:
                v = body["apertureStop"]
            elif "normalised" in body:
                v = lo + body["normalised"] * (hi - lo)
            elif "adjustmentStep" in body:
                v = s["iris"] * (2 ** (body["adjustmentStep"] / 6))   # sixth-stop steps
            else:
                return self._send(400)
            if not lo <= v <= hi:
                return self._send(400)
            s["iris"] = round(v, 2); return ok()
        if method == "PUT" and p == "/lens/zoom":
            v = body.get("normalised", s["zoom"] + body.get("adjustmentNormalised", 0))
            s["zoom"] = max(0.0, min(1.0, v)); return ok()
        if method == "PUT" and p == "/lens/focus":
            s["focus"] = max(0.0, min(1.0, body.get("normalised", s["focus"]))); return ok()
        if method == "PUT" and p == "/lens/focus/doAutoFocus":
            s["focus"] = 0.62; return ok()
        if method == "POST" and p == "/transports/0/record":
            s["recording"] = True; return ok()
        if method == "POST" and p == "/transports/0/stop":
            s["recording"] = False; return ok()
        if method == "PUT" and p == "/presets/active":
            if body.get("preset") != "default" and body.get("preset") not in s["presets"]:
                return self._send(404)
            s["preset"] = body["preset"]; return ok()
        return self._send(501)

    def do_PUT(self):
        with self.lock:
            self._set("PUT")

    def do_POST(self):
        with self.lock:
            self._set("POST")


def serve(port=8811, name="cam1"):
    """One pretend camera with its own state (srv.state), so a test can run several."""
    h = type("CamHandler", (Handler,), {"state": new_state(), "lock": threading.Lock()})
    h.state["name"] = name
    srv = ThreadingHTTPServer(("127.0.0.1", port), h)
    srv.state = h.state
    return srv


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8811
    print(f"pretend URSA Broadcast G2 on http://127.0.0.1:{port}{API}/")
    serve(port).serve_forever()
