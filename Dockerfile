# Clipper Engine watcher — Fly.io image
# Build context is clipper-engine/ (run `flyctl deploy` from there).
# The gtavi-claudecode scripts/ are vendored into fly/gtavi-scripts/
# (upstream repo untouched, MIT) because Docker cannot COPY outside
# the build context. ssl_fix.py expects them at
# $HOME/workspace/gta-test/gtavi-claudecode/scripts, so the image
# recreates exactly that tree under HOME=/home/clipper.
FROM python:3.12-slim

ENV HOME=/home/clipper \
    PYTHONUNBUFFERED=1 \
    CLIPPER_WHISPER_MODEL=tiny \
    DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg libgomp1 \
    && rm -rf /var/lib/apt/lists/*

RUN mkdir -p /home/clipper/workspace/gta-test/gtavi-claudecode/scripts \
    /home/clipper/workspace/gta-test/clipper-engine /data

WORKDIR /home/clipper/workspace/gta-test/clipper-engine

COPY fly/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

COPY fly/gtavi-scripts/ /home/clipper/workspace/gta-test/gtavi-claudecode/scripts/
COPY *.py ./
COPY watchlist.json ./
COPY models/ ./models/

# Watcher loop only: dashboard + autopilot, every 15 min.
# Never records, cuts or posts (Vice City Vibes Rule 2/11).
CMD ["python", "watcher.py"]
