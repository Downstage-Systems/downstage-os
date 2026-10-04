"""A small mDNS browser, standard library only.

Every Downstage box advertises `_downstage._tcp` with its id, kind, name and
firmware in the TXT record, so the One can find the rig by asking rather than
by knocking on 254 doors. Sweeping a /24 is what tripped UniFi's threat
protection on a venue network (Rob, 2026-10-02), and it is slow besides.

Why not avahi-browse: the One runs avahi-daemon, but not avahi-utils - the
daemon picks up service files on its own, so the tools were never installed.
Rather than add an apt dependency to every unit and the golden image, this
speaks mDNS directly. It is about 120 lines of DNS parsing and no dependency.

It asks on port 5353 alongside avahi where the system allows it (SO_REUSEPORT),
and falls back to asking from an ephemeral port with the unicast-response bit
set, which works where the port cannot be shared.
"""

import socket
import struct
import time

GROUP = "224.0.0.251"
PORT = 5353
SERVICE = "_downstage._tcp.local"

# record types we care about
PTR, TXT, SRV, A = 12, 16, 33, 1


def _encode_name(name):
    out = b""
    for part in name.split("."):
        if part:
            out += bytes([len(part)]) + part.encode()
    return out + b"\0"


def _read_name(buf, i):
    """A DNS name, following compression pointers. Returns (name, next index)."""
    parts, jumped, start = [], False, i
    hops = 0
    while True:
        if i >= len(buf):
            break
        n = buf[i]
        if n == 0:
            i += 1
            break
        if n & 0xC0 == 0xC0:                      # a pointer: follow it once
            if i + 1 >= len(buf):
                break
            ptr = struct.unpack("!H", buf[i:i + 2])[0] & 0x3FFF
            if not jumped:
                start = i + 2
            jumped = True
            hops += 1
            if hops > 16 or ptr >= len(buf):      # a loop, or nonsense
                break
            i = ptr
            continue
        i += 1
        parts.append(buf[i:i + n].decode("utf-8", "replace"))
        i += n
    return ".".join(parts), (start if jumped else i)


def _query(service=SERVICE, unicast=False):
    flags = 0
    qclass = 0x8001 if unicast else 0x0001        # 0x8000 = answer me directly
    return (struct.pack("!HHHHHH", 0, flags, 1, 0, 0, 0)
            + _encode_name(service) + struct.pack("!HH", PTR, qclass))


def _parse(buf, want=SERVICE):
    """Pull instances, hosts, ports, addresses and TXT out of one message."""
    try:
        _, _, qd, an, ns, ar = struct.unpack("!HHHHHH", buf[:12])
    except struct.error:
        return {}, {}, {}
    i = 12
    for _ in range(qd):                            # skip the questions
        _, i = _read_name(buf, i)
        i += 4
    svc, srv, addr, txt = [], {}, {}, {}
    for _ in range(an + ns + ar):
        name, i = _read_name(buf, i)
        if i + 10 > len(buf):
            break
        rtype, _rclass, _ttl, rdlen = struct.unpack("!HHIH", buf[i:i + 10])
        i += 10
        rd, i = buf[i:i + rdlen], i + rdlen
        if rtype == PTR and name.rstrip(".") == want:
            inst, _ = _read_name(buf, i - rdlen)
            svc.append(inst)
        elif rtype == SRV and rdlen >= 6:
            port = struct.unpack("!H", rd[4:6])[0]
            host, _ = _read_name(buf, i - rdlen + 6)
            srv[name] = (host, port)
        elif rtype == A and rdlen == 4:
            addr[name] = socket.inet_ntoa(rd)
        elif rtype == TXT:
            kv, j = {}, 0
            while j < len(rd):
                ln = rd[j]
                item = rd[j + 1:j + 1 + ln].decode("utf-8", "replace")
                j += 1 + ln
                if "=" in item:
                    k, v = item.split("=", 1)
                    kv[k] = v
                elif item:
                    kv[item] = ""
            txt[name] = kv
    return {"instances": svc, "srv": srv, "addr": addr, "txt": txt}, srv, addr


def browse(service=SERVICE, timeout=2.0):
    """Ask who is out there. Returns a list of
    {instance, host, ip, port, txt{...}}, one per box that answers."""
    service = service.rstrip(".")
    socks = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "SO_REUSEPORT"):
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        s.bind(("", PORT))
        s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                     struct.pack("4s4s", socket.inet_aton(GROUP), socket.inet_aton("0.0.0.0")))
        unicast = False
        socks.append(s)
    except OSError:
        # avahi has 5353 to itself here: ask from our own port instead and
        # set the bit that says "answer me directly"
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.bind(("", 0))
        unicast = True
        socks.append(s)
    s.settimeout(0.4)
    s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 255)

    q = _query(service, unicast)
    found = {"srv": {}, "addr": {}, "txt": {}, "instances": set()}
    end = time.time() + timeout
    asked = 0
    try:
        while time.time() < end:
            if asked < 3 and (asked == 0 or time.time() > end - timeout + asked * 0.5):
                try:
                    s.sendto(q, (GROUP, PORT))
                except OSError:
                    pass
                asked += 1
            try:
                buf, _ = s.recvfrom(9000)
            except (socket.timeout, OSError):
                continue
            got, _, _ = _parse(buf, service)
            found["instances"].update(got.get("instances", []))
            found["srv"].update(got.get("srv", {}))
            found["addr"].update(got.get("addr", {}))
            found["txt"].update(got.get("txt", {}))
    finally:
        for s in socks:
            try:
                s.close()
            except OSError:
                pass

    out = []
    for inst in sorted(found["instances"]):
        host, port = found["srv"].get(inst, ("", 0))
        ip = found["addr"].get(host, "")
        out.append({"instance": inst, "host": host, "ip": ip, "port": port,
                    "txt": found["txt"].get(inst, {})})
    # a box that answered with an address but no PTR still counts
    for name, (host, port) in found["srv"].items():
        if name not in found["instances"] and name.endswith(service):
            out.append({"instance": name, "host": host, "ip": found["addr"].get(host, ""),
                        "port": port, "txt": found["txt"].get(name, {})})
    return [u for u in out if u["ip"]]
