#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AUTOSTART_DIR="$HOME/.config/autostart"
DESKTOP_FILE="$AUTOSTART_DIR/ontime-kiosk.desktop"

echo "=== OnTime Kiosk Installer ==="
echo ""

# Dependencies
echo "[1/6] Installing system dependencies..."
sudo apt-get update -qq
sudo apt-get install -y \
    python3-flask \
    python3-requests \
    python3-pil \
    python3-smbus \
    python3-pip \
    i2c-tools \
    chromium \
    ffmpeg \
    unclutter \
    xdotool

# luma.oled (OLED driver for Argon ONE V5 display)
echo "[2/6] Installing luma.oled..."
pip3 install --break-system-packages luma.oled 2>/dev/null \
    || pip3 install luma.oled

# Passwordless sudo for kiosk user (required for timezone, reboot, companion)
echo "[3/6] Configuring passwordless sudo and USB permissions..."
echo "$USER ALL=(ALL) NOPASSWD: ALL" | sudo tee /etc/sudoers.d/010_${USER}-nopasswd > /dev/null
sudo chmod 440 /etc/sudoers.d/010_${USER}-nopasswd

# udev rules for Elgato Stream Deck (all models) — required for Companion
sudo tee /etc/udev/rules.d/50-elgato-streamdeck.rules > /dev/null << 'EOF'
# Elgato Stream Deck — allow access without root for Companion
SUBSYSTEM=="usb", ATTRS{idVendor}=="0fd9", GROUP="plugdev", MODE="0664"
KERNEL=="hidraw*", ATTRS{idVendor}=="0fd9", GROUP="plugdev", MODE="0664"
EOF
sudo usermod -aG plugdev "$USER"
sudo udevadm control --reload-rules
sudo udevadm trigger

# Enable I2C (required for OLED)
echo "[4/6] Enabling I2C interface..."
sudo raspi-config nonint do_i2c 0
# Also add to /boot/firmware/config.txt if not already present
if ! grep -q "^dtparam=i2c_arm=on" /boot/firmware/config.txt 2>/dev/null; then
    echo "dtparam=i2c_arm=on" | sudo tee -a /boot/firmware/config.txt > /dev/null
fi
# Add user to i2c group so no sudo needed
sudo usermod -aG i2c "$USER"

# Disable screen blanking via X11 config
echo "[5/6] Disabling screen blanking..."
sudo mkdir -p /etc/X11/xorg.conf.d
sudo tee /etc/X11/xorg.conf.d/10-no-blank.conf > /dev/null << 'EOF'
Section "ServerFlags"
    Option "BlankTime"    "0"
    Option "StandbyTime"  "0"
    Option "SuspendTime"  "0"
    Option "OffTime"      "0"
EndSection
EOF

# Set up autostart
echo "[6/6] Setting up autostart..."
mkdir -p "$AUTOSTART_DIR"

cat > "$DESKTOP_FILE" << EOF
[Desktop Entry]
Type=Application
Name=OnTime Kiosk
Exec=bash -c "xset s off; xset -dpms; xset s noblank; unclutter -idle 1 -root & python3 $SCRIPT_DIR/app.py"
X-GNOME-Autostart-enabled=true
EOF

chmod +x "$DESKTOP_FILE"
chmod +x "$SCRIPT_DIR/app.py"

echo "Done."
echo ""
echo "=============================="
LOCAL_IP=$(hostname -I | awk '{print $1}')
echo "  Config UI : http://$LOCAL_IP:8080"
echo "  OLED      : 4 screens, 5s rotation"
echo "              ① OnTime status"
echo "              ② Current event"
echo "              ③ Kiosk URL"
echo "              ④ CPU temp / RAM"
echo ""
echo "  NOTE: A reboot is required for I2C to activate."
echo ""
echo "  To start now (without rebooting):"
echo "    python3 $SCRIPT_DIR/app.py"
echo ""
echo "  To reset display to config UI via SSH:"
echo "    curl -X POST http://localhost:8080/reset"
echo "=============================="
echo ""
read -p "Reboot now to activate I2C? [y/N] " yn
if [[ "$yn" =~ ^[Yy]$ ]]; then
    sudo reboot
fi
