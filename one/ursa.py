"""Blackmagic camera control for the One: the Camera Control REST API.

Blackmagic's URSA Broadcast G2 (and the Studio cameras, PYXIS, Cinema Camera
6K) answer a REST API at <camera>/control/api/v1/ once the web media manager
is on (Blackmagic Camera Setup, network access). The URSA has no RJ45: a
USB-C Ethernet adapter in its rear USB-C port puts it on the network (R&D,
2026-09-29). Every camera ships named ursa-broadcast-g2(.local), so each needs
its own name or a fixed address.

This module reads a camera's state (lens, exposure, colour, record, tally,
timecode, power), checks settings against what that camera says it supports
before sending them, and captures and applies "looks": the One's own saved
exposure and colour settings for a show, kept apart from the camera's .cset
presets. Endpoints and fields are from Blackmagic's developer manual
(BlackmagicCameraControl.pdf); anything a camera does not implement answers
501 and is simply left out.

What the API cannot do: set the camera's tally. /camera/tallyStatus is
read-only (None, Preview, Program - from the switcher over SDI), so a Cue Lite
can show it but the One cannot drive it.

A Camera made with read_only=True refuses every write before it reaches the
network: that is how a real camera is first looked at.

Standard library only (the One has requests; the bench Mac does not).
"""

import json
import ssl
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

API = "/control/api/v1"
TIMEOUT = 2.0
# The fields a look holds, in the order they are applied: ND and ISO/gain
# before the iris, so exposure is never briefly far over; colour last
LOOK_FIELDS = ("nd", "iso", "gain", "shutter", "iris", "wb", "tint")

# The camera's own certificate is self-signed: on its own network, from the
# One, an unverified TLS link is the only kind it can make
_TLS = ssl.create_default_context()
_TLS.check_hostname = False
_TLS.verify_mode = ssl.CERT_NONE


_STATUS_KEYS = ("name", "model", "version", "iris", "iris_auto", "iris_norm", "zoom_mm", "focus", "iso", "gain",
                "wb", "tint", "shutter_speed", "shutter_angle", "shutter_auto", "nd", "ae", "recording", "tally",
                "timecode", "format", "preset", "power", "battery")


class ReadOnly(Exception):
    """A write asked of a camera opened read-only."""


