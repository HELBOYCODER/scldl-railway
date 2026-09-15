#!/bin/bash
cd /home/ersaz/scldl-bot
export PATH=$HOME/bin:$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin:$PATH

if ! pgrep -f "python3 bot.py" > /dev/null; then
    echo "[$(date)] Bot is down, launching via run.sh..." >> keepalive.log
    nohup ./run.sh > /dev/null 2>&1 &
fi
