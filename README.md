# Downstage OS

Kiosk software for Downstage Systems timer appliances.

| Directory | Product | Hardware |
|---|---|---|
| `one/` | Downstage One — dual-HDMI timer server | Raspberry Pi 5, Argon ONE case, OLED |
| `view/` | Downstage View — network display node | Raspberry Pi, single HDMI, e-ink panel |

Both are Flask apps (`app.py`, port 8080) driving Chromium kiosk windows,
with WiFi/hotspot management, an OnTime connection watchdog, and status
panels on the front of each enclosure.

Built on [Ontime](https://www.getontime.no), free open-source software (GPL v3).

## Releases

Devices check this repo's latest release tag against their `OS_VERSION`
constant and surface available updates in the setup UI.
