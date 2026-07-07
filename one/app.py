import json
import os
import re
import socket
import subprocess
import threading
import time
from pathlib import Path

import requests
from flask import Flask, jsonify, render_template, request, send_file, Response

OS_VERSION = "1.1.1"   # Downstage OS release — bump on tagged releases
OS_PRODUCT = "Downstage One"

app = Flask(__name__)
BASE_DIR    = Path(__file__).parent
CONFIG_FILE = BASE_DIR / "config.json"
ONTIME_DIR  = BASE_DIR / "ontime-server"
ONTIME_BIN  = ONTIME_DIR / "ontime.AppImage"
ONTIME_ROOT = ONTIME_DIR / "squashfs-root"

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

# ── Process handles ───────────────────────────────────────────────────────────
_win   = [None, None]   # [hdmi1_proc, hdmi2_proc]
_wlock = threading.Lock()
_ontime_proc = None
_ontime_lock = threading.Lock()

_blackout_active   = False
_watchdog_override = False
_watchdog_lock     = threading.Lock()

_update_status = {
    "ontime":    {"installed": None, "latest": None, "update_available": False, "checked": False},
    "companion": {"installed": None, "latest": None, "update_available": False, "checked": False},
    "os":        {"installed": None, "latest": None, "update_available": False, "checked": False},
}

# ── OLED (optional) ───────────────────────────────────────────────────────────
try:
    from luma.core.interface.serial import i2c as luma_i2c
    from luma.oled.device import ssd1306
    from luma.core.render import canvas as luma_canvas
    _OLED_LIB = True
except ImportError:
    _OLED_LIB = False


# ── Config ────────────────────────────────────────────────────────────────────

def load_config():
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE) as f:
                data = json.load(f)
        except Exception as e:
            print(f"[config] WARNING: could not parse config.json ({e}), using defaults")
            data = {}
    else:
        data = {}
    data.setdefault("mode", "remote")
    data.setdefault("ip", "")
    # Migrate old single-view format
    old_view = data.pop("view", "/timer")
    data.pop("swap_displays", None)
    data.setdefault("hdmi1_source", "config")
    data.setdefault("hdmi2_source", old_view)
    data.setdefault("ip_history",   [])
    data.setdefault("hdmi1_res",    "1920x1080")
    data.setdefault("hdmi2_res",    "1920x1080")
    data.setdefault("hdmi1_rotate", "normal")
    data.setdefault("hdmi2_rotate", "normal")
    data.setdefault("presets",           [])
    data.setdefault("watchdog",          True)
    data.setdefault("companion_channel", "stable")
    data.setdefault("hdmi1_external_url", "")
    data.setdefault("hdmi2_external_url", "")
    # Per-unit identity from the build log (this unit: DS1-A-2607-0001)
    data.setdefault("hotspot_ssid", "Downstage-0001")
    data.setdefault("hotspot_pass", "cue-grip-28")
    data.setdefault("hotspot_auto", True)
    data.setdefault("os_update_repo", "")   # e.g. "youruser/downstage-os"
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

import fcntl
import struct
from functools import wraps


def _ttl_cache(seconds):
    """Cache a zero-arg function's result for `seconds`. The /status route is
    polled by every open browser tab; without this each poll spawns several
    subprocesses (systemctl, xrandr)."""
    def deco(fn):
        state = {"val": None, "at": 0.0}
        lock  = threading.Lock()
        @wraps(fn)
        def wrapper(*, fresh=False):
            with lock:
                if fresh or time.monotonic() - state["at"] > seconds:
                    state["val"] = fn()
                    state["at"]  = time.monotonic()
                return state["val"]
        return wrapper
    return deco

def _iface_ip(iface: str):
    """Return the IPv4 address of a network interface, or None if not up."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        r = fcntl.ioctl(s.fileno(), 0x8915,  # SIOCGIFADDR
                        struct.pack("256s", iface[:15].encode()))
        s.close()
        return socket.inet_ntoa(r[20:24])
    except Exception:
        return None


def get_network_info():
    """Return {ip, iface} preferring eth0 over wlan0."""
    for iface in ["eth0", "wlan0"]:
        ip = _iface_ip(iface)
        if ip:
            return {"ip": ip, "iface": iface}
    # routing fallback
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return {"ip": ip, "iface": "unknown"}
    except Exception:
        return {"ip": "unknown", "iface": "unknown"}


def get_local_ip():
    return get_network_info()["ip"]


def check_ontime(ip, timeout=3):
    try:
        r = requests.get(f"http://{ip}:4001/api/version", timeout=timeout)
        return r.status_code < 300
    except Exception:
        return False


def _power_state():
    """Pi firmware power flags (vcgencmd get_throttled): bit0 undervoltage
    now, bit16 undervoltage since boot — the 'lightning bolt' warning."""
    try:
        out = subprocess.check_output(["vcgencmd", "get_throttled"],
                                      text=True, timeout=3).strip()
        val = int(out.split("=")[1], 16)
        return {
            "undervolt_now":  bool(val & 0x1),
            "throttled_now":  bool(val & 0x4),
            "undervolt_boot": bool(val & 0x10000),
        }
    except Exception:
        return {"undervolt_now": False, "throttled_now": False, "undervolt_boot": False}


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


# ── Background CPU sampler ────────────────────────────────────────────────────
# psutil.cpu_percent(interval=N) blocks for N seconds, so we sample in a
# background thread and serve the cached value instead of blocking requests.

_cpu_percent_cache  = None
_cpu_percent_lock   = threading.Lock()

def _cpu_sampler():
    global _cpu_percent_cache
    try:
        import psutil as _psutil
    except ImportError:
        return
    while True:
        try:
            pct = _psutil.cpu_percent(interval=1)
            with _cpu_percent_lock:
                _cpu_percent_cache = round(pct, 1)
        except Exception:
            pass
        time.sleep(1)

def _cpu_percent():
    with _cpu_percent_lock:
        return _cpu_percent_cache

def _gpu_clock_mhz():
    """Return V3D (GPU) clock in MHz via vcgencmd, or None if unavailable."""
    try:
        out = subprocess.check_output(
            ["vcgencmd", "measure_clock", "v3d"],
            text=True, timeout=3,
        ).strip()                          # e.g. "frequency(46)=960000000"
        m = re.search(r"=(\d+)", out)
        if m:
            hz = int(m.group(1))
            return hz // 1_000_000        # → MHz
    except Exception:
        pass
    return None


def _uptime():
    try:
        secs = float(Path("/proc/uptime").read_text().split()[0])
        h, rem = divmod(int(secs), 3600)
        m, s   = divmod(rem, 60)
        return f"{h}h {m:02d}m {s:02d}s"
    except Exception:
        return None


def _get_all_connected_outputs():
    """Return all physically connected output names from xrandr (active or not)."""
    try:
        env = os.environ.copy()
        env.setdefault("DISPLAY", ":0")
        out = subprocess.check_output(
            ["xrandr"], text=True, env=env, timeout=5
        )
        names = []
        for line in out.splitlines():
            # "HDMI-1 connected ..." or "HDMI-1 connected primary ..."
            m = re.match(r"^(\S+)\s+connected", line)
            if m:
                names.append(m.group(1))
        return names
    except Exception:
        return []


def _get_output_names():
    """Return active (mode-set) output names sorted left-to-right by x offset."""
    try:
        env = os.environ.copy()
        env.setdefault("DISPLAY", ":0")
        out = subprocess.check_output(
            ["xrandr", "--listmonitors"], text=True, env=env, timeout=5
        )
        outputs = []
        for line in out.splitlines()[1:]:
            m = re.search(r"\d+/\d+x\d+/\d+\+(\d+)\+\d+\s+(\S+)", line)
            if m:
                outputs.append((int(m.group(1)), m.group(2)))
        outputs.sort()
        return [name for _, name in outputs]
    except Exception:
        return []


def _activate_all_connected_outputs():
    """
    Ensure every physically connected output has a mode set.
    On a fresh boot X11 sometimes leaves secondary outputs connected but
    inactive (no mode).  This enables them left-to-right in port order so
    get_displays() sees all of them.
    """
    env = {**os.environ, "DISPLAY": ":0"}
    connected = _get_all_connected_outputs()
    active    = set(_get_output_names())
    inactive  = [o for o in connected if o not in active]
    if not inactive:
        return
    print(f"[xrandr] activating inactive outputs: {inactive}")
    # Anchor: use the first active output, or the first connected if none active
    anchor = list(active)[0] if active else None
    for name in inactive:
        cmd = ["xrandr", "--output", name, "--auto"]
        if anchor:
            cmd += ["--right-of", anchor]
        try:
            subprocess.run(cmd, env=env, timeout=10, check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            anchor = name
            print(f"[xrandr] activated {name}")
        except Exception as e:
            print(f"[xrandr] could not activate {name}: {e}")


def _apply_display_settings():
    """
    Activate any connected-but-inactive outputs, then apply per-output
    resolution and rotation from config via xrandr.
    """
    _activate_all_connected_outputs()
    config  = load_config()
    outputs = _get_output_names()
    env     = {**os.environ, "DISPLAY": ":0"}
    for idx, name in enumerate(outputs):
        n   = idx + 1
        res = config.get(f"hdmi{n}_res",    "1920x1080")
        rot = config.get(f"hdmi{n}_rotate", "normal")
        # re-anchor positions: a mode change alters widths, and stale offsets
        # leave gaps/overlap — content then isn't centered on the glass
        cmd = ["xrandr", "--output", name, "--mode", res, "--rotate", rot]
        if idx == 0:
            cmd += ["--pos", "0x0"]
        else:
            cmd += ["--right-of", outputs[idx - 1]]
        try:
            subprocess.run(cmd, env=env, timeout=10, check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as e:
            print(f"[xrandr] failed for {name}: {e}")


def _version_tuple(v):
    try:
        return tuple(int(x) for x in str(v).lstrip("v").split(".")[:3])
    except Exception:
        return (0, 0, 0)


def _ontime_installed_version_str():
    try:
        pkg = ONTIME_ROOT / "resources" / "app" / "package.json"
        if pkg.exists():
            return json.loads(pkg.read_text()).get("version")
    except Exception:
        pass
    return None


def _companion_installed_version_str():
    try:
        out = subprocess.check_output(
            ["systemctl", "show", "companion", "--property=Description", "--value"],
            text=True, timeout=5,
        ).strip()
        m = re.search(r"v?(\d+\.\d+[\.\d]*)", out)
        if m:
            return m.group(1)
    except Exception:
        pass
    for path in ["/opt/companion/package.json", "/home/companion/companion/package.json"]:
        try:
            data = json.loads(Path(path).read_text())
            v = data.get("version")
            if v:
                return v
        except Exception:
            pass
    return None


def _latest_companion_version(channel: str):
    """Return latest Companion version string for the given channel."""
    if channel == "beta":
        r = requests.get(
            "https://api.github.com/repos/bitfocus/companion/releases",
            timeout=10,
        )
        releases = r.json()
        for rel in releases:
            tag = rel.get("tag_name", "").lstrip("v")
            if tag:
                return tag
        return None
    else:
        r = requests.get(
            "https://api.github.com/repos/bitfocus/companion/releases/latest",
            timeout=10,
        )
        return r.json().get("tag_name", "").lstrip("v") or None


def _check_updates_background():
    global _update_status
    # OnTime
    try:
        installed = _ontime_installed_version_str()
        r = requests.get(
            "https://api.github.com/repos/cpvalente/ontime/releases/latest",
            timeout=10,
        )
        latest = r.json().get("tag_name", "").lstrip("v") or None
        _update_status["ontime"] = {
            "installed": installed,
            "latest":    latest,
            "update_available": bool(
                installed and latest and
                _version_tuple(latest) > _version_tuple(installed)
            ),
            "checked": True,
        }
    except Exception as e:
        _update_status["ontime"]["checked"] = True
        print(f"[updates] ontime check failed: {e}")
    # Companion
    try:
        channel   = load_config().get("companion_channel", "stable")
        installed = _companion_installed_version_str()
        latest    = _latest_companion_version(channel)
        _update_status["companion"] = {
            "installed": installed,
            "latest":    latest,
            "channel":   channel,
            "update_available": bool(
                installed and latest and
                _version_tuple(latest) > _version_tuple(installed)
            ),
            "checked": True,
        }
    except Exception as e:
        _update_status["companion"]["checked"] = True
        print(f"[updates] companion check failed: {e}")
    # Downstage OS itself
    try:
        repo = load_config().get("os_update_repo", "")
        if repo:
            r = requests.get(f"https://api.github.com/repos/{repo}/releases/latest", timeout=10)
            latest = r.json().get("tag_name", "").lstrip("v") or None
        else:
            latest = None
        _update_status["os"] = {
            "installed": OS_VERSION,
            "latest":    latest,
            "update_available": bool(
                latest and _version_tuple(latest) > _version_tuple(OS_VERSION)
            ),
            "checked": True,
        }
    except Exception as e:
        _update_status["os"]["checked"] = True
        _update_status["os"]["installed"] = OS_VERSION
        print(f"[updates] os check failed: {e}")


def _clean_external_url(url):
    """Normalize a user-entered external viewer URL; prepend https:// if bare."""
    url = (url or "").strip()
    if url and not re.match(r"^https?://", url, re.I):
        url = "https://" + url
    return url


