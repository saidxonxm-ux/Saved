FROM python:3.11-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg ca-certificates curl unzip \
    && rm -rf /var/lib/apt/lists/*
# YouTube uchun JS runtime (yt-dlp talab qiladi). O'rnatilmasa ham build to'xtamaydi.
RUN curl -fsSL https://deno.land/install.sh | DENO_INSTALL=/usr/local sh || true
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -U -r requirements.txt
COPY . .
# Har ishga tushganda yt-dlp yangilanadi (Instagram/YouTube tez-tez o'zgaradi). Tarmoq bo'lmasa ham bot ishlayveradi.
CMD ["sh", "-c", "pip install --no-cache-dir -q -U 'yt-dlp[default]' || true; exec python -m bot.main"]
