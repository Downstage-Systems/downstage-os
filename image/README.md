# Building sellable Downstage units

## One-time: capture the golden image (per product)

1. Get a bench unit (One or View) to the software state you want to ship.
2. On the Pi: `bash prepare-golden.sh` — strips owner WiFi, logs, SSH
   identity, remote-access keys, OnTime data; resets config to factory.
3. `sudo poweroff`, pull the SD card.
4. On the Mac, image it (Disk Utility or `dd`), then shrink with PiShrink:
   `sudo pishrink.sh downstage-one-golden.img` — this also makes the image
   auto-expand to fill any card on first boot.
5. Archive as `downstage-one-golden-vX.Y.Z.img.gz` (or `-view-`).

The provisioning service (`downstage-provision.service` +
`firstboot-provision.py`) must be installed in the image beforehand:

    sudo cp firstboot-provision.py /usr/local/sbin/
    sudo cp downstage-provision.service /etc/systemd/system/
    sudo systemctl enable downstage-provision.service

It is inert unless a provisioning file exists, so it is safe on bench units.

## Per unit: flash + provision (minutes per unit)

1. Add the unit to `downstage-build-log.csv` (serial, hotspot ssid/pass).
2. Flash the golden image with Raspberry Pi Imager (no customization —
   identity comes from provisioning, not the imager).
3. With the card still mounted:
   `./provision-unit.sh DS1-A-2607-0002`
   (writes `downstage-provision.json` to the boot partition from the log)
4. Boot the unit once. It renames itself, applies its hotspot identity,
   consumes the file, and disables the provisioning service.
5. Verify: `http://downstage-0002.local:8080` — check hostname, hotspot
   SSID in the WiFi panel, serial in the Downstage OS panel.
6. Fill in the build log: MAC, storage serial, burn-in pass, date.

## What ships in every unit

- Password `pi` / `downstage` (documented appliance default)
- Hotspot fallback with per-unit SSID (auto-starts when no network)
- Self-updating Downstage OS pointed at downstage-systems/downstage-os
- Ontime GPL v3 credit (legal requirement — do not remove)
