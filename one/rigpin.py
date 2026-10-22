"""The rig PIN on the One: the lock, and who may pass it.

The design is the Cue repo's docs/control-token.md (Rob, 2026-10-18):
"lets make it an option but the default is off. but keep in mind the goal
is 'Magic' just works."

- No PIN set: nothing here does anything. A rig that never taps Secure is
  exactly as open as before.
- One PIN for the rig. It is what this unit sends to its boxes (the old
  "rig token", config key cue_token, unchanged so nothing else moves) and
  what this unit asks for before the actions that can stop a show:
  restart, power, network, blackout, looks and sources.
- It arrives from the paired Relay's pair exchange, is made here by Secure
  this rig, or is typed on this unit's page.

Who passes without typing it:
- This unit itself: its own screen and the Companion running on it. A
  blackout from the box must never wait for a PIN (Coding Main, 2026-10-18).
- A known browser: one that has entered the PIN on this page. It carries a
  pass cookie (HttpOnly, SameSite=Strict), an HMAC of the PIN under a key
  kept on this unit. Known means this browser, not this address: venues hand
  addresses out again, and an address can be taken on purpose.
- A controller that sends the PIN as Authorization: Bearer (the Slate, the
  Companion connection, another One).

Six digits. No slowing and no lockout (Rob, FINAL, 2026-10-18): a wrong PIN
answers 401 "wrong rig PIN", every time, at once. Every refusal says what to
do in words.

Forgotten PIN: hands on the unit only. Its own screen (the console: the
kiosk, proven by a key made at each start and given only to it), or a browser
on its own hotspot, whose password is printed on the unit. Never from the
venue network.

Standard library only, no network calls: the caller hands in what it knows.
"""

import hashlib
import hmac
import ipaddress
import secrets
import subprocess

DIGITS = 6
COOKIE = "ds_rig"
COOKIE_MAX_AGE = 365 * 86400
HOTSPOT_NET = ipaddress.ip_network("10.42.0.0/24")   # NetworkManager's shared mode

NEEDS = "rig PIN"
FORGOTTEN = ("Forgotten? Reveal it from a device that knows it, or clear it on this unit's "
             "own screen or from its hotspot (the name and password are printed on the unit).")
RELAY_LINE = "Or hold BOOT on the Relay for 10 s just after power-up: the Relay opens again."

_console_key = secrets.token_urlsafe(24)   # made at each start, handed only to the kiosk
_hooks = {}


def configure(get_config, save_config, my_ips, audit=None):
    """Wire to the unit without importing app.py (which imports this)."""
    _hooks.update(get_config=get_config, save_config=save_config, my_ips=my_ips,
                  audit=audit or (lambda *a, **k: None))


def _cfg():
    return _hooks["get_config"]() if _hooks else {}


# -- the PIN itself ---------------------------------------------------------

def pin():
    return str(_cfg().get("cue_token", "") or "")


def is_set():
    """A rig PIN, set through Secure, this page, or a Relay. An old "rig token"
    typed before the PIN existed (no epoch) is only what this unit sends its
    boxes: it must not quietly lock the One on the update that brings the PIN
    (0001, 2026-10-18)."""
    return bool(pin()) and "rig_pin_epoch" in _cfg()


def valid_new(p):
    """A PIN someone types on this unit's page: six digits."""
    p = str(p or "").strip()
    if len(p) != DIGITS or not p.isdigit():
        return f"a Cue PIN is {DIGITS} digits"
    return ""


def make():
    """Six digits, never all one digit and never a run (000000, 123456,
    987654), as the Relay makes them."""
    while True:
        p = "".join(secrets.choice("0123456789") for _ in range(DIGITS))
        d = [int(c) for c in p]
        steps = {d[i + 1] - d[i] for i in range(DIGITS - 1)}
        if steps in ({0}, {1}, {-1}):
            continue
        return p


