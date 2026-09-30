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

Safe by default (Coding Main's review, 2026-09-30):
- A Camera is read-only unless made with read_only=False: a write is refused
  before it reaches the network. The One sets this per unit in its config,
  never per request, so a bad caller cannot write to a camera.
- Record is apart even then: start/stop needs allow_record=True (the One's
  own opt-in, off until the operator turns it on) - a missed take cannot be
  re-run.
- Every write that goes out is reported to audit(event, detail) - the One's
  _audit - so when a shot changes mid-show the log says what touched it.
- Pool polls cameras on background threads and serves the last answer:
  nothing a page asks for ever waits on a camera that has gone away.
- A camera is keyed by the address it is configured under on the One, and
  labelled by the name it reports (deviceName, set in Blackmagic Camera
  Setup). The API reports no serial or hardware id (/system/product has only
  deviceName, productName, softwareVersion), and every camera ships with the
  same name - so the name cannot be the key. A rename is cosmetic: same key,
  same history. Cameras sharing a name all still work, with a clash message
  that says what to fix. Configure each by IP, or by a .local name only once
  it is unique: two cameras on one default name share one .local address.

Standard library only (the One has requests; the bench Mac does not).
"""

import json
import ssl
import sys
import threading
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


class RecordOff(Exception):
    """Record start/stop asked of a camera without allow_record."""


class Camera:
    def __init__(self, host, read_only=True, allow_record=False, audit=None, scheme=None, timeout=TIMEOUT):
        self.host = host.strip().rstrip("/")
        self.read_only = read_only
        self.allow_record = allow_record
        self.audit = audit            # audit(event, detail) for every write sent
        self.name = ""                # as the camera reports it, once it has answered
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
        if self.audit:
            try:
                self.audit("CAMERA", f"{self.name or self.host} ({self.host}) {method} {path}"
                                     f"{' ' + json.dumps(body) if body is not None else ''} -> {st or 'no answer'}")
            except Exception:
                pass
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
        if prod and prod.get("deviceName"):
            self.name = prod["deviceName"]
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
        if not self.allow_record:
            raise RecordOff(f"record {'start' if on else 'stop'} refused: record control is off for {self.name or self.host}")
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


class Pool:
    """The One's cameras, each polled on its own thread; everything a page asks
    for is the last answer, never a wait on the camera.

    hosts: the configured addresses (10.0.0.21, cam1.local) - each one's key.
    writes and record come from the unit's config. snapshot() lists them with
    the name each reports as its label; camera(key) gives the Camera to set
    things on (a unique reported name works too)."""

    STALE_S = 5.0

    def __init__(self, hosts, writes=False, record=False, audit=None, every=1.0, timeout=TIMEOUT):
        self.writes, self.record, self.audit, self.every, self.timeout = writes, record, audit, every, timeout
        self._lock = threading.Lock()
        self._cams, self._state, self._threads = {}, {}, {}
        self._stop = threading.Event()
        self.set_hosts(hosts)

    def set_hosts(self, hosts):
        with self._lock:
            want = [h.strip() for h in hosts if h and h.strip()]
            for h in list(self._cams):
                if h not in want:
                    del self._cams[h]
                    self._state.pop(h, None)
            for h in want:
                if h not in self._cams:
                    self._cams[h] = Camera(h, read_only=not self.writes, allow_record=self.record,
                                           audit=self.audit, timeout=self.timeout)
                    t = threading.Thread(target=self._poll, args=(h,), daemon=True, name=f"ursa-{h}")
                    self._threads[h] = t
                    t.start()

    def _poll(self, host):
        while not self._stop.is_set():
            with self._lock:
                cam = self._cams.get(host)
            if cam is None:
                return                                   # taken out of the config
            s = cam.status()
            s["seen"] = time.time()
            with self._lock:
                if host in self._cams:
                    prev = self._state.get(host) or {}
                    if not s["online"] and prev.get("name"):  # keep who it was while it is away
                        s = dict(prev, online=False, seen=prev.get("seen", 0))
                    self._state[host] = s
            self._stop.wait(self.every if s.get("online") else max(self.every, 3.0))

    def snapshot(self):
        now = time.time()
        with self._lock:
            rows = [dict(v, host=h) for h, v in self._state.items()]
            pending = [h for h in self._cams if h not in self._state]
        by_name = {}
        for r in rows:
            if r.get("name"):
                by_name.setdefault(r["name"], []).append(r["host"])
        for r in rows:
            r["key"] = r["host"]
            r["label"] = r.get("name") or r["host"]
            r["stale"] = r["online"] and now - r.get("seen", 0) > self.STALE_S
            dup = by_name.get(r.get("name"), [])
            r["duplicate"] = dup if len(dup) > 1 else []
            r["clash"] = (f'{len(dup)} cameras call themselves "{r["name"]}" - give each its own name in '
                          f'Blackmagic Camera Setup (they still work here, by address)') if len(dup) > 1 else ""
        rows += [{"host": h, "key": h, "label": h, "online": None, "stale": False, "duplicate": [], "clash": ""}
                 for h in pending]
        return sorted(rows, key=lambda r: (r["label"], r["key"]))

    def camera(self, key):
        """The Camera for a key (its configured address), or for a reported
        name that only one camera has. None when unknown or ambiguous."""
        with self._lock:
            if key in self._cams:
                return self._cams[key]
            hits = [h for h, v in self._state.items() if v.get("name") == key]
            return self._cams[hits[0]] if len(hits) == 1 else None

    def stop(self):
        self._stop.set()


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
    cam = Camera(host, read_only=not writes, allow_record=cmd == "rec",
                 audit=lambda ev, d: print(f"[{ev}] {d}", file=sys.stderr))
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
