"""A small ATEM client: where every aux output is routed, and changing it.

Rob, 2026-10-21: "i want a router style touch interface that shows where all
ATEM aux outputs are routed and the ability to change those easily." The
One's touch page draws it; this keeps the switcher's state.

Standard library only, like mdns.py: the ATEM speaks a small UDP protocol on
port 9910, and what a router needs from it is a handful of commands, so this
speaks it directly rather than adding a package to every unit and the golden
image. The protocol is as the open atem-connection library (NRK, MIT) and
Blackmagic's own clients use it:

- every packet starts with 12 bytes: flags (5 bits) and length (11 bits),
  the session id, the id of a packet being acknowledged, and this packet's
  own id. Flags: 0x01 wants an ack, 0x02 new session (the hello and its
  answer), 0x04 a resend, 0x08 asks for a resend, 0x10 an ack.
- a packet that wants an ack carries commands: a 2-byte length (header
  included), 2 spare bytes, a 4-letter name, then its data.
- the switcher sends its whole state on connect, then changes as they
  happen, and drops a client that stops acking.

What is read: _pin (the model), InPr (every source's names and where it may
go), AuxS (each aux's source), PrgI / PrvI (program and preview, for tally on
the source keys). What is sent: CAuS (put a source on an aux).

Nothing here blocks the caller: a thread keeps the connection and the state;
the app reads state() and calls route().
"""

import random
import socket
import struct
import threading
import time

PORT = 9910

ACK_REQUEST, NEW_SESSION, RESEND, RESEND_REQUEST, ACK_REPLY = 0x01, 0x02, 0x04, 0x08, 0x10
MAX_ID = 1 << 15                      # packet ids wrap at 15 bits
HELLO = bytes([0x10, 0x14, 0x53, 0xAB, 0x00, 0x00, 0x00, 0x00, 0x00, 0x3A, 0x00, 0x00,
               0x01, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00])
SILENT_AFTER = 5.0                    # the switcher pings far more often than this
AUX_OK = 0x01                         # InPr availability: the source may go to an aux


def _cstr(b):
    return b.split(b"\x00", 1)[0].decode("utf-8", "replace").strip()


