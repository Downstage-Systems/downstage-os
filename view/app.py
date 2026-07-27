import json
import os
import re
import subprocess
import threading
import datetime
import time
import socket
from pathlib import Path

import requests
from flask import Flask, jsonify, render_template, request, send_file

OS_VERSION = "1.5.1"   # Downstage OS release — bump on tagged releases
OS_PRODUCT = "Downstage View"

app = Flask(__name__)


@app.after_request
def _no_html_cache(resp):
    """The UI must never be stale: after a self-update, a cached page in a
    phone's home-screen web app would keep showing the old interface."""
    if resp.mimetype == "text/html":
        resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.after_request
def _no_store(resp):
    # the config UI must never be served stale from browser cache — the page
    # changes with every OS update, and a cached copy silently breaks controls
    if resp.content_type and resp.content_type.startswith("text/html"):
        resp.headers["Cache-Control"] = "no-store, must-revalidate"
    return resp
BASE_DIR    = Path(__file__).parent
CONFIG_FILE = BASE_DIR / "config.json"

# ── e-Paper display (optional) ────────────────────────────────────────────────
try:
    try:
        from waveshare_epd import epd2in13_V4 as _epd_mod
    except ImportError:
        from waveshare_epd import epd2in13 as _epd_mod
    from PIL import Image, ImageDraw, ImageFont
    _EPAPER_LIB = True
except ImportError:
    _EPAPER_LIB = False

VIEWS = [
    {"label": "Stage Timer",      "path": "/timer",          "group": "Display"},
    {"label": "Countdown",        "path": "/countdown",      "group": "Display"},
    {"label": "Backstage / Crew", "path": "/backstage",      "group": "Display"},
    {"label": "Studio Clock",     "path": "/studio",         "group": "Display"},
    {"label": "Timeline",         "path": "/timeline",       "group": "Display"},
    {"label": "Public Info",      "path": "/info",           "group": "Display"},
    {"label": "Operator",         "path": "/op",             "group": "Operator"},
    {"label": "Cue Sheet",        "path": "/cuesheet",       "group": "Operator"},
    {"label": "Editor",           "path": "/editor",         "group": "Editor"},
    {"label": "Timer Control",    "path": "/timercontrol",   "group": "Editor"},
    {"label": "Message Control",  "path": "/messagecontrol", "group": "Editor"},
    {"label": "Rundown",          "path": "/rundown",        "group": "Editor"},
]

_win   = None
_wlock = threading.Lock()

_COMMON_FLAGS = [
    "--noerrdialogs",
    "--disable-session-crashed-bubble",
    "--hide-crash-restore-bubble",
    # paint House Black from the first frame — otherwise every fresh window
    # flashes white on screen before the page's dark background loads
    "--default-background-color=0b0d10",
    # Pi Zero 2 W memory diet — chromium subprocesses OOM under pressure,
    # leaving a black page that never retries
    "--renderer-process-limit=1",
    "--disable-extensions",
    "--disable-background-networking",
    "--disable-crash-reporter",
    "--disk-cache-size=1048576",
    "--js-flags=--max-old-space-size=128",
    "--disable-infobars",
    "--no-first-run",
    "--no-memcheck",
    "--disable-gpu",
    "--disable-dev-shm-usage",
    "--disable-restore-session-state",
    "--disable-translate",
    "--disable-features=TranslateUI",
    "--check-for-update-interval=31536000",
    "--password-store=basic",
]


# ── Config ────────────────────────────────────────────────────────────────────

def load_config():
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE) as f:
            data = json.load(f)
    else:
        data = {}
    data.setdefault("ip", "")
    data.setdefault("source", "/timer")
    data.setdefault("external_url", "")
    # Per-unit identity from the build log (this unit: DSV-A-2607-0001)
    data.setdefault("hotspot_ssid", f"Downstage-{socket.gethostname().split(chr(45))[-1].upper() or chr(48)*4}")
    data.setdefault("hotspot_pass", "downstage")
    data.setdefault("hotspot_auto", True)
    data.setdefault("cleantimer_freeze", True)
    data.setdefault("cleantimer_hideprogress", True)
    data.setdefault("cleantimer_hideclock", True)
    data.setdefault("cleantimer_hidecards", True)
    data.setdefault("cleantimer_keycolour",   "000000")
    data.setdefault("cleantimer_timercolour", "ffffff")
    data.setdefault("watchdog", True)
    data.setdefault("os_update_repo", "")   # e.g. "youruser/downstage-os"
    data.setdefault("ip_history", [])
    return data


def save_config(updates: dict):
    current = load_config()
    current.update(updates)
    with open(CONFIG_FILE, "w") as f:
        json.dump(current, f)


def _update_ip_history(ip: str) -> list:
    config  = load_config()
    history = [h for h in config.get("ip_history", []) if h != ip]
    return ([ip] + history)[:5]


# ── Helpers ───────────────────────────────────────────────────────────────────

def get_local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "unknown"


def _iface_ip(iface):
    try:
        import fcntl, struct
        sk = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        r = fcntl.ioctl(sk.fileno(), 0x8915,
                        struct.pack("256s", iface[:15].encode()))
        sk.close()
        return socket.inet_ntoa(r[20:24])
    except Exception:
        return None


def _net_ifaces():
    try:
        return sorted(n for n in os.listdir("/sys/class/net")
                      if n.startswith(("eth", "enx", "wlan")))
    except Exception:
        return ["eth0", "wlan0"]


def get_all_interfaces():
    out = []
    for iface in _net_ifaces():
        ip = _iface_ip(iface)
        if not ip:
            continue
        kind = "WiFi" if iface.startswith("wlan") else "Ethernet"
        out.append({"iface": iface, "ip": ip, "kind": kind})
    out.sort(key=lambda x: 0 if x["kind"] == "Ethernet" else 1)
    return out


def primary_iface():
    """The interface carrying the default route (wired-first by metric)."""
    try:
        out = subprocess.check_output(["ip", "route", "get", "8.8.8.8"],
                                      text=True, timeout=5)
        for tok in out.split():
            if tok == "dev":
                return out.split("dev")[1].split()[0]
    except Exception:
        pass
    ifs = get_all_interfaces()
    return ifs[0]["iface"] if ifs else "unknown"


# ── captive portal / internet probe (ported from the One) ─────────────────────
_portal = {"detected": False, "iface": "", "checked": 0, "internet": None}
_probe_busy = threading.Lock()

def _probe_portal():
    detected, piface, internet = False, "", False
    for iface in _net_ifaces():
        try:
            if open(f"/sys/class/net/{iface}/operstate").read().strip() != "up":
                continue
            r = subprocess.run(
                ["curl", "-s", "-m", "8", "--interface", iface,
                 "-o", "/dev/null", "-w", "%{http_code} %{redirect_url}",
                 "http://connectivitycheck.gstatic.com/generate_204"],
                capture_output=True, text=True, timeout=12)
            code, _, redirect = r.stdout.strip().partition(" ")
            if code == "204":
                internet = True
            elif code.startswith("3") and redirect:
                detected = True
                piface = piface or iface
        except Exception:
            continue
    _portal.update(detected=detected, iface=piface, internet=internet)
    _portal["checked"] = time.time()


def _portal_loop():
    while True:
        _probe_portal()
        time.sleep(120)


def _probe_async_if_stale(max_age=60):
    if time.time() - _portal["checked"] < max_age:
        return
    if not _probe_busy.acquire(blocking=False):
        return
    def run():
        try:
            _probe_portal()
        finally:
            _probe_busy.release()
    threading.Thread(target=run, daemon=True).start()


threading.Thread(target=_portal_loop, daemon=True).start()


def check_ontime(ip, timeout=3):
    try:
        r = requests.get(f"http://{ip}:4001/api/version", timeout=timeout)
        return r.status_code < 300
    except Exception:
        return False


def _cpu_temp():
    try:
        raw = Path("/sys/class/thermal/thermal_zone0/temp").read_text().strip()
        return f"{int(raw) / 1000:.1f}°C"
    except Exception:
        return None


def _ram_usage():
    try:
        info = {}
        for line in Path("/proc/meminfo").read_text().splitlines():
            k, v = line.split(":")
            info[k.strip()] = int(v.strip().split()[0])
        used  = (info["MemTotal"] - info["MemAvailable"]) // 1024
        total = info["MemTotal"] // 1024
        return used, total
    except Exception:
        return None


def _ontime_runtime(ip, timeout=2):
    try:
        r = requests.get(f"http://{ip}:4001/data/runtime", timeout=timeout)
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return None


def _active_link():
    """("Ethernet"|"WiFi: <ssid>"|"Hotspot"|"Not connected") for the front
    panel — a rack tech should see HOW the unit is on the network."""
    try:
        out = subprocess.check_output(["ip", "-4", "-o", "addr", "show"],
                                      text=True, timeout=5)
        # collect everything first — kernel order lists wlan0 before a USB
        # eth0, so returning on first match hid Ethernet when both were up
        has_eth = has_wifi = has_hotspot = False
        for line in out.splitlines():
            parts = line.split()
            iface, addr = parts[1], parts[3].split("/")[0]
            if iface == "lo":
                continue
            if addr.startswith("10.42."):
                has_hotspot = True
            elif iface.startswith(("eth", "enx", "en")):
                has_eth = True
            elif iface.startswith("wlan"):
                has_wifi = True
        if has_eth:
            return "Ethernet"
        if has_wifi:
            ssid = _active_ssid()
            return f"WiFi: {ssid}" if ssid else "WiFi"
        if has_hotspot:
            return "Hotspot"
    except Exception:
        pass
    return "Not connected"


def _active_ssid():
    try:
        out = subprocess.check_output(
            ["nmcli", "-t", "-f", "active,ssid", "dev", "wifi"],
            text=True, timeout=5,
        )
        for line in out.splitlines():
            parts = line.split(":")
            if parts[0] == "yes" and len(parts) > 1 and parts[1]:
                return parts[1]
    except Exception:
        pass
    return None


# ── Browser ───────────────────────────────────────────────────────────────────

def _chromium_env():
    env = os.environ.copy()
    env.setdefault("DISPLAY", ":0")
    env.setdefault("XAUTHORITY", str(Path.home() / ".Xauthority"))
    return env


def _kill(proc):
    if proc and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def _clean_external_url(url):
    """Normalize a user-entered external viewer URL; prepend https:// if bare."""
    url = (url or "").strip()
    if url and not re.match(r"^https?://", url, re.I):
        url = "https://" + url
    return url


def _is_ontime_source(source):
    if source and source.startswith("pattern-"):
        return False
    return source not in ("config", "off", "external", None, "")


def _hex6(v, default):
    """Sanitize a colour value to 6-digit hex (no #) or fall back."""
    v = (v or "").lstrip("#").lower()
    return v if re.fullmatch(r"[0-9a-f]{6}", v) else default


def _cleantimer_params():
    """Query string for the Custom Timer preset — always chromakey-ready
    (black key, white timer, no cards/logo), with the show-day options
    from config."""
    cfg = load_config()
    params = ["hideLogo=true",
              "keyColour=" + _hex6(cfg.get("cleantimer_keycolour"), "000000"),
              "timerColour=" + _hex6(cfg.get("cleantimer_timercolour"), "ffffff")]
    if cfg.get("cleantimer_hidecards", True):
        params.append("hideCards=true")
    if cfg.get("cleantimer_hideprogress", True):
        params.append("hideProgress=true")
    if cfg.get("cleantimer_hideclock", True):
        params.append("hideClock=true")
    if cfg.get("cleantimer_freeze", True):
        params.append("freezeOvertime=true")
    return "&".join(params)


def _source_url(source):
    """Map a source name to the URL the kiosk window should show."""
    if source.startswith("pattern-"):
        return f"http://localhost:8080/pattern/{source.split('-', 1)[1]}"
    if source == "off":
        return "http://localhost:8080/blackout-page"
    if source == "config":
        return "http://localhost:8080"
    if source == "holding":
        return "http://localhost:8080/holding"
    config = load_config()
    ip     = config.get("ip", "")
    if source == "external":
        return config.get("external_url", "").strip() or "http://localhost:8080"
    if source == "welcome":
        return "http://localhost:8080/welcome"
    if not ip:
        return "http://localhost:8080/holding"
    if _is_ontime_source(source) and not check_ontime(ip, timeout=2):
        # server down — branded holding page instead of a browser error;
        # the watchdog navigates back when it answers
        return "http://localhost:8080/holding"
    if source == "cleantimer":
        return f"http://{ip}:4001/timer/?" + _cleantimer_params()
    return f"http://{ip}:4001{source}"


_DEBUG_PORT = 9222   # chromium DevTools, loopback only


def _navigate(url):
    """Point the RUNNING kiosk page at a new URL via CDP Page.navigate —
    ~2s versus ~20s cold start on the Zero 2 W. Kiosk-mode chromium ignores
    second-instance URL handoffs and this build's /json/new is unreliable,
    so the websocket protocol is the one lever that works.
    Requires python3-websocket (in the golden image)."""
    try:
        import websocket
    except ImportError:
        return False
    base = f"http://127.0.0.1:{_DEBUG_PORT}"
    try:
        pages = [t for t in requests.get(f"{base}/json", timeout=2).json()
                 if t.get("type") == "page"]
        if not pages:
            return False
        ws = websocket.create_connection(pages[0]["webSocketDebuggerUrl"],
                                         timeout=8, suppress_origin=True)
        ws.send(json.dumps({"id": 1, "method": "Page.navigate",
                            "params": {"url": url}}))
        ws.recv()
        ws.close()
        return True
    except Exception as e:
        print(f"[navigate] {type(e).__name__}: {e}")
        return False


def _cdp_cmd(method, params=None, timeout=15):
    """One CDP command against the kiosk page; None when the kiosk is down."""
    try:
        import websocket
        pages = [t for t in requests.get(f"http://127.0.0.1:{_DEBUG_PORT}/json",
                                         timeout=2).json() if t.get("type") == "page"]
        if not pages:
            return None
        ws = websocket.create_connection(pages[0]["webSocketDebuggerUrl"],
                                         timeout=timeout, suppress_origin=True)
        ws.send(json.dumps({"id": 1, "method": method, "params": params or {}}))
        while True:
            m = json.loads(ws.recv())
            if m.get("id") == 1:
                break
        ws.close()
        return m.get("result")
    except Exception as e:
        print(f"[cdp] {method}: {type(e).__name__}: {e}")
        return None


# ── HDMI output confidence chain ──────────────────────────────────────────────
# Everything here is read, never guessed: port state from the kernel's DRM
# connector, display identity from the monitor's own EDID handshake, render
# truth from the kiosk browser over CDP.

def _edid_identity():
    """(mfr, name) from the connected display's EDID, '' when not offered."""
    import glob
    try:
        raw = open(glob.glob("/sys/class/drm/card*-HDMI*/edid")[0], "rb").read()
        if len(raw) < 128:
            return "", ""
        w = (raw[8] << 8) | raw[9]
        mfr = "".join(chr(64 + ((w >> sh) & 31)) for sh in (10, 5, 0))
        name = ""
        for i in range(54, 126, 18):
            if raw[i:i + 5] == bytes([0, 0, 0, 0xFC, 0]):
                name = raw[i + 5:i + 18].decode("ascii", "ignore").strip()
        return mfr, name
    except Exception:
        return "", ""


_output_cache = {"ts": 0.0, "data": None}