def _is_ontime_source(source):
    # NB: "cleantimer" IS an OnTime source (it loads :4001/timer with styling
    # params) — it must not appear in this exclusion list or the watchdog
    # will relaunch a dead page instead of the holding screen.
    if source and source.startswith("pattern-"):
        return False
    return source not in ("config", "companion", "off",
                          "blackout", "holding", "welcome", "external", None, "")


def _ontime_runtime(ip, timeout=2):
    try:
        r = requests.get(f"http://{ip}:4001/data/runtime", timeout=timeout)
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return None


# ── Display detection ─────────────────────────────────────────────────────────

@_ttl_cache(10)
def get_displays():
    """
    Return connected displays sorted left-to-right by x offset.
    Each entry: {"w", "h", "x", "y"}
    Cached for 10s (status polling); pass fresh=True where geometry must be
    current, e.g. right before opening windows.
    """
    try:
        env = os.environ.copy()
        env.setdefault("DISPLAY", ":0")
        out = subprocess.check_output(
            ["xrandr", "--listmonitors"], text=True, env=env, timeout=5
        )
        displays = []
        for line in out.splitlines()[1:]:
            m = re.search(r"(\d+)/\d+x(\d+)/\d+\+(\d+)\+(\d+)", line)
            if m:
                displays.append({
                    "w": int(m.group(1)), "h": int(m.group(2)),
                    "x": int(m.group(3)), "y": int(m.group(4)),
                })
        if displays:
            return sorted(displays, key=lambda d: d["x"])
    except Exception:
        pass
    return [{"w": 1920, "h": 1080, "x": 0, "y": 0}]


# ── Browser launcher ──────────────────────────────────────────────────────────

_COMMON_FLAGS = [
    "--noerrdialogs",
    "--disable-session-crashed-bubble",
    "--hide-crash-restore-bubble",
    # paint House Black from the first frame — otherwise every fresh window
    # flashes white on screen before the page's dark background loads
    "--default-background-color=0b0d10",
    "--disable-infobars",
    "--no-first-run",
    "--disable-restore-session-state",
    "--disable-translate",
    "--disable-features=TranslateUI",
    "--check-for-update-interval=31536000",
    "--password-store=basic",   # skip GNOME keyring prompt
]


def _kill(proc):
    if proc and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def _chromium_env():
    env = os.environ.copy()
    env.setdefault("DISPLAY", ":0")
    return env


def _open_window(source, display, hdmi_index):
    """
    Launch a Chromium window on the given display.
    source: "config" | "off" | "/ontime-view-path"
    hdmi_index: 1 or 2 — used to give each instance its own profile dir.
    Returns the Popen handle, or None if source is "off".
    """
    pos      = f"--window-position={display['x']},{display['y']}"

    size     = f"--window-size={display['w']},{display['h']}"
    profile  = f"--user-data-dir=/tmp/kiosk-hdmi{hdmi_index}"

    if source in ("off", "blackout"):
        # "off" renders true black — leaving no window would show the desktop
        return subprocess.Popen([
            "chromium", *_COMMON_FLAGS,
            profile, pos, size, "--kiosk",
            "http://localhost:8080/blackout-page",
        ], env=_chromium_env())

    if source.startswith("pattern-"):
        return subprocess.Popen([
            "chromium", *_COMMON_FLAGS,
            profile, pos, size, "--kiosk",
            f"http://localhost:8080/pattern/{source.split('-', 1)[1]}",
        ], env=_chromium_env())

    if source == "welcome":
        return subprocess.Popen([
            "chromium", *_COMMON_FLAGS,
            profile, pos, size, "--kiosk",
            "http://localhost:8080/welcome",
        ], env=_chromium_env())

    if source == "holding":
        return subprocess.Popen([
            "chromium", *_COMMON_FLAGS,
            profile, pos, size, "--kiosk",
            "http://localhost:8080/holding",
        ], env=_chromium_env())

    if source == "config":
        return subprocess.Popen([
            "chromium", *_COMMON_FLAGS,
            profile, pos, size,
            "--app=http://localhost:8080",
            "--start-maximized",
        ], env=_chromium_env())

    if source == "companion":
        return subprocess.Popen([
            "chromium", *_COMMON_FLAGS,
            profile, pos, size,
            "--kiosk",
            "http://localhost:8000",
        ], env=_chromium_env())

    config = load_config()
    mode   = config.get("mode", "remote")
    ip     = "127.0.0.1" if mode == "local" else config.get("ip", "")

    if source == "external":
        url = config.get(f"hdmi{hdmi_index}_external_url", "").strip()
        if not url:
            # No URL configured — show the holding page rather than a browser error
            url = "http://localhost:8080/holding"
        return subprocess.Popen([
            "chromium", *_COMMON_FLAGS,
            profile, pos, size,
            "--kiosk",
            url,
        ], env=_chromium_env())

    if not ip and source != "custom":
        # OnTime source but no server configured (fresh unit) — a browser
        # error page is a terrible first impression; show the welcome screen
        url = "http://localhost:8080/welcome"
    elif source == "cleantimer":
        url = (f"http://{ip}:4001/timer/"
               f"?hideClock=true&hideCards=true&hideProgress=true"
               f"&hideLogo=true&keyColour=000000&timerColour=ffffff")
    elif source == "custom":
        url = "http://localhost:8080/view/custom"
    else:
        url = f"http://{ip}:4001{source}"

    return subprocess.Popen([
        "chromium", *_COMMON_FLAGS,
        profile, pos, size,
        "--kiosk",
        url,
    ], env=_chromium_env())


