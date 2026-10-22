"""Pairing with Cue Relays, and holding the token each one gives back.

Rob's rule for the ecosystem (2026-10-04): "make sure the One has a direct
handshake with the Relay when plugged in". So this unit does the asking - the
Relay never calls us - and it keeps asking quietly until a Relay says yes,
because the owner's part should be plugging a cable in.

The contract is the Cue repo's docs/companion-integration.md, "The One and
the Relay: pairing":

- POST /api/v1/pair, flat JSON, with this unit's serial as `id`.
- 200 {ok, id, name, fw, token}: the Relay is ours; the token is Bearer for
  its API and page routes from then on.
- 409: another One holds it. Its own page offers "Switch to it", which lets
  our next ask through for two minutes - so we keep asking.
- Every ask also refreshes the Relay's idea of where our Companion and timer
  feed are. Each pairing records the address we handed over as `told`, and
  every pass compares it against our address toward that Relay: if this unit
  has moved networks, the Relay is re-told within about five seconds rather
  than at the next five-minute refresh.

Standard library only, and every network call is short: this runs on its own
thread and must never be able to hold up a page.
"""

import json
import threading
import time
import urllib.error
import urllib.request

ASK_EVERY = 30.0          # while unpaired or refused
REFRESH_EVERY = 300.0     # a paired Relay, to keep its idea of us current
TIMEOUT = 4.0

_lock = threading.RLock()
_seen = {}                # relay id -> what we know right now
_hooks = {}
_wake = threading.Event()  # "stop waiting, go round again"
_force_after = 0.0         # anything told before this moment is out of date


def configure(get_config, save_config, my_ip_toward, audit=None, rig_pin=None, take_pin=None):
    """Wire this module to the unit without importing it (app.py is the One).
    rig_pin() is this unit's rig PIN ("" when open); take_pin(pin, rid, name)
    makes a secured Relay's PIN this unit's own (docs/control-token.md)."""
    _hooks.update(get_config=get_config, save_config=save_config,
                  my_ip_toward=my_ip_toward, audit=audit or (lambda *a, **k: None),
                  rig_pin=rig_pin or (lambda: ""), take_pin=take_pin or (lambda *a: None))


def _cfg():
    return _hooks["get_config"]() if _hooks else {}


def _me_toward(ip):
    """This unit's address as that Relay sees it, or "" if we cannot say."""
    try:
        return _hooks["my_ip_toward"](ip) or ""
    except Exception:
        return ""


def _paired():
    """{relay id: {token, name, fw, at}} - what this unit has been given."""
    return dict(_cfg().get("relays") or {})


def _remember(rid, **fields):
    with _lock:
        relays = _paired()
        entry = dict(relays.get(rid) or {})
        entry.update(fields)
        relays[rid] = entry
        _hooks["save_config"]({"relays": relays})


def forget(rid):
    """Drop a Relay's token. The Relay keeps thinking it is ours until it is
    told otherwise, so callers should try unpair() first."""
    with _lock:
        relays = _paired()
        if rid in relays:
            del relays[rid]
            _hooks["save_config"]({"relays": relays})
            _hooks["audit"]("RELAY_FORGET", rid)
            return True
    return False


def _post(ip, path, body, token=""):
    data = json.dumps(body).encode()
    req = urllib.request.Request(f"http://{ip}{path}", data=data, method="POST",
                                 headers={"Content-Type": "application/json"})
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode() or "{}")
        except Exception:
            return e.code, {}
    except Exception as e:
        return 0, {"error": str(e)[:120]}


