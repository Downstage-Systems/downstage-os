import json
import os
import re
import subprocess
import threading
import time
import socket
from pathlib import Path

import requests
from flask import Flask, jsonify, render_template, request, send_file

OS_VERSION = "1.1.1"   # Downstage OS release — bump on tagged releases
OS_PRODUCT = "Downstage View"

app = Flask(__name__)
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
    data.setdefault("hotspot_ssid", "Downstage-V001")
    data.setdefault("hotspot_pass", "dolly-wrap-45")
    data.setdefault("hotspot_auto", True)
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
    return source not in ("config", "off", "external", None, "")


def _source_url(source):
    """Map a source name to the URL the kiosk window should show."""
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
    if source == "cleantimer":
        return (f"http://{ip}:4001/timer/"
                f"?hideClock=true&hideCards=true&hideProgress=true"
                f"&hideLogo=true&keyColour=000000&timerColour=ffffff")
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


def _show(url):
    """Show a URL on the output: reuse the live window when possible,
    cold-start chromium only when there isn't one. A freshly cold-started
    chromium needs ~20s before its DevTools port answers on this hardware,
    so navigation retries patiently rather than triggering cascading
    cold starts."""
    global _win
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


def _check_os_update():
    """Boot + daily refresh loop."""
    while True:
        _refresh_os_update()
        time.sleep(86400)


@app.route("/os/recheck", methods=["POST"])
def os_recheck():
    _refresh_os_update()
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


@app.route("/holding")
def holding_page():
    return (
        '<!DOCTYPE html><html><head><style>'
        '*{margin:0;padding:0}'
        'body{background:#000;color:#222;display:flex;align-items:center;'
        'justify-content:center;height:100vh;font-family:sans-serif;text-align:center}'
        '</style></head><body>'
        '<div><div style="font-size:48px;margin-bottom:16px">&#9201;</div>'
        '<div style="font-size:18px;color:#333">Waiting for OnTime&#8230;</div></div>'
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


def _hotspot_fallback():
    """First-boot aid: no network ~90s after boot → start the hotspot so the
    setup UI is reachable. Stands down once any network is seen."""
    time.sleep(90)
    config = load_config()
    if not config.get("hotspot_auto", True) or hotspot_is_active():
        return
    if get_local_ip() != "unknown":
        return
    print("[hotspot] no network found — starting fallback hotspot")
    ok, msg = start_hotspot()
    print(f"[hotspot] fallback start: ok={ok} ({msg})")


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
        return jsonify({"ok": True, "message": f"Updating to {tag} — service will restart"})
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


# ── e-Paper display ───────────────────────────────────────────────────────────

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
        "cleantimer": "Clean Timer", "/timer": "Stage Timer",
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
            self._stop.wait(self.INTERVAL)

    def _new_image(self):
        return Image.new('1', (self.W, self.H), 255)

    def _flush(self, image):
        image = image.rotate(180)   # panel is mounted upside-down in the enclosure
        with self._lock:
            # e-ink refreshes flash the panel — skip if nothing changed
            frame = image.tobytes()
            if frame == self._last_frame:
                return
            self._last_frame = frame
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

    def _render(self):
        if not self._epd:
            return
        try:
            img  = self._new_image()
            draw = ImageDraw.Draw(img)
            hs = hotspot_is_active()
            if hs and not _real_network_ip():
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

    # ── Normal page: network + OnTime status ─────────────────────────────────
    def _page_status(self, draw, hotspot=False):
        config    = load_config()
        ip        = config.get("ip", "")
        local_ip  = get_local_ip()
        connected = check_ontime(ip, timeout=2) if ip else False
        ssid      = _active_ssid()
        source    = config.get("source", "/timer")
        temp      = _cpu_temp() or ""

        self._header(draw, "DOWNSTAGE VIEW", "HOTSPOT ON" if hotspot else temp)

        self._row(draw, 26, "WiFi",   (ssid or "Not connected")[:22])
        self._row(draw, 44, "Setup",  f"{local_ip}:8080" if local_ip != "unknown" else "No network")
        draw.line([(5, 62), (self.W - 5, 62)], fill=0)
        self._row(draw, 68, "OnTime", ip if ip else "Not configured")
        status = "CONNECTED" if connected else "OFFLINE"
        marker = chr(9679) if connected else chr(9675)   # filled / hollow dot
        draw.text((5, 86), f"{marker} {status}", font=self._font_md, fill=0)
        self._row(draw, 106, "View", self.SOURCE_LABELS.get(source, source))

    # ── Hotspot page: everything a tech needs to get in ──────────────────────
    def _page_hotspot(self, draw):
        config = load_config()
        ssid   = config.get("hotspot_ssid", "")
        pw     = config.get("hotspot_pass", "")

        self._header(draw, "HOTSPOT MODE")
        self._row(draw, 28, "WiFi",  ssid, font=self._font_lg)
        self._row(draw, 52, "Pass",  pw,   font=self._font_lg)
        draw.line([(5, 78), (self.W - 5, 78)], fill=0)
        draw.text((5, 84), "Join the WiFi above, then open:", font=self._font_sm, fill=0)
        draw.text((5, 99), "http://10.42.0.1:8080", font=self._font_md, fill=0)

    def force_refresh(self):
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
    config    = load_config()
    ip        = config.get("ip", "")
    connected = check_ontime(ip, timeout=2) if ip else False
    return jsonify({
        "ip":        ip,
        "source":    config.get("source", "/timer"),
        "external_url": config.get("external_url", ""),
        "connected": connected,
        "local_ip":  get_local_ip(),
        "os_version": OS_VERSION,
        "serial": config.get("serial", ""),
        "os_latest": _os_update["latest"],
        "os_update_available": _os_update["update_available"],
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


@app.route("/network/info")
def network_info():
    conn, iface = _default_conn()
    info = _conn_ipv4(conn) if conn else {}
    return jsonify({
        "conn": conn, "iface": iface,
        "method": info.get("ipv4.method", "auto"),
        "address": info.get("ipv4.addresses", ""),
        "gateway": info.get("ipv4.gateway", ""),
        "dns": info.get("ipv4.dns", ""),
        "current_ip": get_local_ip(),
        "reverting": _net_revert["event"] is not None,
    })


@app.route("/network/apply", methods=["POST"])
def network_apply():
    data = request.get_json() or {}
    method = data.get("method", "auto")
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
        return jsonify({"ok": True, "hotspot": hotspot, "current": current, "networks": networks})
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


@app.route("/system/timezones", methods=["GET"])
def list_timezones():
    try:
        out   = subprocess.check_output(["timedatectl", "list-timezones"], text=True, timeout=10)
        zones = [z.strip() for z in out.splitlines() if z.strip()]
    except Exception:
        zones = []
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
    def do_shutdown():
        time.sleep(1)
        subprocess.Popen(["sudo", "poweroff"])
    threading.Thread(target=do_shutdown, daemon=True).start()
    return jsonify({"ok": True})


# ── Boot ──────────────────────────────────────────────────────────────────────

def boot():
    try:
        subprocess.Popen(
            ["unclutter", "-idle", "2", "-root"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        pass

    time.sleep(3)
    config = load_config()
    _show(_source_url(config.get("source", "/timer") if config.get("ip") else "config"))


if __name__ == "__main__":
    epaper.start()
    threading.Thread(target=boot, daemon=True).start()
    threading.Thread(target=_hotspot_fallback, daemon=True).start()
    threading.Thread(target=_ontime_watchdog, daemon=True).start()
    threading.Thread(target=_check_os_update, daemon=True).start()
    app.run(host="0.0.0.0", port=8080, use_reloader=False, threaded=True)