class Camera:
    def __init__(self, host, read_only=False, scheme=None, timeout=TIMEOUT):
        self.host = host.strip().rstrip("/")
        self.read_only = read_only
        self.scheme = scheme          # "https" or "http"; found on first contact when None
        self.timeout = timeout
        self._caps = None

    # -- the wire ----------------------------------------------------------
    def _open(self, scheme, method, path, body):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(f"{scheme}://{self.host}{API}{path}", data=data, method=method,
                                     headers={"Content-Type": "application/json"} if data else {})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout,
                                        context=_TLS if scheme == "https" else None) as r:
                raw = r.read()
                return r.status, (json.loads(raw) if raw else None)
        except urllib.error.HTTPError as e:
            return e.code, None

    def request(self, method, path, body=None):
        """(status, json or None). status 0: no answer at all."""
        if method != "GET" and self.read_only:
            raise ReadOnly(f"{method} {path} refused: camera opened read-only")
        schemes = [self.scheme] if self.scheme else ["https", "http"]
        for s in schemes:
            try:
                st, js = self._open(s, method, path, body)
                self.scheme = s
                return st, js
            except (urllib.error.URLError, OSError, ValueError):
                continue
        return 0, None

    def get(self, path):
        st, js = self.request("GET", path)
        return js if st == 200 else None

    def _write(self, method, path, body=None):
        st, _ = self.request(method, path, body)
        return {"ok": st in (200, 204), "status": st, "path": path}

    # -- reading -----------------------------------------------------------
    def product(self):
        p = self.get("/system/product") or {}
        return {"name": p.get("deviceName", ""), "model": p.get("productName", ""),
                "version": p.get("softwareVersion", "")}

    def status(self):
        """Everything worth showing, in one go (the GETs run side by side).
        None for anything the camera does not answer."""
        st, _ = self.request("GET", "/system/product")   # one knock first: a camera that is off costs one timeout, not seventeen
        if st == 0:
            return {"host": self.host, "online": False, **{k: None for k in _STATUS_KEYS}}
        paths = ["/system/product", "/lens/iris", "/lens/zoom", "/lens/focus", "/video/iso",
                 "/video/gain", "/video/whiteBalance", "/video/whiteBalanceTint", "/video/shutter",
                 "/video/ndFilter", "/video/autoExposure", "/transports/0/record",
                 "/camera/tallyStatus", "/transports/0/timecode", "/camera/power",
                 "/presets/active", "/system/videoFormat"]
        with ThreadPoolExecutor(max_workers=6) as ex:
            got = dict(zip(paths, ex.map(self.get, paths)))
        g = lambda p, k: (got.get(p) or {}).get(k)
        power = got.get("/camera/power") or {}
        batts = [b.get("chargeRemainingPercent") for b in power.get("batteries") or []
                 if b.get("chargeRemainingPercent") is not None]
        prod = got.get("/system/product")
        return {
            "host": self.host, "online": prod is not None or any(v is not None for v in got.values()),
            "name": g("/system/product", "deviceName"), "model": g("/system/product", "productName"),
            "version": g("/system/product", "softwareVersion"),
            "iris": g("/lens/iris", "apertureStop"), "iris_auto": g("/lens/iris", "continuousApertureAutoExposure"),
            "iris_norm": g("/lens/iris", "normalised"),
            "zoom_mm": g("/lens/zoom", "focalLength"), "focus": g("/lens/focus", "normalised"),
            "iso": g("/video/iso", "iso"), "gain": g("/video/gain", "gain"),
            "wb": g("/video/whiteBalance", "whiteBalance"), "tint": g("/video/whiteBalanceTint", "whiteBalanceTint"),
            "shutter_speed": g("/video/shutter", "shutterSpeed"), "shutter_angle": g("/video/shutter", "shutterAngle"),
            "shutter_auto": g("/video/shutter", "continuousShutterAutoExposure"),
            "nd": g("/video/ndFilter", "stop"), "ae": g("/video/autoExposure", "mode"),
            "recording": g("/transports/0/record", "recording"),
            "tally": g("/camera/tallyStatus", "status"),      # None / Preview / Program, read-only
            "timecode": g("/transports/0/timecode", "display"),
            "format": g("/system/videoFormat", "name"),
            "preset": g("/presets/active", "preset"),
            "power": power.get("source"), "battery": min(batts) if batts else None,
        }

    def capabilities(self, refresh=False):
        """What this camera accepts: supported lists and ranges (kept once read;
        refresh after a format change - shutters depend on the frame rate)."""
        if self._caps is not None and not refresh:
            return self._caps
        paths = ["/video/supportedISOs", "/video/supportedGains", "/video/supportedShutters",
                 "/video/supportedNDFilters", "/video/whiteBalance/description",
                 "/video/whiteBalanceTint/description", "/lens/iris/description",
                 "/lens/zoom/description", "/lens/focus/description", "/presets", "/video/shutter/measurement"]
        with ThreadPoolExecutor(max_workers=6) as ex:
            got = dict(zip(paths, ex.map(self.get, paths)))
        g = lambda p, k, d=None: (got.get(p) or {}).get(k, d)
        iris = got.get("/lens/iris/description") or {}
        self._caps = {
            "isos": g("/video/supportedISOs", "supportedISOs", []),
            "gains": g("/video/supportedGains", "supportedGains", []),
            "shutter_speeds": g("/video/supportedShutters", "shutterSpeeds", []),
            "shutter_angles": g("/video/supportedShutters", "shutterAngles", []),
            "shutter_measure": g("/video/shutter/measurement", "measurement"),
            "nd_stops": g("/video/supportedNDFilters", "supportedStops", []),
            "wb": g("/video/whiteBalance/description", "whiteBalance"),            # {min, max} kelvin
            "tint": g("/video/whiteBalanceTint/description", "whiteBalanceTint"),  # {min, max}
            "iris": iris.get("apertureStop"), "iris_ok": bool(iris.get("controllable")),
            "zoom_ok": bool(g("/lens/zoom/description", "controllable")),
            "focus_ok": bool(g("/lens/focus/description", "controllable")),
            "presets": g("/presets", "presets", []),
        }
        return self._caps

    # -- setting -----------------------------------------------------------
    @staticmethod
    def _nearest(value, allowed):
        return min(allowed, key=lambda a: abs(a - value)) if allowed else value

    @staticmethod
    def _clamp(value, rng):
        if not rng:
            return value
        return max(rng.get("min", value), min(rng.get("max", value), value))

    def set_iso(self, iso):
        return self._write("PUT", "/video/iso", {"iso": int(self._nearest(iso, self.capabilities()["isos"]))})

    def set_gain(self, db):
        return self._write("PUT", "/video/gain", {"gain": int(self._nearest(db, self.capabilities()["gains"]))})

    def set_wb(self, kelvin):
        return self._write("PUT", "/video/whiteBalance", {"whiteBalance": int(self._clamp(kelvin, self.capabilities()["wb"]))})

    def set_tint(self, tint):
        return self._write("PUT", "/video/whiteBalanceTint", {"whiteBalanceTint": int(self._clamp(tint, self.capabilities()["tint"]))})

    def auto_wb(self):
        return self._write("PUT", "/video/whiteBalance/doAuto")

    def set_nd(self, stop):
        return self._write("PUT", "/video/ndFilter", {"stop": self._nearest(stop, self.capabilities()["nd_stops"])})

    def set_shutter(self, speed=None, angle=None):
        c = self.capabilities()
        if angle is not None:
            return self._write("PUT", "/video/shutter", {"shutterAngle": self._nearest(angle, c["shutter_angles"])})
        return self._write("PUT", "/video/shutter", {"shutterSpeed": int(self._nearest(speed, c["shutter_speeds"]))})

    def set_iris(self, stop=None, normalised=None, step=None):
        """An f-stop, a 0-1 position, or a signed number of steps. The camera
        refuses (403) while auto exposure drives the iris."""
        if step is not None:
            body = {"adjustmentStep": int(step)}
        elif normalised is not None:
            body = {"normalised": max(0.0, min(1.0, float(normalised)))}
        else:
            body = {"apertureStop": self._clamp(float(stop), self.capabilities()["iris"])}
        return self._write("PUT", "/lens/iris", body)

    def set_zoom(self, normalised=None, step=None):
        body = {"adjustmentNormalised": float(step)} if step is not None else {"normalised": max(0.0, min(1.0, float(normalised)))}
        return self._write("PUT", "/lens/zoom", body)

    def set_focus(self, normalised):
        return self._write("PUT", "/lens/focus", {"normalised": max(0.0, min(1.0, float(normalised)))})

    def auto_focus(self, x=0.5, y=0.5):
        return self._write("PUT", "/lens/focus/doAutoFocus", {"position": {"x": x, "y": y}})

    def record(self, on=True):
        return self._write("POST", "/transports/0/record" if on else "/transports/0/stop")

    def set_preset(self, name):
        """One of the camera's own .cset presets (see capabilities()["presets"])."""
        return self._write("PUT", "/presets/active", {"preset": name})

    # -- looks -------------------------------------------------------------
    def capture_look(self):
        """This camera's exposure and colour now, as a look to keep."""
        s = self.status()
        look = {"nd": s["nd"], "iso": s["iso"], "gain": s["gain"], "iris": s["iris"],
                "wb": s["wb"], "tint": s["tint"],
                "shutter": {"angle": s["shutter_angle"]} if s["shutter_angle"] is not None
                           else ({"speed": s["shutter_speed"]} if s["shutter_speed"] is not None else None)}
        return {k: v for k, v in look.items() if v is not None}

    def apply_look(self, look):
        """Send a look's fields in LOOK_FIELDS order; each result says if it took.
        A field this camera cannot take is reported, and the rest still go."""
        out = []
        for f in LOOK_FIELDS:
            v = look.get(f)
            if v is None:
                continue
            if f == "shutter":
                r = self.set_shutter(angle=v.get("angle")) if v.get("angle") is not None else self.set_shutter(speed=v.get("speed"))
            else:
                r = {"nd": self.set_nd, "iso": self.set_iso, "gain": self.set_gain, "iris": self.set_iris,
                     "wb": self.set_wb, "tint": self.set_tint}[f](v)
            r["field"] = f
            out.append(r)
        return out


