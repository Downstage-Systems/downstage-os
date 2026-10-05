# The timer line's budget

What the One sends its lights on every KEY-STATE line, what it costs, and
what gets cut first when it will not fit. Written because the line has grown
four times in two weeks and the limit is shrinking; agreed between the One
(Downstage Coding Main) and the Cue firmware (Downstage Cue Coding), R&D
holding the design. **If you add a field, measure the line and update this
file in the same change.**

## The limit

Two different limits, and the one that matters is not the obvious one
(Cue Coding, 2026-10-05).

**What is signed is the Relay's rebuilt line, not the One's as sent.** For
each light it carries, the Relay builds its own `KEY-STATE DEVICEID=<light>-T
KEY=0` (about 37 bytes) and copies the One's keys in a fixed order:

    COLOR PATTERN PROGRESS TIME TOD HELD NOTIMER ONECLOCK PHASE CONTROL
    PRESET TOTAL TITLE MESSAGE TEXT

It drops whatever no longer fits, from the end - so TITLE, MESSAGE and TEXT
go first. The signed-text cap is 226 and the Relay stops at 220.

- **The real constraint: `COLOR` through `TOTAL` must stay under 180 bytes**,
  counting each " KEY=value". Measured on the bench: 93 today, 131 worst
  case. About 49 bytes spare, or four more short keys.
- **A new key a carried light must have goes before TITLE in that order, and
  Cue Coding must add it to the Relay's list** or it is silently dropped for
  carried lights only - the light works on its own WiFi and not through a
  Relay, which nobody traces quickly. This happened: `TOTAL` was added by the
  One on 2026-10-04 and was missing from the Relay's list until it was
  spotted here.
- **The One's own line may be 240.** It holds itself to **220**
  (`CUE_LINE_LIMIT` in `one/app.py`) so it never hands the Relay something to
  cut. That is the One's margin, not the protocol's.

## What is never dropped

A face may be wrong about the title. It must never be wrong about the clock,
the phase, or whether its buttons work.

| Field | Why it cannot go |
|---|---|
| `COLOR` | the tally itself |
| `TIME` | the clock |
| `PROGRESS`, `TOTAL` | the ring, and what it is measured against |
| `HELD`, `NOTIMER`, `TOD` | which kind of nothing-is-running this is |
| `ONECLOCK` | a light has no clock of its own; without it the wall clock stops |
| `PHASE` | a START/PAUSE button cannot read colours |
| `CONTROL` | whether to show the buttons or the "turn it on" sentence |
| `PRESET` | which preset is lit |
| `PRESSED` | the protocol's own |

## What is cut, in this order

1. **`MESSAGE`** - the stage manager's words. Shortened to fit with an
   ellipsis; dropped only if fewer than six characters would survive.
2. **`TITLE`** - the event's name. Same treatment, after MESSAGE.

Nothing else is touched. If a line is still over after both, the One sends it
anyway and says so in its log (at most twice, not every 100 ms) - a visible
complaint beats a silent loss.

## What it costs today

Measured, not estimated, on bench 0001 with Rob's rundown:

| Line | Bytes |
|---|---|
| Idle, nothing loaded | ~120 |
| A preset held (`TIME TOTAL HELD PRESET`) | 163 |
| Running, with a title | ~173 |
| Worst case without MESSAGE | ~196 |
| Worst case with a 48-character MESSAGE | 240, trimmed to 220 |

So there is room for about one more short field before MESSAGE starts losing
characters on an ordinary show line. Anything bigger than that needs a
conversation, not a commit.

## Why `PRESET` is a place and not an id

The feed carries `PRESET=3`, the preset's 1-based place in the list from
`GET /api/v1/one/timer`, not the OnTime event id. An id is OnTime's to size -
six characters today, with nothing promising that - and 13 bytes against a
budget this tight buys nothing a number does not. It also matches how a
carried Cue already loads a preset: by index, with the Relay mapping index to
id, so ids never ride CueLink. The id is in the HTTP answer, where it costs
nothing.

The list changes when the operator edits the rundown, so a light must
re-fetch it when its preset page opens, not once at boot.
