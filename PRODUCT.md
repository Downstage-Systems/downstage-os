# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

*(Inferred: both apps are Flask web UIs served to a Chromium kiosk window and to phones/tablets over LAN for remote config. No native app wrapper found.)*

## Users

Live-event, theater, broadcast, and studio operators who run [Ontime](https://www.getontime.no) (open-source show-timer software) and need dedicated, always-on hardware to display and control it, instead of a laptop running a browser tab. *(Inferred from README, the `VIEWS` list — Stage Timer, Backstage/Crew, Studio Clock, Cue Sheet, Operator, Editor, Rundown — and OnTime/Companion/satellite integration in the code.)*

Two device roles, likely used together on the same show:
- **Operator/production side** — runs Downstage One, drives dual HDMI outputs, hosts the actual Ontime server.
- **Remote/venue side** — runs Downstage View, a single-HDMI or e-ink network display node showing a view fed by a Downstage One (or another Ontime source) elsewhere on the network.

*(Inferred; not confirmed by direct user interview.)*

## Product Purpose

Downstage OS is the kiosk operating layer for Downstage Systems' purpose-built timer appliances. It turns a Raspberry Pi into a dedicated, self-managing device that runs Ontime and shows/controls it full-screen, so venues don't need to babysit a laptop or manually keep a browser kiosk alive. *(Inferred from README, `_no_html_cache`/no-store comments about self-updating kiosk pages, and the watchdog/relaunch logic in `one/app.py`.)*

## Positioning

Turnkey appliance vs. general-purpose computer: pairs off-the-shelf open-source Ontime with hardware-specific reliability engineering — auto-recovering kiosk windows, a connection watchdog, OS self-update with one-click revert, WiFi/hotspot provisioning for venues with no existing network, and physical status panels (OLED on One, e-ink on View) so state is readable without opening a browser. *(Inferred — no explicit competitor comparison found in repo; a neighboring product could copy "runs Ontime" but not the fleet-management, patch/revert, and physical-status-panel design work layered on top.)*

## Operating Context

- Both apps are Flask servers on port 8080, driving a Chromium kiosk window on the device's own screen plus a browser-based setup/config UI reachable over the network.
- Downstage One hosts Ontime itself (`ontime-server/`, `ontime.AppImage`) and can install/start/stop/update/revert it in place.
- Devices track update availability against this repo's GitHub release tags via an `OS_VERSION` constant (see README "Releases" and `/os/update*` routes).
- Fleet features: device discovery, "identify" (flash a device to locate it physically), unit naming — implying multi-device venue deployments.
- Companion (Bitfocus) and "satellite" integration exist for external control-surface hardware.
- WiFi/hotspot self-provisioning (`/wifi/*`, `/hotspot/*`, `/network/*`) supports venues without pre-existing network infrastructure.
- `patches.json` tracks an OS build's last bench-validated Debian/Chromium version combination — implies a controlled, tested base image rather than freely-updating Linux packages.

*(All inferred from code structure and route names; not confirmed by direct interview.)*

## Capabilities and Constraints

- Hardware: Raspberry Pi 5 in an Argon ONE case with OLED (Downstage One); Raspberry Pi with e-ink panel (Downstage View).
- Software base: Debian, Chromium kiosk, Flask, Ontime (GPL v3, third-party, not authored by Downstage).
- Self-update mechanism with patch-status tracking and revert capability (`ONTIME_PREV`, `/system/patches/*`, `/os/update*`).
- Terminology carried over from Ontime's own view names (Stage Timer, Countdown, Backstage/Crew, Studio Clock, Timeline, Public Info, Operator, Cue Sheet, Editor, Timer Control, Message Control, Rundown) — future UI copy should stay consistent with these.
- Undecided/unconfirmed: exact customer base (rental houses vs. venues vs. touring productions), pricing/licensing model, and whether One and View are typically sold/deployed together or independently.

## Brand Commitments

- Product family name: **Downstage Systems**, with two shipping products — **Downstage One** and **Downstage View** — under the **Downstage OS** software umbrella.
- Explicit attribution requirement: "Built on Ontime, free open-source software (GPL v3)" (README) — must remain visible/credited.
- Brand assets exist in the repo (`downstage-one-mark.svg`, `downstage-view-mark.svg`) and in the broader `Documents/Downstage Systems/Brand` folder (logos, brand guidelines PDF) — treat as binding visual authority once design work begins.

## Evidence on Hand

- `README.md` — product/device table and release mechanism.
- `docs/renders/` — device mockup and "ship screen" renders for View.
- `one/static/`, `view/static/` — shipped icons, unit photos (`unit-one.png`, `unit-view.png`), marks, fonts, audio assets.
- Sibling folder `Documents/Downstage Systems/` holds business plan, brand guidelines, user guides, and hardware guides — not pulled into this record since they sit outside the repo; consult directly if deeper positioning or audience detail is needed.
- No customer testimonials, case studies, or pricing found in-repo — do not fabricate these in future design work.

## Product Principles

1. **Appliance, not a computer** — the device should never expose the user to Linux, a file system, or a general-purpose desktop; everything is a kiosk screen or a config page.
2. **Self-healing over manual fixes** — watchdogs, auto-relaunch, and one-click revert exist because these devices run unattended in venues; new features should preserve that "don't make the operator SSH in" bar.
3. **Never show a stale UI** — aggressive cache-busting on every response reflects a hard constraint: config pages must always match the installed OS version.
4. **Ontime stays the source of truth for timing** — Downstage OS wraps, provisions, and displays Ontime; it should not fork or replace Ontime's own timing/control logic.
5. **Physical status must be readable without a browser** — OLED/e-ink panels and device "identify" flashing reflect a principle that a technician standing at the rack should be able to diagnose state without opening a laptop.

## Accessibility & Inclusion

Not established in the repository. No specific accessibility standard or user need is documented; do not invent one.
