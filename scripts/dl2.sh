#!/bin/bash
cd ~/workspace/gta-test/clipper-engine/test-real/multi
Y=~/workspace/gta-test/gtavi-claudecode/.venv/bin/yt-dlp
$Y --no-check-certificates --download-sections "*01:00:00-01:12:00" -f "bv*[height<=720]+ba/b[height<=720]" -o "zackrawrr-12min.mp4" "https://www.twitch.tv/videos/2933941381" &
$Y --no-check-certificates --download-sections "*02:00:00-02:12:00" -f "bv*[height<=720]+ba/b[height<=720]" -o "burntpeanut2-12min.mp4" "https://www.twitch.tv/videos/2934030540" &
wait
ls -lh zackrawrr-12min.mp4 burntpeanut2-12min.mp4