def main(argv):
    """Bench use. Read-only unless the command is "set", "look-apply" or "rec".
      python3 ursa.py <host> status | caps | watch
      python3 ursa.py <host> set iso 800
      python3 ursa.py <host> look > look.json ; python3 ursa.py <host> look-apply look.json
    """
    if len(argv) < 3:
        print(main.__doc__)
        return 2
    host, cmd = argv[1], argv[2]
    writes = cmd in ("set", "look-apply", "rec")
    cam = Camera(host, read_only=not writes)
    if cmd == "status":
        print(json.dumps(cam.status(), indent=2))
    elif cmd == "caps":
        print(json.dumps(cam.capabilities(), indent=2))
    elif cmd == "look":
        print(json.dumps(cam.capture_look(), indent=2))
    elif cmd == "watch":
        prev = None
        while True:
            s = cam.status()
            if s != prev:
                print(time.strftime("%H:%M:%S"), json.dumps({k: v for k, v in s.items() if prev is None or prev.get(k) != v}))
                prev = s
            time.sleep(1)
    elif cmd == "set" and len(argv) >= 5:
        field, val = argv[3], float(argv[4])
        fn = {"iso": cam.set_iso, "gain": cam.set_gain, "wb": cam.set_wb, "tint": cam.set_tint,
              "nd": cam.set_nd, "iris": cam.set_iris, "focus": cam.set_focus}.get(field)
        if field == "shutter":
            print(json.dumps(cam.set_shutter(speed=val)))
        elif fn:
            print(json.dumps(fn(val)))
        else:
            print("fields: iso gain wb tint nd iris focus shutter")
            return 2
    elif cmd == "look-apply" and len(argv) >= 4:
        print(json.dumps(cam.apply_look(json.load(open(argv[3]))), indent=2))
    elif cmd == "rec" and len(argv) >= 4:
        print(json.dumps(cam.record(argv[3] == "on")))
    else:
        print(main.__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