class Atem:
    def __init__(self, log=print):
        self._log = log
        self._lock = threading.Lock()
        self._ip = ""
        self._want = threading.Event()
        self._thread = None
        self._sock = None
        self._reset()

    def _reset(self):
        self._session = 0
        self._next_id = 1
        self._last_rx = 0
        self._heard = 0.0
        self._ready = False           # the first state dump is in (InCm)
        self._up = False
        self.product = ""
        self.inputs = {}              # id -> {"long", "short", "avail"}
        self.aux = {}                 # aux index (0-based) -> source id
        self.program = {}             # M/E -> source id
        self.preview = {}
        self._pending = {}            # packet id -> (bytes, sent at, tries)
        self.why = ""

    # ── what the app sees ──
    def state(self):
        with self._lock:
            return {
                "ip": self._ip, "connected": self._up and self._ready, "why": self.why,
                "product": self.product,
                "auxes": [{"n": a + 1, "source": s,
                           "name": (self.inputs.get(8001 + a) or {}).get("long") or f"Aux {a + 1}"}
                          for a, s in sorted(self.aux.items())],
                "sources": [{"id": i, "long": v["long"], "short": v["short"],
                             # an old switcher that does not say where a source may go: offer it
                             "aux": bool(v["avail"] & AUX_OK) or v["avail"] == 0}
                            for i, v in sorted(self.inputs.items())],
                "program": sorted(set(self.program.values())),
                "preview": sorted(set(self.preview.values())),
            }

    def set_ip(self, ip):
        """Follow this switcher, or none with ""."""
        ip = (ip or "").strip()
        with self._lock:
            if ip == self._ip:
                return
            self._ip = ip
        self._close()
        if ip:
            self._want.set()
            if not self._thread or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._run, name="atem", daemon=True)
                self._thread.start()
        else:
            self._want.clear()

    def route(self, aux, source):
        """Put `source` on aux `aux` (1-based). False when not connected."""
        with self._lock:
            if not (self._up and self._ready):
                return False
        data = struct.pack("!BBH", 0x01, int(aux) - 1, int(source))
        self._send_commands([("CAuS", data)])
        return True

    # ── the connection ──
    def _close(self):
        s, self._sock = self._sock, None
        if s:
            try:
                s.close()
            except OSError:
                pass
        with self._lock:
            ip = self._ip
            self._reset()
            self._ip = ip

    def _run(self):
        while True:
            if not self._want.wait(timeout=5):
                continue
            ip = self._ip
            if not ip:
                continue
            try:
                self._session_loop(ip)
            except Exception as e:     # keep the thread; say why on the page
                with self._lock:
                    self.why = f"lost the switcher ({str(e)[:60]})"
            self._close()
            time.sleep(2)

    def _session_loop(self, ip):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.bind(("", 0))
        s.settimeout(0.25)
        self._sock = s
        with self._lock:
            self.why = "asking the switcher"
        s.sendto(HELLO, (ip, PORT))
        hello_at = time.time()
        while self._ip == ip and self._sock is s:
            try:
                pkt, _ = s.recvfrom(65535)
            except socket.timeout:
                pkt = None
            except OSError:
                return
            now = time.time()
            if pkt:
                self._heard = now
                self._receive(pkt, ip)
            elif not self._up and now - hello_at > 1.0:
                s.sendto(HELLO, (ip, PORT))       # no answer yet: ask again
                hello_at = now
                with self._lock:
                    self.why = f"no answer from {ip}:{PORT}"
            if self._up and not self._ready and self.aux and now - hello_at > 3.0:
                with self._lock:              # an older switcher that never says InCm
                    self._ready, self.why = True, ""
            if self._up and now - self._heard > SILENT_AFTER:
                with self._lock:
                    self.why = "the switcher went quiet"
                return
            self._resend(now, ip)

    def _receive(self, pkt, ip):
        if len(pkt) < 12:
            return
        flags = pkt[0] >> 3
        self._session = struct.unpack("!H", pkt[2:4])[0]
        remote = struct.unpack("!H", pkt[10:12])[0]
        if flags & NEW_SESSION:
            self._up = True
            self._last_rx = remote
            self._ack(remote, ip)
            with self._lock:
                self.why = "reading the switcher"
            return
        if flags & ACK_REPLY:
            acked = struct.unpack("!H", pkt[4:6])[0]
            for pid in list(self._pending):
                # everything up to the acked id has landed (ids wrap at 15 bits)
                if (acked - pid) % MAX_ID < MAX_ID // 2:
                    self._pending.pop(pid, None)
        if flags & ACK_REQUEST:
            if remote == (self._last_rx + 1) % MAX_ID:
                self._last_rx = remote
                self._ack(remote, ip)
                if len(pkt) > 12:
                    self._commands(pkt[12:])
            elif (self._last_rx - remote) % MAX_ID < MAX_ID // 2:
                self._ack(self._last_rx, ip)       # a resend of one we have: ack again
            # ahead of us: wait, the switcher resends what we missed

    def _ack(self, pid, ip):
        b = struct.pack("!HHHHHH", (ACK_REPLY << 11) | 12, self._session, pid, 0, 0, 0)
        try:
            self._sock.sendto(b, (ip, PORT))
        except (OSError, AttributeError):
            pass

    def _send_commands(self, cmds):
        body = b"".join(struct.pack("!HH4s", 8 + len(d), 0, n.encode()) + d for n, d in cmds)
        pid = self._next_id
        self._next_id = (self._next_id + 1) % MAX_ID
        pkt = struct.pack("!HHHHHH", (ACK_REQUEST << 11) | (12 + len(body)), self._session, 0, 0, 0, pid) + body
        self._pending[pid] = (pkt, time.time(), 0)
        try:
            self._sock.sendto(pkt, (self._ip, PORT))
        except (OSError, AttributeError):
            pass

    def _resend(self, now, ip):
        for pid, (pkt, at, tries) in list(self._pending.items()):
            if now - at < 0.3:
                continue
            if tries >= 5:
                self._pending.pop(pid, None)
                continue
            again = bytes([pkt[0] | (RESEND << 3)]) + pkt[1:]
            try:
                self._sock.sendto(again, (ip, PORT))
            except (OSError, AttributeError):
                pass
            self._pending[pid] = (pkt, now, tries + 1)

    def _commands(self, buf):
        i = 0
        with self._lock:
            while i + 8 <= len(buf):
                ln = struct.unpack("!H", buf[i:i + 2])[0]
                if ln < 8 or i + ln > len(buf):
                    break
                name, d = buf[i + 4:i + 8].decode("latin-1"), buf[i + 8:i + ln]
                i += ln
                try:
                    self._command(name, d)
                except (struct.error, IndexError):
                    pass

    def _command(self, name, d):
        if name == "_pin":
            self.product = _cstr(d[:44])
        elif name == "InPr" and len(d) >= 26:
            sid = struct.unpack("!H", d[0:2])[0]
            self.inputs[sid] = {"long": _cstr(d[2:22]), "short": _cstr(d[22:26]),
                                "avail": d[34] if len(d) > 34 else 0}
        elif name == "AuxS" and len(d) >= 4:
            self.aux[d[0]] = struct.unpack("!H", d[2:4])[0]
        elif name == "PrgI" and len(d) >= 4:
            self.program[d[0]] = struct.unpack("!H", d[2:4])[0]
        elif name == "PrvI" and len(d) >= 4:
            self.preview[d[0]] = struct.unpack("!H", d[2:4])[0]
        elif name == "InCm":
            self._ready = True
            self.why = ""


def find(timeout=1.5):
    """Switchers that say so on mDNS (_blackmagic._tcp, class AtemSwitcher)."""
    try:
        import mdns
        out = []
        for d in mdns.browse("_blackmagic._tcp.local", timeout=timeout):
            txt = {k.lower(): v for k, v in (d.get("txt") or {}).items()}
            if "atem" in (txt.get("class", "") + txt.get("name", "") + d.get("instance", "")).lower() and d.get("ip"):
                out.append({"ip": d["ip"], "name": txt.get("name") or d.get("instance", "").split("._")[0]})
        return out
    except Exception:
        return []
