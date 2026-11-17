#!/bin/bash
# Decision cycle every 5 minutes at minute :03, :08, :13 ... (Asia/Bangkok),
# after the 6 helper agents write fresh proposals/*.json at :00-:02.
cd /workspace/polymarket
while true; do
  now=$(date +%s); m=$(( (now/60) % 5 )); s=$(( now % 60 ))
  wait=$(( ((3 - m + 5) % 5) * 60 - s ))
  [ $wait -le 0 ] && wait=$(( wait + 300 ))
  sleep $wait
  ( flock -n 9 || exit 0; timeout 240 python3 run.py >> logs/loop.log 2>&1; echo "$(date '+%F %T') exit=$?" >> logs/loop.log ) 9>/tmp/pm_run.lock
done