def pair(ip, rid=""):
    """Ask one Relay to pair. Returns what we now know about it."""
    cfg = _cfg()
    serial = str(cfg.get("serial", "") or "")
    if not serial:
        return {"ok": False, "error": "this unit has no serial yet"}
    me = _me_toward(ip)
    # the name the Relay shows its owner. config has no "hostname" key on a
    # real unit - the operator's label is unit_name - and sending nothing made
    # the Relay fall back to the bare serial (bench, 2026-10-04).
    import socket as _socket
    name = (cfg.get("unit_name") or cfg.get("hostname") or _socket.gethostname() or "").strip()
    body = {"id": serial, "name": name}
    if me:
        # where this Relay should look for our Companion and our timer feed.
        # The address depends on which way it is reaching us: a One on two
        # networks must not hand out the wrong one.
        body.update(companionHost=me, companionPort=16622, timerHost=me, timerPort=16640)
    known = _paired().get(rid or "", {})
    # Our pair token once we have one. Before that, a secured Relay (0.137)
    # pairs a new One only with the rig PIN, so send ours if we have one.
    # Not to a Relay that was opened: it cleared that PIN on purpose.
    bearer = known.get("token", "") or ("" if known.get("opened") else _hooks["rig_pin"]())
    status, ans = _post(ip, "/api/v1/pair", body, bearer)

    now = time.time()
    if status == 200 and ans.get("ok") and ans.get("token"):
        got = str(ans.get("id") or rid or "")
        # `told` is the address this Relay is now holding for us. The tick
        # compares it against our address toward that Relay, which is how a
        # network change gets noticed without anything having to announce it.
        fields = dict(token=ans["token"], name=ans.get("name", ""), fw=ans.get("fw", ""),
                      ip=ip, at=now, told=me)
        # The rig PIN rides the pair answer (Relay 0.137). Secured: it is this
        # unit's PIN too, so one tap on the Relay secures the whole rig.
        # Opened (BOOT on the Relay, or Unsecure there): stop sending it to
        # that Relay and say so, but keep this unit's own PIN until it is
        # cleared at this unit (Coding Main's rule, 2026-10-18). A Relay
        # older than 0.137 says neither, and nothing changes.
        sec = ans.get("rigSecured")
        if sec is True and ans.get("rigToken"):
            fields.update(secured=True, opened=False)
            _hooks["take_pin"](str(ans["rigToken"]), got, ans.get("name", ""))
        elif sec is False:
            if known.get("secured"):
                fields["opened"] = True
                _hooks["audit"]("RELAY_OPENED", f"{got}: its rig PIN was cleared; this One keeps its own")
            fields["secured"] = False
        _remember(got, **fields)
        if not known:
            _hooks["audit"]("RELAY_PAIR", f"{got} at {ip}")
        return {"ok": True, "id": got, "name": ans.get("name", ""), "fw": ans.get("fw", ""),
                "ip": ip, "paired": True}
    if status == 429:
        # this Relay is slowing us for wrong PINs: wait as long as it says
        wait = int(ans.get("retryAfter") or 30)
        return {"ok": False, "id": rid, "ip": ip, "paired": bool(known.get("token")), "retry_after": wait,
                "error": ans.get("error") or f"the Relay is slowing this unit: try again in {wait} s"}
    if status == 409 and (ans.get("needs") == "rig PIN" or "has a PIN" in str(ans.get("error", ""))):
        return {"ok": False, "id": rid, "ip": ip, "paired": False, "needs_pin": True,
                "error": "this Relay has a Cue PIN: enter the same Cue PIN on this One, "
                         "or pair from the Relay's page"}
    if status == 409:
        other = ans.get("paired") or {}
        return {"ok": False, "id": rid, "ip": ip, "paired": False, "taken": True,
                "by": {"id": other.get("id", ""), "name": other.get("name", ""),
                       "found": bool(other.get("found")), "ip": other.get("ip", "")},
                "error": "another One holds this Relay - tap Switch to it on the Relay's page"}
    if not status:
        # the socket never got there: say so plainly, and keep urllib's own
        # wording out of an operator's face
        return {"ok": False, "id": rid, "ip": ip, "paired": False, "offline": True,
                "error": f"no answer from the Relay at {ip}"}
    return {"ok": False, "id": rid, "ip": ip, "paired": False,
            "error": ans.get("error") or f"the Relay answered {status}"}


def unpair(rid):
    """Tell a Relay it is no longer ours, then forget its token either way -
    a Relay we cannot reach must not keep a token we have thrown away."""
    entry = _paired().get(rid) or {}
    ip, token = entry.get("ip", ""), entry.get("token", "")
    told = False
    if ip and token:
        status, _ = _post(ip, "/api/v1/pair", {"unpair": True}, token)
        told = status == 200
    forget(rid)
    return {"ok": True, "told": told}


def token_for(rid):
    return (_paired().get(rid) or {}).get("token", "")


def opened_ip(ip):
    """True when the Relay at that address had its rig PIN cleared: this unit
    stops sending its PIN there."""
    for entry in _paired().values():
        if entry.get("ip") == ip:
            return bool(entry.get("opened"))
    return False


def secured_relays():
    """[(id, ip, token)] for paired Relays that hold the rig PIN."""
    return [(rid, e.get("ip", ""), e.get("token", "")) for rid, e in _paired().items()
            if e.get("secured") and e.get("ip")]


def paired_relays():
    """[(id, ip, token, name)] for every Relay this unit is paired with."""
    return [(rid, e.get("ip", ""), e.get("token", ""), e.get("name", "")) for rid, e in _paired().items()
            if e.get("token") and e.get("ip")]


def mark(rid, **fields):
    """Record what Secure / Unsecure through this Relay changed."""
    if rid in _paired():
        _remember(rid, **fields)


def token_for_ip(ip):
    for entry in _paired().values():
        if entry.get("ip") == ip:
            return entry.get("token", "")
    return ""


