#!/usr/bin/env python3
"""Downstage OS first-boot provisioning.

Runs once at boot (via downstage-provision.service). If
/boot/firmware/downstage-provision.json exists — written at flash time by
provision-unit.sh — applies the unit's identity, then consumes the file and
disables the service. With no file present it exits immediately, so this is
inert on bench units and already-provisioned stock.
"""
import json, os, subprocess, sys
from pathlib import Path

PROV = Path("/boot/firmware/downstage-provision.json")

def run(*cmd):
    subprocess.run(cmd, check=False, timeout=30)

def main():
    if not PROV.exists():
        return 0
    p = json.loads(PROV.read_text())
    serial   = p["serial"]
    hostname = p["hostname"]
    print(f"[provision] applying identity for {serial}")

    # hostname (preserve_hostname drop-in ships in the image, so this sticks)
    run("hostnamectl", "set-hostname", hostname)
    Path("/etc/hosts").write_text(
        Path("/etc/hosts").read_text().replace("downstage-unprovisioned", hostname))
    run("systemctl", "restart", "avahi-daemon")

    # app identity
    app = Path("/home/pi/ontime-kiosk")
    if not app.is_dir():
        app = Path("/home/pi/ontime-kiosk-lite")
    cfg_path = app / "config.json"
    cfg = json.loads(cfg_path.read_text()) if cfg_path.exists() else {}
    cfg.update({
        "serial":        serial,
        "hotspot_ssid":  p["hotspot_ssid"],
        "hotspot_pass":  p["hotspot_pass"],
    })
    cfg_path.write_text(json.dumps(cfg))
    os.system(f"chown pi:pi {cfg_path}")

    # consume the provisioning file and retire the service
    PROV.unlink()
    run("systemctl", "disable", "downstage-provision.service")
    print(f"[provision] {serial} provisioned as {hostname}")
    return 0

if __name__ == "__main__":
    sys.exit(main())
