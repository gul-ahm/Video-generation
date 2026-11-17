#!/bin/bash
# Runs the job files placed in _jobs/, one at a time. Output of each job: _jobs/<name>.log
# Start: bash _runner.sh      Stop: Ctrl+C (or close the window)
cd "$(dirname "$0")" || exit 1
mkdir -p _jobs
echo "runner ready in $(pwd)/_jobs  (Ctrl+C to stop)"
caffeinate -i -w $$ &
while true; do
  for job in _jobs/*.sh; do
    [ -e "$job" ] || continue
    name="${job%.sh}"
    mv "$job" "$name.running"
    echo "== $(date +%H:%M:%S) start ${name#_jobs/}"
    bash -o pipefail "$name.running" > "$name.log" 2>&1
    echo "exit $?" >> "$name.log"
    mv "$name.running" "$name.done"
    echo "== $(date +%H:%M:%S) $(tail -1 "$name.log")"
  done
  sleep 3
done