def note_found(relays):
    """What mDNS heard this round: [{id, name, ip, fw}] with kind=relay."""
    now = time.time()
    with _lock:
        for r in relays:
            rid = r.get("id") or ""
            if not rid:
                continue
            prev = _seen.get(rid, {})
            _seen[rid] = {**prev, **r, "seen": now}


def state():
    """For the fleet page: every Relay we know of, paired or not."""
    now = time.time()
    paired = _paired()
    out = []
    with _lock:
        ids = set(_seen) | set(paired)
        for rid in sorted(ids):
            s = _seen.get(rid, {})
            p = paired.get(rid, {})
            seen = s.get("seen")
            out.append({
                "id": rid,
                "name": s.get("name") or p.get("name", ""),
                "fw": s.get("fw") or p.get("fw", ""),
                "ip": s.get("ip") or p.get("ip", ""),
                "paired": bool(p.get("token")),
                "found": bool(seen and now - seen < 120),
                "last_seen": round(now - seen, 1) if seen else None,
                "taken_by": s.get("taken_by") or None,
                "why": s.get("why", ""),
                "secured": bool(p.get("secured")),
                "opened": bool(p.get("opened")),
                "needs_pin": bool(s.get("needs_pin")),
                "retry_in": max(0, int(float(s.get("retry_at") or 0) - now)),
            })
    return {"relays": out}


def _candidates(paired):
    """Every Relay worth asking: what mDNS heard, plus anything we hold a
    token for. A Relay we cannot currently hear is still ours - and after a
    network change it is exactly the one that needs telling, which is when
    mDNS is least likely to have caught up (bench, 2026-10-16: the One had
    not heard its Relay in 38 minutes while the Relay sat there answering)."""
    out = {}
    with _lock:
        for rid, s in _seen.items():
            if s.get("ip"):
                out[rid] = dict(s)
    with _lock:
        for rid, entry in paired.items():
            if entry.get("ip") and rid not in out:
                out[rid] = {**(_seen.get(rid) or {}), "ip": entry["ip"]}
    return list(out.items())


def _tick():
    """One pass: pair anything unpaired, refresh anything paired and stale,
    and re-tell at once any Relay holding an address we no longer have."""
    now = time.time()
    paired = _paired()
    for rid, s in _candidates(paired):
        ip = s.get("ip", "")
        if not ip:
            continue
        entry = paired.get(rid, {})
        # The whole point: our address toward this Relay, every pass. If it
        # is not what the Relay was told, it is pointing at a One that is no
        # longer there and nothing else will correct it for five minutes.
        mine = _me_toward(ip)
        moved = bool(entry.get("token")) and mine and mine != entry.get("told", "")
        at = float(entry.get("at") or 0)
        if entry.get("token") and not moved and now - at < REFRESH_EVERY and at >= _force_after:
            continue
        if now < float(s.get("retry_at") or 0):
            continue     # it asked us to wait (429): never earlier, not even on news
        asked = float(s.get("asked") or 0)
        if not entry and now - asked < ASK_EVERY and asked >= _force_after:
            continue
        # A Relay that is not answering gets the same patience as an unpaired
        # one. Without this, a One carrying a token for a Relay left at the
        # shop would POST at it every five seconds for the length of a show.
        if entry and not s.get("last_ok") and now - asked < ASK_EVERY and asked >= _force_after:
            continue
        if moved:
            print(f"[relay] {rid} holds {entry.get('told') or 'nothing'}; we are {mine} - telling it now",
                  flush=True)
        res = pair(ip, rid)
        with _lock:
            cur = _seen.setdefault(rid, {})
            cur["asked"] = now
            cur["last_ok"] = bool(res.get("ok"))
            cur["retry_at"] = now + res["retry_after"] if res.get("retry_after") else 0
            cur["needs_pin"] = bool(res.get("needs_pin"))
            cur["taken_by"] = res.get("by") if res.get("taken") else None
            cur["why"] = "" if res.get("ok") else res.get("error", "")


def address_changed(ip=""):
    """This unit has landed somewhere new. Every Relay we know is holding the
    old address, so drop the refresh timers and go round again now instead of
    at the next five-minute refresh (Rob, 2026-10-16: "shouldnt the one be
    able to send the new companion info to the relay")."""
    global _force_after
    _force_after = time.time()
    _wake.set()
    _hooks.get("audit", lambda *a, **k: None)("RELAY_READDRESS", ip or "")


def _tick_soon():
    """Wait up to five seconds, or until somebody has news."""
    _wake.wait(5)
    _wake.clear()


def start():
    def run():
        time.sleep(20)          # let the network and the first browse settle
        while True:
            try:
                _tick()
            except Exception as e:
                print(f"[relay] {e}", flush=True)
            _tick_soon()
    threading.Thread(target=run, daemon=True).start()
