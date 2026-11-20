#!/usr/bin/env python3
"""Fly entrypoint — run the watcher AND the web dashboard in one machine.

Fly's single shared-cpu-1x/512MB machine runs this process as its CMD.
It starts the watcher loop (clipper.app.watcher, scouting every 15 min)
as a child process and the web dashboard (clipper.app.web, port $PORT,
default 8080) in the foreground. If the watcher child exits, it is
restarted after 30s; if the web server exits, this process exits so
Fly restarts the machine.

Why one process: the machine has no web service of its own in the old
config, and adding an http_service must NOT stop the watcher — both
live in the same machine, sharing the /data volume.
"""
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))


def main():
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")
    watcher = None
    stop = False

    def _sig(signum, frame):
        nonlocal stop
        stop = True

    signal.signal(signal.SIGTERM, _sig)
    signal.signal(signal.SIGINT, _sig)

    def start_watcher():
        print("run_fly: starting watcher", flush=True)
        return subprocess.Popen(
            [sys.executable, "-m", "clipper.app.watcher"],
            env=env)

    watcher = start_watcher()
    port = os.environ.get("PORT", "8080")
    web = subprocess.Popen(
        [sys.executable, "-m", "clipper.app.web", "--port", port], env=env)
    print(f"run_fly: watcher pid={watcher.pid}, web pid={web.pid} port={port}",
          flush=True)
    try:
        while not stop:
            if watcher.poll() is not None:
                print(f"run_fly: watcher exited {watcher.returncode} — "
                      "restarting in 30s", flush=True)
                time.sleep(30)
                if not stop:
                    watcher = start_watcher()
            if web.poll() is not None:
                print(f"run_fly: web server exited {web.returncode} — "
                      "exiting so Fly restarts the machine", flush=True)
                break
            time.sleep(5)
    finally:
        for p in (watcher, web):
            if p and p.poll() is None:
                p.terminate()
        for p in (watcher, web):
            if p:
                try:
                    p.wait(timeout=10)
                except Exception:
                    p.kill()
    sys.exit(0 if stop else 1)


if __name__ == "__main__":
    main()
