#!/bin/bash
# PM-Crypto background runner: re-scan every 60s and rewrite proposals/crypto.json atomically.
# PAPER ONLY. Read-only public APIs; never touches ledger.sqlite (read-only queries only) or pm/*.py.
# Start:  nohup proposals/crypto_loop.sh >/dev/null 2>&1 &
# Stop:   kill "$(cat /workspace/polymarket/proposals/crypto_loop.pid)"
DIR=/workspace/polymarket/proposals
LOG=$DIR/crypto_scan.log
PID=$DIR/crypto_loop.pid
MAXLOG=$((2*1024*1024))   # rotate at 2 MB, keep one old copy
cd /workspace/polymarket || exit 1
if [ -f "$PID" ] && kill -0 "$(cat "$PID")" 2>/dev/null; then
  echo "crypto_loop already running (pid $(cat "$PID"))"; exit 0
fi
echo $$ > "$PID"
trap 'rm -f "$PID"; exit 0' INT TERM
while true; do
  if [ -f "$LOG" ] && [ "$(stat -c %s "$LOG")" -gt "$MAXLOG" ]; then mv -f "$LOG" "$LOG.1"; fi
  start=$(date +%s)
  echo "=== $(date '+%F %T %z') scan start" >> "$LOG"
  timeout 50 python3 -B "$DIR/crypto_scan.py" >> "$LOG" 2>&1
  echo "=== $(date '+%F %T %z') exit=$? ($(( $(date +%s) - start ))s)" >> "$LOG"
  sleep $(( 60 - ( $(date +%s) - start ) > 5 ? 60 - ( $(date +%s) - start ) : 5 )) &
  wait $!
done
