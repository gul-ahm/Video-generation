#!/bin/bash
# Double click in Finder: opens the History Studio dashboard in the browser. Keep the window open while videos are made.
cd "$(dirname "$0")" || exit 1
PY="$PWD/.venv-mlx/bin/python"
for try in 1 2 3; do
  "$PY" -c "import sys" 2>/dev/null && break
  sleep 2
done
if ! "$PY" -c "import sys" 2>/dev/null; then
  echo
  echo "Mac ne Terminal ko is folder ki permission nahi di, isliye History Studio nahi khula."
  echo "System Settings > Privacy & Security > Files and Folders > Terminal > Desktop Folder ko ON karein."
  echo "Phir ye window band karke History Studio.command par dobara double click karein."
  echo
  read -r -p "Band karne ke liye Enter dabayein "
  exit 1
fi
# caffeinate: the Mac does not go to sleep while a video is being made
exec caffeinate -i "$PY" studio.py
