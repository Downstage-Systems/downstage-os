#!/bin/bash
# Downstage burn-in test — run ON THE UNIT during build.
#   bash burn-in.sh [stress-seconds]   (default 300; use 3600 for real burn-in)
# Exercises every subsystem and prints PASS/FAIL per check plus a verdict
# for the build log's burn_in_pass column. Run on ethernet: the hotspot
# check cycles the WiFi radio.
DUR="${1:-300}"
PASS=0; FAIL=0
ck() {  # ck <name> <cmd...>
  if eval "$2" > /dev/null 2>&1; then echo "PASS  $1"; PASS=$((PASS+1))
  else echo "FAIL  $1"; FAIL=$((FAIL+1)); fi
}
echo "== Downstage burn-in ($(hostname), ${DUR}s stress) =="

# 1. app + identity
ck "setup UI answering"        "curl -s -m 5 http://127.0.0.1:8080/status | grep -q os_version"
ck "serial provisioned"        "curl -s -m 5 http://127.0.0.1:8080/status | grep -qE '\"serial\": ?\"D'"
ck "update check reachable"    "curl -s -m 10 https://api.github.com/repos/downstage-systems/downstage-os/releases/latest | grep -q tag_name"

# 2. displays (One expects >=1 HDMI; View drives one output)
ck "display detected"          "DISPLAY=:0 xrandr --listmonitors 2>/dev/null | grep -qE 'Monitors: [1-9]'"

# 3. RTC (only judged if hardware present)
if [ -e /sys/class/rtc/rtc0/battery_voltage ]; then
  BV=$(cat /sys/class/rtc/rtc0/battery_voltage)
  ck "RTC battery > 2.5V"      "[ $BV -gt 2500000 ]"
  ck "RTC trickle charge on"   "grep -q rtc_bbat_vchg /boot/firmware/config.txt"
fi

# 4. hotspot radio cycle
ck "hotspot starts"            "curl -s -m 40 -X POST http://127.0.0.1:8080/hotspot/start | grep -qE 'active.: ?true'"
sleep 3
ck "hotspot stops"             "curl -s -m 30 -X POST http://127.0.0.1:8080/hotspot/stop | grep -qE 'active.: ?false'"

# 5. thermal stress
echo "----  stressing ${DUR}s (all cores) ----"
for i in 1 2 3 4; do yes > /dev/null & done
MAXT=0
END=$((SECONDS + DUR))
while [ $SECONDS -lt $END ]; do
  T=$(vcgencmd measure_temp 2>/dev/null | grep -oE '[0-9]+\.[0-9]' | cut -d. -f1)
  [ -n "$T" ] && [ "$T" -gt "$MAXT" ] && MAXT=$T
  sleep 10
done
kill %1 %2 %3 %4 2>/dev/null; wait 2>/dev/null
echo "peak temp under load: ${MAXT}C"
ck "thermals under 80C"        "[ $MAXT -lt 80 ] && [ $MAXT -gt 0 ]"
ck "UI alive after stress"     "curl -s -m 5 http://127.0.0.1:8080/status | grep -q os_version"

echo "=================================="
if [ $FAIL -eq 0 ]; then echo "BURN-IN: PASS ($PASS checks)"; exit 0
else echo "BURN-IN: FAIL ($FAIL of $((PASS+FAIL)) checks failed)"; exit 1; fi
