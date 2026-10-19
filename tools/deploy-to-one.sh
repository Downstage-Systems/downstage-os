#!/bin/bash
# Deploy this repo's one/ to a Downstage One with Downstage's service key.
#
#   tools/deploy-to-one.sh <host-or-ip> [--no-restart]
#
# In order, stopping at the first problem:
#   1. compile-check every one/*.py here, before anything is touched
#   2. back up the unit's app (*.py, templates/, static/) to
#      ~/ontime-kiosk/.predeploy-<time>/
#   3. copy every one/*.py, templates/ and static/ - never config.json,
#      never anything outside the app folder. Every module, not only
#      app.py: app.py imports its neighbours (4847d7a's lesson)
#   4. compile-check on the unit; on failure put the backup back
#   5. restart ontime-kiosk and wait up to a minute for /status; if it
#      does not answer, put the backup back and restart again
#
# The key: $DOWNSTAGE_SERVICE_KEY, else ../downstage-factory/service-key
# (made by the factory; it never leaves the build Mac). A unit built before
# per-unit SSH has no service key yet: add it once, by hand:
#   ssh-copy-id -i ../downstage-factory/service-key.pub pi@<host>
#
# Not for a unit in a show (Rob: "in a show, dont push to the one").
set -euo pipefail

HOST="${1:?usage: tools/deploy-to-one.sh <host-or-ip> [--no-restart]}"
RESTART=1; [ "${2:-}" = "--no-restart" ] && RESTART=0
REPO="$(cd "$(dirname "$0")/.." && pwd)"
KEY="${DOWNSTAGE_SERVICE_KEY:-$REPO/../downstage-factory/service-key}"
[ -f "$KEY" ] || { echo "no service key at $KEY"; exit 1; }
OPTS=(-i "$KEY" -o IdentitiesOnly=yes -o BatchMode=yes -o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new)
APP=/home/pi/ontime-kiosk
TAG=".predeploy-$(date +%Y%m%d-%H%M%S)"
on_unit() { ssh "${OPTS[@]}" "pi@$HOST" "$@"; }
restart() { on_unit 'XDG_RUNTIME_DIR=/run/user/$(id -u) systemctl --user restart ontime-kiosk'; }
put_back() {
  echo "!! putting $TAG back"
  on_unit "cd $APP && cp $TAG/*.py . && cp -r $TAG/templates $TAG/static . 2>/dev/null; true"
}

echo "== compile-checking one/*.py here"
python3 -m py_compile "$REPO"/one/*.py

echo "== backing up $HOST:$APP to $TAG"
on_unit "cd $APP && mkdir -p $TAG && cp *.py $TAG/ && cp -r templates static $TAG/ 2>/dev/null; ls $TAG | tr '\n' ' '"
echo

echo "== copying one/*.py, templates/, static/"
scp -q "${OPTS[@]}" "$REPO"/one/*.py "pi@$HOST:$APP/"
scp -q -r "${OPTS[@]}" "$REPO/one/templates" "$REPO/one/static" "pi@$HOST:$APP/"

echo "== compile-checking there"
if ! on_unit "cd $APP && python3 -m py_compile *.py"; then
  put_back; exit 1
fi

[ "$RESTART" = 1 ] || { echo "== copied; not restarted (--no-restart)"; exit 0; }
echo "== restarting ontime-kiosk"
restart
for i in $(seq 1 12); do
  sleep 5
  if curl -s -m 3 -o /dev/null "http://$HOST:8080/status"; then
    echo "== up: http://$HOST:8080 (backup kept in $APP/$TAG)"
    exit 0
  fi
done
echo "!! $HOST did not answer within a minute"
put_back
restart
exit 1