def _output_chain():
    """Live state of the output path; cached briefly — /status polls often.

    The golden image sets hdmi_force_hotplug=1 so the kiosk renders headless,
    which pins the DRM connector to "connected" forever — useless for cable
    truth. A real display is detected by its EDID answering on DDC, and we
    force a reprobe each refresh so a cable pulled mid-show goes red on the
    next poll instead of lingering as a stale cached handshake."""
    if time.time() - _output_cache["ts"] < 8 and _output_cache["data"]:
        return _output_cache["data"]
    import glob
    hdmi = {"connected": False, "enabled": False, "mode": ""}
    try:
        c = glob.glob("/sys/class/drm/card*-HDMI*")[0]
        subprocess.run(["sudo", "sh", "-c", f"echo detect > {c}/status"],
                       timeout=3, capture_output=True)
        edid_len = len(open(f"{c}/edid", "rb").read())
        hdmi["connected"] = edid_len > 0          # a live display answered DDC
        hdmi["enabled"]  = open(f"{c}/enabled").read().strip() == "enabled"
        modes = open(f"{c}/modes").read().split()
        hdmi["mode"] = modes[0] if modes else ""
    except Exception:
        pass
    render = {"up": False, "url": ""}
    try:
        pages = [t for t in requests.get(f"http://127.0.0.1:{_DEBUG_PORT}/json",
                                         timeout=2).json() if t.get("type") == "page"]
        if pages:
            render = {"up": True, "url": pages[0].get("url", "")}
    except Exception:
        pass
    mfr, name = _edid_identity()
    data = {"hdmi": hdmi, "render": render,
            "display": {"mfr": mfr, "name": name}}
    _output_cache.update(ts=time.time(), data=data)
    return data


@app.route("/output/snapshot")
def output_snapshot():
    """One frame of what the kiosk is rendering right now (~2-3s on this
    hardware). On demand only — never polled."""
    import base64
    r = _cdp_cmd("Page.captureScreenshot",
                 {"format": "jpeg", "quality": 60}, timeout=20)
    if not r or "data" not in r:
        return jsonify({"ok": False, "error": "kiosk not responding"}), 503
    from flask import Response
    return Response(base64.b64decode(r["data"]), mimetype="image/jpeg",
                    headers={"Cache-Control": "no-store"})


@app.route("/output/identify", methods=["POST"])
def output_identify():
    """Flash the unit's name on the physical output for 5 seconds."""
    config = load_config()
    label = config.get("unit_name") or socket.gethostname()
    serial = config.get("serial", "")
    js = (
        "(() => { const o=document.createElement('div');"
        "o.style.cssText='position:fixed;inset:0;z-index:2147483647;"
        "background:#0b0d10;color:#e8ecef;display:flex;flex-direction:column;"
        "align-items:center;justify-content:center;font-family:sans-serif';"
        "o.innerHTML='<div style=\"font-size:9vw;font-weight:700;"
        "letter-spacing:0.06em\">DOWNSTAGE VIEW</div>"
        f"<div style=\"font-size:4vw;color:#2fd97b;margin-top:3vh\">{label}"
        f"{' &middot; ' + serial if serial else ''}</div>';"
        "document.body.appendChild(o); setTimeout(() => o.remove(), 5000); })()"
    )
    r = _cdp_cmd("Runtime.evaluate", {"expression": js}, timeout=8)
    return jsonify({"ok": r is not None})


_blackout_active = False


def _show(url, force=False):
    """Show a URL on the output: reuse the live window when possible,
    A live blackout overrides every navigation except its own (force=True) —
    the watchdog can swap pages underneath without lifting the black.
    cold-start chromium only when there isn't one. A freshly cold-started
    chromium needs ~20s before its DevTools port answers on this hardware,
    so navigation retries patiently rather than triggering cascading
    cold starts."""
    global _win
    if _blackout_active and not force:
        url = "http://localhost:8080/blackout-page"
    with _wlock:
        if _win and _win.poll() is None:
            deadline = time.time() + 75
            while time.time() < deadline:
                if _navigate(url):
                    print(f"[window] navigated -> {url}")
                    return
                if _win.poll() is not None:
                    break   # window died — cold start below
                time.sleep(2)
            print("[window] navigation unavailable — cold starting")
        _kill(_win)
        _kill_orphan_windows()
        _win = subprocess.Popen([
            "chromium", *_COMMON_FLAGS,
            "--user-data-dir=/tmp/kiosk-lite",
            f"--remote-debugging-port={_DEBUG_PORT}",
            "--remote-allow-origins=*",
            "--kiosk", url,
        ], env=_chromium_env())
        print(f"[window] cold start -> {url}")


def _open_window(source):
    """Compatibility shim — shows the source and returns the window handle."""
    _show(_source_url(source))
    return _win


_os_update = {"latest": None, "update_available": False, "checked": False}


def _refresh_os_update():
    """Compare OS_VERSION to the latest GitHub release; update _os_update."""
    def vt(v):
        try:
            return tuple(int(x) for x in str(v).lstrip("v").split(".")[:3])
        except Exception:
            return (0, 0, 0)
    try:
        repo = load_config().get("os_update_repo", "")
        if repo:
            r = requests.get(f"https://api.github.com/repos/{repo}/releases/latest", timeout=10)
            latest = r.json().get("tag_name", "").lstrip("v") or None
            _os_update["latest"] = latest
            _os_update["update_available"] = bool(latest and vt(latest) > vt(OS_VERSION))
            _os_update["checked"] = True
    except Exception as e:
        _os_update["checked"] = True
        print(f"[updates] os check failed: {e}")


# ── Blessed system patches — see the One's app.py for the full rationale.
# The Zero 2 W is slow; a big backlog can take 30+ minutes, hence the timeout.

PATCH_STAMP    = BASE_DIR / ".os-patched"
PATCH_BASELINE = "2026-04-27"
PATCH_LOG      = BASE_DIR / "patch.log"

_patch_state  = {"state": "idle", "message": ""}
_patch_status = {"tested_through": None, "last_applied": None,
                 "available": False, "notes": "", "checked": False}


def _patches_last_applied():
    try:
        return PATCH_STAMP.read_text().strip() or PATCH_BASELINE
    except Exception:
        return PATCH_BASELINE


def _refresh_patches():
    try:
        repo = load_config().get("os_update_repo", "")
        tested = notes = None
        if repo:
            r = requests.get(
                f"https://raw.githubusercontent.com/{repo}/main/patches.json",
                timeout=10,
            )
            if r.status_code == 200:
                marker = r.json()
                tested = marker.get("tested_through")
                notes  = marker.get("notes", "")
        last = _patches_last_applied()
        _patch_status.update(
            tested_through=tested, last_applied=last, notes=notes or "",
            available=bool(tested and tested > last), checked=True)
    except Exception as e:
        _patch_status["checked"] = True
        print(f"[updates] patches check failed: {e}")


def _patch_worker(tested):
    env = {**os.environ, "DEBIAN_FRONTEND": "noninteractive"}
    try:
        with open(PATCH_LOG, "ab") as log:
            log.write(f"\n===== patch run {time.strftime('%F %T')} (through {tested}) =====\n".encode())
            log.flush()
            _patch_state["message"] = "Downloading package lists…"
            subprocess.run(["sudo", "-E", "apt-get", "update"],
                           env=env, check=True, timeout=900, stdout=log, stderr=log)
            _patch_state["message"] = "Installing tested patches — 30+ minutes on this hardware…"
            subprocess.run(
                ["sudo", "-E", "apt-get", "-y",
                 "-o", "Dpkg::Options::=--force-confdef",
                 "-o", "Dpkg::Options::=--force-confold",
                 "upgrade"],
                env=env, check=True, timeout=7200, stdout=log, stderr=log)
        PATCH_STAMP.write_text(tested)
        _audit("OS_PATCH", f"system patches applied (tested through {tested})")
        _patch_state.update(state="done",
                            message="Patches installed. Restart the unit when convenient.")
        _refresh_patches()
    except Exception as e:
        _audit("OS_PATCH", f"patch run FAILED: {e}")
        _patch_state.update(state="failed",
                            message=f"Patch run failed: {e} — see patch.log in diagnostics")


@app.route("/system/patches/apply", methods=["POST"])
def patches_apply():
    if _patch_state["state"] == "running":
        return jsonify({"ok": False, "message": "A patch run is already in progress"})
    if not _patch_status.get("available"):
        return jsonify({"ok": False, "message": "No tested patches available"})
    _patch_state.update(state="running", message="Starting…")
    threading.Thread(target=_patch_worker,
                     args=(_patch_status["tested_through"],), daemon=True).start()
    return jsonify({"ok": True})


@app.route("/system/patches/status")
def patches_status():
    return jsonify({**_patch_status, **_patch_state})


def _check_os_update():
    """Boot + daily refresh loop."""
    while True:
        _refresh_os_update()
        _refresh_patches()
        time.sleep(86400)


