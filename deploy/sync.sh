#!/usr/bin/env bash
# Record on this machine and ship the append-only log to the dashboard host.
#
#   REMOTE=joy@163.61.38.144 CLIP=5 deploy/sync.sh          start
#   deploy/sync.sh stop                                    stop
#
# rsync --append only sends bytes past the remote file's end, which matches an
# append-only log. The remote host never contacts Polymarket.
set -euo pipefail
cd "$(dirname "$0")/.."
REMOTE="${REMOTE:-joy@163.61.38.144}"; RDIR="${RDIR:-5MinuteRepo}"; CLIP="${CLIP:-5}"
mkdir -p run logs
for name in recorder sync; do
  f="run/local-$name.pid"
  if [[ -f "$f" ]] && kill -0 "$(cat "$f")" 2>/dev/null; then kill "$(cat "$f")"; fi
  rm -f "$f"
done
[[ "${1:-}" == "stop" ]] && { echo "stopped"; exit 0; }

PYTHONPATH=src nohup .venv/bin/python -m fivemin.recorder --out logs/phase1.jsonl --clip "$CLIP" >> logs/recorder.out 2>&1 &
echo $! > run/local-recorder.pid
nohup bash -c "while true; do rsync -az --append -e 'ssh -o BatchMode=yes' logs/phase1.jsonl '$REMOTE:$RDIR/logs/phase1.jsonl' >> logs/sync.out 2>&1; sleep 5; done" >/dev/null 2>&1 &
echo $! > run/local-sync.pid
echo "recording locally, syncing to $REMOTE:$RDIR/logs/phase1.jsonl every 5 s"
