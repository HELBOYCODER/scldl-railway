FROM python:3.11-slim
RUN apt-get update && apt-get install -y ffmpeg && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir yt-dlp requests
WORKDIR /app
COPY bale.py picofile.py sync_playlist.py bot.py db.py ./
# optional: keepalive dumb loop not needed on Railway (Railway keeps process up)
CMD ["python", "sync_playlist.py"]