def _mark_profiles_clean():
    """Chromium shows 'Restore pages?' if the profile says it crashed —
    which it will after any hard kill. Rewrite the exit state before launch."""
    import glob
    for pref in glob.glob("/tmp/kiosk-*/Default/Preferences"):
        try:
            s = Path(pref).read_text()
            s = s.replace('"exited_cleanly":false', '"exited_cleanly":true')
            s = s.replace('"exit_type":"Crashed"', '"exit_type":"Normal"')
            Path(pref).write_text(s)
        except Exception:
            pass


def _kill_orphan_windows():
    """
    Kill any kiosk Chromium windows left over from a previous Flask instance.
    After a service restart our _win handles are gone, but the old windows
    stay on screen and hold the profile lock — new launches with the same
    user-data-dir get absorbed by the old instance and never appear.
    """
    try:
        subprocess.run(
            ["pkill", "-f", "user-data-dir=/tmp/kiosk-hdmi"],
            timeout=5, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        time.sleep(1)   # let X release the windows
    except Exception:
        pass
    _mark_profiles_clean()


def launch_all_windows():
    """Read config and (re)open windows on both displays."""
    global _win
    config   = load_config()
    displays = get_displays(fresh=True)
    d1 = displays[0]
    d2 = displays[1] if len(displays) > 1 else None

    # Out-of-box: sources that need the (unconfigured) OnTime server show the
    # welcome screen — but deliberately chosen non-OnTime sources (test
    # patterns, external, off) are honored as-is
    unconfigured = config.get("mode", "remote") == "remote" and not config.get("ip")
    h1 = config.get("hdmi1_source", "config")
    h2 = config.get("hdmi2_source", "/timer")
    s1 = "welcome" if (unconfigured and (_is_ontime_source(h1) or h1 == "config")) else h1
    s2 = "welcome" if (unconfigured and (_is_ontime_source(h2) or h2 == "config")) else h2

    with _wlock:
        _kill(_win[0])
        _kill(_win[1])
        _kill_orphan_windows()

        _win[0] = _open_window(s1, d1, 1)
        if d2:
            _win[1] = _open_window(s2, d2, 2)
        else:
            _win[1] = None


def close_all_windows():
    global _win
    with _wlock:
        for i in range(2):
            _kill(_win[i])
            _win[i] = None
        _kill_orphan_windows()


def _get_connected_outputs():
    """Return set of connected HDMI output names from xrandr."""
    try:
        env = os.environ.copy()
        env.setdefault("DISPLAY", ":0")
        out = subprocess.check_output(
            ["xrandr"], text=True, env=env, timeout=5
        )
        return {line.split()[0] for line in out.splitlines()
                if " connected" in line}
    except Exception:
        return set()


def _hdmi_monitor():
    """Background thread: relaunch windows when displays connect/disconnect."""
    prev = _get_connected_outputs()
    while True:
        time.sleep(5)
        curr = _get_connected_outputs()
        if curr != prev:
            print(f"[hdmi] display change detected: {prev} → {curr}")
            time.sleep(2)   # let X settle after hotplug
            launch_all_windows()
            prev = curr


def _launch_watchdog_windows():
    """Replace OnTime windows with holding page without touching config."""
    global _win
    try:
        config   = load_config()
        displays = get_displays(fresh=True)
        d1 = displays[0]
        d2 = displays[1] if len(displays) > 1 else None
        h1 = config.get("hdmi1_source", "config")
        h2 = config.get("hdmi2_source", "/timer")
        with _wlock:
            _kill(_win[0])
            _kill(_win[1])
            _kill_orphan_windows()
            _win[0] = _open_window("holding" if _is_ontime_source(h1) else h1, d1, 1)
            if d2:
                _win[1] = _open_window("holding" if _is_ontime_source(h2) else h2, d2, 2)
            else:
                _win[1] = None
        print("[watchdog] holding windows launched")
    except Exception as e:
        print(f"[watchdog] FAILED to launch holding windows: {e}")


def _ontime_watchdog():
    """
    Background thread: switch to holding page when OnTime goes offline.
    Requires 2 consecutive failed checks (~60s) before tripping so a single
    slow/dropped check on busy WiFi doesn't flap the displays.
    """
    global _watchdog_override
    was_connected = None   # None = not yet established
    misses = 0
    while True:
        time.sleep(30)
        config = load_config()
        if not config.get("watchdog", True):
            was_connected = None
            misses = 0
            continue
        mode = config.get("mode", "remote")
        ip   = "127.0.0.1" if mode == "local" else config.get("ip", "")
        if not ip:
            was_connected = None
            misses = 0
            continue
        connected = check_ontime(ip, timeout=3)
        with _watchdog_lock:
            if was_connected is None:
                was_connected = connected
                continue
            if not connected:
                misses += 1
            else:
                misses = 0
            if was_connected and misses >= 2:
                if _blackout_active:
                    print("[watchdog] OnTime offline but blackout active — leaving displays black")
                else:
                    print("[watchdog] OnTime offline (2 checks) — switching to holding page")
                    _watchdog_override = True
                    threading.Thread(target=_launch_watchdog_windows, daemon=True).start()
                was_connected = False
            elif not was_connected and connected:
                print("[watchdog] OnTime back online — restoring windows")
                _watchdog_override = False
                if not _blackout_active:
                    threading.Thread(target=launch_all_windows, daemon=True).start()
                was_connected = True


# ── Companion ─────────────────────────────────────────────────────────────────

@_ttl_cache(30)
def companion_is_installed():
    try:
        out = subprocess.check_output(
            ["systemctl", "show", "-p", "LoadState", "--value", "companion"],
            text=True, timeout=3,
        ).strip()
        return out == "loaded"
    except Exception:
        return False


def companion_is_running():
    try:
        r = subprocess.run(
            ["systemctl", "is-active", "companion"],
            capture_output=True, text=True, timeout=3,
        )
        return r.stdout.strip() == "active"
    except Exception:
        return False


# ── Local OnTime ──────────────────────────────────────────────────────────────

def _ontime_entry():
    for c in [ONTIME_ROOT / "ontime", ONTIME_ROOT / "AppRun"]:
        if c.exists():
            return c
    return None


def ontime_installed():
    return _ontime_entry() is not None


def ontime_is_running():
    # Check our tracked process handle first
    if _ontime_proc is not None and _ontime_proc.poll() is None:
        return True
    # Fall back to pgrep — catches OnTime running from a previous Flask instance
    # (e.g. after a Flask crash/restart where OnTime stayed alive as an orphan)
    try:
        r = subprocess.run(
            ["pgrep", "-f", "squashfs-root/ontime.*--headless"],
            capture_output=True, text=True, timeout=3,
        )
        return r.returncode == 0
    except Exception:
        return False


def start_local_ontime():
    global _ontime_proc
    entry = _ontime_entry()
    if not entry:
        return False, "OnTime not installed"
    with _ontime_lock:
        if ontime_is_running():
            return True, "already running"
        log = open(BASE_DIR / "ontime.log", "a")
        _ontime_proc = subprocess.Popen(
            [str(entry), "--no-sandbox", "--headless"],
            stdout=log, stderr=log, cwd=str(ONTIME_ROOT),
        )
    return True, "started"


def stop_local_ontime():
    global _ontime_proc
    with _ontime_lock:
        _kill(_ontime_proc)
        _ontime_proc = None
        # Also kill any OnTime processes that outlived a previous Flask instance
        try:
            subprocess.run(
                ["pkill", "-f", "squashfs-root/ontime"],
                timeout=5, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except Exception:
            pass


def install_ontime_server():
    try:
        resp  = requests.get(
            "https://api.github.com/repos/cpvalente/ontime/releases/latest",
            timeout=15,
        )
        data    = resp.json()
        version = data.get("tag_name", "unknown")
        assets  = data.get("assets", [])
        asset   = next(
            (a for a in assets if re.search(r"arm64.*\.AppImage", a["name"], re.I)),
            next((a for a in assets if a["name"].endswith(".AppImage")), None),
        )
        if not asset:
            return False, "No AppImage found in latest release"

        ONTIME_DIR.mkdir(exist_ok=True)
        with requests.get(asset["browser_download_url"], stream=True, timeout=300) as r:
            r.raise_for_status()
            with open(ONTIME_BIN, "wb") as f:
                for chunk in r.iter_content(65536):
                    f.write(chunk)
        ONTIME_BIN.chmod(0o755)

        subprocess.run(
            [str(ONTIME_BIN), "--appimage-extract"],
            cwd=str(ONTIME_DIR), check=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        if not (ONTIME_DIR / "squashfs-root").exists():
            return False, "Extraction produced no output"
        return True, version
    except Exception as e:
        return False, str(e)


# ── OLED ─────────────────────────────────────────────────────────────────────

class OLEDDisplay:
    """Single adaptive status page on the 128x64 OLED. Shows the Downstage One
    mark at boot, hotspot credentials when the hotspot is up, and network +
    OnTime status otherwise. Brand surface — same logic as the View's e-ink."""

    INTERVAL = 5

    def __init__(self):
        self._device = None
        self._stop   = threading.Event()
        self._jitter = 0   # alternate 1px vertical shift — OLED burn-in relief

    def start(self):
        if not _OLED_LIB:
            print("[oled] luma.oled not installed — skipping")
            return
        try:
            serial = luma_i2c(port=1, address=0x3C)
            self._device = ssd1306(serial, width=128, height=64)
            print("[oled] display initialised")
        except Exception as e:
            print(f"[oled] init failed: {e}")
            return
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        self._splash()
        self._stop.wait(4)   # hold the mark while the box boots
        while not self._stop.is_set():
            self._render()
            self._stop.wait(self.INTERVAL)

    # ── Boot splash — the Downstage One mark ─────────────────────────────────
    def _splash(self):
        if not self._device:
            return
        try:
            with luma_canvas(self._device) as draw:
                # mark geometry from downstage-one-mark.svg (96 viewBox), 0.42 scale
                ox, oy, s = 44, 2, 0.42
                def R(x, y, w, h, **kw):
                    draw.rectangle([ox + x*s, oy + y*s, ox + (x+w)*s, oy + (y+h)*s], **kw)
                R(6, 10, 84, 66, outline=255, width=2)   # enclosure
                R(20, 54, 40, 9,  fill=255)              # timer bar
                R(20, 83, 26, 7,  fill=255)              # downstage edge bars
                R(50, 83, 26, 7,  fill=255)              # (the two HDMI outputs)
                draw.text((22, 46), "DOWNSTAGE ONE", fill=255)
        except Exception as e:
            print(f"[oled] splash error: {e}")

    def _render(self):
        if not self._device:
            return
        try:
            with luma_canvas(self._device) as draw:
                hs = hotspot_is_active()
                # hotspot page only when it's the only way in — with ethernet
                # up, techs need the real address, not the fallback
                if hs and not _real_network_ip():
                    self._page_hotspot(draw)
                else:
                    self._page_status(draw, hotspot=hs)
            self._jitter = 1 - self._jitter
        except Exception as e:
            print(f"[oled] render error: {e}")

    # ── Normal page ───────────────────────────────────────────────────────────
    def _page_status(self, draw, hotspot=False):
        j         = self._jitter
        config    = load_config()
        mode      = config.get("mode", "remote")
        ip        = "127.0.0.1" if mode == "local" else config.get("ip", "")
        connected = check_ontime(ip, timeout=2) if ip else False
        net       = get_network_info()

        pw = _power_state()
        if pw["undervolt_now"]:
            title, right = "LOW POWER!", (_cpu_temp() or "")
        elif hotspot:
            title, right = "DOWNSTAGE ONE", "HS ON"
        else:
            title, right = "DOWNSTAGE ONE", (_cpu_temp() or "")
        draw.text((0, 0 + j), title, fill=255)
        if right:
            draw.text((128 - len(right) * 6, 0 + j), right, fill=255)
        draw.line([(0, 12 + j), (127, 12 + j)], fill=255)

        # setup address — the single most useful line on the box
        addr = f"{net['ip']}:8080" if net["ip"] != "unknown" else "No network"
        draw.text((0, 16 + j), addr, fill=255)

        # OnTime state — dot shows link health at a glance
        ontime_ok = connected or (mode == "local" and ontime_is_running())
        mark = chr(9679) if ontime_ok else chr(9675)
        if mode == "local":
            ot = "OnTime local" if ontime_is_running() else "OnTime stopped"
        elif connected:
            ot = f"OnTime {ip}"
        else:
            ot = "OnTime offline" if ip else "OnTime not set"
        draw.text((0, 30 + j), f"{mark} {ot}", fill=255)

        # network type + companion, in words
        link = {"eth0": "Wired", "wlan0": "WiFi"}.get(net["iface"], "No network")
        comp = "Companion ON" if companion_is_running() else "Companion off"
        draw.text((0, 44 + j), f"{link} + {comp}", fill=255)

    # ── Hotspot page ──────────────────────────────────────────────────────────
    def _page_hotspot(self, draw):
        j      = self._jitter
        config = load_config()
        draw.text((0, 0 + j), "HOTSPOT MODE", fill=255)
        draw.line([(0, 12 + j), (127, 12 + j)], fill=255)
        draw.text((0, 16 + j), config.get("hotspot_ssid", ""), fill=255)
        draw.text((0, 30 + j), config.get("hotspot_pass", ""), fill=255)
        draw.text((0, 44 + j), "10.42.0.1:8080", fill=255)

    def force_refresh(self):
        threading.Thread(target=self._render, daemon=True).start()


oled = OLEDDisplay()


# ── Flask routes ──────────────────────────────────────────────────────────────

@app.route("/view/custom")
def custom_view():
    return render_template("custom_view.html")


@app.route("/")
def index():
    try:
        config = load_config()
    except Exception:
        config = {}
    try:
        net = get_network_info()
    except Exception:
        net = {"ip": "unknown", "iface": "unknown"}
    try:
        num_displays = len(get_displays())
    except Exception:
        num_displays = 1
    return render_template(
        "index.html",
        config=config,
        views=VIEWS,
        local_ip=net["ip"],
        net_iface=net["iface"],
        hostname=socket.gethostname(),
        ontime_installed=ontime_installed(),
        ontime_running=ontime_is_running(),
        companion_installed=companion_is_installed(),
        companion_running=companion_is_running(),
        num_displays=num_displays,
        ip_history=config.get("ip_history", []),
    )


@app.route("/save", methods=["POST"])
def save():
    global _blackout_active, _watchdog_override
    data         = request.get_json()
    mode         = data.get("mode", "remote")
    ip           = (data.get("ip") or "").strip() if mode == "remote" else "127.0.0.1"
    hdmi1_source = data.get("hdmi1_source", "config")
    hdmi2_source = data.get("hdmi2_source", "/timer")
    hdmi1_res    = data.get("hdmi1_res",    "1920x1080")
    hdmi2_res    = data.get("hdmi2_res",    "1920x1080")
    hdmi1_rotate = data.get("hdmi1_rotate", "normal")
    hdmi2_rotate = data.get("hdmi2_rotate", "normal")
    watchdog     = bool(data.get("watchdog", True))
    hdmi1_ext    = _clean_external_url(data.get("hdmi1_external_url", ""))
    hdmi2_ext    = _clean_external_url(data.get("hdmi2_external_url", ""))

    # An External source needs a URL to show
    if hdmi1_source == "external" and not hdmi1_ext:
        return jsonify({"ok": False, "error": "Enter a URL for HDMI 1's external viewer"})
    if hdmi2_source == "external" and not hdmi2_ext:
        return jsonify({"ok": False, "error": "Enter a URL for HDMI 2's external viewer"})

    # Only demand a reachable OnTime server when a chosen source needs one —
    # test patterns / external / off / welcome work on an unconfigured unit
    needs_ontime = _is_ontime_source(hdmi1_source) or _is_ontime_source(hdmi2_source)
    if mode == "remote" and needs_ontime:
        if not ip:
            return jsonify({"ok": False, "error": "IP address required"})
        if not check_ontime(ip):
            return jsonify({"ok": False, "error": f"Cannot reach OnTime at {ip}:4001"})

    if mode == "local":
        if not ontime_installed():
            return jsonify({"ok": False, "error": "OnTime is not installed yet"})
        if not ontime_is_running():
            ok, msg = start_local_ontime()
            if not ok:
                return jsonify({"ok": False, "error": msg})
            time.sleep(3)

    history = _update_ip_history(ip) if mode == "remote" else load_config().get("ip_history", [])
    save_config({
        "ip": ip, "mode": mode,
        "hdmi1_source": hdmi1_source, "hdmi2_source": hdmi2_source,
        "hdmi1_res": hdmi1_res, "hdmi2_res": hdmi2_res,
        "hdmi1_rotate": hdmi1_rotate, "hdmi2_rotate": hdmi2_rotate,
        "hdmi1_external_url": hdmi1_ext, "hdmi2_external_url": hdmi2_ext,
        "ip_history": history, "watchdog": watchdog,
    })
    _blackout_active   = False
    _watchdog_override = False
    oled.force_refresh()
    def _apply_and_launch():
        _apply_display_settings()
        time.sleep(0.5)
        launch_all_windows()
    threading.Thread(target=_apply_and_launch, daemon=True).start()
    return jsonify({"ok": True})


@app.route("/check", methods=["POST"])
def check():
    data = request.get_json()
    ip   = (data.get("ip") or "").strip()
    ok   = check_ontime(ip) if ip else False
    return jsonify({"ok": ok})


@app.route("/status")
def status():
    try:
        config = load_config()
    except Exception:
        config = {}
    mode      = config.get("mode", "remote")
    ip        = "127.0.0.1" if mode == "local" else config.get("ip", "")
    try:
        connected = check_ontime(ip, timeout=2) if ip else False
    except Exception:
        connected = False
    ram = _ram_usage()
    try:
        net = get_network_info()
    except Exception:
        net = {"ip": "unknown", "iface": "unknown"}
    try:
        displays = len(get_displays())
    except Exception:
        displays = 1
    return jsonify({
        "ip":                   ip,
        "mode":                 mode,
        "hdmi1_source":         config.get("hdmi1_source", "config"),
        "hdmi2_source":         config.get("hdmi2_source", "/timer"),
        "hdmi1_res":            config.get("hdmi1_res",    "1920x1080"),
        "hdmi2_res":            config.get("hdmi2_res",    "1920x1080"),
        "hdmi1_rotate":         config.get("hdmi1_rotate", "normal"),
        "hdmi2_rotate":         config.get("hdmi2_rotate", "normal"),
        "hdmi1_external_url":   config.get("hdmi1_external_url", ""),
        "hdmi2_external_url":   config.get("hdmi2_external_url", ""),
        "connected":            connected,
        "os_version":           OS_VERSION,
        "serial":               config.get("serial", ""),
        "local_ip":             net["ip"],
        "net_iface":            net["iface"],
        "ontime_installed":     ontime_installed(),
        "ontime_running":       ontime_is_running(),
        "companion_installed":  companion_is_installed(),
        "companion_running":    companion_is_running(),
        "displays":             displays,
        "blackout":             _blackout_active,
        "watchdog_override":    _watchdog_override,
        "watchdog":             config.get("watchdog", True),
        "hotspot_active":       hotspot_is_active(),
        "hotspot_ssid":         config.get("hotspot_ssid", ""),
        "cpu_temp":             _cpu_temp(),
        "power":                _power_state(),
        "cpu_percent":          _cpu_percent(),
        "gpu_clock_mhz":        _gpu_clock_mhz(),
        "ram_used":             ram[0] if ram else None,
        "ram_total":            ram[1] if ram else None,
        "uptime":               _uptime(),
    })


@app.route("/reset", methods=["POST"])
def reset():
    save_config({"ip": "", "mode": "remote", "hdmi1_source": "config", "hdmi2_source": "/timer"})
    close_all_windows()
    oled.force_refresh()
    return jsonify({"ok": True})


def _refresh_windows():
    """Send F5 to every Chromium window we launched."""
    env = {**os.environ, "DISPLAY": ":0"}
    with _wlock:
        procs = [p for p in _win if p and p.poll() is None]
    for proc in procs:
        try:
            ids = subprocess.check_output(
                ["xdotool", "search", "--pid", str(proc.pid)],
                env=env, text=True, timeout=5,
            ).strip().split()
            for wid in ids:
                subprocess.run(
                    ["xdotool", "key", "--window", wid, "F5"],
                    env=env, timeout=3,
                )
        except Exception as e:
            print(f"[refresh] xdotool pid {proc.pid}: {e}")


@app.route("/refresh", methods=["POST"])
def refresh_displays():
    threading.Thread(target=_refresh_windows, daemon=True).start()
    return jsonify({"ok": True})


@app.route("/desktop", methods=["POST"])
def go_to_desktop():
    close_all_windows()
    return jsonify({"ok": True})


def _mjpeg_stream(x, y, w, h):
    """Capture a display region via ffmpeg and yield MJPEG frames."""
    env = os.environ.copy()
    env.setdefault("DISPLAY", ":0")
    cmd = [
        "ffmpeg", "-f", "x11grab",
        "-framerate", "5",
        "-video_size", f"{w}x{h}",
        "-i", f":0+{x},{y}",
        "-vf", "scale=480:270",
        "-f", "mjpeg",
        "-q:v", "8",
        "pipe:1",
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env)
    buf = b""
    try:
        while True:
            chunk = proc.stdout.read(8192)
            if not chunk:
                break
            buf += chunk
            while True:
                start = buf.find(b"\xff\xd8")
                end   = buf.find(b"\xff\xd9", start + 2) if start != -1 else -1
                if start == -1 or end == -1:
                    break
                frame = buf[start:end + 2]
                buf   = buf[end + 2:]
                yield (b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + frame + b"\r\n")
    except GeneratorExit:
        pass
    finally:
        proc.kill()
        proc.wait()


@app.route("/stream/hdmi<int:n>")
def stream_hdmi(n):
    displays = get_displays()
    if n < 1 or n > len(displays):
        return "Display not available", 404
    d = displays[n - 1]
    return Response(
        _mjpeg_stream(d["x"], d["y"], d["w"], d["h"]),
        mimetype="multipart/x-mixed-replace; boundary=frame",
    )


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


@app.route("/system/timezones", methods=["GET"])
def list_timezones():
    try:
        out = subprocess.check_output(
            ["timedatectl", "list-timezones"], text=True, timeout=10
        )
        zones = [z.strip() for z in out.splitlines() if z.strip()]
    except Exception:
        zones = []
    return jsonify({"timezones": zones})


def _apply_timezone(tz):
    """Set timezone by writing /etc/timezone and symlinking /etc/localtime."""
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


def _apply_timezone_and_restart(tz):
    _apply_timezone(tz)
    if ontime_is_running():
        stop_local_ontime()
        time.sleep(1)
        start_local_ontime()


@app.route("/system/timezone", methods=["POST"])
def set_timezone():
    tz = (request.get_json() or {}).get("timezone", "").strip()
    if not tz:
        return jsonify({"ok": False, "message": "No timezone provided"})
    try:
        _apply_timezone_and_restart(tz)
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
        r = requests.get("http://ip-api.com/json/", timeout=6)
        tz = r.json().get("timezone", "")
        if not tz:
            return jsonify({"ok": False, "message": "Could not detect timezone"})
        _apply_timezone_and_restart(tz)
        return jsonify({"ok": True, "timezone": tz})
    except Exception as e:
        return jsonify({"ok": False, "message": str(e)})


@app.route("/system/restart", methods=["POST"])
def system_restart():
    close_all_windows()
    def do_restart():
        time.sleep(1)
        subprocess.Popen(["sudo", "reboot"])
    threading.Thread(target=do_restart, daemon=True).start()
    return jsonify({"ok": True})


_output_power = {}   # output name -> False when powered off (live, non-persistent)


def _open_identify_window(display, label, idx):
    return subprocess.Popen([
        "chromium", *_COMMON_FLAGS,
        f"--user-data-dir=/tmp/kiosk-hdmi{idx}",
        f"--window-position={display['x']},{display['y']}",
        f"--window-size={display['w']},{display['h']}",
        "--kiosk", f"http://localhost:8080/identify-page/{label}",
    ], env=_chromium_env())


@app.route("/displays/identify", methods=["POST"])
def displays_identify():
    """Flash a big number on each output for a few seconds, then restore."""
    global _win
    displays = get_displays(fresh=True)
    def run():
        with _wlock:
            _kill(_win[0]); _kill(_win[1]); _kill_orphan_windows()
            _win[0] = _open_identify_window(displays[0], "1", 1)
            if len(displays) > 1:
                _win[1] = _open_identify_window(displays[1], "2", 2)
        time.sleep(5)
        launch_all_windows()
    threading.Thread(target=run, daemon=True).start()
    return jsonify({"ok": True, "displays": len(displays)})


@app.route("/displays/power", methods=["POST"])
def displays_power():
    """Turn a physical HDMI output on/off (lets TVs/projectors sleep).
    Live action — everything returns on reboot."""
    data = request.get_json() or {}
    idx  = int(data.get("output", 1))
    on   = bool(data.get("on", True))
    names = _get_all_connected_outputs()
    if idx < 1 or idx > len(names):
        return jsonify({"ok": False, "message": "Output not connected"})
    name = names[idx - 1]
    env  = {**os.environ, "DISPLAY": ":0"}
    try:
        if on:
            subprocess.run(["xrandr", "--output", name, "--auto"], env=env,
                           timeout=10, check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            _output_power[name] = True
            threading.Thread(target=launch_all_windows, daemon=True).start()
        else:
            subprocess.run(["xrandr", "--output", name, "--off"], env=env,
                           timeout=10, check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            _output_power[name] = False
        return jsonify({"ok": True, "output": idx, "on": on})
    except Exception as e:
        return jsonify({"ok": False, "message": str(e)})


@app.route("/displays/power-status")
def displays_power_status():
    active = set(_get_output_names())
    conn   = _get_all_connected_outputs()
    return jsonify({"outputs": [
        {"index": i + 1, "name": n, "on": n in active}
        for i, n in enumerate(conn)
    ]})


@app.route("/logs")
def logs():
    n = min(int(request.args.get("lines", 200)), 1000)
    try:
        out = subprocess.check_output(
            ["tail", "-n", str(n), str(BASE_DIR / "kiosk.log")],
            text=True, timeout=5)
    except Exception as e:
        out = f"(no log available: {e})"
    return jsonify({"log": out})


@app.route("/system/shutdown", methods=["POST"])
def system_shutdown():
    close_all_windows()
    def do_shutdown():
        time.sleep(1)
        subprocess.Popen(["sudo", "poweroff"])
    threading.Thread(target=do_shutdown, daemon=True).start()
    return jsonify({"ok": True})


# ── Update check ──────────────────────────────────────────────────────────────

@app.route("/update-status")
def update_status_route():
    d = dict(_update_status)
    d["os"] = dict(d["os"], last_result=_os_update_result())
    return jsonify(d)


@app.route("/updates/recheck", methods=["POST"])
def updates_recheck():
    threading.Thread(target=_check_updates_background, daemon=True).start()
    return jsonify({"ok": True})


# ── Downstage OS self-update ──────────────────────────────────────────────────
# Downloads a release tarball, syntax-checks it, then hands off to a detached
# swap script (via systemd-run, so restarting our own service can't kill it).
# The script swaps files, restarts, health-checks /status for 60s, and rolls
# back to the automatic backup if the new version doesn't come up.

_OS_VARIANT = "one"
_OS_APPDIR  = str(BASE_DIR)

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

_OS_RESTART_CMD = "systemctl --user restart ontime-kiosk"


def _os_update_result():
    try:
        return (Path(_OS_APPDIR) / ".update-result").read_text().strip()
    except Exception:
        return None


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
        if not force and _version_tuple(ver) <= _version_tuple(OS_VERSION):
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

        # refuse to install code that won't even parse
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
        subprocess.Popen(
            ["systemd-run", "--user", "--collect", f"--unit=ds-os-update-{int(time.time())}",
             "bash", str(script)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        return jsonify({"ok": True, "message": f"Updating to {tag} — service will restart"})
    except py_compile.PyCompileError as e:
        return jsonify({"ok": False, "message": f"Release failed validation: {e}"})
    except Exception as e:
        return jsonify({"ok": False, "message": str(e)})


# ── Local OnTime routes ───────────────────────────────────────────────────────

@app.route("/ontime/install", methods=["POST"])
def ontime_install():
    ok, msg = install_ontime_server()
    if ok:
        threading.Thread(target=_check_updates_background, daemon=True).start()
    return jsonify({"ok": ok, "message": msg})


@app.route("/ontime/start", methods=["POST"])
def ontime_start():
    ok, msg = start_local_ontime()
    return jsonify({"ok": ok, "message": msg, "running": ontime_is_running()})


@app.route("/ontime/stop", methods=["POST"])
def ontime_stop():
    stop_local_ontime()
    return jsonify({"ok": True, "running": False})


@app.route("/ontime/status")
def ontime_status():
    return jsonify({"installed": ontime_installed(), "running": ontime_is_running()})


@app.route("/ontime/update", methods=["POST"])
def ontime_update():
    was_running = ontime_is_running()
    if was_running:
        stop_local_ontime()
        time.sleep(1)
    ok, msg = install_ontime_server()
    if ok:
        threading.Thread(target=_check_updates_background, daemon=True).start()
        if was_running:
            start_local_ontime()
    return jsonify({"ok": ok, "message": msg, "running": ontime_is_running()})


# ── Companion routes ──────────────────────────────────────────────────────────

_companion_install = {"state": "idle", "message": ""}   # idle|installing|done|failed


def _companion_install_worker():
    """Customer-initiated install using Bitfocus's official companion-pi
    script — the unit ships without Companion; the end user's click fetches
    it from the official source onto their device."""
    _companion_install["state"] = "installing"
    _companion_install["message"] = ""
    try:
        r = subprocess.run(
            ["sudo", "bash", "-c",
             "curl -sL https://raw.githubusercontent.com/bitfocus/companion-pi/main/install.sh | bash"],
            capture_output=True, text=True, timeout=1800,
        )
        if r.returncode == 0 and companion_is_installed(fresh=True):
            subprocess.run(["sudo", "systemctl", "enable", "--now", "companion"],
                           timeout=30, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            _companion_install["state"] = "done"
            threading.Thread(target=_check_updates_background, daemon=True).start()
        else:
            _companion_install["state"] = "failed"
            _companion_install["message"] = (r.stderr or r.stdout or "install script failed")[-300:]
    except Exception as e:
        _companion_install["state"] = "failed"
        _companion_install["message"] = str(e)
    print(f"[companion] install: {_companion_install['state']} {_companion_install['message'][:120]}")


@app.route("/companion/install", methods=["POST"])
def companion_install_route():
    if _companion_install["state"] == "installing":
        return jsonify({"ok": True, "state": "installing"})
    threading.Thread(target=_companion_install_worker, daemon=True).start()
    return jsonify({"ok": True, "state": "installing"})


@app.route("/companion/status")
def companion_status_route():
    return jsonify({
        "installed": companion_is_installed(),
        "running":   companion_is_running(),
        "install_state":   _companion_install["state"],
        "install_message": _companion_install["message"],
    })


@app.route("/companion/start", methods=["POST"])
def companion_start():
    subprocess.run(["sudo", "systemctl", "start", "companion"], timeout=10)
    time.sleep(1)
    return jsonify({"ok": True, "running": companion_is_running()})


@app.route("/companion/stop", methods=["POST"])
def companion_stop():
    subprocess.run(["sudo", "systemctl", "stop", "companion"], timeout=10)
    return jsonify({"ok": True, "running": False})


@app.route("/companion/restart", methods=["POST"])
def companion_restart():
    subprocess.run(["sudo", "systemctl", "restart", "companion"], timeout=15)
    time.sleep(2)
    return jsonify({"ok": True, "running": companion_is_running()})


@app.route("/companion/rescan-usb", methods=["POST"])
def companion_rescan_usb():
    try:
        subprocess.run(
            ["sudo", "udevadm", "trigger", "--subsystem-match=usb"],
            timeout=5, check=True,
        )
        time.sleep(1)
        subprocess.run(["sudo", "systemctl", "restart", "companion"],
                       timeout=15, check=True)
        time.sleep(2)
        return jsonify({"ok": True, "running": companion_is_running()})
    except Exception as e:
        return jsonify({"ok": False, "message": str(e)})


@app.route("/companion/update", methods=["POST"])
def companion_update():
    channel = load_config().get("companion_channel", "stable")
    build   = "beta" if channel == "beta" else "stable"
    try:
        # companion-update is already installed and handles stable/beta correctly.
        # Calling it with one arg (channel only, no version) lets it pick the
        # latest from the bitfocus API — which also handles downgrades from beta.
        subprocess.run(
            ["sudo", "companion-update", build],
            timeout=300, check=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        threading.Thread(target=_check_updates_background, daemon=True).start()
        return jsonify({"ok": True, "running": companion_is_running()})
    except subprocess.TimeoutExpired:
        return jsonify({"ok": False, "message": "Update timed out after 5 minutes"})
    except Exception as e:
        return jsonify({"ok": False, "message": str(e)})


@app.route("/companion/set-channel", methods=["POST"])
def companion_set_channel():
    channel = request.json.get("channel", "stable")
    if channel not in ("stable", "beta"):
        return jsonify({"ok": False, "message": "Invalid channel"})
    save_config({"companion_channel": channel})
    threading.Thread(target=_check_updates_background, daemon=True).start()
    return jsonify({"ok": True, "channel": channel})


# ── Hotspot ───────────────────────────────────────────────────────────────────
# Fallback access point (SSID/password come from the per-unit build log via
# config.json). Auto-starts at boot ONLY if the unit finds no network at all —
# solves first-boot setup at a venue with no known WiFi. Never auto-starts
# after a network has been seen, so a mid-show WiFi blip can't hijack the radio.

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
    ssid   = config.get("hotspot_ssid") or "Downstage-0000"
    pw     = config.get("hotspot_pass") or "downstage"
    # remove any stale profile so settings changes take effect
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
    # don't fight normal WiFi for the radio on the next boot
    subprocess.run(["sudo", "nmcli", "connection", "modify", HOTSPOT_CON,
                    "connection.autoconnect", "no"],
                   timeout=10, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print(f"[hotspot] broadcasting {ssid}")
    return True, ssid


def stop_hotspot():
    # NB: do NOT run `nmcli device connect wlan0` here — that reactivates the
    # most recent profile on the radio, which is the hotspot itself. Once the
    # hotspot is down, NetworkManager rejoins known WiFi on its own.
    r = subprocess.run(["sudo", "nmcli", "connection", "down", HOTSPOT_CON],
                       capture_output=True, text=True, timeout=15)
    if r.returncode != 0:
        msg = (r.stderr or r.stdout).strip()
        print(f"[hotspot] stop failed: {msg}")
        return False, msg
    print("[hotspot] stopped")
    return True, "stopped"


def _hotspot_fallback():
    """First-boot aid: if the unit has no network at all ~90s after boot,
    start the hotspot so the setup UI is reachable. Stops watching as soon
    as any real network is seen."""
    time.sleep(90)
    while True:
        config = load_config()
        if not config.get("hotspot_auto", True):
            return
        if hotspot_is_active():
            return
        net = get_network_info()
        if net["ip"] != "unknown":
            return   # network exists — provisioning aid no longer needed
        print("[hotspot] no network found — starting fallback hotspot")
        ok, msg = start_hotspot()
        print(f"[hotspot] fallback start: ok={ok} ({msg})")
        return


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
        subprocess.Popen(["systemd-run", "--user", "--collect", f"--unit=ds-factory-{int(time.time())}", "bash", str(script)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return jsonify({"ok": True, "message": "Factory reset started — the unit will reboot"})
    except Exception as e:
        return jsonify({"ok": False, "message": str(e)})


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
                   sh("vcgencmd measure_temp 2>/dev/null") +
                   sh("cat /proc/device-tree/model 2>/dev/null; echo"))
        z.writestr("storage.txt",
                   sh("lsblk -o NAME,SIZE,TYPE,MOUNTPOINT") + "\n=== boot device errors ===\n" +
                   sh("dmesg 2>/dev/null | grep -iE 'mmc.*error|nvme.*(err|timeout)' | tail -5 || echo none"))
        z.writestr("rtc.txt",
                   "battery_uV: " + sh("cat /sys/class/rtc/rtc0/battery_voltage 2>/dev/null") +
                   "charging_uV: " + sh("cat /sys/class/rtc/rtc0/charging_voltage 2>/dev/null"))
        z.writestr("app.log", sh(f"tail -n 400 {_OS_APPDIR}/kiosk.log 2>/dev/null"))
    buf.seek(0)
    return send_file(buf, as_attachment=True, mimetype="application/zip",
                     download_name=f"downstage-diag-{serial}.zip")


# ── Network (static IP) ───────────────────────────────────────────────────────
# Configuring the IP of the interface you're reachable through is risky: a
# wrong setting strands the unit. So every apply arms a revert timer — the UI
# must reconnect and confirm within the window, or the previous config is
# restored automatically (the "reload in 5" pattern from managed switches).

import ipaddress

_net_revert = {"event": None, "snapshot": None, "conn": None}


def _default_conn():
    """(connection-name, iface) carrying the default route — the one the user
    is most likely reaching the unit through."""
    try:
        route = subprocess.check_output(["ip", "route", "show", "default"],
                                        text=True, timeout=5)
        m = re.search(r"dev (\S+)", route)
        iface = m.group(1) if m else "eth0"
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


@app.route("/network/info")
def network_info():
    conn, iface = _default_conn()
    info = _conn_ipv4(conn) if conn else {}
    net = get_network_info()
    return jsonify({
        "conn":    conn,
        "iface":   iface,
        "method":  info.get("ipv4.method", "auto"),
        "address": info.get("ipv4.addresses", ""),
        "gateway": info.get("ipv4.gateway", ""),
        "dns":     info.get("ipv4.dns", ""),
        "current_ip": net["ip"],
        "reverting": _net_revert["event"] is not None,
    })


def _apply_ipv4(conn, method, addr=None, gw=None, dns=None):
    cmd = ["sudo", "nmcli", "connection", "modify", conn, "ipv4.method", method]
    if method == "manual":
        cmd += ["ipv4.addresses", addr, "ipv4.gateway", gw or "",
                "ipv4.dns", dns or ""]
    else:
        cmd += ["ipv4.addresses", "", "ipv4.gateway", "", "ipv4.dns", ""]
    subprocess.run(cmd, timeout=15, check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(["sudo", "nmcli", "connection", "up", conn],
                   timeout=30, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _revert_worker(conn, snapshot, event):
    if event.wait(90):
        return   # confirmed in time
    print("[network] not confirmed in 90s — reverting")
    try:
        method = snapshot.get("ipv4.method", "auto")
        _apply_ipv4(conn, method,
                    snapshot.get("ipv4.addresses") or None,
                    snapshot.get("ipv4.gateway") or None,
                    snapshot.get("ipv4.dns") or None)
    except Exception as e:
        print(f"[network] revert failed: {e}")
    _net_revert["event"] = None


@app.route("/network/apply", methods=["POST"])
def network_apply():
    data   = request.get_json() or {}
    method = data.get("method", "auto")
    conn, iface = _default_conn()
    if not conn:
        return jsonify({"ok": False, "message": "No active connection found"})

    addr = gw = dns = None
    if method == "manual":
        try:
            ip     = data["ip"].strip()
            prefix = int(data.get("prefix", 24))
            ipaddress.ip_address(ip)
            if not (1 <= prefix <= 32):
                raise ValueError("prefix")
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
        threading.Thread(target=_revert_worker, args=(conn, snapshot, event),
                         daemon=True).start()
    threading.Thread(target=do, daemon=True).start()
    new_ip = data.get("ip") if method == "manual" else None
    return jsonify({"ok": True, "revert_in": 90, "new_ip": new_ip,
                    "hostname": socket.gethostname()})


@app.route("/network/confirm", methods=["POST"])
def network_confirm():
    ev = _net_revert.get("event")
    if ev is None:
        return jsonify({"ok": True, "message": "Nothing pending"})
    ev.set()
    _net_revert["event"] = None
    return jsonify({"ok": True, "message": "Network settings kept"})


# ── WiFi ──────────────────────────────────────────────────────────────────────

def _active_ssid():
    try:
        out = subprocess.check_output(
            ["nmcli", "-t", "-f", "active,ssid", "dev", "wifi"],
            text=True, timeout=5,
        )
        for line in out.strip().splitlines():
            parts = line.split(":")
            if parts[0] == "yes" and len(parts) > 1:
                return parts[1]
    except Exception:
        pass
    return None


def _scan_wifi():
    out = subprocess.check_output(
        ["nmcli", "-t", "-f", "active,ssid,signal,security", "dev", "wifi"],
        text=True, timeout=8,
    )
    seen     = {}   # ssid -> index in networks
    networks = []
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
            # The AP shows up as the "active" network — it isn't a client
            # connection, so report no current network and keep the list
            # pickable (selecting one triggers the stop-hotspot-and-join flow)
            config   = load_config()
            hs_ssid  = config.get("hotspot_ssid", "")
            networks = [n for n in networks if n["ssid"] != hs_ssid]
            for n in networks:
                n["active"] = False
            current = None
        return jsonify({"ok": True, "hotspot": hotspot, "current": current, "networks": networks})
    except Exception as e:
        # scan can fail in AP mode on some chips — still report hotspot state
        return jsonify({"ok": hotspot, "hotspot": hotspot,
                        "current": None, "networks": [], "error": str(e)})


@app.route("/wifi/scan", methods=["POST"])
def wifi_scan():
    hotspot = hotspot_is_active()
    try:
        # Best-effort rescan — in AP mode the radio often can't actively scan;
        # fall back to the cached list rather than failing the request.
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
        return jsonify({"ok": True, "hotspot": hotspot, "current": current, "networks": networks})
    except Exception as e:
        return jsonify({"ok": False, "hotspot": hotspot, "current": None, "networks": [], "error": str(e)})


@app.route("/wifi/connect", methods=["POST"])
def wifi_connect():
    data     = request.get_json() or {}
    ssid     = data.get("ssid", "").strip()
    password = data.get("password", "").strip()
    if not ssid:
        return jsonify({"ok": False, "message": "SSID required"})

    # Single radio: joining a network while the hotspot runs requires stopping
    # the hotspot first. If the join fails, bring the hotspot back so the
    # device is never left unreachable.
    hotspot_was_active = hotspot_is_active()
    try:
        if hotspot_was_active:
            print(f"[wifi] stopping hotspot to join '{ssid}'")
            stop_hotspot()
            time.sleep(3)   # let the radio switch back to client mode

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

        if not ok and hotspot_was_active:
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


# ── Blackout ──────────────────────────────────────────────────────────────────

@app.route("/blackout", methods=["POST"])
def blackout():
    global _blackout_active, _win
    _blackout_active = True
    displays = get_displays(fresh=True)
    d1 = displays[0]
    d2 = displays[1] if len(displays) > 1 else None
    with _wlock:
        _kill(_win[0])
        _kill(_win[1])
        _kill_orphan_windows()
        _win[0] = _open_window("blackout", d1, 1)
        if d2:
            _win[1] = _open_window("blackout", d2, 2)
        else:
            _win[1] = None
    return jsonify({"ok": True, "blackout": True})


@app.route("/blackout/clear", methods=["POST"])
def blackout_clear():
    global _blackout_active
    _blackout_active = False
    threading.Thread(target=launch_all_windows, daemon=True).start()
    return jsonify({"ok": True, "blackout": False})


@app.route("/blackout-page")
def blackout_page_view():
    return '<html><body style="margin:0;background:#000"></body></html>', 200, {"Content-Type": "text/html"}


@app.route("/identify-page/<label>")
def identify_page(label):
    label = re.sub(r"[^A-Za-z0-9 ]", "", label)[:12]
    return (
        '<!DOCTYPE html><html><head><style>'
        '*{margin:0;padding:0}'
        'body{background:#12A95C;color:#fff;display:flex;flex-direction:column;'
        'align-items:center;justify-content:center;height:100vh;'
        'font-family:sans-serif;text-align:center}'
        '.n{font-size:40vh;font-weight:800;line-height:1}'
        '.l{font-size:5vh;letter-spacing:0.3em;text-transform:uppercase;opacity:.85}'
        '</style></head><body>'
        f'<div class="n">{label}</div><div class="l">This Output</div>'
        '</body></html>'
    ), 200, {"Content-Type": "text/html"}


@app.route("/welcome")
def welcome_page():
    """Boot / out-of-box screen for the HDMI outputs: the mark, the unit's
    live address, and hotspot credentials when broadcasting. Polls status so
    the address updates as networks come and go."""
    config = load_config()
    return f"""<!DOCTYPE html><html><head><style>
@font-face {{ font-family:'Rajdhani'; font-weight:700; src:url('/static/fonts/rajdhani-700.woff2') format('woff2'); }}
@font-face {{ font-family:'STMono'; src:url('/static/fonts/share-tech-mono-400.woff2') format('woff2'); }}
* {{ margin:0; padding:0; box-sizing:border-box; }}
body {{ background:#0B0D10; color:#E8ECEF; height:100vh; display:flex; flex-direction:column;
       align-items:center; justify-content:center; gap:3.5vh; font-family:'Rajdhani',sans-serif;
       text-align:center; cursor:none; }}
.mark {{ width:18vh; height:18vh; }}
h1 {{ font-size:7vh; font-weight:700; text-transform:uppercase; letter-spacing:0.05em; }}
h1 span {{ color:#2FD97B; }}
.addr {{ font-family:'STMono',monospace; font-size:4.2vh; color:#2FD97B; }}
.sub  {{ font-family:'STMono',monospace; font-size:2.2vh; color:#9AA4AD; }}
#hs {{ display:none; border:1px solid #F5A52440; border-radius:1.5vh; padding:2vh 3.5vh; }}
#hs .t {{ font-family:'STMono',monospace; font-size:2vh; color:#F5A524; letter-spacing:0.2em;
          text-transform:uppercase; margin-bottom:1vh; }}
#hs .v {{ font-family:'STMono',monospace; font-size:2.6vh; color:#E8ECEF; }}
.ser {{ position:fixed; bottom:3vh; font-family:'STMono',monospace; font-size:1.6vh; color:#3a444d;
        letter-spacing:0.2em; }}
</style></head><body>
<svg class="mark" viewBox="0 0 96 96"><rect x="6" y="10" width="84" height="66" rx="10" fill="none" stroke="#e8ecef" stroke-width="7"/><rect x="20" y="54" width="40" height="9" rx="4.5" fill="#2fd97b"/><rect x="64" y="54" width="12" height="9" rx="4.5" fill="#e8ecef" opacity="0.28"/><rect x="20" y="83" width="26" height="7" rx="3.5" fill="#2fd97b"/><rect x="50" y="83" width="26" height="7" rx="3.5" fill="#2fd97b"/></svg>
<h1>Downstage <span>One</span></h1>
<div>
  <div class="addr" id="addr">{socket.gethostname()}.local:8080</div>
  <div class="sub" id="ip"></div>
</div>
<div id="hs">
  <div class="t">No network — join this WiFi</div>
  <div class="v" id="hs-ssid"></div>
  <div class="v" style="color:#9AA4AD" id="hs-pass"></div>
  <div class="v" style="margin-top:0.6vh">10.42.0.1:8080</div>
</div>
<div class="ser">{config.get("serial", "")} · DOWNSTAGE.SYSTEMS</div>
<script>
async function tick() {{
  try {{
    const d = await (await fetch('/status')).json();
    document.getElementById('ip').textContent = d.local_ip !== 'unknown' ? 'or ' + d.local_ip + ':8080' : '';
    const hs = document.getElementById('hs');
    if (d.hotspot_active) {{
      const h = await (await fetch('/hotspot/status')).json();
      document.getElementById('hs-ssid').textContent = h.ssid;
      document.getElementById('hs-pass').textContent = 'password: ' + h.pass;
      hs.style.display = 'block';
    }} else hs.style.display = 'none';
  }} catch {{}}
}}
tick(); setInterval(tick, 5000);
</script></body></html>""", 200, {"Content-Type": "text/html"}


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
    x.fillStyle = "#2FD97B";                      // downstage edge
    x.fillRect(bx, H*0.73, bw2*0.42, H*0.012);
    x.fillRect(bx + bw2*0.46, H*0.73, bw2*0.12, H*0.012);
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


@app.route("/holding")
def holding_page():
    return (
        '<!DOCTYPE html><html><head><style>'
        '*{margin:0;padding:0}'
        'body{background:#000;color:#222;display:flex;align-items:center;'
        'justify-content:center;height:100vh;font-family:sans-serif;text-align:center}'
        '</style></head><body>'
        '<div><div style="font-size:48px;margin-bottom:16px">⏱</div>'
        '<div style="font-size:18px;color:#333">Waiting for OnTime…</div></div>'
        '</body></html>'
    ), 200, {"Content-Type": "text/html"}


# ── Config backup / restore ───────────────────────────────────────────────────

@app.route("/config/download")
def config_download():
    if not CONFIG_FILE.exists():
        return jsonify({"error": "No config file"}), 404
    return send_file(
        CONFIG_FILE, as_attachment=True,
        download_name="kiosk-config.json",
        mimetype="application/json",
    )


@app.route("/config/upload", methods=["POST"])
def config_upload():
    f = request.files.get("config")
    if not f:
        return jsonify({"ok": False, "message": "No file uploaded"})
    try:
        data = json.loads(f.read().decode())
    except Exception:
        return jsonify({"ok": False, "message": "Invalid JSON"})
    with open(CONFIG_FILE, "w") as fh:
        json.dump(data, fh)
    oled.force_refresh()
    threading.Thread(target=launch_all_windows, daemon=True).start()
    return jsonify({"ok": True})


# ── Presets ───────────────────────────────────────────────────────────────────

@app.route("/presets")
def presets_list():
    config = load_config()
    return jsonify({"presets": config.get("presets", [])})


@app.route("/presets/save", methods=["POST"])
def presets_save():
    data  = request.get_json() or {}
    name  = (data.get("name") or "").strip()
    if not name:
        return jsonify({"ok": False, "message": "Name required"})
    config  = load_config()
    presets = [p for p in config.get("presets", []) if p.get("name") != name]
    presets.append({
        "name":         name,
        "hdmi1_source": data.get("hdmi1_source", config.get("hdmi1_source", "config")),
        "hdmi2_source": data.get("hdmi2_source", config.get("hdmi2_source", "/timer")),
    })
    save_config({"presets": presets})
    return jsonify({"ok": True, "presets": presets})


@app.route("/presets/apply", methods=["POST"])
def presets_apply():
    global _watchdog_override, _blackout_active
    data   = request.get_json() or {}
    name   = (data.get("name") or "").strip()
    config = load_config()
    preset = next((p for p in config.get("presets", []) if p.get("name") == name), None)
    if not preset:
        return jsonify({"ok": False, "message": "Preset not found"})
    _watchdog_override = False
    _blackout_active   = False
    save_config({
        "hdmi1_source": preset["hdmi1_source"],
        "hdmi2_source": preset["hdmi2_source"],
    })
    threading.Thread(target=launch_all_windows, daemon=True).start()
    return jsonify({"ok": True,
                    "hdmi1_source": preset["hdmi1_source"],
                    "hdmi2_source": preset["hdmi2_source"]})


@app.route("/presets/delete", methods=["POST"])
def presets_delete():
    name    = ((request.get_json() or {}).get("name") or "").strip()
    config  = load_config()
    presets = [p for p in config.get("presets", []) if p.get("name") != name]
    save_config({"presets": presets})
    return jsonify({"ok": True, "presets": presets})


# ── Boot ──────────────────────────────────────────────────────────────────────

def boot():
    # Hide idle mouse cursor
    try:
        subprocess.Popen(
            ["unclutter", "-idle", "2", "-root"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        pass

    time.sleep(3)
    config = load_config()
    mode   = config.get("mode", "remote")
    ip     = "127.0.0.1" if mode == "local" else config.get("ip", "")

    if mode == "local" and ontime_installed() and not ontime_is_running():
        start_local_ontime()
        time.sleep(4)

    _activate_all_connected_outputs()   # ensure both HDMI ports are active before opening windows

    # Boot splash: the welcome screen (mark + address) while services settle,
    # then the configured views. Unconfigured units simply stay on welcome.
    global _win
    displays = get_displays(fresh=True)
    with _wlock:
        _kill_orphan_windows()
        _win[0] = _open_window("welcome", displays[0], 1)
        if len(displays) > 1:
            _win[1] = _open_window("welcome", displays[1], 2)
    time.sleep(8)
    launch_all_windows()
    threading.Thread(target=_check_updates_background, daemon=True).start()


if __name__ == "__main__":
    oled.start()
    threading.Thread(target=boot,             daemon=True).start()
    threading.Thread(target=_hdmi_monitor,    daemon=True).start()
    threading.Thread(target=_ontime_watchdog, daemon=True).start()
    threading.Thread(target=_cpu_sampler,     daemon=True).start()
    threading.Thread(target=_hotspot_fallback, daemon=True).start()
    app.run(host="0.0.0.0", port=8080, use_reloader=False, threaded=True)
