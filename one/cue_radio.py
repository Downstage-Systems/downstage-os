"""CueLink radio for the One: a USB ESP32 that hears the lights' own radio.

A Waveshare ESP32-S3-GEEK plugged into one of the One's USB-A ports, running
the listen-only CueLink firmware from the Downstage Cue repo
(tools/bench/radio), prints every CueLink frame it hears as a line of JSON.
This module finds it among the USB serial ports, reads it on a thread, and
keeps what it says: each light heard on the air (level, channel, what it
carries, what it is touching) and the recent link traffic between them. That
includes lights with no WiFi at all, which the network scan cannot see.

Everything here is best-effort, like cue_ble: no radio plugged in means an
empty state, never an error. The radio itself never transmits, so nothing
here can disturb a rig. The only thing ever written to it is its channel
command ("auto" or "ch N"), and only once it has said it is a bench-radio.

Standard library only.
"""

import glob
import json
import os
import re
import select
import sys
import threading
import time
import tty

GONE_SECONDS = 20        # a light not heard for this long is gone (a full channel sweep takes ~15)
KEEP_SECONDS = 600       # forgotten after this
PROBE_SECONDS = 5        # a port that has not said "bench-radio" by now is not the radio
FRAMES_KEPT = 300
# link traffic worth keeping for the page; beacons are the heard list itself
_QUIET = {"beacon"}

_lock = threading.Lock()
_state = {"port": "", "connected": False, "status": {}, "since": 0.0}
_heard = {}              # light id -> what its last beacon said, and when
_frames = []
_mac_to_id = {}
_fd = None
_started = False


def _ports():
    """Candidate serial ports. On the One (Linux) only Espressif devices by
    their stable by-id names, so nothing else on USB is ever opened; on a Mac
    (development) the USB modem ports."""
    want = os.environ.get("DOWNSTAGE_RADIO")
    if want:
        return [want]
    if sys.platform.startswith("linux"):
        ports = sorted(glob.glob("/dev/serial/by-id/usb-Espressif*"))
    else:
        ports = sorted(glob.glob("/dev/cu.usbmodem*"))
    last = _state.get("port")
    return ([last] if last in ports else []) + [p for p in ports if p != last]


def _take(d):
    now = time.time()
    t = d.get("t")
    if t == "radio":
        with _lock:
            _state["status"] = d
        return
    src = d.get("src", "")
    if t == "beacon":
        lid = d.get("id") or src
        with _lock:
            _mac_to_id[src] = lid
            prev = _heard.get(lid)
            rssi = d.get("rssi", 0)
            if prev and now - prev["seen"] < GONE_SECONDS:
                rssi = round(prev["rssi"] * 0.6 + rssi * 0.4, 1)
            _heard[lid] = {
                "id": lid, "mac": src, "label": d.get("label", ""), "model": d.get("model", ""),
                "rank": d.get("rank", 0), "channel": d.get("bch") or d.get("ch"), "rssi": rssi,
                "carrier": bool(d.get("carrier")), "guests": d.get("guests", 0),
                "sharing": bool(d.get("share")), "touching": d.get("touching", ""),
                "ip": "" if d.get("ip") in (None, "0.0.0.0") else d.get("ip"), "seen": now,
            }
        return
    if t == "pkt" and d.get("type") not in _QUIET:
        with _lock:
            dst = d.get("dst", "")
            f = {"t": now, "type": d.get("type", "?"), "from": _mac_to_id.get(src, src),
                 "to": "all" if dst == "FF:FF:FF:FF:FF:FF" else _mac_to_id.get(dst, dst),
                 "rssi": d.get("rssi"), "channel": d.get("ch")}
            for k in ("text", "name", "mirror", "on", "live", "accepted", "ok", "talent", "page", "row", "col"):
                if k in d:
                    f[k] = d[k]
            _frames.append(f)
            del _frames[:-FRAMES_KEPT]


