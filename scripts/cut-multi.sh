#!/bin/bash
cd ~/workspace/gta-test/clipper-engine
 export PYTHONPATH=~/workspace/gta-test/clipper-engine/src:$PYTHONPATH
export CLIPPER_WHISPER_MODEL=tiny
V=~/workspace/gta-test/gtavi-claudecode/.venv/bin/python
# jobdir pick hook
cut_one() {
  echo "=== CUT $1 pick $2 hook $3 ==="
  $V -m clipper.core.clip cut jobs/$1 --pick $2 --hook "$3" 2>&1 | tail -5
}
cut_one kaicenat-12min-a98c77a5 1 "BEACH FIGHT"
cut_one kaicenat-12min-a98c77a5 4 "WHY ARE YOU CHASING ME"
cut_one kaicenat-12min-a98c77a5 5 "DEAD BODIES"
cut_one stableronaldo-12min-a4b9e1e3 5 "MEET AT THE FUN FAIR"
cut_one stableronaldo-12min-a4b9e1e3 6 "TAKE OFF WITH THE MONEY"
cut_one stableronaldo-12min-a4b9e1e3 7 "CREEPY PLACE"
cut_one burntpeanut-12min-5143c054 3 "MISSION CAR"
cut_one burntpeanut-12min-5143c054 4 "MISSION DONE"
cut_one zackrawrr-12min-36966931 2 "TAKE A SHOWER"
cut_one zackrawrr-12min-36966931 8 "PLAYING TODAY"
echo DONE-CUTS
