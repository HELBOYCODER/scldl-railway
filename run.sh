#!/bin/bash
LOCKFILE="/tmp/scldl_bot.lock"
exec 200>"$LOCKFILE"
flock -n 200 || { echo "Another instance of run.sh is already running."; exit 1; }

cd /home/ersaz/scldl-bot
export PATH=$HOME/bin:$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin:$PATH

while true; do
    echo "[$(date)] Starting bot.py..." >> bot.log
    python3 bot.py >> bot.log 2>&1
    echo "[$(date)] bot.py exited with code $?. Restarting in 3s..." >> bot.log
    sleep 3
done
