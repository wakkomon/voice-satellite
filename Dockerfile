FROM python:3.11-slim

# arecord/aplay + ALSA-Libs (USB-Mic & Headphone-Jack).
# alsa-utils zieht libasound2 (trixie: libasound2t64) automatisch nach.
# mpg123 dekodiert MP3 (Mediathek + Radio-Streams) fuer den Musik-Player.
RUN apt-get update && apt-get install -y --no-install-recommends \
        alsa-utils \
        mpg123 \
        curl \
        && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
# --no-deps: pvporcupine deklariert numpy+enum34, benutzt sie aber nicht
# (nur ctypes/enum aus der stdlib). numpy laesst sich auf armv7l/cp311 nicht
# bauen und wuerde den Build sprengen.
RUN pip install --no-cache-dir --no-deps -r requirements.txt

COPY app/ ./app/
COPY wake/ ./wake/
COPY asound.conf /etc/asound.conf

# Datenverzeichnis (SQLite-Gedächtnis) – Volume in der compose-Datei
RUN mkdir -p /data

# Läuft nicht privilegiert; braucht /dev/snd + /dev/fb1
# (via compose devices + audio-/video-Gruppe; video = GID 44 auf dem Pi)
# (Gruppe heisst 'sat', nicht 'voice' – die Basis-Debian hat bereits eine voice-Gruppe)
RUN useradd -m -u 1000 -G audio sat && chown -R sat /app /data
USER sat

EXPOSE 8770

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8770/health', timeout=3).status==200 else 1)"

CMD ["python", "-m", "app.main"]
