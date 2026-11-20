#!/bin/bash
cd ~/workspace/gta-test/clipper-engine
 export PYTHONPATH=~/workspace/gta-test/clipper-engine/src:$PYTHONPATH
export CLIPPER_WHISPER_MODEL=tiny
~/workspace/gta-test/gtavi-claudecode/.venv/bin/python -m clipper.core.clip scout test-real/multi/zackrawrr-12min.mp4 --top 8 --min 7 --max 30 2>&1 | tail -30
echo DONE
