FROM python:3.12-slim

# ffmpeg is required by yt-dlp to extract/convert audio to mp3
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py healthcheck.py ./
COPY app ./app

# Run as non-root
RUN useradd -m appuser
USER appuser

# Liveness: the bot refreshes /tmp/heartbeat from its event loop.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "healthcheck.py"]

CMD ["python", "bot.py"]
