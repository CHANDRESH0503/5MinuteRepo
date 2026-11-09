#!/usr/bin/env bash
# Start (or restart) the fivemin recorder and dashboard as the current user.
# No sudo, no system config, no other service touched. Processes are tracked
# by pid files in ./run and only those pids are ever stopped.
#
#   PORT=8765 CLIP=5 deploy/run.sh          start / restart
#   deploy/run.sh stop                      stop only our two processes
#   NO_RECORDER=1 HOST=127.0.0.1 deploy/run.sh
#       dashboard only, private; the log arrives from deploy/sync.sh (used on
#       the India VPS, whose network does not resolve polymarket.com)
#
# Chainlink needs POLY_API_KEY / POLY_API_SECRET / POLY_API_PASSPHRASE in
# ./.env (chmod 600, gitignored). Without it the recorder logs "no fair value".
set -euo pipefail
cd "$(dirname "$0")/.."
PORT="${PORT:-8765}"; CLIP="${CLIP:-5}"; HOST="${HOST:-0.0.0.0}"
mkdir -p run logs

stop_one() {
  local f="run/$1.pid"
  if [[ -f "$f" ]] && kill -0 "$(cat "$f")" 2>/dev/null; then
    # only kill it if it is still our process
    if ps -o args= -p "$(cat "$f")" | grep -q "fivemin.$1"; then kill "$(cat "$f")"; fi
  fi
  rm -f "$f"
}

stop_one recorder; stop_one dashboard
[[ "${1:-}" == "stop" ]] && { echo "stopped"; exit 0; }

if ss -ltn "( sport = :$PORT )" | grep -q LISTEN; then
  echo "port $PORT is already in use; pick another with PORT=..." >&2; exit 1
fi

[[ -f .env ]] && { set -a; . ./.env; set +a; }
export PYTHONPATH=src
if [[ -z "${NO_RECORDER:-}" ]]; then
  nohup .venv/bin/python -m fivemin.recorder --out logs/phase1.jsonl --clip "$CLIP" >> logs/recorder.out 2>&1 &
  echo $! > run/recorder.pid
fi
nohup .venv/bin/python -m fivemin.dashboard --log logs/phase1.jsonl --host "$HOST" --port "$PORT" >> logs/dashboard.out 2>&1 &
echo $! > run/dashboard.pid
sleep 2
kill -0 "$(cat run/dashboard.pid)" && echo "running: dashboard on $HOST:$PORT${NO_RECORDER:+ (no recorder)}"