def _read_port(port):
    """Read one port for as long as it is the radio and stays plugged in.
    Returns quickly for a port that is not the radio."""
    global _fd
    try:
        fd = os.open(port, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        tty.setraw(fd)
    except OSError:
        return
    buf, ours, opened = b"", False, time.time()
    try:
        while True:
            r, _, _ = select.select([fd], [], [], 1.0)
            if not ours and time.time() - opened > PROBE_SECONDS:
                return
            if not r:
                continue
            chunk = os.read(fd, 8192)
            if not chunk:
                return   # unplugged
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                try:
                    d = json.loads(line)
                except ValueError:
                    continue   # the half line we opened into
                if not ours and d.get("t") == "radio" and d.get("fw") == "bench-radio":
                    ours = True
                    with _lock:
                        _fd = fd
                        _state.update(port=port, connected=True, since=time.time())
                    print(f"[radio] CueLink radio on {port} ({d.get('mac', '')})", flush=True)
                if ours:
                    _take(d)
    except OSError:
        pass
    finally:
        if ours:
            with _lock:
                _fd = None
                _state["connected"] = False
            print("[radio] CueLink radio unplugged", flush=True)
        try:
            os.close(fd)
        except OSError:
            pass


def _loop():
    while True:
        for port in _ports():
            _read_port(port)
        time.sleep(3)


def start():
    """Look for the radio, now and whenever it is plugged in. Safe to call twice."""
    global _started
    if _started:
        return
    _started = True
    threading.Thread(target=_loop, daemon=True, name="cue-radio").start()


def send(cmd):
    """The radio's only command: "auto" (follow the lights' channel) or "ch N" (hold N)."""
    cmd = (cmd or "").strip()
    if not (cmd == "auto" or re.fullmatch(r"ch (1[0-3]|[1-9])", cmd)):
        return False, "auto, or ch 1-13"
    with _lock:
        fd = _fd
    if fd is None:
        return False, "no radio plugged in"
    try:
        os.write(fd, (cmd + "\n").encode())
        return True, ""
    except OSError as e:
        return False, str(e)


def state():
    now = time.time()
    with _lock:
        for k in [k for k, h in _heard.items() if now - h["seen"] > KEEP_SECONDS]:
            del _heard[k]
        heard = [dict(h, age=round(now - h["seen"], 1), gone=now - h["seen"] > GONE_SECONDS) for h in _heard.values()]
        heard.sort(key=lambda h: (h["gone"], -h["rssi"]))
        st = _state["status"] or {}
        return {
            "connected": _state["connected"],
            "port": _state["port"],
            "channel": st.get("realCh") or st.get("ch"),
            "mode": st.get("mode", ""),
            "frames_seen": st.get("seen", 0),
            "dropped": st.get("dropped", 0),
            "heard": heard,
            "frames": _frames[-40:],
        }


# ---- the One's side of the USB port (Linux) -----------------------------------

UDEV_RULE = "/etc/udev/rules.d/60-downstage-radio.rules"
UDEV_TEXT = (
    "# Downstage One: the CueLink radio (an Espressif ESP32-S3 on USB).\n"
    "# Readable by the One's service without a group change (which would need a\n"
    "# restart to take), and never probed by ModemManager, whose AT commands\n"
    "# would land on the radio.\n"
    'SUBSYSTEM=="tty", ATTRS{idVendor}=="303a", MODE="0666", ENV{ID_MM_DEVICE_IGNORE}="1"\n'
)


def udev_guard(run):
    """Write the udev rule once and apply it to anything already plugged in.
    `run` is the caller's subprocess.run, so tests can stub it. Linux only."""
    if not sys.platform.startswith("linux"):
        return
    try:
        try:
            have = open(UDEV_RULE).read()
        except OSError:
            have = ""
        if have == UDEV_TEXT:
            return   # the usual boot: nothing to do, no sudo
        # the same rule install.sh writes, for a unit built before it existed
        run(["sudo", "tee", UDEV_RULE], input=UDEV_TEXT, text=True, timeout=10)
        run(["sudo", "udevadm", "control", "--reload-rules"], timeout=10)
        run(["sudo", "udevadm", "trigger", "--subsystem-match=tty", "--attr-match=idVendor=303a"], timeout=10)
        print("[radio] udev rule written", flush=True)
    except Exception as e:
        print(f"[radio] udev guard: {e}", flush=True)
