# Cue firmware changes the One should know about (2026-10-02)

From Cue Coding, for Coding Main. Cue firmware went from 0.98 to **0.100.6**
today; Rob asked that the One be told. The bench lights (Lite, 360, round
Cue) and the Cue Relay run 0.100.4 to 0.100.6. Details live in the Cue repo:
`docs/cue-relay.md`, `docs/companion-connection.md`,
`docs/companion-integration.md`.

## Deployed to bench 0001 today (Rob's go)

Repo HEAD 14a962a: the Relay card and its watch (6629875, 14a962a), plus the
commits since 0001's last deploy (fb1b273 / 2f8426c): f4d81c2, 6afa8f2,
6ecbd80, ad9b3cc, 6a827c3. **Not the URSA module** (Rob: it is not built
yet): `ursa.py` was copied and then removed again the same minute; app.py and
index.html do not reference it. `install.sh` was NOT run (0001 has 7fb6920's). Backups on 0001: `app.py.bak-*`, `cue_radio.py.bak-*`,
`templates/index.html.bak-*`. The fleet list still shows the last scan (4 h
old at deploy): the Relay card appears after the next scan.

## What changed on the Cue side that touches the One

1. **Find boxes by mDNS, not a sweep.** Every Cue box (each light, the Relay,
   the tally box) now advertises `_downstage._tcp` with TXT `id kind name fw
   ctl=1` (`kind`: relay | cue | lite | 360 | mini | tally; the Relay also
   `api=1 ws=81`). The One's Scan for Units still sweeps the /24 on every
   interface; Rob does not want sweeps (they tripped UniFi threat protection
   before). Suggest: browse `_downstage._tcp` first, sweep only on request.
2. **`/api/v1/device` on every box** (identify, restart with a token, health
   warnings, mac, temp). The One's own `/api/v1/device` is yours, per
   `companion-integration.md`.
3. **Control token.** Each box can have an owner-set token (set on its own
   page; `/status` says `tokenSet`). Once one is set, a light's own routes
   `/save`, `/net-host`, `/forget`, `/setup-mode`, `/factory-reset` and
   `/update` need it (form field `token`, `?token=` on `/update`, or
   `Authorization: Bearer`); the Relay's `/restart`, `/reset`, `/update`,
   `/save/network`. **The One's fleet calls to those routes will get 401 on
   a light with a token** - nothing is set on the bench yet. The One needs a
   way to hold the owner's token (one per rig, as the Companion connection
   does) and send it.
4. **Cue numbers.** A light's `/status` now has `cueNumber` (the one it
   answers to), `numberFrom` ("set" | "camera" | "none") and `cueSet` (its
   own; 0 = same as its camera number). The Relay's `/status` lights carry
   `cue` and `numberFrom`. Worth showing on the fleet cards.
5. **CueLink beacon** grew two bytes at the end: `linkWait` (0.99.12: 1 the
   link steadying, 2 on air - a light waiting to link) and `numberFrom`
   (0.100.3). Packed struct, appended: `cue_radio.py` parsing by offset from
   the start is unaffected, but if it checks the frame length exactly, it
   will see longer beacons.
6. **A light whose carrier goes quiet** shows CueLink searching (status
   `CarrierSearching`, round Cue FINDING CUELINK) and dials Companion itself
   only after 15 s; it hands back to the carrier only when steady 5 s, not
   on air, and not within 30 s of going direct.
7. **The Relay keeps its link stats across a restart**: `/linkstats.json`
   gains `"previous": {"savedS", "stats"}` after a power cut.
8. **Not deployed / still open on the Cue side:** the Relay's partition move
   (its app is at 91.5% of 1.25 MB; needs one USB visit, Rob's call).