@app.route("/os/update-file", methods=["POST"])
def os_update_file():
    """Offline update: install a release archive uploaded through the
    browser — same staging, validation, swap, and auto-rollback as the
    GitHub path. For venues with no internet."""
    import tarfile, py_compile
    f = request.files.get("release")
    if not f:
        return jsonify({"ok": False, "message": "No file uploaded"})
    force = request.form.get("force") == "true"
    try:
        work = Path("/tmp/ds-os-update")
        subprocess.run(["rm", "-rf", str(work)])
        work.mkdir(parents=True)
        tarball = work / "src.tar.gz"
        f.save(tarball)
        with tarfile.open(tarball) as tf:
            tf.extractall(work, filter="data")
        roots = [p for p in work.iterdir() if p.is_dir()]
        src_dir = next((p / _OS_VARIANT for p in roots
                        if (p / _OS_VARIANT / "app.py").exists()), None)
        if not src_dir:
            return jsonify({"ok": False,
                            "message": f"Not a Downstage OS release — the archive has no {_OS_VARIANT}/app.py"})
        py_compile.compile(str(src_dir / "app.py"), doraise=True)
        if not (src_dir / "templates" / "index.html").exists():
            return jsonify({"ok": False, "message": "Archive is missing templates/index.html"})
        m = re.search(r'OS_VERSION = "([^"]+)"', (src_dir / "app.py").read_text())
        ver = m.group(1) if m else None
        if not ver:
            return jsonify({"ok": False, "message": "Archive has no OS_VERSION"})
        if not force and _version_tuple(ver) <= _version_tuple(OS_VERSION):
            return jsonify({"ok": False,
                            "message": f"Archive is v{ver} — this unit already runs v{OS_VERSION}"})
        tag = f"v{ver}"
        script = work / "swap.sh"
        script.write_text(_SWAP_SCRIPT.format(
            src=src_dir, app=_OS_APPDIR, tag=tag, restart=_OS_RESTART_CMD))
        script.chmod(0o755)
        subprocess.Popen(
            ["systemd-run", "--user", "--collect", f"--unit=ds-os-update-{int(time.time())}",
             "bash", str(script)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        return jsonify({"ok": True, "message": f"Installing {tag} — service will restart"})
    except py_compile.PyCompileError as e:
        return jsonify({"ok": False, "message": f"Archive failed validation: {e}"})
    except Exception as e:
        return jsonify({"ok": False, "message": str(e)})


@app.route("/os/recheck", methods=["POST"])
def os_recheck():
    _refresh_os_update()
    _refresh_patches()
    return jsonify({"ok": True, "installed": OS_VERSION,
                    "latest": _os_update["latest"],
                    "update_available": _os_update["update_available"]})


_watchdog_override = False


def _launch_watchdog_window():
    """Swap to the holding page without touching config."""
    try:
        source = load_config().get("source", "/timer")
        _show(_source_url("holding" if _is_ontime_source(source) else source))
        print("[watchdog] holding window shown")
    except Exception as e:
        print(f"[watchdog] FAILED to show holding window: {e}")


def _ontime_watchdog():
    """Background thread: swap to a holding page when OnTime goes offline.
    Two consecutive failed checks (~60s) required, so one slow response on
    venue WiFi doesn't flap the display."""
    global _watchdog_override
    was_connected = None
    misses = 0
    while True:
        time.sleep(30)
        config = load_config()
        if not config.get("watchdog", True):
            was_connected = None
            misses = 0
            continue
        ip = config.get("ip", "")
        if not ip or not _is_ontime_source(config.get("source", "/timer")):
            was_connected = None
            misses = 0
            continue
        connected = check_ontime(ip, timeout=3)
        if was_connected is None:
            was_connected = connected
            continue
        if not connected:
            misses += 1
        else:
            misses = 0
        if was_connected and misses >= 2:
            print("[watchdog] OnTime offline (2 checks) — switching to holding page")
            _watchdog_override = True
            threading.Thread(target=_launch_watchdog_window, daemon=True).start()
            was_connected = False
        elif not was_connected and connected:
            print("[watchdog] OnTime back online — restoring view")
            _watchdog_override = False
            threading.Thread(target=launch_window, daemon=True).start()
            was_connected = True


@app.route("/blackout-page")
def blackout_page_view():
    return '<html><body style="margin:0;background:#000"></body></html>', 200, {"Content-Type": "text/html"}


_PATTERN_PAGE = """<!DOCTYPE html><html><head><meta charset="utf-8"><style>
*{margin:0;padding:0}body{background:#000;overflow:hidden;cursor:none}canvas{display:block}
</style></head><body><canvas id="c"></canvas><script>
const name = location.pathname.split("/").pop();
const cv = document.getElementById("c"), x = cv.getContext("2d");
function draw() {
  const W = cv.width = innerWidth, H = cv.height = innerHeight;
  x.fillStyle = "#000"; x.fillRect(0, 0, W, H);
  const mono = px => px + "px 'Courier New', monospace";

  function grid(color) {
    x.strokeStyle = color; x.lineWidth = 1;
    const step = W / 16;
    for (let i = 0; i <= 16; i++) { x.beginPath(); x.moveTo(i*step, 0); x.lineTo(i*step, H); x.stroke(); }
    for (let j = 0; j <= Math.ceil(H/step); j++) { x.beginPath(); x.moveTo(0, j*step); x.lineTo(W, j*step); x.stroke(); }
  }
  function circles() {
    x.strokeStyle = "#E8ECEF"; x.lineWidth = 2;
    const r = H * 0.12;
    // corner circles tangent to both screen edges
    [[r, r], [W-r, r], [r, H-r], [W-r, H-r]].forEach(([cx, cy]) => {
      x.beginPath(); x.arc(cx, cy, r-1, 0, 7); x.stroke();
      x.fillStyle = "#fff"; x.beginPath(); x.arc(cx, cy, r*0.3, 0, 7); x.fill();
    });
    // center circle touches top and bottom edges
    x.beginPath(); x.arc(W/2, H/2, H/2 - 1, 0, 7); x.stroke();
  }
  function label(txt, y) {
    x.font = "bold " + mono(H*0.022);
    x.textAlign = "center"; x.textBaseline = "middle";
    const tw = x.measureText(txt).width, bw = tw + H*0.06, bh = H*0.055;
    x.fillStyle = "#000"; x.fillRect(W/2 - bw/2, y - bh/2, bw, bh);
    x.strokeStyle = "#E8ECEF"; x.lineWidth = 2; x.strokeRect(W/2 - bw/2, y - bh/2, bw, bh);
    x.fillStyle = "#E8ECEF"; x.fillText(txt, W/2, y);
    x.textBaseline = "alphabetic";
  }
  function aspect() {
    const r = W / H;
    const known = [[16/9, "16:9"], [16/10, "16:10"], [4/3, "4:3"], [21/9, "21:9"], [1, "1:1"], [9/16, "9:16"]];
    for (const [v, n] of known) if (Math.abs(r - v) < 0.02) return n;
    return r.toFixed(2) + ":1";
  }

  if (name === "bars") {                          // SMPTE-style 75% bars
    const cols = ["#c0c0c0","#c0c000","#00c0c0","#00c000","#c000c0","#c00000","#0000c0"];
    const bw = W / 7, h1 = H * 0.67;
    cols.forEach((c, i) => { x.fillStyle = c; x.fillRect(i*bw, 0, bw+1, h1); });
    const rev = ["#0000c0","#131313","#c000c0","#131313","#00c0c0","#131313","#c0c0c0"];
    rev.forEach((c, i) => { x.fillStyle = c; x.fillRect(i*bw, h1, bw+1, H*0.08); });
    const y2 = h1 + H*0.08;
    const plu = [["#00214c", W*0.25], ["#fff", W*0.125], ["#32006a", W*0.125], ["#131313", W*0.5]];
    let px0 = 0;
    plu.forEach(([c, w]) => { x.fillStyle = c; x.fillRect(px0, y2, w+1, H - y2); px0 += w; });
    const pw = W*0.5/6, py = px0 - W*0.5;
    [["#090909",1],["#131313",3],["#1d1d1d",5]].forEach(([c, k]) => {
      x.fillStyle = c; x.fillRect(py + pw*k, y2, pw, H - y2);
    });
  }
  else if (name === "grid") {                     // geometry / overscan
    grid("#E8ECEF"); circles();
    x.strokeStyle = "#2FD97B"; x.lineWidth = 3;
    x.strokeRect(1, 1, W-2, H-2);                 // outermost pixel frame
    x.beginPath(); x.moveTo(W/2, H*0.42); x.lineTo(W/2, H*0.58); x.stroke();
    x.beginPath(); x.moveTo(W*0.46, H/2); x.lineTo(W*0.54, H/2); x.stroke();
    label(W + " x " + H, H*0.9);
  }
  else if (name === "ramp") {                     // levels / banding
    const g = x.createLinearGradient(0, 0, W, 0);
    g.addColorStop(0, "#000"); g.addColorStop(1, "#fff");
    x.fillStyle = g; x.fillRect(0, 0, W, H*0.45);
    for (let i = 0; i < 12; i++) {                // 0-100% steps
      const v = Math.round(255 * (i / 11));
      x.fillStyle = "rgb(" + v + "," + v + "," + v + ")";
      x.fillRect(i * W/12, H*0.5, W/12+1, H*0.25);
      x.fillStyle = v > 128 ? "#000" : "#fff"; x.font = mono(H*0.02); x.textAlign = "center";
      x.fillText(Math.round(i/11*100) + "%", i*W/12 + W/24, H*0.63);
    }
    for (let i = 0; i < 12; i++) {                // near-black 1-12%
      const v = Math.round(255 * ((i+1) / 100));
      x.fillStyle = "rgb(" + v + "," + v + "," + v + ")";
      x.fillRect(i * W/12, H*0.8, W/12+1, H*0.2);
      x.fillStyle = "#666"; x.font = mono(H*0.018);
      x.fillText((i+1) + "%", i*W/12 + W/24, H*0.91);
    }
  }
  else {                                          // "card" — the full plate
    grid("#3a3a3a"); circles();
    x.strokeStyle = "#fff"; x.lineWidth = 4;
    x.strokeRect(2, 2, W-4, H-4);                 // outer frame — edge check
    const bx = W*0.125, bw2 = W*0.75;
    const hues = ["#f00","#f80","#ff0","#8f0","#0f0","#0f8","#0ff","#08f","#00f","#80f","#f0f","#f08"];
    hues.forEach((c, i) => { x.fillStyle = c; x.fillRect(bx + i*bw2/12, H*0.2, bw2/12 - 4, H*0.11); });
    ["#f00", "#0f0", "#00f"].forEach((c, k) => {  // RGB ramps
      const g = x.createLinearGradient(bx, 0, bx + bw2, 0);
      g.addColorStop(0, "#000"); g.addColorStop(0.75, c); g.addColorStop(1, "#fff");
      x.fillStyle = g; x.fillRect(bx, H*(0.34 + k*0.075), bw2, H*0.07);
    });
    for (let i = 0; i < 12; i++) {                // gray steps
      const v = Math.round(255 * (i / 11));
      x.fillStyle = "rgb(" + v + "," + v + "," + v + ")";
      x.fillRect(bx + i*bw2/12, H*0.6, bw2/12 - 4, H*0.1);
      x.fillStyle = v > 128 ? "#000" : "#fff"; x.font = mono(H*0.016); x.textAlign = "center";
      x.fillText(Math.round(i/11*100) + "%", bx + i*bw2/12 + bw2/24, H*0.66);
    }
    label("DOWNSTAGE  ·  " + W + " x " + H + "  ·  " + aspect(), H*0.83);
  }
}
draw(); addEventListener("resize", draw);
</script></body></html>"""


@app.route("/pattern/<name>")
def pattern_page(name):
    if name not in ("card", "bars", "grid", "ramp"):
        name = "card"
    return _PATTERN_PAGE, 200, {"Content-Type": "text/html"}


@app.route("/identify-page/<label>")
def identify_page(label):
    label = re.sub(r"[^A-Za-z0-9 ]", "", label)[:12]
    return (
        '<!DOCTYPE html><html><head><style>'
        '*{margin:0;padding:0}'
        'body{background:#12A95C;color:#fff;display:flex;flex-direction:column;'
        'align-items:center;justify-content:center;height:100vh;'
        'font-family:sans-serif;text-align:center}'
        '.n{font-size:30vh;font-weight:800;line-height:1}'
        '.l{font-size:5vh;letter-spacing:0.3em;text-transform:uppercase;opacity:.85}'
        '</style></head><body>'
        f'<div class="n">{label}</div><div class="l">This Screen</div>'
        '</body></html>'
    ), 200, {"Content-Type": "text/html"}


@app.route("/welcome")
def welcome_page():
    config = load_config()
    host   = socket.gethostname()
    ip     = get_local_ip()
    addr   = f"{host}.local:8080"
    ip_line = f"http://{ip}:8080" if ip != "unknown" else "connect network or join the setup hotspot"
    return (
        '<!DOCTYPE html><html><head><meta charset="utf-8">'
        '<meta http-equiv="refresh" content="10"><style>'
        '*{margin:0;padding:0}'
        'body{background:#0B0D10;display:flex;flex-direction:column;align-items:center;'
        'justify-content:center;height:100vh;font-family:sans-serif;text-align:center;'
        'gap:2.6vh;cursor:none}'
        'svg{width:16vh;height:16vh}'
        '.brand{font-size:3.2vh;color:#e8ecef;letter-spacing:0.4em;font-weight:600}'
        '.brand span{color:#2fd97b}'
        '.addr{font-family:monospace;font-size:3.4vh;color:#2fd97b}'
        'p{font-size:1.9vh;color:#565e66;letter-spacing:0.06em}'
        '</style></head><body>'
        '<svg viewBox="0 0 96 96"><rect x="6" y="10" width="84" height="66" rx="10" '
        'fill="none" stroke="#e8ecef" stroke-width="7"/>'
        '<rect x="20" y="54" width="40" height="9" rx="4.5" fill="#2fd97b"/>'
        '<rect x="20" y="83" width="56" height="7" rx="3.5" fill="#2fd97b"/></svg>'
        '<div class="brand">DOWNSTAGE <span>VIEW</span></div>'
        f'<div class="addr">{addr}</div>'
        f'<p>{ip_line} &middot; set up from any browser on the same network</p>'
        f'<p>{config.get("serial", "")}</p>'
        '</body></html>'
    ), 200, {"Content-Type": "text/html"}


@app.route("/holding")
def holding_page():
    return (
        '<!DOCTYPE html><html><head><meta charset="utf-8"><style>'
        '*{margin:0;padding:0}'
        'body{background:#000;display:flex;flex-direction:column;align-items:center;'
        'justify-content:center;height:100vh;font-family:sans-serif;text-align:center;'
        'gap:3vh;cursor:none}'
        'svg{width:15vh;height:15vh;opacity:0.5}'
        '.brand{font-size:2.2vh;color:#3d444c;letter-spacing:0.45em;font-weight:600}'
        'h1{font-size:3vh;color:#565e66;font-weight:500;letter-spacing:0.05em}'
        'p{font-size:1.9vh;color:#2a2f35;letter-spacing:0.08em}'
        '</style></head><body>'
        '<svg viewBox="0 0 96 96"><rect x="6" y="10" width="84" height="66" rx="10" '
        'fill="none" stroke="#e8ecef" stroke-width="7"/>'
        '<rect x="20" y="54" width="40" height="9" rx="4.5" fill="#2fd97b"/>'
        '<rect x="20" y="83" width="56" height="7" rx="3.5" fill="#2fd97b"/></svg>'
        '<div class="brand">DOWNSTAGE</div>'
        '<h1>OnTime server is off</h1>'
        '<p>This display reconnects automatically</p>'
        '</body></html>'
    ), 200, {"Content-Type": "text/html"}


def _mark_profiles_clean():
    """Chromium shows 'Restore pages?' if the profile says it crashed —
    which it will after any hard kill. Rewrite the exit state before launch."""
    import glob
    for pref in glob.glob("/tmp/kiosk-lite/Default/Preferences"):
        try:
            s = Path(pref).read_text()
            s = s.replace('"exited_cleanly":false', '"exited_cleanly":true')
            s = s.replace('"exit_type":"Crashed"', '"exit_type":"Normal"')
            Path(pref).write_text(s)
        except Exception:
            pass


def _kill_orphan_windows():
    """Kill kiosk Chromium left over from a previous Flask instance — the old
    window keeps the profile lock and swallows new launches."""
    try:
        r = subprocess.run(["pkill", "-f", "user-data-dir=/tmp/kiosk-lite"],
                       timeout=5, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if r.returncode == 0:      # only pause if something was actually killed
            time.sleep(1)
    except Exception:
        pass
    _mark_profiles_clean()


# ── Hotspot ───────────────────────────────────────────────────────────────────
HOTSPOT_CON = "downstage-hotspot"


def _real_network_ip():
    """First non-hotspot, non-loopback IPv4 — None when the hotspot is the
    only network. Used by the front panel to decide which page matters."""
    try:
        out = subprocess.check_output(["ip", "-4", "-o", "addr", "show"],
                                      text=True, timeout=5)
        for line in out.splitlines():
            parts = line.split()
            iface, addr = parts[1], parts[3].split("/")[0]
            if iface == "lo" or addr.startswith("10.42."):
                continue
            return addr
    except Exception:
        pass
    return None


def hotspot_is_active():
    try:
        out = subprocess.check_output(
            ["nmcli", "-t", "-f", "NAME", "connection", "show", "--active"],
            text=True, timeout=5,
        )
        return HOTSPOT_CON in [l.strip() for l in out.splitlines()]
    except Exception:
        return False


def start_hotspot():
    config = load_config()
    ssid   = config.get("hotspot_ssid") or "Downstage-V000"
    pw     = config.get("hotspot_pass") or "downstage"
    subprocess.run(["sudo", "nmcli", "connection", "delete", HOTSPOT_CON],
                   timeout=10, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    r = subprocess.run(
        ["sudo", "nmcli", "device", "wifi", "hotspot",
         "ifname", "wlan0", "con-name", HOTSPOT_CON,
         "band", "bg", "ssid", ssid, "password", pw],
        capture_output=True, text=True, timeout=30,
    )
    if r.returncode != 0:
        return False, (r.stderr or r.stdout).strip()
    subprocess.run(["sudo", "nmcli", "connection", "modify", HOTSPOT_CON,
                    "connection.autoconnect", "no"],
                   timeout=10, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print(f"[hotspot] broadcasting {ssid}")
    return True, ssid


def stop_hotspot():
    # NB: no `nmcli device connect wlan0` here — it would reactivate the
    # hotspot profile itself. NM rejoins known WiFi on its own.
    r = subprocess.run(["sudo", "nmcli", "connection", "down", HOTSPOT_CON],
                       capture_output=True, text=True, timeout=15)
    if r.returncode != 0:
        msg = (r.stderr or r.stdout).strip()
        print(f"[hotspot] stop failed: {msg}")
        return False, msg
    print("[hotspot] stopped")
    return True, "stopped"




# ── portal-stranded rescue ────────────────────────────────────────────────────
# A unit that auto-joined a remembered hotel SSID can end up wifi-only behind
# a captive portal: no internet, and (with client isolation) no reachable UI.
# If that state holds and nobody is using the UI, drop the portal network and
# raise the hotspot — control beats a dead network. The SSID is blocked from
# auto-rejoin until ethernet returns or a human picks a network.
_last_request_ts = 0.0
_portal_blocked_ssids = set()


@app.before_request
def _touch_request_ts():
    global _last_request_ts
    _last_request_ts = time.time()


def _stranded_watch():
    strikes = 0
    while True:
        time.sleep(60)
        try:
            config = load_config()
            if not config.get("hotspot_auto", True) or hotspot_is_active():
                strikes = 0
                continue
            if any(i["kind"] == "Ethernet" for i in get_all_interfaces()):
                if _portal_blocked_ssids:
                    print("[stranded] ethernet back — portal SSID block cleared")
                _portal_blocked_ssids.clear()
                strikes = 0
                continue
            stranded = (_portal.get("detected") and _portal.get("internet") is False
                        and time.time() - _last_request_ts > 180)
            if not stranded:
                strikes = 0
                continue
            strikes += 1
            if strikes < 2:
                continue
            strikes = 0
            ssid = _active_ssid() or ""
            print(f"[stranded] portal-only, UI unreached -- dropping '{ssid}', hotspot up")
            if ssid:
                _portal_blocked_ssids.add(ssid)
                subprocess.run(["sudo", "nmcli", "connection", "down", ssid],
                               capture_output=True, timeout=15)
                time.sleep(3)
            ok, msg = start_hotspot()
            print(f"[stranded] hotspot: ok={ok} ({msg})")
        except Exception as e:
            print(f"[stranded] watch error: {e}")


threading.Thread(target=_stranded_watch, daemon=True).start()


def _saved_wifi_profiles():
    """Names of saved infrastructure WiFi profiles (excludes the hotspot)."""
    try:
        out = subprocess.check_output(
            ["nmcli", "-t", "-f", "NAME,TYPE", "connection", "show"],
            text=True, timeout=10)
        return [l.rsplit(":", 1)[0] for l in out.splitlines()
                if l.endswith(":802-11-wireless")
                and l.rsplit(":", 1)[0] != HOTSPOT_CON]
    except Exception:
        return []


def _hotspot_has_clients():
    try:
        out = subprocess.check_output(["iw", "dev", "wlan0", "station", "dump"],
                                      text=True, timeout=5)
        return "Station" in out
    except Exception:
        return False


def _try_saved_wifi():
    """Ask NM to bring up each saved WiFi profile; True on first success."""
    for name in _saved_wifi_profiles():
        if name in _portal_blocked_ssids:
            continue
        try:
            r = subprocess.run(["sudo", "nmcli", "connection", "up", name],
                               capture_output=True, text=True, timeout=75)
            if r.returncode == 0:
                print(f"[hotspot] rejoined WiFi: {name}")
                return True
        except Exception:
            pass
    return False


_hunt_lock = threading.Lock()


def _network_supervisor():
    """Boot handles the first hunt; this catches connectivity LOST at
    runtime (cable pulled mid-show) — two 20s strikes with no real network
    and no hotspot re-runs the same searching -> scan -> hotspot flow."""
    strikes = 0
    while True:
        time.sleep(20)
        try:
            ip = get_local_ip()
            have = ip != "unknown" and not ip.startswith("169.254.")
            if have or hotspot_is_active() or not load_config().get("hotspot_auto", True):
                strikes = 0
                continue
            strikes += 1
            if strikes < 2:
                continue
            strikes = 0
            print("[supervisor] network lost at runtime -- hunting")
            _hotspot_fallback(grace=2)
        except Exception as e:
            print(f"[supervisor] {e}")


threading.Thread(target=_network_supervisor, daemon=True).start()


def _hotspot_fallback(grace=8):
    """Provisioning aid: no network after boot -> start the hotspot so the
    setup UI is reachable. When saved WiFi credentials exist, keep nudging
    NetworkManager at them first (the radio can miss its first association
    after a reboot) and only give up after several tries. Once the hotspot
    is up it owns the radio, so NM can never rejoin WiFi on its own -- while
    nobody is connected to the hotspot, quietly retry the saved WiFi every
    10 minutes and retire the hotspot if it succeeds."""
    if not _hunt_lock.acquire(blocking=False):
        return          # a hunt is already running
    try:
        _hotspot_fallback_inner(grace)
    finally:
        _hunt_lock.release()


def _hotspot_fallback_inner(grace):
    def _real_ip():
        ip = get_local_ip()
        return ip != "unknown" and not ip.startswith("169.254.")

    time.sleep(grace)    # short ethernet-DHCP grace
    config = load_config()
    if not config.get("hotspot_auto", True) or hotspot_is_active():
        return
    if _real_ip():
        return

    # genuinely no network — flag the e-ink and decide fast (scan-first,
    # ported from the One: only retry saved WiFi that's actually in range)
    epaper._searching = True
    epaper.force_refresh()
    ok = False
    try:
        for _ in range(6):
            time.sleep(2)
            if _real_ip():
                return
        saved = _saved_wifi_profiles()
        if saved:
            try:
                _, visible = _scan_wifi()
                in_range = ({n["ssid"].lower() for n in visible}
                            & {x.lower() for x in saved})
            except Exception:
                in_range = set()
            if in_range:
                for attempt in range(2):
                    print(f"[hotspot] saved WiFi in range -- joining ({attempt + 1}/2)")
                    if _try_saved_wifi():
                        return
                    time.sleep(10)
                    if _real_ip():
                        return
            else:
                print("[hotspot] no saved network in range -- hotspot now")
        print("[hotspot] no network found -- starting fallback hotspot")
        ok, msg = start_hotspot()
        print(f"[hotspot] fallback start: ok={ok} ({msg})")
    finally:
        epaper._searching = False
        epaper.force_refresh()
    while ok and hotspot_is_active():
        time.sleep(600)
        if not hotspot_is_active():
            return
        if not _saved_wifi_profiles() or _hotspot_has_clients():
            continue
        print("[hotspot] idle -- retrying saved WiFi")
        stop_hotspot()
        time.sleep(8)
        if _try_saved_wifi():
            return
        ok, msg = start_hotspot()
        print(f"[hotspot] WiFi still absent -- hotspot back up: ok={ok}")


@app.route("/hotspot/status")
def hotspot_status():
    config = load_config()
    return jsonify({
        "active": hotspot_is_active(),
        "ssid":   config.get("hotspot_ssid", ""),
        "pass":   config.get("hotspot_pass", ""),
        "auto":   config.get("hotspot_auto", True),
    })


@app.route("/hotspot/start", methods=["POST"])
def hotspot_start_route():
    ok, msg = start_hotspot()
    return jsonify({"ok": ok, "message": msg, "active": hotspot_is_active()})


@app.route("/hotspot/stop", methods=["POST"])
def hotspot_stop_route():
    ok, msg = stop_hotspot()
    return jsonify({"ok": ok, "message": msg, "active": hotspot_is_active()})


_FACTORY_RESET_SCRIPT = """#!/bin/bash
sleep 2
# wipe all WiFi profiles (unit reverts to hotspot-on-boot provisioning state)
nmcli -t -f NAME,TYPE connection show | grep ':802-11-wireless$' | cut -d: -f1 | \\
  while read -r c; do sudo nmcli connection delete "$c" || true; done
# wipe user data + logs
rm -rf /home/pi/.config/ontime-electron
rm -rf {app}/.backup
rm -f  {app}/ontime.log {app}/kiosk.log {app}/.update-result
# factory config — keep unit identity + update repo only
python3 - << 'PY'
import json
cfg = json.load(open("{app}/config.json"))
keep = {{k: cfg[k] for k in ("serial", "hotspot_ssid", "hotspot_pass", "os_update_repo") if k in cfg}}
json.dump(keep, open("{app}/config.json", "w"))
PY
sudo reboot
"""


@app.route("/system/factory-reset", methods=["POST"])
def system_factory_reset():
    """Wipe user data back to out-of-box state. Keeps unit identity (serial,
    hostname, hotspot credentials). WiFi credentials are erased, so the unit
    comes back up on ethernet or its fallback hotspot."""
    if (request.get_json(silent=True) or {}).get("confirm") != "RESET":
        return jsonify({"ok": False, "message": "Confirmation missing"})
    try:
        script = Path("/tmp/ds-factory-reset.sh")
        script.write_text(_FACTORY_RESET_SCRIPT.format(app=_OS_APPDIR))
        script.chmod(0o755)
        subprocess.Popen(["setsid", "bash", str(script)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return jsonify({"ok": True, "message": "Factory reset started — the unit will reboot"})
    except Exception as e:
        return jsonify({"ok": False, "message": str(e)})


# ── Tamper-evidence / audit log ───────────────────────────────────────────────
# Disclosed hardware-integrity + access log (documented in the user guide).
# Watches the unit's own hardware composition and access/power events — not
# operator usage.

AUDIT_LOG      = BASE_DIR / "audit.log"
AUDIT_BASELINE = BASE_DIR / ".hw-baseline.json"


def _hw_snapshot():
    def sh(c):
        try:
            return subprocess.check_output(c, shell=True, text=True,
                                           stderr=subprocess.DEVNULL, timeout=8).strip()
        except Exception:
            return ""
    return {
        "storage": sorted(l for l in sh("lsblk -dn -o NAME,SERIAL,MODEL 2>/dev/null").splitlines() if l.strip()),
        "macs":    sorted(sh("cat /sys/class/net/*/address 2>/dev/null").split()),
        "usb":     sorted(l.split("ID ", 1)[-1].split()[0] for l in sh("lsusb").splitlines() if " ID " in l),
        "board":   sh("cat /proc/cpuinfo | grep -i serial | awk '{print $3}'"),
    }


def _audit(event, detail=""):
    try:
        ts = subprocess.check_output(["date", "-Is"], text=True, timeout=3).strip()
        with open(AUDIT_LOG, "a") as f:
            f.write(f"{ts}\t{event}\t{detail}\n")
    except Exception:
        pass


def _audit_boot_and_hw():
    _audit("BOOT", f"os={OS_VERSION} uptime_reset")
    try:
        now = _hw_snapshot()
        if AUDIT_BASELINE.exists():
            old = json.loads(AUDIT_BASELINE.read_text())
            for cat in ("storage", "macs", "usb", "board"):
                a = set(old.get(cat, []) if isinstance(old.get(cat), list) else [old.get(cat)])
                b = set(now.get(cat, []) if isinstance(now.get(cat), list) else [now.get(cat)])
                for gone in a - b:
                    _audit("HW_REMOVED", f"{cat}: {gone}")
                for added in b - a:
                    _audit("HW_ADDED", f"{cat}: {added}")
        else:
            _audit("HW_BASELINE", "first boot — baseline recorded")
        AUDIT_BASELINE.write_text(json.dumps(now))
    except Exception as e:
        _audit("HW_ERROR", str(e))


def _audit_access_watch():
    seen = set()
    while True:
        try:
            out = subprocess.check_output(
                "journalctl -q --since '-6min' 2>/dev/null | "
                "grep -iE 'sshd.*(Accepted|Failed|session opened)|login\\[' | tail -40",
                shell=True, text=True, timeout=10)
            for line in out.splitlines():
                key = line[-120:]
                if key and key not in seen:
                    seen.add(key)
                    kind = "SSH_LOGIN" if "Accepted" in line else \
                           "SSH_FAILED" if "Failed" in line else "LOGIN"
                    who = line.split("for ", 1)[-1].split(" from ")[0] if "for " in line else ""
                    frm = line.split(" from ", 1)[-1].split()[0] if " from " in line else ""
                    _audit(kind, f"{who} {frm}".strip())
            if len(seen) > 500:
                seen = set(list(seen)[-200:])
        except Exception:
            pass
        time.sleep(300)


@app.route("/diagnostics")
def diagnostics():
    """Support bundle: versions, config (secrets stripped), network, system
    state, logs. 'Email me the diagnostics file' beats guided SSH surgery."""
    import io, zipfile
    def sh(cmd):
        try:
            return subprocess.check_output(cmd, shell=True, text=True,
                                           stderr=subprocess.STDOUT, timeout=10)
        except Exception as e:
            return f"error: {e}"
    cfg = load_config()
    cfg.pop("hotspot_pass", None)
    # external viewer URLs may carry private tokens (stagetimer signatures,
    # api keys) — keep the destination, drop the query string
    for k in list(cfg):
        if k.endswith("external_url") and isinstance(cfg[k], str) and "?" in cfg[k]:
            base, _, q = cfg[k].partition("?")
            cfg[k] = base + "?[redacted: " + str(len(q)) + " chars]"
    serial = cfg.get("serial", "unknown")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("info.txt",
                   f"product: {OS_PRODUCT}\nos_version: {OS_VERSION}\n"
                   f"serial: {serial}\nhostname: {socket.gethostname()}\n"
                   f"generated: {sh('date -Is')}")
        z.writestr("config.json", json.dumps(cfg, indent=2))
        z.writestr("network.txt", sh("ip addr") + "\n=== connections ===\n" +
                   sh("nmcli connection show") + "\n=== wifi ===\n" +
                   sh("nmcli -t -f active,ssid,signal dev wifi 2>/dev/null | head -20"))
        z.writestr("system.txt", sh("uptime") + sh("free -m") + sh("df -h /") +
                   sh("sudo vcgencmd measure_temp 2>/dev/null") +
                   sh("sudo vcgencmd measure_volts 2>/dev/null") +
                   "throttling (0x0 = never undervolted/throttled; bit0=UV now, "
                   "bit16=UV since boot): " + sh("sudo vcgencmd get_throttled 2>/dev/null") +
                   sh("cat /proc/device-tree/model 2>/dev/null; echo"))
        z.writestr("storage.txt",
                   sh("lsblk -o NAME,SIZE,TYPE,MOUNTPOINT") + "\n=== boot device errors ===\n" +
                   sh("dmesg 2>/dev/null | grep -iE 'mmc.*error|nvme.*(err|timeout)' | tail -5 || echo none"))
        z.writestr("rtc.txt",
                   "battery_uV: " + sh("cat /sys/class/rtc/rtc0/battery_voltage 2>/dev/null") +
                   "charging_uV: " + sh("cat /sys/class/rtc/rtc0/charging_voltage 2>/dev/null"))
        z.writestr("app.log", sh(f"tail -n 400 {_OS_APPDIR}/kiosk.log 2>/dev/null"))
        z.writestr("devices.txt",
                   "=== USB devices ===\n" + sh("lsusb") +
                   "\n=== services ===\n" +
                   "ontime-kiosk: " + sh("systemctl --user is-active ontime-kiosk 2>/dev/null") +
                   "companion: " + sh("systemctl is-active companion 2>/dev/null") +
                   "ontime-server: " + sh("pgrep -f squashfs-root/ontime >/dev/null && echo running || echo stopped") +
                   "\n=== recent service restarts ===\n" +
                   sh("journalctl --user -u ontime-kiosk -q --since '-2h' 2>/dev/null | "
                      "grep -iE 'started|stopped|failed|main process' | tail -10 || echo none"))
        z.writestr("audit.log",
                   "# Hardware-integrity and access log (disclosed; see user guide).\n"
                   "# Records hardware changes and logins, not operator usage.\n\n" +
                   sh(f"tail -n 500 {AUDIT_LOG} 2>/dev/null || echo '(no events logged yet)'"))
    buf.seek(0)
    return send_file(buf, as_attachment=True, mimetype="application/zip",
                     download_name=f"downstage-diag-{serial}.zip")


# ── Downstage OS self-update ──────────────────────────────────────────────────
# Same machinery as the One, adapted for xinitrc supervision: the swap script
# is detached with setsid (killing Flask can't kill it), swaps files, kills
# Flask so the xinitrc loop restarts it, health-checks, rolls back on failure.

_OS_VARIANT = "view"
_OS_APPDIR  = str(Path(__file__).parent)

_SWAP_SCRIPT = """#!/bin/bash
SRC="{src}"
APP="{app}"
BK="$APP/.backup"
LOG="$APP/.update-result"
sleep 2
rm -rf "$BK"; mkdir -p "$BK"
cp    "$APP/app.py"    "$BK/" 2>/dev/null
cp -r "$APP/templates" "$BK/" 2>/dev/null
cp -r "$APP/static"    "$BK/" 2>/dev/null
cp    "$SRC/app.py"    "$APP/app.py"
[ -d "$SRC/templates" ] && cp -r "$SRC/templates/." "$APP/templates/"
[ -d "$SRC/static" ]    && cp -r "$SRC/static/."    "$APP/static/"
{restart}
for i in $(seq 1 12); do
  sleep 5
  curl -s -m 3 http://127.0.0.1:8080/status > /dev/null && {{ echo "ok {tag} $(date -Is)" > "$LOG"; exit 0; }}
done
cp    "$BK/app.py"    "$APP/app.py"
cp -r "$BK/templates/." "$APP/templates/" 2>/dev/null
cp -r "$BK/static/."    "$APP/static/"    2>/dev/null
{restart}
echo "rolled-back {tag} $(date -Is)" > "$LOG"
"""

_OS_RESTART_CMD = "pkill -f 'python3 -u /home/pi/ontime-kiosk-lite/app.py'"


# Strip dismissal: stored on the unit so it holds across every browser/device.
# Keyed to version numbers — a newer release un-dismisses itself.
_UPD_DISMISS_FILE = BASE_DIR / ".upd-dismissed"

def _upd_dismissed():
    try:
        return json.loads(_UPD_DISMISS_FILE.read_text())
    except Exception:
        return {}


@app.route("/updates/dismiss", methods=["POST"])
def updates_dismiss():
    snap = _upd_dismissed()
    if _os_update.get("update_available") and _os_update.get("latest"):
        snap["os"] = _os_update["latest"]
    try:
        _UPD_DISMISS_FILE.write_text(json.dumps(snap))
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    return jsonify({"ok": True, "dismissed": snap})


def _os_update_result():
    try:
        return (Path(_OS_APPDIR) / ".update-result").read_text().strip()
    except Exception:
        return None


def _vt(v):
    try:
        return tuple(int(x) for x in str(v).lstrip("v").split(".")[:3])
    except Exception:
        return (0, 0, 0)


@app.route("/os/update", methods=["POST"])
def os_update():
    import tarfile, py_compile
    data  = request.get_json(silent=True) or {}
    force = bool(data.get("force"))
    repo  = load_config().get("os_update_repo", "")
    if not repo:
        return jsonify({"ok": False, "message": "No update repo configured"})
    try:
        r   = requests.get(f"https://api.github.com/repos/{repo}/releases/latest", timeout=10)
        tag = r.json().get("tag_name", "")
        ver = tag.lstrip("v")
        if not ver:
            return jsonify({"ok": False, "message": "No published release found"})
        if not force and _vt(ver) <= _vt(OS_VERSION):
            return jsonify({"ok": False, "message": f"Already on v{OS_VERSION}"})

        work = Path("/tmp/ds-os-update")
        subprocess.run(["rm", "-rf", str(work)])
        work.mkdir(parents=True)
        tarball = work / "src.tar.gz"
        with requests.get(f"https://github.com/{repo}/archive/refs/tags/{tag}.tar.gz",
                          stream=True, timeout=120) as resp:
            resp.raise_for_status()
            with open(tarball, "wb") as f:
                for chunk in resp.iter_content(65536):
                    f.write(chunk)
        with tarfile.open(tarball) as tf:
            tf.extractall(work)
        roots = [p for p in work.iterdir() if p.is_dir()]
        src_dir = next((p / _OS_VARIANT for p in roots if (p / _OS_VARIANT / "app.py").exists()), None)
        if not src_dir:
            return jsonify({"ok": False, "message": f"Release has no {_OS_VARIANT}/app.py"})

        py_compile.compile(str(src_dir / "app.py"), doraise=True)
        if not (src_dir / "templates" / "index.html").exists():
            return jsonify({"ok": False, "message": "Release is missing templates/index.html"})

        # Guard against stale GitHub archive caches: the code inside the
        # tarball must actually be the version the tag claims (this bit us —
        # a v1.1.0 archive once served v1.0.0 code).
        m = re.search(r'OS_VERSION = "([^"]+)"', (src_dir / "app.py").read_text())
        staged_ver = m.group(1) if m else None
        if staged_ver != ver:
            return jsonify({"ok": False,
                            "message": f"Release archive is stale: tag says v{ver} but code inside is v{staged_ver}. Try again later."})

        script = work / "swap.sh"
        script.write_text(_SWAP_SCRIPT.format(
            src=src_dir, app=_OS_APPDIR, tag=tag, restart=_OS_RESTART_CMD))
        script.chmod(0o755)
        subprocess.Popen(["setsid", "bash", str(script)],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
        return jsonify({"ok": True, "from": OS_VERSION,
                        "message": f"Updating to {tag} — service will restart"})
    except py_compile.PyCompileError as e:
        return jsonify({"ok": False, "message": f"Release failed validation: {e}"})
    except Exception as e:
        return jsonify({"ok": False, "message": str(e)})


def launch_window():
    config = load_config()
    _show(_source_url(config.get("source", "/timer")))


def close_window():
    global _win
    with _wlock:
        _kill(_win)
        _win = None
        _kill_orphan_windows()


# ── Timezone ──────────────────────────────────────────────────────────────────

def _apply_timezone(tz):
    zoneinfo = Path(f"/usr/share/zoneinfo/{tz}")
    if not zoneinfo.exists():
        raise ValueError(f"Unknown timezone: {tz}")
    subprocess.run(
        ["sudo", "tee", "/etc/timezone"],
        input=tz + "\n", text=True, check=True, timeout=5,
        stdout=subprocess.DEVNULL,
    )
    subprocess.run(
        ["sudo", "ln", "-sf", f"/usr/share/zoneinfo/{tz}", "/etc/localtime"],
        check=True, timeout=5,
    )
    # our own process cached the old zone at startup — re-read it, or any
    # strftime (e-ink clock included) keeps rendering the previous timezone
    time.tzset()


# ── e-Paper display ───────────────────────────────────────────────────────────

# ── e-ink touch (GT1151 on the Touch e-Paper HAT) ────────────────────────────
_touch = {"raw": None, "mapped": None, "at": 0.0, "mode": "idle"}
_TOUCH_MAP = "portrait-180"   # first guess — calibrated from field taps


def _touch_map(rx, ry):
    if _TOUCH_MAP == "portrait-180":
        return (249 - min(ry, 249), min(rx, 121))
    if _TOUCH_MAP == "portrait":
        return (min(ry, 249), 121 - min(rx, 121))
    if _TOUCH_MAP == "landscape-180":
        return (249 - min(rx, 249), 121 - min(ry, 121))
    return (min(rx, 249), min(ry, 121))


def _touch_loop():
    try:
        from smbus2 import SMBus, i2c_msg
    except ImportError:
        print("[touch] smbus2 missing — touch disabled")
        return
    ADDR = 0x14

    def rd(bus, reg, n):
        w = i2c_msg.write(ADDR, [reg >> 8, reg & 0xFF])
        r = i2c_msg.read(ADDR, n)
        bus.i2c_rdwr(w, r)
        return list(r)

    def wr(bus, reg, vals):
        bus.i2c_rdwr(i2c_msg.write(ADDR, [reg >> 8, reg & 0xFF] + vals))

    last_tap = 0.0
    print("[touch] GT1151 poller up")
    while True:
        time.sleep(0.08)
        try:
            n = 0
            rx = ry = 0
            with SMBus(1) as bus:
                st = rd(bus, 0x814E, 1)[0]
                if not (st & 0x80):
                    continue
                n = st & 0x0F
                if n:
                    d = rd(bus, 0x8150, 4)
                    rx = d[0] | (d[1] << 8)
                    ry = d[2] | (d[3] << 8)
                wr(bus, 0x814E, [0])
            if not n:
                continue
            now = time.time()
            if now - last_tap < 0.35:
                continue
            last_tap = now
            px, py = _touch_map(rx, ry)
            _touch.update(raw=(rx, ry), mapped=(px, py), at=now)
            print(f"[touch] raw=({rx},{ry}) mapped=({px},{py}) mode={_touch['mode']}")
            _touch_dispatch(px, py)
        except Exception:
            time.sleep(1)


def _touch_dispatch(px, py):
    now = time.time()
    if _touch["mode"] == "confirm":
        if now > getattr(epaper, "_confirm_until", 0):
            _touch["mode"] = "idle"
            return
        if py > 45:
            _touch["mode"] = "idle"
            epaper._confirm_until = 0
            if px < 87:                    # RESTART
                print("[touch] restart confirmed by touch")
                epaper.restart_screen()
                subprocess.Popen(["sudo", "reboot"])
            elif px < 171:                 # SHUT DOWN
                print("[touch] shutdown confirmed by touch")
                epaper.shutdown_screen()
                subprocess.Popen(["sudo", "poweroff"])
            else:                          # CANCEL
                epaper.force_refresh()
        return
    zx, zy = getattr(epaper, "_power_zone", (218, 92))
    if px > zx - 6 and py > zy - 6:
        _touch["mode"] = "confirm"
        epaper._confirm_until = now + 8
        epaper.force_refresh()


threading.Thread(target=_touch_loop, daemon=True).start()


# ── WiFi health ───────────────────────────────────────────────────────────────
# Signal straight from the kernel (/proc/net/wireless — no tools needed) plus
# a watcher that logs link drops. The UI announces only when WiFi is actually
# carrying the unit (no ethernet) and it's weak or churning.

_WIFI = {"drops": [], "up": None}

def _wifi_signal():
    try:
        for ln in open("/proc/net/wireless").read().splitlines():
            if ln.strip().startswith("wlan0:"):
                f = ln.split()
                dbm = float(f[3].rstrip("."))
                if dbm >= 0 or dbm < -110:      # 0 / -256 = not associated
                    return {"up": False}
                return {"up": True, "dbm": int(dbm),
                        "quality": int(float(f[2].rstrip(".")))}
    except Exception:
        pass
    return {"up": False}


def _wifi_watch():
    while True:
        up = _wifi_signal()["up"]
        if _WIFI["up"] and not up:
            _WIFI["drops"].append(time.time())
            print("[wifi] link dropped")
        _WIFI["up"] = up
        cutoff = time.time() - 600
        _WIFI["drops"] = [t for t in _WIFI["drops"] if t > cutoff]
        time.sleep(5)

threading.Thread(target=_wifi_watch, daemon=True).start()


def _wifi_health():
    sig = _wifi_signal()
    drops = len(_WIFI["drops"])
    concern = None
    if sig["up"]:
        eth_up = any(i["kind"] == "Ethernet" for i in get_all_interfaces())
        if not eth_up:
            if drops >= 2:
                concern = f"WiFi dropped {drops}x in the last 10 minutes"
            elif sig["dbm"] <= -70:
                concern = f"Weak WiFi signal ({sig['dbm']} dBm)"
    return {**sig, "drops_10m": drops, "concern": concern}


# Burn-in arms this flag on PASS; the factory's final shutdown then paints
# the branded ship screen, which e-ink holds with no power — so the unit
# sits in the box wearing the brand. The customer's first boot lands here,
# clears the flag, and goes straight to the normal info screen.
_SHIP_FLAG = BASE_DIR / ".ship"
_SHIP_ARMED = _SHIP_FLAG.exists()
if _SHIP_ARMED:
    try:
        _SHIP_FLAG.unlink()
    except Exception:
        pass


class EPaperDisplay:
    """Single status page on the 250x122 e-ink panel. No touch, no paging —
    the display adapts: hotspot credentials when the hotspot is up, otherwise
    network + OnTime status. This panel is the 'IP on the front of the box'."""

    W = 250   # landscape width
    H = 122   # landscape height
    INTERVAL             = 10   # seconds between data refreshes
    FULL_REFRESH_EVERY   = 5    # full refresh every N updates (prevents ghosting)

    SOURCE_LABELS = {
        "config": "Config UI", "off": "Off", "external": "External URL",
        "cleantimer": "Custom Timer", "/timer": "Stage Timer",
        "/countdown": "Countdown", "/backstage": "Backstage",
        "/studio": "Studio Clock", "/timeline": "Timeline",
        "/info": "Public Info", "/op": "Operator", "/cuesheet": "Cue Sheet",
        "/editor": "Editor", "/timercontrol": "Timer Control",
        "/messagecontrol": "Msg Control", "/rundown": "Rundown",
    }

    def __init__(self):
        self._epd          = None
        self._stop         = threading.Event()
        self._update_count = 0
        self._lock         = threading.Lock()
        self._last_frame   = None   # skip e-ink writes when nothing changed
        self._font_sm      = None
        self._font_md      = None
        self._font_lg      = None

    def start(self):
        if not _EPAPER_LIB:
            print("[epaper] waveshare_epd not installed — skipping")
            return
        try:
            self._epd = _epd_mod.EPD()
            self._epd.init()
            self._epd.Clear()
            self._load_fonts()
            print("[epaper] 250x122 display initialized")
        except Exception as e:
            print(f"[epaper] init failed: {e}")
            self._epd = None
            return
        threading.Thread(target=self._loop, daemon=True).start()

    def _load_fonts(self):
        candidates = [
            '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
            '/usr/share/fonts/truetype/freefont/FreeSans.ttf',
        ]
        bold_candidates = [
            '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf',
            '/usr/share/fonts/truetype/freefont/FreeSansBold.ttf',
        ]
        try:
            fp = next((p for p in candidates      if Path(p).exists()), None)
            bp = next((p for p in bold_candidates if Path(p).exists()), fp)
            self._font_sm = ImageFont.truetype(fp, 11)
            self._font_md = ImageFont.truetype(fp, 14)
            self._font_lg = ImageFont.truetype(bp, 17)
        except Exception:
            f = ImageFont.load_default()
            self._font_sm = self._font_md = self._font_lg = f

    def _loop(self):
        while not self._stop.is_set():
            self._render()
            self._stop.wait(2 if getattr(self, "_searching", False) else self.INTERVAL)

    def _new_image(self):
        return Image.new('1', (self.W, self.H), 255)

    def _flush(self, image, partial=False):
        image = image.rotate(180)   # panel is mounted upside-down in the enclosure
        with self._lock:
            # e-ink refreshes flash the panel — skip if nothing changed
            frame = image.tobytes()
            if frame == self._last_frame:
                return
            self._last_frame = frame

            # partial mode (searching spinner): panel stays AWAKE for the
            # burst — displayPartial updates without the flash. First partial
            # sets the base image; the next normal flush ends the burst with
            # a ghost-clearing full refresh and returns to the sleep cycle.
            if partial and hasattr(self._epd, "displayPartial"):
                try:
                    buf = self._epd.getbuffer(image)
                    if not getattr(self, "_part_awake", False):
                        self._epd.init()
                        self._epd.displayPartBaseImage(buf)
                        self._part_awake = True
                    else:
                        self._epd.displayPartial(buf)
                    return
                except Exception as e:
                    print(f"[epaper] partial: {e}")
                    self._part_awake = False
            if getattr(self, "_part_awake", False):
                self._part_awake = False
                # end of a partial burst: land on the full-refresh branch to
                # clear accumulated ghosting
                self._update_count = self.FULL_REFRESH_EVERY - 1

            self._update_count += 1
            if self._update_count % self.FULL_REFRESH_EVERY == 0:
                self._epd.init()
                self._epd.display(self._epd.getbuffer(image))
            else:
                try:
                    self._epd.init_fast()
                    self._epd.display_fast(self._epd.getbuffer(image))
                except AttributeError:
                    self._epd.init()
                    self._epd.display(self._epd.getbuffer(image))
            # deep-sleep between refreshes. sleep() ends in module_exit(),
            # which closes the SPI handle that init() opened — without it
            # every refresh leaks one /dev/spidev fd and the process hits
            # EMFILE (Errno 24) after ~1.5 days, killing e-ink AND HTTP.
            try:
                self._epd.sleep()
            except Exception as e:
                print(f"[epaper] sleep: {e}")

    def _render(self):
        if getattr(self, "_final", False):
            return   # shutdown screen is on the panel — nothing may overwrite it
        if not self._epd:
            return
        try:
            img  = self._new_image()
            draw = ImageDraw.Draw(img)
            draw._image = img   # pages paste QR codes onto the frame
            if getattr(self, "_confirm_until", 0) > time.time():
                self._page_confirm(draw)
                self._flush(img)
                return
            if getattr(self, "_searching", False) and _SHIP_FLAG.exists():  # armed this session
                self._page_ship(draw)
                self._flush(img)
                return
            if getattr(self, "_searching", False):
                self._header(draw, "DOWNSTAGE VIEW")
                self._spin = (getattr(self, "_spin", 0) + 1) % 8
                dots = "." * (1 + self._spin % 3)
                draw.text((5, 34), f"Searching for a network{dots}", font=self._font_md, fill=0)
                draw.text((5, 58), "Setup hotspot starts if", font=self._font_sm, fill=0)
                draw.text((5, 73), "none is found (about 30s).", font=self._font_sm, fill=0)
                # segmented wheel, one segment advancing per refresh — visible
                # motion so the wait never reads as a hang
                cx, cy, r = 215, 72, 24
                for i in range(8):
                    a0 = i * 45 - 90 + self._spin * 45
                    lead = (8 + i) % 8   # 0 = leading segment
                    width = 7 if i < 3 else 2   # 3 bold leading segments
                    draw.arc([cx - r, cy - r, cx + r, cy + r],
                             a0 + 4, a0 + 41, fill=0, width=width)
                self._flush(img, partial=True)   # no flash while the wheel turns
                return
            hs = hotspot_is_active()
            if _SHIP_FLAG.exists() and not (hs and not _real_network_ip()):
                self._page_ship(draw)
            elif hs and not _real_network_ip():
                self._page_hotspot(draw)
            else:
                self._page_status(draw, hotspot=hs)
            self._flush(img)
        except Exception as e:
            print(f"[epaper] render error: {e}")

    def _header(self, draw, title, right=""):
        draw.rectangle([0, 0, self.W, 20], fill=0)
        draw.text((5, 3), title, font=self._font_md, fill=255)
        if right:
            w = draw.textlength(right, font=self._font_sm)
            draw.text((self.W - w - 5, 5), right, font=self._font_sm, fill=255)

    def _row(self, draw, y, label, value, font=None):
        draw.text((5, y), label, font=self._font_sm, fill=0)
        draw.text((58, y - 2), value, font=font or self._font_md, fill=0)

    def _qr(self, data, scale=2):
        """1-bit QR image for the panel, or None if unavailable."""
        try:
            import qrcode
            q = qrcode.QRCode(border=2, box_size=scale,
                              error_correction=qrcode.constants.ERROR_CORRECT_L)
            q.add_data(data)
            q.make(fit=True)
            return q.make_image().convert("1")
        except Exception as e:
            print(f"[epaper] qr: {e}")
            return None

    def _paste_qr(self, img, draw, data, caption, scale=2):
        """QR pinned to the right edge under the header; returns the x where
        the text column must stop (or panel width if no QR)."""
        qr = self._qr(data, scale)
        if qr is None or qr.width > 100:
            return self.W
        x = self.W - 4 - qr.width
        cap_h = 14 if caption else 0
        block = qr.height + cap_h
        y = 24   # top-anchored: the power box sits beneath
        self._qr_geom = (x, y, qr.width, block)
        img.paste(qr, (x, y))
        if caption and y + qr.height + cap_h <= self.H:
            w = draw.textlength(caption, font=self._font_sm)
            draw.text((x + (qr.width - w) / 2, y + qr.height), caption, font=self._font_sm, fill=0)
        return x - 14   # generous gap to the text column

    # ── Normal page: network + OnTime status ─────────────────────────────────
    def _page_status(self, draw, hotspot=False):
        config    = load_config()
        ip        = config.get("ip", "")
        local_ip  = get_local_ip()
        connected = check_ontime(ip, timeout=2) if ip else False
        ssid      = _active_ssid()
        source    = config.get("source", "/timer")
        # header corner: the unit's short identity — "V001" from
        # downstage-v001 — so a rack of Views reads at a glance
        host = socket.gethostname()
        unit = host.rsplit("-", 1)[-1].upper() if "-" in host else host.upper()

        self._header(draw, "DOWNSTAGE VIEW", "HOTSPOT ON" if hotspot else unit)

        # image handle for the QR paste (draw only wraps it)
        img = draw._image if hasattr(draw, "_image") else None
        col = self.W
        if img is not None and local_ip != "unknown":
            col = self._paste_qr(img, draw, f"http://{local_ip}:8080", "SETUP")

        netv = _active_link()
        # flag the portal only when the portaled wifi IS the path — a
        # dual-homed unit with working ethernet stays calm (no-false-alarm)
        if (_portal.get("detected") and _portal.get("iface", "").startswith("wlan")
                and primary_iface().startswith("wlan")):
            netv = "PORTAL " + (_active_ssid() or "")
        self._row(draw, 26, "Net", netv[:14 if col < self.W else 24])
        setup_v = f"{local_ip}:8080" if local_ip != "unknown" else "No network"
        draw.text((5, 44), "Setup", font=self._font_sm, fill=0)
        draw.text((50, 42), setup_v, font=self._font_sm, fill=0)
        draw.line([(5, 62), (col - 5, 62)], fill=0)
        self._row(draw, 68, "OnTime", (ip if ip else "Not configured")[:15])
        status = "CONNECTED" if connected else "OFFLINE"
        marker = chr(9679) if connected else chr(9675)   # filled / hollow dot
        draw.text((5, 86), f"{marker} {status}", font=self._font_md, fill=0)
        if _portal.get("detected") and _portal.get("internet") is False:
            eth_up = any(i["kind"] == "Ethernet" for i in get_all_interfaces())
            msg = "PORTAL! No internet" if eth_up else "PORTAL! Hotspot soon..."
            draw.text((5, 104), msg, font=self._font_md, fill=0)
        elif _blackout_active:
            draw.text((5, 104), "BLACKOUT - resume in UI", font=self._font_md, fill=0)
        else:
            view_lbl = self.SOURCE_LABELS.get(source, source)
            self._row(draw, 106, "Shows", view_lbl[:18])
        self._draw_power_glyph(draw)

    def _draw_power_glyph(self, draw):
        # boxed power button, centered under the QR block above it
        qx, qy, qw, qh = getattr(self, "_qr_geom", (188, 24, 58, 72))
        cx = qx + qw // 2
        top = min(qy + qh + 3, 100)
        box_w = 40
        draw.rectangle([cx - box_w // 2, top, cx + box_w // 2, self.H - 3],
                       outline=0, width=1)
        # classic power symbol: gap-top arc with a line through the gap
        cy = (top + self.H - 3) // 2 + 1
        r = min(8, (self.H - 3 - top) // 2 - 2)
        draw.arc([cx - r, cy - r, cx + r, cy + r], -60, 240, fill=0, width=2)
        draw.line([(cx, cy - r - 2), (cx, cy - 1)], fill=0, width=3)
        self._power_zone = (cx - box_w // 2, top)   # tap zone follows the box

    def _page_confirm(self, draw):
        self._header(draw, "POWER")
        draw.text((5, 26), "Restart or shut down this View?", font=self._font_sm, fill=0)
        boxes = [("RESTART", 6, 84, False), ("SHUT DOWN", 90, 168, True), ("CANCEL", 174, 244, False)]
        for label, x1, x2, solid in boxes:
            if solid:
                draw.rectangle([x1, 48, x2, 114], fill=0)
            else:
                draw.rectangle([x1, 48, x2, 114], outline=0, width=2)
            w = draw.textlength(label, font=self._font_sm)
            draw.text((x1 + (x2 - x1 - w) / 2, 74), label, font=self._font_sm,
                      fill=255 if solid else 0)

    def restart_screen(self):
        if not self._epd:
            return
        try:
            self._final = True
            self._stop.set()
            img = self._new_image()
            draw = ImageDraw.Draw(img)
            self._header(draw, "RESTARTING")
            draw.text((5, 40), "Back in about a minute...", font=self._font_md, fill=0)
            self._flush(img)
        except Exception as e:
            print(f"[epaper] restart screen: {e}")

    # ── Hotspot page: everything a tech needs to get in ──────────────────────
    def _page_ship(self, draw):
        # Branded first-boot screen. Layout mirrors docs/make-device-renders.py
        # ship_frame() — keep the two in sync.
        img = draw._image
        try:
            fdir = BASE_DIR / "static" / "fonts"
            wordmark = ImageFont.truetype(str(fdir / "Rajdhani-700.ttf"), 26)
            sub      = ImageFont.truetype(str(fdir / "ShareTechMono-400.ttf"), 12)
        except Exception:
            wordmark, sub = self._font_lg, self._font_sm
        mark_s = 44
        img.paste(self._ship_mark(mark_s), ((self.W - mark_s) // 2, 4))
        text = "DOWNSTAGE VIEW"
        tw = draw.textlength(text, font=wordmark)
        draw.text(((self.W - tw) / 2, 50), text, font=wordmark, fill=0)
        draw.line([78, 92, 172, 92], fill=0, width=1)
        serial = load_config().get("serial", "")
        if serial:
            sw = draw.textlength(serial, font=sub)
            draw.text(((self.W - sw) / 2, 100), serial, font=sub, fill=0)

    def _ship_mark(self, size):
        # View brand mark, supersampled then thresholded so 1-bit stays crisp
        if getattr(self, "_ship_mark_cache", None) is not None:
            return self._ship_mark_cache
        big = Image.new("L", (size * 4, size * 4), 255)
        d = ImageDraw.Draw(big)
        u = size * 4 / 96.0
        w = max(2, round(7 * u))
        d.rounded_rectangle([6 * u, 10 * u, 90 * u, 76 * u], radius=10 * u,
                            outline=0, width=w)
        d.rounded_rectangle([20 * u, 54 * u, 50 * u, 63 * u], radius=4.5 * u, fill=0)
        d.rounded_rectangle([20 * u, 83 * u, 76 * u, 90 * u], radius=3.5 * u, fill=0)
        cx, cy, r = 64 * u, 58 * u, 4 * u
        d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=0)
        aw = max(2, round(5.5 * u))
        r2 = 12 * u
        d.arc([cx - r2, cy - r2, cx + r2, cy + r2], -90, 0, fill=0, width=aw)
        r3 = 25 * u
        d.arc([cx - r3, cy - r3, cx + r3, cy + r3], -90, -35, fill=0,
              width=max(1, aw - 2))
        self._ship_mark_cache = big.resize((size, size), Image.LANCZOS).point(
            lambda v: 0 if v < 150 else 255, "1")
        return self._ship_mark_cache

    def _page_hotspot(self, draw):
        config = load_config()
        ssid   = config.get("hotspot_ssid", "")
        pw     = config.get("hotspot_pass", "")

        self._header(draw, "HOTSPOT MODE")
        img = draw._image if hasattr(draw, "_image") else None
        col = self.W
        if img is not None and ssid and pw:
            wifi_qr = f"WIFI:T:WPA;S:{ssid};P:{pw};;"
            col = self._paste_qr(img, draw, wifi_qr, "JOIN", scale=2)
        self._row(draw, 28, "WiFi",  ssid, font=self._font_md)
        self._row(draw, 52, "Pass",  pw,   font=self._font_md)
        draw.line([(5, 78), (col - 5, 78)], fill=0)
        draw.text((5, 84), "Scan to join, then open:", font=self._font_sm, fill=0)
        draw.text((5, 99), "10.42.0.1:8080", font=self._font_md, fill=0)

    def shutdown_screen(self):
        """Drawn just before poweroff — e-ink holds the image with no power,
        so a shut-down unit in a case reads as deliberately, safely off."""
        # halt the periodic loop and refuse any late force_refresh FIRST —
        # otherwise a status repaint races us and the powered-off panel
        # ends up holding the normal page instead of "safe to unplug"
        self._final = True
        self._stop.set()
        if not self._epd:
            return
        try:
            img  = self._new_image()
            draw = ImageDraw.Draw(img)
            if _SHIP_FLAG.exists():
                # armed by burn-in: the boxed unit wears the brand, not "off"
                draw._image = img
                self._page_ship(draw)
                self._last_frame = None
                self._flush(img)
                return
            # the Downstage mark, drawn in PIL: screen outline + stage bars
            mx, my = 18, 26
            draw.rounded_rectangle([mx, my, mx + 62, my + 48], radius=8, outline=0, width=5)
            draw.rounded_rectangle([mx + 12, my + 30, mx + 40, my + 37], radius=3, fill=0)
            draw.rounded_rectangle([mx + 12, my + 56, mx + 52, my + 62], radius=3, fill=0)
            tx = 100
            draw.text((tx, 24), "DOWNSTAGE VIEW", font=self._font_md, fill=0)
            draw.text((tx, 46), socket.gethostname(), font=self._font_md, fill=0)
            draw.text((tx, 68), "Powered off", font=self._font_md, fill=0)
            draw.text((tx, 86), "Safe to unplug", font=self._font_sm, fill=0)
            self._last_frame = None   # never skip this write
            self._flush(img)
        except Exception as e:
            print(f"[epaper] shutdown screen: {e}")

    def force_refresh(self):
        if getattr(self, "_final", False):
            return
        threading.Thread(target=self._render, daemon=True).start()


epaper = EPaperDisplay()


# ── Flask routes ──────────────────────────────────────────────────────────────

@app.route("/")
def index():
    config = load_config()
    return render_template(
        "index.html",
        config=config,
        local_ip=get_local_ip(),
        hostname=socket.gethostname(),
        ip_history=config.get("ip_history", []),
    )


@app.route("/status")
def status():
    _probe_async_if_stale()
    config    = load_config()
    ip        = config.get("ip", "")
    connected = check_ontime(ip, timeout=2) if ip else False
    return jsonify({
        "ip":        ip,
        "source":    config.get("source", "/timer"),
        "output":    _output_chain(),
        "wifi":      _wifi_health(),
        "primary_ip": get_local_ip(),
        "name": config.get("unit_name", ""),
        "now_showing": _now_showing(),
        "health": _health_summary(),
        "primary_kind": next((i["kind"] for i in get_all_interfaces()
                              if i["ip"] == get_local_ip()), ""),
        "external_url": config.get("external_url", ""),
        "connected": connected,
        "local_ip":  get_local_ip(),
        "blackout": _blackout_active,
        "touch": {"raw": _touch["raw"], "mapped": _touch["mapped"], "at": _touch["at"]},
        "net_iface": primary_iface(),
        "interfaces": get_all_interfaces(),
        "portal": {"detected": _portal["detected"], "iface": _portal["iface"],
                   "internet": _portal["internet"], "checked": _portal["checked"]},
        "clock": {"epoch": time.time(),
                  "offset_min": int((datetime.datetime.now().astimezone().utcoffset() or datetime.timedelta()).total_seconds() // 60)},
        "os_version": OS_VERSION,
        "cpu_temp": _cpu_temp(),
        "hotspot_active": hotspot_is_active(),
        "serial": config.get("serial", ""),
        "os_latest": _os_update["latest"],
        "os_update_available": _os_update["update_available"],
        "os_dismissed": _upd_dismissed().get("os"),
        "os_checked": _os_update.get("checked", False),
        "os_update_result": _os_update_result(),
        "watchdog":  config.get("watchdog", True),
        "watchdog_override": _watchdog_override,
    })


@app.route("/check", methods=["POST"])
def check():
    ip = ((request.get_json() or {}).get("ip") or "").strip()
    return jsonify({"ok": check_ontime(ip) if ip else False})


@app.route("/save", methods=["POST"])
def save():
    data         = request.get_json()
    ip           = (data.get("ip") or "").strip()
    source       = data.get("source", "/timer")
    external_url = _clean_external_url(data.get("external_url", ""))

    if source == "external" and not external_url:
        return jsonify({"ok": False, "error": "Enter a URL for the external viewer"})

    # Only OnTime sources need a reachable OnTime server
    if _is_ontime_source(source):
        if not ip:
            return jsonify({"ok": False, "error": "IP address required"})
        if not check_ontime(ip):
            return jsonify({"ok": False, "error": f"Cannot reach OnTime at {ip}:4001"})

    history = _update_ip_history(ip) if ip else load_config().get("ip_history", [])
    global _watchdog_override
    _watchdog_override = False
    save_config({"ip": ip, "source": source, "external_url": external_url,
                 "watchdog": bool(data.get("watchdog", True)),
                 "cleantimer_freeze": bool(data.get("cleantimer_freeze", True)),
                 "cleantimer_hideprogress": bool(data.get("cleantimer_hideprogress", True)),
                 "cleantimer_hideclock": bool(data.get("cleantimer_hideclock", True)),
                 "cleantimer_hidecards": bool(data.get("cleantimer_hidecards", True)),
                 "cleantimer_keycolour": _hex6(data.get("cleantimer_keycolour"), "000000"),
                 "cleantimer_timercolour": _hex6(data.get("cleantimer_timercolour"), "ffffff"),
                 "ip_history": history})
    epaper.force_refresh()
    threading.Thread(target=launch_window, daemon=True).start()
    return jsonify({"ok": True})


@app.route("/reset", methods=["POST"])
def reset():
    save_config({"ip": "", "source": "/timer"})
    close_window()
    epaper.force_refresh()
    return jsonify({"ok": True})


@app.route("/refresh", methods=["POST"])
def refresh_display():
    """Relaunch the display window — the one-tap heal for a stuck/black page."""
    threading.Thread(target=launch_window, daemon=True).start()
    return jsonify({"ok": True})


@app.route("/desktop", methods=["POST"])
def desktop():
    close_window()
    return jsonify({"ok": True})


# ── Network (static IP) ───────────────────────────────────────────────────────
# Same revert-on-timeout safety as the One: a wrong static setting can't
# strand the unit — it reverts to the previous config after 90s unless the
# UI reconnects and confirms.

import ipaddress

_net_revert = {"event": None, "snapshot": None, "conn": None}


def _default_conn():
    try:
        route = subprocess.check_output(["ip", "route", "show", "default"],
                                        text=True, timeout=5)
        m = re.search(r"dev (\S+)", route)
        iface = m.group(1) if m else "wlan0"
        out = subprocess.check_output(
            ["nmcli", "-t", "-f", "NAME,DEVICE", "connection", "show", "--active"],
            text=True, timeout=5)
        for line in out.splitlines():
            name, _, dev = line.rpartition(":")
            if dev == iface:
                return name, iface
    except Exception:
        pass
    return None, None


def _conn_ipv4(conn):
    try:
        out = subprocess.check_output(
            ["nmcli", "-t", "-f",
             "ipv4.method,ipv4.addresses,ipv4.gateway,ipv4.dns",
             "connection", "show", conn], text=True, timeout=5)
        d = {}
        for line in out.splitlines():
            k, _, v = line.partition(":")
            d[k] = v
        return d
    except Exception:
        return {}


def _apply_ipv4(conn, method, addr=None, gw=None, dns=None):
    cmd = ["sudo", "nmcli", "connection", "modify", conn, "ipv4.method", method]
    if method == "manual":
        cmd += ["ipv4.addresses", addr, "ipv4.gateway", gw or "", "ipv4.dns", dns or ""]
    else:
        cmd += ["ipv4.addresses", "", "ipv4.gateway", "", "ipv4.dns", ""]
    subprocess.run(cmd, timeout=15, check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(["sudo", "nmcli", "connection", "up", conn], timeout=30,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _revert_worker(conn, snapshot, event):
    if event.wait(90):
        return
    print("[network] not confirmed in 90s — reverting")
    try:
        _apply_ipv4(conn, snapshot.get("ipv4.method", "auto"),
                    snapshot.get("ipv4.addresses") or None,
                    snapshot.get("ipv4.gateway") or None,
                    snapshot.get("ipv4.dns") or None)
    except Exception as e:
        print(f"[network] revert failed: {e}")
    _net_revert["event"] = None


def _conn_for_iface(iface):
    """Connection to configure for a chosen interface. The active connection
    wins, except ethernet's link-local fallback profile — a static belongs on
    the REAL wired profile, and targeting the saved profile also covers the
    no-DHCP venue where the port never came up on its own."""
    try:
        out = subprocess.check_output(
            ["nmcli", "-t", "-f", "NAME,DEVICE", "connection", "show", "--active"],
            text=True, timeout=5)
        for line in out.splitlines():
            name, _, dev = line.rpartition(":")
            if dev == iface and name != "wired-fallback-ll":
                return name
        if iface == "eth0":
            out = subprocess.check_output(
                ["nmcli", "-t", "-f", "NAME,TYPE", "connection", "show"],
                text=True, timeout=5)
            for line in out.splitlines():
                name, _, typ = line.rpartition(":")
                if typ in ("802-3-ethernet", "ethernet") and name != "wired-fallback-ll":
                    return name
    except Exception:
        pass
    return None


@app.route("/network/info")
def network_info():
    iface_q = request.args.get("iface")
    if iface_q in ("eth0", "wlan0"):
        conn, iface = _conn_for_iface(iface_q), iface_q
    else:
        conn, iface = _default_conn()
    info = _conn_ipv4(conn) if conn else {}
    return jsonify({
        "conn": conn, "iface": iface,
        "method": info.get("ipv4.method", "auto"),
        "address": info.get("ipv4.addresses", ""),
        "gateway": info.get("ipv4.gateway", ""),
        "dns": info.get("ipv4.dns", ""),
        "current_ip": get_local_ip(),
        "available": conn is not None,
        "reverting": _net_revert["event"] is not None,
    })


@app.route("/network/apply", methods=["POST"])
def network_apply():
    data = request.get_json() or {}
    method = data.get("method", "auto")
    iface_q = data.get("iface")
    if iface_q in ("eth0", "wlan0"):
        conn, iface = _conn_for_iface(iface_q), iface_q
    else:
        conn, iface = _default_conn()
    if not conn:
        return jsonify({"ok": False, "message": "No active connection found"})
    addr = gw = dns = None
    if method == "manual":
        try:
            ip = data["ip"].strip()
            prefix = int(data.get("prefix", 24))
            ipaddress.ip_address(ip)
            if not (1 <= prefix <= 32):
                raise ValueError
            addr = f"{ip}/{prefix}"
            gw = (data.get("gateway") or "").strip()
            dns = (data.get("dns") or "").strip().replace(" ", ",")
            if gw:
                ipaddress.ip_address(gw)
        except Exception:
            return jsonify({"ok": False, "message": "Invalid IP, prefix, or gateway"})
    if _net_revert["event"] is not None:
        return jsonify({"ok": False, "message": "A network change is already pending confirmation"})
    snapshot = _conn_ipv4(conn)
    event = threading.Event()
    _net_revert.update({"event": event, "snapshot": snapshot, "conn": conn})
    def do():
        try:
            _apply_ipv4(conn, method, addr, gw, dns)
        except Exception as e:
            print(f"[network] apply failed: {e}")
        threading.Thread(target=_revert_worker, args=(conn, snapshot, event), daemon=True).start()
    threading.Thread(target=do, daemon=True).start()
    return jsonify({"ok": True, "revert_in": 90,
                    "new_ip": data.get("ip") if method == "manual" else None,
                    "hostname": socket.gethostname()})


@app.route("/network/confirm", methods=["POST"])
def network_confirm():
    ev = _net_revert.get("event")
    if ev is None:
        return jsonify({"ok": True, "message": "Nothing pending"})
    ev.set()
    _net_revert["event"] = None
    return jsonify({"ok": True, "message": "Network settings kept"})


# ── WiFi routes ───────────────────────────────────────────────────────────────

def _scan_wifi():
    out = subprocess.check_output(
        ["nmcli", "-t", "-f", "active,ssid,signal,security", "dev", "wifi"],
        text=True, timeout=8,
    )
    seen     = {}   # ssid -> index in networks (nmcli lists the active SSID
    networks = []   # twice — a set would skip the entry carrying active=yes)
    current  = None
    for line in out.strip().splitlines():
        parts    = line.split(":")
        active   = parts[0] == "yes"
        ssid     = parts[1] if len(parts) > 1 else ""
        signal   = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
        security = parts[3] if len(parts) > 3 else ""
        if not ssid:
            continue
        if active:
            current = ssid
        if ssid in seen:
            if active:
                networks[seen[ssid]]["active"] = True
            continue
        seen[ssid] = len(networks)
        networks.append({"ssid": ssid, "signal": signal, "secured": bool(security), "active": active})
    networks.sort(key=lambda n: -n["signal"])
    return current, networks


@app.route("/wifi/status")
def wifi_status():
    hotspot = hotspot_is_active()
    try:
        current, networks = _scan_wifi()
        if hotspot:
            hs_ssid  = load_config().get("hotspot_ssid", "")
            networks = [n for n in networks if n["ssid"] != hs_ssid]
            for n in networks:
                n["active"] = False
            current = None
        return jsonify({"ok": True, "hotspot": hotspot, "current": current, "networks": networks,
                        "saved": _saved_wifi_profiles()})
    except Exception as e:
        return jsonify({"ok": hotspot, "hotspot": hotspot, "current": None, "networks": [], "error": str(e)})


@app.route("/wifi/scan", methods=["POST"])
def wifi_scan():
    hotspot = hotspot_is_active()
    try:
        # Best-effort rescan — in AP mode the radio often can't actively scan
        # (times out); fall back to the cached list from before the hotspot
        # started rather than failing the whole request.
        try:
            subprocess.run(["sudo", "nmcli", "dev", "wifi", "rescan"], timeout=10,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            time.sleep(2)
        except subprocess.TimeoutExpired:
            print("[wifi] rescan timed out (AP mode) — serving cached list")
        current, networks = _scan_wifi()
        if hotspot:
            hs_ssid  = load_config().get("hotspot_ssid", "")
            networks = [n for n in networks if n["ssid"] != hs_ssid]
            for n in networks:
                n["active"] = False
            current = None
        return jsonify({"ok": True, "hotspot": hotspot, "current": current, "networks": networks,
                        "saved": _saved_wifi_profiles()})
    except Exception as e:
        return jsonify({"ok": False, "hotspot": hotspot, "current": None, "networks": [], "error": str(e)})


@app.route("/wifi/connect", methods=["POST"])
def wifi_connect():
    _portal_blocked_ssids.clear()   # a human picked a network — trust them
    data     = request.get_json() or {}
    ssid     = data.get("ssid", "").strip()
    password = data.get("password", "").strip()
    if not ssid:
        return jsonify({"ok": False, "message": "SSID required"})
    hotspot_was_active = hotspot_is_active()
    try:
        if hotspot_was_active:
            print(f"[wifi] stopping hotspot to join '{ssid}'")
            stop_hotspot()
            time.sleep(3)
        if password:
            # Explicit profile with key-mgmt set — `nmcli dev wifi connect`
            # generates a profile netplan's NM backend rejects
            # ("802-11-wireless-security.key-mgmt: property is missing")
            subprocess.run(["sudo", "nmcli", "connection", "delete", ssid],
                           timeout=10, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            result = subprocess.run(
                ["sudo", "nmcli", "connection", "add", "type", "wifi",
                 "con-name", ssid, "ifname", "wlan0", "ssid", ssid,
                 "802-11-wireless-security.key-mgmt", "wpa-psk",
                 "802-11-wireless-security.psk", password,
                 "connection.autoconnect", "yes"],
                capture_output=True, text=True, timeout=30)
            if result.returncode == 0:
                result = subprocess.run(["sudo", "nmcli", "connection", "up", ssid],
                                        capture_output=True, text=True, timeout=45)
        else:
            result = subprocess.run(["sudo", "nmcli", "dev", "wifi", "connect", ssid],
                                    capture_output=True, text=True, timeout=45)
        ok  = result.returncode == 0
        msg = (result.stdout + result.stderr).strip()
        if ok:
            epaper.force_refresh()
        elif hotspot_was_active:
            print(f"[wifi] join failed — restarting hotspot ({msg})")
            start_hotspot()
            msg += " — hotspot restarted so the device stays reachable"
        return jsonify({"ok": ok, "message": msg,
                        "hotspot_stopped": hotspot_was_active and ok})
    except subprocess.TimeoutExpired:
        if hotspot_was_active:
            start_hotspot()
            return jsonify({"ok": False, "message": "Connection timed out — hotspot restarted"})
        return jsonify({"ok": False, "message": "Connection timed out after 45s"})
    except Exception as e:
        return jsonify({"ok": False, "message": str(e)})


@app.route("/qr.png")
def qr_png():
    try:
        import io as _io
        import qrcode
    except Exception:
        return ("QR support not installed", 404)
    ip = get_local_ip()
    url = f"http://{ip}:8080/" if ip != "unknown" else f"http://{request.host}/"
    img = qrcode.make(url, box_size=5, border=2)
    buf = _io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return send_file(buf, mimetype="image/png")


# ── fleet discovery: find other Downstage units on the LAN ───────────────────
def _avahi_advertise():
    """Publish _downstage._tcp via an avahi service file (the daemon
    auto-loads /etc/avahi/services — no avahi-utils needed)."""
    time.sleep(15)
    try:
        config = load_config()
        xml = ('<?xml version="1.0" standalone="no"?><!DOCTYPE service-group '
               'SYSTEM "avahi-service.dtd">\n<service-group>\n'
               '  <name replace-wildcards="yes">%h</name>\n'
               '  <service><type>_downstage._tcp</type><port>8080</port>\n'
               f'    <txt-record>serial={config.get("serial", "")}</txt-record>\n'
               '  </service>\n</service-group>\n')
        cur = ""
        try:
            cur = open("/etc/avahi/services/downstage.service").read()
        except Exception:
            pass
        if cur != xml:
            subprocess.run(["sudo", "tee", "/etc/avahi/services/downstage.service"],
                           input=xml, text=True, timeout=10,
                           stdout=subprocess.DEVNULL)
            print("[discover] avahi service published")
    except Exception as e:
        print(f"[discover] advertise: {e}")


threading.Thread(target=_avahi_advertise, daemon=True).start()


_FLEET_SRC_LABELS = {
    "off": "Off", "companion": "Companion", "config": "Setup UI",
    "cleantimer": "Custom Timer", "external": "External URL",
    "/timer": "Stage Timer", "/countdown": "Countdown", "/clock": "Studio Clock",
    "/backstage": "Backstage", "/studio": "Studio Clock", "/timeline": "Timeline",
    "/info": "Public Info", "/op": "Operator", "/cuesheet": "Cue Sheet",
    "/editor": "Editor", "/timercontrol": "Timer Control",
    "/messagecontrol": "Message Control", "/rundown": "Rundown",
}

def _fleet_src_label(key):
    if not key:
        return "?"
    if str(key).startswith("pattern"):
        return "Test Pattern"
    return _FLEET_SRC_LABELS.get(key, str(key).lstrip("/")[:12].capitalize())


def _now_showing():
    return _fleet_src_label(load_config().get("source", "/timer"))


def _health_summary():
    probs = []
    try:
        o = _output_chain()
        if not o["render"]["up"]:
            probs.append("Kiosk not responding")
        if not o["hdmi"]["connected"]:
            probs.append("No display")
    except Exception:
        pass
    try:
        t = _cpu_temp()
        if t and float(t) >= 80:
            probs.append(f"CPU hot ({int(float(t))}\u00b0C)")
    except Exception:
        pass
    return {"ok": not probs, "why": " \u00b7 ".join(probs)}


@app.route("/fleet/identify", methods=["POST"])
def fleet_identify():
    """Flash Identify on ANOTHER unit — proxied server-side because the
    browser can't POST cross-origin to a peer."""
    ip = str((request.get_json() or {}).get("ip", ""))
    try:
        ipaddress.ip_address(ip)
    except Exception:
        return jsonify({"ok": False, "error": "bad ip"}), 400
    for path in ("/output/identify", "/displays/identify"):
        try:
            r = requests.post(f"http://{ip}:8080{path}", timeout=4)
            if r.ok:
                try:
                    body_ok = bool(r.json().get("ok", True))
                except Exception:
                    body_ok = True
                if body_ok:
                    return jsonify({"ok": True})
        except Exception:
            continue
    return jsonify({"ok": False})


@app.route("/unit-name", methods=["POST"])
def set_unit_name():
    name = str((request.get_json() or {}).get("name", "")).strip()[:24]
    save_config({"unit_name": name})
    return jsonify({"ok": True, "name": name})


@app.route("/discover", methods=["POST"])
def discover_units():
    """Sweep this unit's /24s for other Downstage units (identified by their
    /status signature — works across firmware generations)."""
    import concurrent.futures
    mine = {i["ip"] for i in get_all_interfaces()}
    ips = set()
    for i in get_all_interfaces():
        if i["ip"].startswith("169.254."):
            continue
        base = i["ip"].rsplit(".", 1)[0]
        ips |= {f"{base}.{n}" for n in range(1, 255)}
    ips -= mine

    def probe(ip):
        try:
            r = requests.get(f"http://{ip}:8080/status", timeout=0.6)
            d = r.json()
            serial = str(d.get("serial", ""))
            if serial.startswith("DS"):
                return {"ip": ip, "serial": serial,
                        "product": "View" if serial.startswith("DSV") else "One",
                        "version": d.get("os_version", ""),
                        "kind": d.get("primary_kind", ""),
                        "name": d.get("name", ""),
                        "showing": d.get("now_showing", ""),
                        "health_ok": (d.get("health") or {}).get("ok", True),
                        "health_why": (d.get("health") or {}).get("why", ""),
                        "upd": bool(d.get("os_update_available")),
                        "primary": d.get("primary_ip", "") in ("", ip)}
        except Exception:
            pass
        return None

    found = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=32) as ex:
        for res in ex.map(probe, sorted(ips)):
            if res:
                found.append(res)
    best = {}
    for u in found:
        key = u["serial"] or u["ip"]
        if key not in best or (u["primary"] and not best[key]["primary"]):
            best[key] = u
    found = [{k: v for k, v in u.items() if k != "primary"}
             for u in best.values()]
    cache = {"units": found, "ts": time.time()}
    try:
        _FLEET_CACHE.write_text(json.dumps(cache))
    except Exception:
        pass
    return jsonify({"ok": True, **cache})


# Last scan is remembered on the unit, so every browser sees the same list
# after a refresh. A scan is always a manual, explicit sweep — no background
# probing of customer networks.
_FLEET_CACHE = BASE_DIR / ".fleet-cache"

@app.route("/discover/last")
def discover_last():
    try:
        return jsonify({"ok": True, **json.loads(_FLEET_CACHE.read_text())})
    except Exception:
        return jsonify({"ok": True, "units": [], "ts": None})


@app.route("/wifi/forget", methods=["POST"])
def wifi_forget():
    ssid = ((request.get_json() or {}).get("ssid") or "").strip()
    if not ssid:
        return jsonify({"ok": False, "message": "SSID required"})
    try:
        subprocess.run(["sudo", "nmcli", "connection", "delete", ssid],
                       capture_output=True, timeout=10)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "message": str(e)})


@app.route("/blackout", methods=["POST"])
def blackout_toggle():
    """Show-safety blackout: instant black, source untouched; press again to
    resume exactly where the display was."""
    global _blackout_active
    body = request.get_json(silent=True) or {}
    on = bool(body.get("on", not _blackout_active))
    _blackout_active = on
    if on:
        _show("http://localhost:8080/blackout-page", force=True)
    else:
        config = load_config()
        _show(_source_url(config.get("source", "/timer") if config.get("ip") else "welcome"),
              force=True)
    print(f"[blackout] {'ON' if on else 'off — resumed'}")
    epaper.force_refresh()
    return jsonify({"ok": True, "blackout": _blackout_active})


@app.route("/wifi/disconnect", methods=["POST"])
def wifi_disconnect():
    """Drop the current WiFi but keep the profile. If that leaves no path to
    the unit at all, raise the hotspot so it stays reachable."""
    name = ((request.get_json() or {}).get("ssid") or "").strip()
    if not name:
        return jsonify({"ok": False, "message": "SSID required"})
    try:
        subprocess.run(["sudo", "nmcli", "connection", "down", name],
                       capture_output=True, timeout=15)
        time.sleep(2)
        eth_up = any(i["kind"] == "Ethernet" for i in get_all_interfaces())
        raised = False
        if not eth_up and not hotspot_is_active():
            ok, _ = start_hotspot()
            raised = bool(ok)
        return jsonify({"ok": True, "hotspot_raised": raised})
    except Exception as e:
        return jsonify({"ok": False, "message": str(e)})


# ── Timezone routes ───────────────────────────────────────────────────────────

@app.route("/system/timezone", methods=["GET"])
def get_timezone():
    try:
        tz = subprocess.check_output(
            ["timedatectl", "show", "--property=Timezone", "--value"],
            text=True, timeout=5,
        ).strip()
    except Exception:
        tz = "Unknown"
    return jsonify({"timezone": tz})


# Full IANA list is ~600 entries of noise for a show device — offer the
# zones a touring/AV crew actually lands in, west to east.
_CURATED_TIMEZONES = [
    "UTC",
    # Americas
    "Pacific/Honolulu", "America/Anchorage", "America/Los_Angeles",
    "America/Vancouver", "America/Phoenix", "America/Denver",
    "America/Edmonton", "America/Chicago", "America/Winnipeg",
    "America/Mexico_City", "America/New_York", "America/Toronto",
    "America/Bogota", "America/Lima", "America/Halifax",
    "America/Puerto_Rico", "America/Santiago", "America/Sao_Paulo",
    "America/Argentina/Buenos_Aires",
    # Europe / Africa
    "Europe/London", "Europe/Dublin", "Europe/Lisbon", "Europe/Madrid",
    "Europe/Paris", "Europe/Amsterdam", "Europe/Berlin", "Europe/Rome",
    "Europe/Stockholm", "Europe/Warsaw", "Europe/Athens", "Europe/Istanbul",
    "Europe/Moscow", "Africa/Cairo", "Africa/Lagos", "Africa/Nairobi",
    "Africa/Johannesburg",
    # Asia / Pacific
    "Asia/Jerusalem", "Asia/Dubai", "Asia/Karachi", "Asia/Kolkata",
    "Asia/Dhaka", "Asia/Bangkok", "Asia/Singapore", "Asia/Hong_Kong",
    "Asia/Shanghai", "Asia/Taipei", "Asia/Manila", "Asia/Tokyo",
    "Asia/Seoul", "Australia/Perth", "Australia/Adelaide",
    "Australia/Brisbane", "Australia/Sydney", "Pacific/Auckland",
    "Pacific/Fiji",
]


@app.route("/system/timezones", methods=["GET"])
def list_timezones():
    zones = [z for z in _CURATED_TIMEZONES
             if Path(f"/usr/share/zoneinfo/{z}").exists()]
    try:
        current = subprocess.check_output(
            ["timedatectl", "show", "--property=Timezone", "--value"],
            text=True, timeout=5).strip()
        if current and current not in zones:
            zones.insert(1, current)   # keep an off-list zone selectable
    except Exception:
        pass
    return jsonify({"timezones": zones})


@app.route("/system/timezone", methods=["POST"])
def set_timezone():
    tz = ((request.get_json() or {}).get("timezone") or "").strip()
    if not tz:
        return jsonify({"ok": False, "message": "No timezone provided"})
    try:
        _apply_timezone(tz)
        epaper.force_refresh()
        return jsonify({"ok": True, "timezone": tz})
    except Exception as e:
        return jsonify({"ok": False, "message": str(e)})


@app.route("/system/set-time", methods=["POST"])
def system_set_time():
    """Set system time from the browser's clock — covers venues with no
    internet (the Pi has no reliable time source there). Also writes the
    hardware RTC when one is present."""
    ms = (request.get_json() or {}).get("epoch_ms")
    if not isinstance(ms, (int, float)) or ms < 1e12:
        return jsonify({"ok": False, "message": "Invalid timestamp"})
    try:
        subprocess.run(["sudo", "date", "-s", f"@{ms/1000:.3f}"],
                       check=True, timeout=5,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        # persist to RTC if fitted; harmless no-op otherwise
        subprocess.run(["sudo", "hwclock", "-w"], timeout=5,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        now = subprocess.check_output(["date", "+%H:%M:%S %Z"], text=True, timeout=5).strip()
        return jsonify({"ok": True, "now": now})
    except Exception as e:
        return jsonify({"ok": False, "message": str(e)})


@app.route("/system/timezone/detect", methods=["POST"])
def detect_timezone():
    try:
        r  = requests.get("http://ip-api.com/json/", timeout=6)
        tz = r.json().get("timezone", "")
        if not tz:
            return jsonify({"ok": False, "message": "Could not detect timezone"})
        _apply_timezone(tz)
        epaper.force_refresh()
        return jsonify({"ok": True, "timezone": tz})
    except Exception as e:
        return jsonify({"ok": False, "message": str(e)})


@app.route("/system/restart", methods=["POST"])
def system_restart():
    close_window()
    def do_restart():
        time.sleep(1)
        subprocess.Popen(["sudo", "reboot"])
    threading.Thread(target=do_restart, daemon=True).start()
    return jsonify({"ok": True})


def _x_env():
    """Env for X clients. The X server's auth cookie lives in the file named
    on the Xorg '-auth' argument; ~/.Xauthority can be stale after an X
    restart (esp. following a hostname change), so read the live one."""
    env = os.environ.copy()
    env.setdefault("DISPLAY", ":0")
    try:
        ps = subprocess.check_output(["pgrep", "-af", "Xorg"], text=True, timeout=5)
        m = re.search(r"-auth (\S+)", ps)
        if m and Path(m.group(1)).exists():
            env["XAUTHORITY"] = m.group(1)
    except Exception:
        pass
    return env


def _view_output():
    """Name of the single connected output (HDMI-1 / HDMI-A-1 etc)."""
    try:
        out = subprocess.check_output(["xrandr"], text=True, env=_x_env(), timeout=5)
        for line in out.splitlines():
            if " connected" in line:
                return line.split()[0]
    except Exception:
        pass
    return None


@app.route("/displays/identify", methods=["POST"])
def displays_identify():
    label = (load_config().get("serial", "") or "VIEW").split("-")[-1] or "VIEW"
    def run():
        _show(f"http://localhost:8080/identify-page/{label}")
        time.sleep(5)
        launch_window()
    threading.Thread(target=run, daemon=True).start()
    return jsonify({"ok": True})


@app.route("/displays/power", methods=["POST"])
def displays_power():
    on   = bool((request.get_json() or {}).get("on", True))
    name = _view_output()
    if not name:
        return jsonify({"ok": False, "message": "No output detected"})
    try:
        subprocess.run(["xrandr", "--output", name, "--auto" if on else "--off"],
                       env=_x_env(), timeout=10, check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if on:
            threading.Thread(target=launch_window, daemon=True).start()
        return jsonify({"ok": True, "on": on})
    except Exception as e:
        return jsonify({"ok": False, "message": str(e)})


@app.route("/displays/power-status")
def displays_power_status():
    name = _view_output()
    on = False
    try:
        out = subprocess.check_output(["xrandr", "--listmonitors"], text=True, env=_x_env(), timeout=5)
        on = bool(name) and name in out
    except Exception:
        pass
    return jsonify({"output": name, "on": on})


@app.route("/logs")
def logs():
    n = min(int(request.args.get("lines", 200)), 1000)
    try:
        out = subprocess.check_output(
            ["tail", "-n", str(n), str(BASE_DIR / "kiosk.log")], text=True, timeout=5)
    except Exception as e:
        out = f"(no log available: {e})"
    return jsonify({"log": out})


@app.route("/system/shutdown", methods=["POST"])
def system_shutdown():
    close_window()
    epaper.shutdown_screen()
    def do_shutdown():
        time.sleep(3)   # let the e-ink finish its refresh before power drops
        subprocess.Popen(["sudo", "poweroff"])
    threading.Thread(target=do_shutdown, daemon=True).start()
    return jsonify({"ok": True})


# ── Boot ──────────────────────────────────────────────────────────────────────

def _enforce_network_priority():
    """Wired always beats WiFi when both are up: give every ethernet profile
    top autoconnect priority and a lower route metric than WiFi's default
    600. WiFi stays connected in the background — routes just prefer copper."""
    try:
        out = subprocess.check_output(
            ["nmcli", "-t", "-f", "NAME,TYPE", "con"], text=True, timeout=10)
        for line in out.strip().splitlines():
            name, _, ctype = line.rpartition(":")
            if ctype == "802-3-ethernet":
                subprocess.run(["sudo", "nmcli", "con", "mod", name,
                                "connection.autoconnect", "yes",
                                "connection.autoconnect-priority", "999",
                                "ipv4.route-metric", "100",
                                "ipv6.route-metric", "100"],
                               timeout=10, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                print(f"[net] wired profile '{name}' pinned above WiFi")
    except Exception as e:
        print(f"[net] priority enforcement failed: {e}")


def _usb_ethernet_retry():
    """The USB ethernet adapter can fail cold-boot enumeration on the Zero 2 W
    (marginal power timing). If no wired interface exists, cycle the USB bus
    binding a few times before giving up — recovers the marginal case."""
    for attempt in range(3):
        time.sleep(10)
        try:
            eth = [i for i in os.listdir("/sys/class/net") if i.startswith(("eth", "enx"))]
            if eth:
                if attempt:
                    print(f"[net] wired interface {eth[0]} recovered after USB rebind")
                return
            print(f"[net] no wired interface — USB rebind attempt {attempt + 1}")
            subprocess.run(["sudo", "sh", "-c",
                            "for d in /sys/bus/usb/drivers/usb/[0-9]-*; do b=$(basename $d); "
                            "echo $b > /sys/bus/usb/drivers/usb/unbind; done; sleep 2; "
                            "for d in /sys/bus/usb/devices/[0-9]-[0-9]; do b=$(basename $d); "
                            "echo $b > /sys/bus/usb/drivers/usb/bind 2>/dev/null; done"],
                           timeout=30, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as e:
            print(f"[net] usb retry error: {e}")
            return
    print("[net] wired interface absent after retries — check the adapter")


def boot():
    threading.Thread(target=_enforce_network_priority, daemon=True).start()
    threading.Thread(target=_usb_ethernet_retry, daemon=True).start()
    try:
        subprocess.Popen(
            ["unclutter", "-idle", "2", "-root"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        pass

    time.sleep(3)
    config = load_config()
    _show(_source_url(config.get("source", "/timer") if config.get("ip") else "welcome"))


if __name__ == "__main__":
    _audit_boot_and_hw()
    threading.Thread(target=_audit_access_watch, daemon=True).start()
    epaper.start()
    threading.Thread(target=boot, daemon=True).start()
    threading.Thread(target=_hotspot_fallback, daemon=True).start()
    threading.Thread(target=_ontime_watchdog, daemon=True).start()
    threading.Thread(target=_check_os_update, daemon=True).start()
    app.run(host="0.0.0.0", port=8080, use_reloader=False, threaded=True)
