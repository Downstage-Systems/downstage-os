"""Bluetooth LE onboarding for Downstage Cue lights.

A Cue advertises over BLE whenever it is not on WiFi: out of the box, and
again any time it loses its network. The One scans for that advertisement and
can hand across WiFi credentials and a Companion address, so nobody has to
join a setup hotspot and type an SSID on a phone.

Everything here is best-effort. A One with no Bluetooth adapter, or with the
bleak package missing, simply reports no lights - BLE is an accelerator, not
a requirement. The setup portal on the light remains the universal path.
"""

import asyncio
import threading
import time
from urllib.parse import urlencode

try:
    from bleak import BleakScanner, BleakClient
    BLE_AVAILABLE = True
except Exception:                                   # no bleak, no adapter, no BLE
    BLE_AVAILABLE = False

SVC_UUID  = "44534355-4500-1000-8000-00805f9b34fb"
INFO_UUID = "44534355-4501-1000-8000-00805f9b34fb"
PROV_UUID = "44534355-4502-1000-8000-00805f9b34fb"
CMD_UUID  = "44534355-4503-1000-8000-00805f9b34fb"

SCAN_SECONDS = 5.0

_lock = threading.Lock()
_last = {"at": 0.0, "units": [], "error": ""}
CACHE_SECONDS = 20


def _decode_mfr(data):
    """Scan-response payload: "DS", a flags byte, the camera number."""
    blob = (data or {}).get(0xFFFF)
    if not blob or len(blob) < 4 or blob[0:2] != b"DS":
        return {}
    return {"adopted": bool(blob[2] & 0x01), "camera": blob[3]}


async def _scan(seconds=SCAN_SECONDS):
    found = {}

    def seen(device, adv):
        uuids = [u.lower() for u in (adv.service_uuids or [])]
        name = adv.local_name or device.name or ""
        if SVC_UUID not in uuids and not name.startswith("DSCUE-"):
            return
        unit = {
            "id": name or device.address,
            "address": device.address,
            "rssi": adv.rssi,
            "product": "Cue",
            "via": "ble",
            "adopted": None,
            "camera": None,
        }
        unit.update(_decode_mfr(adv.manufacturer_data))
        found[device.address] = unit

    scanner = BleakScanner(detection_callback=seen)
    await scanner.start()
    await asyncio.sleep(seconds)
    await scanner.stop()
    return sorted(found.values(), key=lambda u: -(u["rssi"] or -999))


def scan(force=False):
    """Cached scan. Returns (units, error)."""
    if not BLE_AVAILABLE:
        return [], "bluetooth not available on this unit"
    with _lock:
        fresh = time.time() - _last["at"] < CACHE_SECONDS
        if fresh and not force:
            return _last["units"], _last["error"]
        try:
            units = asyncio.run(_scan())
            _last.update(at=time.time(), units=units, error="")
        except Exception as e:
            _last.update(at=time.time(), units=[], error=str(e))
        return _last["units"], _last["error"]


def cached():
    """What the last scan saw, without starting one. Returns (units, error)."""
    with _lock:
        return _last["units"], _last["error"]


async def _write(address, uuid, payload, read_back=False):
    async with BleakClient(address, timeout=15.0) as client:
        await client.write_gatt_char(uuid, payload.encode(), response=True)
        if not read_back:
            return ""
        await asyncio.sleep(0.5)
        try:
            return (await client.read_gatt_char(INFO_UUID)).decode()
        except Exception:
            return ""


def adopt(address, ssid, password, host, port=16622, label="", camera=None):
    """Hand a light its network and its Companion. The light saves and reboots."""
    if not BLE_AVAILABLE:
        return False, "bluetooth not available on this unit", ""
    fields = {"ssid": ssid, "pass": password, "host": host, "port": int(port)}
    if label:
        fields["label"] = label
    if camera is not None:
        fields["camera"] = int(camera)
    try:
        info = asyncio.run(_write(address, PROV_UUID, urlencode(fields), read_back=True))
        with _lock:
            _last["at"] = 0.0          # the light is about to leave the air
        return True, "", info
    except Exception as e:
        return False, str(e), ""


def command(address, word):
    """'identify' blinks the light white; 'reboot' restarts it."""
    if not BLE_AVAILABLE:
        return False, "bluetooth not available on this unit"
    try:
        asyncio.run(_write(address, CMD_UUID, word))
        return True, ""
    except Exception as e:
        return False, str(e)


async def _read_info(address):
    async with BleakClient(address, timeout=15.0) as client:
        return (await client.read_gatt_char(INFO_UUID)).decode()


def info(address):
    if not BLE_AVAILABLE:
        return "", "bluetooth not available on this unit"
    try:
        return asyncio.run(_read_info(address)), ""
    except Exception as e:
        return "", str(e)
