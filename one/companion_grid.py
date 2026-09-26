"""A live picture of one Companion page, for choosing a light's button.

Rob, 2026-09-26: "choose the page offset and button in Companion with a
visual grid from the One". Companion's Satellite API (1.10+, Companion 4.3+)
lets a client follow any button by its absolute location - ADD-SUB
LOCATION=page/row/col - without registering a surface. The One subscribes to
every button of the page being looked at, keeps what Companion streams (the
button's PNG, colours and text), and the page polls for what changed.

The Subscriptions API is off by default in Companion: Settings > Satellite >
"Enable Button Subscriptions API". Toggling it drops every Satellite link for
a moment. A Companion without it answers "Subscriptions not enabled", which
is passed on to the page as it is.

One session per Companion (host, port); it closes itself after a quiet
spell with nobody polling. Rows and columns are zero-based, pages one-based.
"""

import base64
import socket
import threading
import time

_IDLE_CLOSE_S = 45       # nobody has polled for this long: close
_PING_EVERY_S = 2        # the Satellite API asks for a ping every couple of seconds
_BITMAP_PX = 72

_sessions = {}
_lock = threading.Lock()


def _parse(line):
    """COMMAND KEY=VALUE KEY="quoted value" -> (command, {key: value})."""
    line = line.strip()
    if not line:
        return "", {}
    cmd, _, rest = line.partition(" ")
    args, i, n = {}, 0, len(rest)
    while i < n:
        while i < n and rest[i] == " ":
            i += 1
        if i >= n:
            break
        eq = rest.find("=", i)
        sp = rest.find(" ", i)
        if eq < 0 or (0 <= sp < eq):   # a bare flag
            end = n if sp < 0 else sp
            args[rest[i:end]] = True
            i = end
            continue
        key = rest[i:eq]
        i = eq + 1
        if i < n and rest[i] == '"':
            j = i + 1
            buf = []
            while j < n and rest[j] != '"':
                if rest[j] == "\\" and j + 1 < n:
                    j += 1
                buf.append(rest[j])
                j += 1
            args[key] = "".join(buf)
            i = j + 1
        else:
            end = n if rest.find(" ", i) < 0 else rest.find(" ", i)
            args[key] = rest[i:end]
            i = end
    return cmd, args


def _b64text(v):
    try:
        return base64.b64decode(v).decode("utf-8", "replace")
    except Exception:
        return ""


