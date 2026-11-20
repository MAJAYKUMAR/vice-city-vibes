# Clipper Engine watcher — Fly.io image
# Build context is clipper-engine/ (run `flyctl deploy` from there, with
# --config deploy/fly/fly.toml --dockerfile deploy/fly/Dockerfile).
# The gtavi-claudecode scripts/ are vendored into src/clipper/infra/gtavi/
# (upstream repo untouched, MIT). The image also recreates the
# $HOME/workspace/gta-test/gtavi-claudecode/scripts tree that ssl_fix.py
# falls back to, under HOME=/home/clipper.
FROM python:3.12-slim

ENV HOME=/home/clipper \
    PYTHONUNBUFFERED=1 \
    CLIPPER_WHISPER_MODEL=tiny \
    PYTHONPATH=/home/clipper/workspace/gta-test/clipper-engine/src \
    DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg libgomp1 \
    && rm -rf /var/lib/apt/lists/*

RUN mkdir -p /home/clipper/workspace/gta-test/gtavi-claudecode/scripts \
    /home/clipper/workspace/gta-test/clipper-engine /data

WORKDIR /home/clipper/workspace/gta-test/clipper-engine

COPY deploy/fly/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

COPY src/clipper/infra/gtavi/ /home/clipper/workspace/gta-test/gtavi-claudecode/scripts/
COPY pyproject.toml ./
COPY src/ ./src/

# Watcher loop only: dashboard + autopilot, every 15 min.
# Never records, cuts or posts (Vice City Vibes Rule 2/11).
CMD ["python", "-m", "clipper.app.watcher"]
