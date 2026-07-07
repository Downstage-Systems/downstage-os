# Building sellable Downstage units

## Storage per product (affects capture + flash)

| Product | Boot storage | Capture/flash path |
|---|---|---|
| Downstage One | NVMe SSD (M.2 HAT) | M.2-to-USB adapter on the Mac; Raspberry Pi Imager writes NVMe drives fine |
| Downstage View | microSD | normal SD card workflow |

NVMe notes (One): fresh Pi 5 boards may need the bootloader set to try NVMe
first — `sudo rpi-eeprom-config --edit` and set `BOOT_ORDER=0xf416` before
first NVMe boot. NVMe is also far more resilient to power cuts than SD
(controller-level wear leveling and power-loss handling) — the SD-corruption
concern applies mainly to the View.

View notes: golden card is small (8GB class) — keep the image lean so it
shrinks/restores fast; PiShrink makes it auto-expand onto larger cards.

View boot config (already on the bench unit; verify it survives capture):
- `/boot/firmware/config.txt`: `dtoverlay=vc4-kms-v3d,cma-128` and
  `gpu_mem=16` — NOT fkms/gpu_mem=128, which starved the 512MB board into
  swap thrash (~120MB reclaimed). Legacy `hdmi_group/mode` lines are
  commented; they do nothing under full KMS.
- `/boot/firmware/cmdline.txt`: ends with `video=HDMI-A-1:1280x720@60D`
  (forced digital output for fbcon).
- `~/.xinitrc`: injects a 1280x720 modeline via xrandr before setting it —
  the View is 720p only (TV upscales, the Zero 2 W saves RAM/CPU), and
  EDID-less display chains (adapters, walls, TVs in standby at boot)
  advertise no modes under KMS and would land on 1024x768.

## One-time: capture the golden image (per product)

1. Get a bench unit (One or View) to the software state you want to ship.
2. On the Pi: `bash prepare-golden.sh` — strips owner WiFi, logs, SSH
   identity, remote-access keys, OnTime data; resets config to factory.
3. `sudo poweroff`, pull the SD card.
4. On the Mac, image it (Disk Utility or `dd`), then shrink with PiShrink:
   `sudo pishrink.sh downstage-one-golden.img` — this also makes the image
   auto-expand to fill any card on first boot.
5. Archive as `downstage-one-golden-vX.Y.Z.img.gz` (or `-view-`).

Golden image package requirements beyond stock: `xdotool`, `x11-utils` (One: window management + display refresh), `python3-websocket`
(the View's fast source-switching drives chromium via CDP), `util-linux-extra`
(hwclock, One only), `iw` (both: hotspot client detection for the
WiFi-rejoin fallback), `wmctrl` (One: reassert fullscreen after blackout).

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

## Power-cut test (manual, part of burn-in)

SD corruption from power cuts is the classic Pi failure. Every unit ships
with volatile system journal, zram-only swap, and fsck.repair — but verify
per unit: pull the power cord 5 times (twice mid-boot, three times while
running with displays up). The unit must boot clean each time. Record with
burn_in_pass in the build log.
