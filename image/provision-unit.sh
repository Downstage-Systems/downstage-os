#!/bin/bash
# Downstage OS — flash-time provisioning (runs on the build Mac).
# Usage: ./provision-unit.sh DS1-A-2607-0002 [/Volumes/bootfs]
# Reads the build log CSV, writes downstage-provision.json to the freshly
# flashed SD card's boot partition. Then boot the unit once; it provisions
# itself and the file is consumed.
set -e
SERIAL="$1"
BOOT="${2:-/Volumes/bootfs}"
LOG="${DOWNSTAGE_BUILD_LOG:-$HOME/Downloads/downstage-build-log.csv}"

[ -n "$SERIAL" ]  || { echo "usage: $0 <serial> [boot-mount]"; exit 1; }
[ -d "$BOOT" ]    || { echo "boot partition not mounted at $BOOT"; exit 1; }
[ -f "$LOG" ]     || { echo "build log not found at $LOG"; exit 1; }

LINE=$(grep "^$SERIAL," "$LOG") || { echo "serial $SERIAL not in build log"; exit 1; }
SSID=$(echo "$LINE" | cut -d, -f7)
PASS=$(echo "$LINE" | cut -d, -f8)

# hostname from serial: DS1-A-2607-0002 -> downstage-0002, DSV-... -> downstage-v002
case "$SERIAL" in
  DS1-*) HOSTNAME="downstage-$(echo "$SERIAL" | awk -F- '{print $NF}')" ;;
  DSV-*) HOSTNAME="downstage-v$(echo "$SERIAL" | awk -F- '{print $NF}' | sed 's/^0//')" ;;
  *) echo "unknown serial prefix"; exit 1 ;;
esac

cat > "$BOOT/downstage-provision.json" << JSON
{
  "serial":       "$SERIAL",
  "hostname":     "$HOSTNAME",
  "hotspot_ssid": "$SSID",
  "hotspot_pass": "$PASS"
}
JSON
echo "provisioned $SERIAL -> $HOSTNAME (hotspot: $SSID)"
echo "eject the card, boot the unit, verify at http://$HOSTNAME.local:8080"