def set_pin(new, why=""):
    """Replace or clear (new="") the PIN. Every browser's pass is forgotten
    with it: the epoch changes, so no old pass matches."""
    new = str(new or "").strip()
    cfg = _cfg()
    _hooks["save_config"]({"cue_token": new, "rig_pin_epoch": int(cfg.get("rig_pin_epoch") or 0) + 1})
    _hooks["audit"]("RIG_PIN", ("set" if new else "cleared") + (f" ({why})" if why else ""))


# -- who is asking ----------------------------------------------------------

def _addr(a):
    try:
        return ipaddress.ip_address(str(a or "").split("%")[0])
    except ValueError:
        return None


def is_self(remote):
    """This unit asking itself: its kiosk, or the Companion running on it."""
    a = _addr(remote)
    if a is None:
        return False
    if a.is_loopback or (a.version == 6 and a.ipv4_mapped and a.ipv4_mapped.is_loopback):
        return True
    try:
        return str(a) in set(_hooks["my_ips"]() or [])
    except Exception:
        return False


def console_key():
    return _console_key


def is_console(remote, header_key):
    """The unit's own screen: a request from this unit carrying the key the
    kiosk was started with. Loopback alone is not enough: Companion runs on
    this box too, and its admin page can make it send anything to 127.0.0.1."""
    a = _addr(remote)
    if a is None or not (a.is_loopback or (a.version == 6 and a.ipv4_mapped and a.ipv4_mapped.is_loopback)):
        return False
    return bool(header_key) and hmac.compare_digest(str(header_key), _console_key)


def _ifaces():
    """[(iface, ip_interface)] for this unit's IPv4 addresses."""
    out = []
    try:
        txt = subprocess.run(["ip", "-4", "-o", "addr", "show"], capture_output=True,
                             text=True, timeout=3).stdout
        for line in txt.splitlines():
            parts = line.split()
            if len(parts) >= 4:
                out.append((parts[1], ipaddress.ip_interface(parts[3])))
    except Exception:
        pass
    return out


def on_hotspot(remote, hotspot_active):
    """A browser on this unit's own hotspot. Safe to trust because TCP needs
    a way back: a reply to a hotspot address leaves by the hotspot. That
    holds only if no other interface of ours is on the same subnet, so a
    venue network that happens to use 10.42.0.x says no rather than yes."""
    a = _addr(remote)
    if not hotspot_active or a is None or a.version != 4 or a not in HOTSPOT_NET:
        return False
    on_net = [ifc for ifc, i in _ifaces() if i.ip in HOTSPOT_NET]
    return len(on_net) == 1


def _pass_for(p):
    cfg = _cfg()
    key = str(cfg.get("rig_pass_key", "") or "")
    if not key:
        key = secrets.token_hex(32)
        _hooks["save_config"]({"rig_pass_key": key})
    msg = f"{int(cfg.get('rig_pin_epoch') or 0)}:{p}".encode()
    return hmac.new(key.encode(), msg, hashlib.sha256).hexdigest()


def pass_cookie():
    """The value a browser that knows the PIN carries."""
    return _pass_for(pin()) if is_set() else ""


def browser_known(cookie):
    return is_set() and bool(cookie) and hmac.compare_digest(str(cookie), pass_cookie())


def known(remote, cookie="", header_key=""):
    """May reveal and manage: the console, or a browser that has entered the
    PIN on this page before."""
    return is_console(remote, header_key) or browser_known(cookie)


def try_pin(presented):
    """Check one presented PIN: (status, body). No slowing, no counting."""
    if presented and hmac.compare_digest(str(presented), pin()):
        return 200, {"ok": True}
    return 401, {"ok": False, "error": "wrong Cue PIN", "needs": NEEDS}


def _bearer(auth):
    auth = str(auth or "")
    return auth[7:].strip() if auth[:7].lower() == "bearer " else ""


def check(remote, auth="", cookie="", header_key=""):
    """May this request do a locked action? (ok, status, body)."""
    if not is_set() or is_self(remote) or known(remote, cookie, header_key):
        return True, 200, {}
    presented = _bearer(auth)
    if not presented:
        return False, 401, {"ok": False, "error": "this Cue system has a PIN: enter it to do that", "needs": NEEDS}
    status, body = try_pin(presented)
    return status == 200, status, body
