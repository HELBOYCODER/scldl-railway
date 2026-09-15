#!/bin/bash
LOCKFILE="/tmp/sync_wym.lock"
exec 201>"$LOCKFILE"
flock -n 201 || { echo "Sync daemon is already running."; exit 1; }

cd /home/ersaz/scldl-bot
export PATH=$HOME/bin:$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin:$PATH

while true; do
    echo "[$(date)] Launching sync_playlist.py..." >> /home/ersaz/scldl-bot/sync.log
    python3 sync_playlist.py >> /home/ersaz/scldl-bot/sync.log 2>&1
    echo "[$(date)] sync_playlist.py exited with code $?. Auto-resuming in 5s..." >> /home/ersaz/scldl-bot/sync.log
    sleep 5
done