class _Session:
    def __init__(self, host, port):
        self.host, self.port = host, port
        self.sock = None
        self.api = ""
        self.version = ""
        self.subs_ok = None      # CAPS SUBSCRIPTIONS, once known
        self.formats = []
        self.error = ""
        self.page = 0
        self.rows = self.cols = 0
        self.buttons = {}        # (row, col) -> dict, each with its "rev"
        self.rev = 0
        self.polled = time.time()
        self.alive = True
        self.cv = threading.Condition()
        threading.Thread(target=self._run, daemon=True).start()

    # -- the link ---------------------------------------------------------
    def _send(self, line):
        s = self.sock
        if s:
            try:
                s.sendall((line + "\n").encode())
            except OSError:
                pass

    def _run(self):
        try:
            self.sock = socket.create_connection((self.host, self.port), timeout=4)
            self.sock.settimeout(1.0)
        except OSError as e:
            self.error = f"Could not reach Companion at {self.host}:{self.port} ({e.strerror or e})"
            self.alive = False
            return
        buf = b""
        last_ping = 0
        while self.alive:
            if time.time() - self.polled > _IDLE_CLOSE_S:
                break
            if time.time() - last_ping > _PING_EVERY_S:
                last_ping = time.time()
                self._send("PING grid")
            try:
                chunk = self.sock.recv(65536)
                if not chunk:
                    self.error = self.error or "Companion closed the connection"
                    break
                buf += chunk
            except socket.timeout:
                continue
            except OSError:
                self.error = self.error or "The connection to Companion dropped"
                break
            while b"\n" in buf:
                raw, buf = buf.split(b"\n", 1)
                self._line(raw.decode("utf-8", "replace"))
        self.alive = False
        try:
            self.sock.close()
        except OSError:
            pass

    def _line(self, line):
        cmd, a = _parse(line)
        with self.cv:
            if cmd == "BEGIN":
                self.api = str(a.get("ApiVersion", ""))
                self.version = str(a.get("CompanionVersion", ""))
            elif cmd == "CAPS":
                self.subs_ok = str(a.get("SUBSCRIPTIONS", "0")).lower() in ("1", "true")
                self.formats = str(a.get("BITMAP_FORMATS", "")).split(",")
                if self.subs_ok is False:
                    self.error = "subscriptions-off"
                elif self.page:
                    self._subscribe()
            elif cmd == "ADD-SUB" and "ERROR" in a:
                msg = str(a.get("MESSAGE", "error"))
                self.error = "subscriptions-off" if "not enabled" in msg.lower() else msg
            elif cmd == "SUB-STATE":
                sid = str(a.get("SUBID", ""))
                # g<page>/<row>/<col>: a state for a page we have moved on from is dropped
                try:
                    p, r, c = (int(x) for x in sid[1:].split("/"))
                except ValueError:
                    return
                if p != self.page:
                    return
                self.rev += 1
                b = {"row": r, "col": c, "rev": self.rev,
                     "type": str(a.get("TYPE", "BUTTON")),
                     "pressed": str(a.get("PRESSED", "")).lower() == "true",
                     "color": str(a.get("COLOR", "")), "textcolor": str(a.get("TEXTCOLOR", "")),
                     "text": _b64text(a["TEXT"]) if "TEXT" in a else ""}
                if "BITMAP" in a:
                    img = str(a["BITMAP"])
                    # a PNG comes as a whole data: URL (Companion 5.0); raw RGB as bare base64
                    if img.startswith("data:"):
                        b["fmt"] = "png"
                        img = img.split(",", 1)[1] if "," in img else ""
                    else:
                        b["fmt"] = self.fmt
                    b["img"] = img
                self.buttons[(r, c)] = b
                self.cv.notify_all()

    # -- the page being looked at -------------------------------------------
    @property
    def fmt(self):
        return "png" if "png" in self.formats else "rgb"

    def _subscribe(self):
        extra = " BITMAP_FORMAT=png" if "png" in self.formats else ""
        for r in range(self.rows):
            for c in range(self.cols):
                self._send(f"ADD-SUB SUBID=g{self.page}/{r}/{c} LOCATION={self.page}/{r}/{c} "
                           f"BITMAP={_BITMAP_PX}{extra} COLORS=hex TEXT=true")

    def show(self, page, rows, cols):
        with self.cv:
            if (page, rows, cols) == (self.page, self.rows, self.cols):
                return
            if self.page and self.subs_ok:
                for r in range(self.rows):
                    for c in range(self.cols):
                        self._send(f"REMOVE-SUB SUBID=g{self.page}/{r}/{c}")
            self.page, self.rows, self.cols = page, rows, cols
            self.buttons = {}
            self.rev += 1
            if self.subs_ok:   # otherwise CAPS has not come yet: it subscribes then
                self._subscribe()

    def since(self, rev):
        return [b for b in self.buttons.values() if b["rev"] > rev]


def grid(host, port, page, rows, cols, since=0, wait=0.0):
    """What the page needs: the buttons changed since `since` (all of them for 0)."""
    key = (host, int(port))
    with _lock:
        s = _sessions.get(key)
        if s is None or not s.alive:
            s = _Session(host, int(port))
            _sessions[key] = s
        for k, other in list(_sessions.items()):   # tidy away the ones nobody polls
            if other is not s and (not other.alive or time.time() - other.polled > _IDLE_CLOSE_S):
                other.alive = False
                _sessions.pop(k, None)
    s.polled = time.time()
    fresh = page != s.page or rows != s.rows or cols != s.cols
    s.show(page, rows, cols)
    if fresh:
        since = 0
    if wait:   # the first look: give Companion a moment to send the page
        end = time.time() + wait
        with s.cv:
            while time.time() < end and len(s.buttons) < rows * cols and s.alive and not s.error:
                s.cv.wait(end - time.time())
    with s.cv:
        return {"ok": s.alive and not s.error, "error": s.error,
                "companion": s.version, "api": s.api,
                "page": s.page, "rows": s.rows, "cols": s.cols, "rev": s.rev,
                "full": since == 0, "buttons": s.since(since)}
