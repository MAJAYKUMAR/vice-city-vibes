#!/bin/bash
cd ~/workspace/gta-test/clipper-engine
export CLIPPER_WHISPER_MODEL=tiny
~/workspace/gta-test/gtavi-claudecode/.venv/bin/python clip.py scout test-real/multi/zackrawrr-12min.mp4 --top 8 --min 7 --max 30 2>&1 | tail -30
echo DONE
