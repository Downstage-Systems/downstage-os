#!/bin/bash
# Downstage OS — prepare a bench unit for golden-image capture.
# Run ON THE PI as pi, then shut down and clone the SD card.
# Strips all owner-specific data; the unit will provision itself on first
# boot from /boot/firmware/downstage-provision.json (see provision-unit.sh).
set -e
echo "== Downstage golden image preparation =="
read -p "This wipes WiFi credentials, logs, and identity from THIS unit. Continue? [y/N] " a
[ "$a" = "y" ] || exit 1

APP_DIR=$([ -d /home/pi/ontime-kiosk ] && echo /home/pi/ontime-kiosk || echo /home/pi/ontime-kiosk-lite)

# 1. WiFi credentials + hotspot profile
sudo nmcli -t -f NAME,TYPE connection show | grep ':802-11-wireless$' | cut -d: -f1 | \
  while read -r c; do sudo nmcli connection delete "$c" || true; done

# 2. Factory config (keep update repo; identity comes from provisioning)
python3 - << 'PY'
import json
app = "/home/pi/ontime-kiosk" if __import__("os").path.isdir("/home/pi/ontime-kiosk") else "/home/pi/ontime-kiosk-lite"
json.dump({"os_update_repo": "downstage-systems/downstage-os"}, open(f"{app}/config.json", "w"))
PY

# 3. OnTime user data (rundowns/projects) + logs + backups
rm -rf /home/pi/.config/ontime-electron ~/.local/share/ontime 2>/dev/null || true
rm -rf $APP_DIR/.backup $APP_DIR/.update-result $APP_DIR/*.log 2>/dev/null || true
sudo journalctl --rotate --vacuum-time=1s 2>/dev/null || true

# 3c. Strip preinstalled third-party software — customers install on first
# run via the setup UI (their download from the official source, not our
# distribution; keeps licensing obligations off the shipped image)
read -p "Remove preinstalled OnTime + Companion from this image? [y/N] " strip
if [ "$strip" = "y" ]; then
  sudo systemctl disable --now companion 2>/dev/null || true
  sudo rm -rf /opt/companion /usr/local/bin/companion* /usr/local/sbin/companion*
  sudo rm -f /etc/udev/rules.d/50-companion.rules /etc/systemd/system/companion.service
  sudo userdel -r companion 2>/dev/null || true
  rm -rf $APP_DIR/ontime-server
  echo "third-party software stripped — first-run UI offers installs"
fi

# 4. Owner remote access (rpi-connect) — must never ship
rm -rf /home/pi/.config/com.raspberrypi.connect 2>/dev/null || true
sudo sed -i '/com.raspberrypi.connect/,+1d;/rpuak_/d' /boot/firmware/user-data 2>/dev/null || true

# 5. Shell history + SSH identity (host keys regenerate on first boot)
rm -f /home/pi/.bash_history /home/pi/.zsh_history 2>/dev/null || true
rm -f /home/pi/.ssh/known_hosts /home/pi/.ssh/authorized_keys 2>/dev/null || true
sudo rm -f /etc/ssh/ssh_host_* 
sudo systemctl enable regenerate_ssh_host_keys 2>/dev/null || \
  echo "NOTE: ensure ssh host keys regenerate on boot (rpi image does this when keys are absent)"

# 6. Storage-write hardening (ships in every unit)
sudo mkdir -p /etc/systemd/journald.conf.d
sudo tee /etc/systemd/journald.conf.d/99-downstage-volatile.conf > /dev/null << 'JEOF'
[Journal]
Storage=volatile
RuntimeMaxUse=32M
JEOF
grep -q "fsck.repair=yes" /boot/firmware/cmdline.txt || \
  sudo sed -i 's/$/ fsck.repair=yes/' /boot/firmware/cmdline.txt
# (swap is zram on this OS — RAM-backed, no SD writes; leave it)

# 7. Neutral hostname until provisioning
sudo hostnamectl set-hostname downstage-unprovisioned
sudo sed -i "s/127.0.1.1 .*/127.0.1.1 downstage-unprovisioned/" /etc/hosts

echo "== Done. Shut down now ('sudo poweroff') and image the SD card. =="
