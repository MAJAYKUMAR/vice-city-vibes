#!/bin/bash
cd ~/workspace/gta-test/clipper-engine
export CLIPPER_WHISPER_MODEL=tiny
V=~/workspace/gta-test/gtavi-claudecode/.venv/bin/python
for s in burntpeanut kaicenat xqc stableronaldo; do
  echo "=== $s ==="
  $V clip.py scout test-real/multi/$s-12min.mp4 --top 8 --min 7 --max 30 2>&1 | tail -30
done
echo DONE-SCOUT
