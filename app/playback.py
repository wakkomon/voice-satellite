"""Musik-Player: MP3/Playlists vom SMB-Share + Radio-Streams, via mpg123.

Angestossen von aussen (POST /play, /stop – Agent oder Webui). Der Player
läuft in einem eigenen Thread.

Transport: mpg123 dekodiert nach stdout (Pipe), aplay schreibt rohen PCM an
die Soundkarte – das umgeht das kaputte ALSA-Ausgabemodul des Container-
mpg123 (JACK-Default, 'failure loading driver module'). Radio: curl holt
den HTTP-Stream (mit Redirects) -> mpg123 -> aplay.

Geraet-Sharing: die bcm2835-Karte ist vmip – dmix haengt, es gibt also
KEINEN Mixer. Musik und Sprachausgabe (TTS/Toene) teilen sich das Geraet
sequenziell: pause() tötet die Wiedergabe und merkt sich die Position
(Datei: Sekunden -> -k Frames; Radio: Stream wird einfach neu verbunden),
resume() startet sie wieder. Die Pipeline hält 'pause' für die gesamte
Unterhaltung (Wake bis Antwort gesprochen); Web-TTS/Haltöne pausieren nur
kurz. Hold-Counter: mehrere Halter dürfen ueberlappen; der Worker wartet
bis hold==0 und setzt das AKTUELLE Stueck an der gemerkten Position fort –
er zählt dabei niemals das naechste Stueck vor.
"""
import logging
import os
import re
import signal
import subprocess
import threading
import time

from . import config

log = logging.getLogger("music")

_SR = 44100                 # erzwungene Ausgabe-Rate (mpg123 -r)
_FRAME_SAMPLES = 1152       # MPEG-1 Layer III Frame -> -k Einheiten
_DEV_RETRIES = 3            # Startversuche, wenn das Device noch belegt war


def _setsid():
    os.setsid()             # eigener Prozess: killpg trifft nur unseren Baum


class MusicPlayer:
    def __init__(self):
        self._lock = threading.Lock()
        self._procs = []                 # [mpg123, aplay] (Radio: +curl)
        self._stop_evt = threading.Event()
        self._worker = None
        self._title = None
        self._source = None              # file | playlist | radio
        self._on_event = None
        # Job-Persistenz fuer pause/resume:
        self._job = None                 # (items, source, title)
        self._idx = 0
        self._item_t0 = 0.0              # monotonic-Start des laufenden Stücks
        self._item_offset = 0.0          # Sek. im Stück, die schon liefen
        self._paused_elapsed = 0.0       # gemerkt bei pause()
        self._hold = 0                   # >0: Geraet gehoert TTS/Pipeline
        self._web_hold = 0               # >0: Web-UI haelt die Wiedergabe an
        # Shuffle- und Radio-Fallback-Zustand
        self._shuffle_on = False         # True = Zufallswiedergabe aktiv
        self._last_playlist = None       # (items, source, title) der letzten Playlist
        self._current_item = None        # absoluter Pfad des aktuellen Stuecks

    # ── Status ─────────────────────────────────────────────────
    def is_playing(self):
        with self._lock:
            return bool(self._procs)

    def has_pending(self):
        return self._job is not None

    def status(self):
        with self._lock:
            paused = self._hold > 0 or self._web_hold > 0
            shuffle = self._shuffle_on
        return {"playing": self.is_playing(),
                "pending": self.has_pending() and not self.is_playing(),
                "paused": paused,
                "title": self._title, "source": self._source,
                "shuffle": shuffle}

    def set_on_event(self, cb):
        """Pipeline ev() als Callback – Events erscheinen im Webui-Feed."""
        self._on_event = cb

    def _ev(self, msg):
        log.info("Musik: %s", msg)
        if self._on_event:
            try:
                self._on_event(msg)
            except Exception:
                pass

    # ── Steuerung ──────────────────────────────────────────────
    def play_path(self, relpath):
        """Datei oder Playliste, RELATIV zu MUSIC_ROOT. Fehlerstring oder None
        (None = armiert; Start sobald das Geraet frei ist)."""
        root = os.path.abspath(config.MUSIC_ROOT)
        if not os.path.isdir(root):
            return f"Musikordner nicht erreichbar: {root}"
        target = self._safe_join(root, relpath)
        if target is None:
            return "Pfad liegt nicht im Musikordner."
        if os.path.isfile(target):
            if target.lower().endswith((".m3u8", ".m3u", ".pls")):
                tracks = self._parse_playlist(target, root)
                if not tracks:
                    return "Playliste leer oder nicht lesbar."
                name = os.path.splitext(os.path.basename(target))[0]
                self._arm(tracks, "playlist", f"Playliste {name}")
            else:
                self._arm([target], "file",
                          os.path.splitext(os.path.basename(target))[0])
            return None
        if os.path.isdir(target):
            # Ordner: alle Audio-Dateien alphabetisch hintereinander
            exts = {".mp3", ".m4a", ".ogg", ".oga", ".flac", ".opus", ".wav"}
            found = []
            for dirpath, dirnames, filenames in os.walk(target):
                dirnames.sort()
                for fn in sorted(filenames):
                    if os.path.splitext(fn)[1].lower() in exts:
                        found.append(os.path.join(dirpath, fn))
            if not found:
                return "Keine Audio-Dateien in: " + relpath
            self._arm(found, "dir", os.path.basename(target.rstrip("/")))
            return None
        return "Nicht gefunden: " + relpath

    def play_url(self, url, title=""):
        """Radio-Stream (http/https). Fehlerstring oder None."""
        if not re.match(r"^https?://", url or ""):
            return "Keine gültige http(s)-URL."
        name = title or re.sub(r"^https?://", "", url).split("/")[0]
        self._arm([url], "radio", name)
        return None

    def stop(self):
        """Wiedergabe komplett beenden (auch pausierte/armede)."""
        was = self.has_pending() or self.is_playing()
        self._stop_evt.set()
        self._job = None
        self._idx = 0
        self._item_t0 = 0.0
        self._item_offset = 0.0
        self._paused_elapsed = 0.0
        self._hold = 0                  # sonst warte die neue Wiedergabe
                                       # ewig auf ein nie kommendes resume
        self._web_hold = 0
        self._kill_procs()
        self.stop_bridge_relay()        # Pi-Relay ebenfalls beenden
        if was:
            self._ev("Musik gestoppt")
        return was

    def pause(self):
        """Geraet fuer Sprachausgabe freigeben. Nur hold-Zaehler erhoeht und
        SIGTERM gesendet (non-blocking, wartet NICHT auf Process-Exit).
        Damit kommt der 'wake'-Ton sofort."""
        with self._lock:
            self._hold += 1
            self._paused_elapsed = self._elapsed_now() if self._procs else 0.0
        self._kill_procs_nonblocking()

    def resume(self):
        """Gegenteil von pause(): bei hold==0 setzt der Worker das aktuelle
        Stueck an der gemerkten Position fort (wartet sonst noch)."""
        with self._lock:
            self._hold = max(0, self._hold - 1)
            free = self._hold == 0
            job = self._job
        if free and job and not self._worker_alive():
            self._start_worker()

    def pause_web(self):
        """Web-UI haelt die Wiedergabe an (eigener Zaeher, nicht der
        Pipeline-Hold fuer Sprachausgabe). Position wird gemerkt."""
        with self._lock:
            self._web_hold += 1
            self._paused_elapsed = self._elapsed_now() if self._procs else 0.0
        self._kill_procs()
        if self._job is not None:
            self._ev(f"Wiedergabe pausiert (Web): {self._title or ''}")

    def resume_web(self):
        """Web-UI setzt die Wiedergabe fort (an der gemerkten Position)."""
        with self._lock:
            self._web_hold = max(0, self._web_hold - 1)
            free = self._hold == 0 and self._web_hold == 0
            job = self._job
        if free and job and not self._worker_alive():
            self._start_worker()

    # ── Shuffle & Next/Prev ────────────────────────────────────
    def enable_shuffle(self):
        """Zufallswiedergabe aktivieren."""
        with self._lock:
            self._shuffle_on = True
            items, source, title = self._job or ([], "file", "")
            if items and source != "radio":
                import random
                self._shuffled_order = list(range(len(items)))
                random.shuffle(self._shuffled_order)
                self._shuffled_idx = 0

    def disable_shuffle(self):
        """Zufallswiedergabe deaktivieren."""
        with self._lock:
            self._shuffle_on = False

    def is_shuffle_on(self):
        with self._lock:
            return self._shuffle_on

    def _next_shuffled_index(self):
        """Naechsten zufaelligen Index zurueckgeben (ohne Wiederholung bis alle)."""
        with self._lock:
            if not self._shuffle_on:
                return None
            items = self._job[0] if self._job else []
            if not items:
                return None
            order = self._shuffled_order
            if not order:
                return None
            idx = order[self._shuffled_idx % len(order)]
            self._shuffled_idx += 1
            # Neu mischen wenn alle durch
            if self._shuffled_idx > len(order):
                random.shuffle(order)
                self._shuffled_idx = 0
            return idx

    def next_track(self):
        """Zum naechsten Titel springen. Gibt Titelname zurueck."""
        with self._lock:
            job = self._job
            if job is None:
                return "Keine Wiedergabe aktiv"
            items, source, title = job
            if not items:
                return "Leere Playlist"
            self._last_playlist = (items[:], source, title)
            if self._shuffle_on and source != "radio":
                new_idx = self._next_shuffled_index()
                if new_idx is not None:
                    self._idx = new_idx
                else:
                    self._idx = (self._idx + 1) % len(items)
            else:
                if len(items) <= 1:
                    return "Nur ein Titel in Wiedergabe"
                self._idx = (self._idx + 1) % len(items)
            new_idx = self._idx
        self._restart_at(new_idx)
        name = os.path.splitext(os.path.basename(items[new_idx]))[0]
        self._ev(f"Naechster Titel: {name}")
        return f"Nächster Titel: {name}" if source != "radio" else f"Titel {new_idx + 1}"

    def prev_track(self):
        """Zum vorherigen Titel springen. Gibt Titelname zurueck."""
        with self._lock:
            job = self._job
            if job is None:
                return "Keine Wiedergabe aktiv"
            items, source, title = job
            if not items:
                return "Leere Playlist"
            self._last_playlist = (items[:], source, title)
            if self._shuffle_on and source != "radio":
                order = self._shuffled_order
                if order and self._shuffled_idx > 0:
                    self._shuffled_idx -= 1
                    self._idx = order[self._shuffled_idx % len(order)]
                else:
                    self._idx = max(0, self._idx - 1)
            else:
                if len(items) <= 1:
                    return "Nur ein Titel in Wiedergabe"
                self._idx = (self._idx - 1) % len(items)
            new_idx = self._idx
        self._restart_at(new_idx)
        name = os.path.splitext(os.path.basename(items[new_idx]))[0]
        self._ev(f"Vorheriger Titel: {name}")
        return f"Vorheriger Titel: {name}" if source != "radio" else f"Titel {new_idx + 1}"

    def get_last_playlist(self):
        """(items, source, title) der zuletzt gestarteten Playlist, sonst None."""
        with self._lock:
            lp = self._last_playlist
            return (list(lp[0]), lp[1], lp[2]) if lp is not None else None

    def resume_last_playlist(self):
        """Letzte Playlist fortsetzen (ohne LLM-Abfrage)."""
        with self._lock:
            playlist = self._last_playlist
            if playlist is None:
                return "Keine vorherige Wiedergabe gefunden"
            items, source, title = playlist
            if not items:
                return "Leere Playlist"
            # Letzten Index aus laufendem/letztem Job übernehmen
            if self._job is not None and self._job[0] == items:
                idx = self._idx
            else:
                idx = 0
            idx = max(0, min(idx, len(items) - 1))
        self._stop_evt.set()
        self._kill_procs()
        with self._lock:
            self._job = (items[:], source, title)
            self._idx = idx
            self._item_t0 = 0.0
            self._item_offset = 0.0
            self._paused_elapsed = 0.0
            self._title = title
            self._source = source
            self._stop_evt.clear()
            self._start_worker()
        self._ev(f"Fortsetzen: {title} (Titel {idx + 1}/{len(items)})")
        return f"Fortsetzen: {title}"

    # ── Playlist-Parser ────────────────────────────────────────
    def _safe_join(self, root, rel):
        p = os.path.abspath(os.path.join(
            root, (rel or "").replace("\\", "/").lstrip("/")))
        return p if p == root or p.startswith(root + os.sep) else None

    def _parse_playlist(self, m3u, root):
        """m3u8 -> Liste existierender Dateipfade (Windows-Backslash+UTF-16).
        Eintraege sind relativ zum Share-ROOT (/m), die Playlisten-Dateien
        liegen direkt dort – zur Sicherheit auch relativ zum Playlist-Ordner."""
        try:
            raw = open(m3u, "rb").read()
        except OSError:
            return []
        text = None
        for enc in ("utf-16", "utf-8-sig", "latin-1"):
            try:
                text = raw.decode(enc)
                break
            except (UnicodeDecodeError, ValueError):
                continue
        if text is None:
            return []
        base = os.path.dirname(m3u)
        tracks = []
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            for b in (root, base):
                p = self._safe_join(b, line)
                if p and os.path.isfile(p):
                    tracks.append(p)
                    break
        return tracks[:500]

    # ── Worker ─────────────────────────────────────────────────
    def _arm(self, items, source, title):
        """Neue Wiedergabe: alte beenden, Job merken, Worker starten
        (wartet selbst auf freies Geraet)."""
        self._stop_evt.set()
        self._kill_procs()
        self._stop_evt.clear()
        self._job = (items, source, title)
        self._idx = 0
        self._item_t0 = 0.0
        self._item_offset = 0.0
        self._paused_elapsed = 0.0
        self._title = title
        self._source = source
        self._start_worker()

    def _worker_alive(self):
        return self._worker is not None and self._worker.is_alive()

    def _start_worker(self):
        self._worker = threading.Thread(target=self._run, daemon=True)
        self._worker.start()

    def _me(self):
        """True, solange dieser Worker der aktive ist (kein _arm/stop hat
        ihn ersetzt) – veraltete Worker verlassen sich ohne zu spielen."""
        with self._lock:
            return self._worker is threading.current_thread()

    def _restart_at(self, idx):
        """Worker atomisch ersetzen und bei Index idx (neu) starten.
        Der alte Worker wird via stop_evt (unter Lock gesetzt) unschaedlich,
        BEVOR er weitzaehlen kann; dann getoetet und neu gestartet."""
        old_worker = None
        with self._lock:
            old_worker = self._worker
            self._idx = idx
            self._item_t0 = 0.0
            self._item_offset = 0.0
            self._paused_elapsed = 0.0
            self._stop_evt.set()
        # Spielprozesse des alten Workers toeten (ohne Blockade)
        self._kill_procs_nonblocking()
        # alten Worker abwarten, bis er sich verabschiedet hat
        if old_worker is not None and old_worker is not threading.current_thread():
            old_worker.join(timeout=3)
        with self._lock:
            self._stop_evt.clear()
            self._start_worker()

    def _elapsed_now(self):
        """Aktuelle Position im laufenden Stueck (Sekunden), inkl. der
        Stueckteile, die vor einer resume() schon liefen."""
        if not self._item_t0:
            return self._item_offset
        return self._item_offset + (time.monotonic() - self._item_t0)

    def _kill_procs_nonblocking(self, procs=None):
        """Non-blocking Variante von _kill_procs: sendet SIGKILL sofort,
        keine Wartezeit. Nur zum schnellen Freigeben des Devices.
        Der eigentliche Process-Cleanup passiert asynchron."""
        if procs is None:
            with self._lock:
                procs, self._procs = self._procs, []
        for p in procs:
            if p.poll() is None:
                try:
                    os.killpg(os.getpgid(p.pid), signal.SIGKILL)
                except OSError:
                    try:
                        p.kill()
                    except OSError:
                        pass
        # kurz warten, aber nicht blockieren (max. 100ms)
        for p in procs:
            try:
                p.wait(timeout=0.1)
            except Exception:
                pass

    def _kill_procs(self, procs=None):
        """Prozessbaum(en) endgueltig beenden. Ohne Argument: die
        registrierten Spieler (self._procs); sonst genau die übergebenen
        (ein veralteter Worker räumt nur SEINE Prozesse auf)."""
        if procs is None:
            with self._lock:
                procs, self._procs = self._procs, []
        for p in procs:
            if p.poll() is None:
                try:
                    os.killpg(os.getpgid(p.pid), signal.SIGTERM)
                except OSError:
                    try:
                        p.terminate()
                    except OSError:
                        pass
        for p in procs:
            try:
                p.wait(timeout=2)
            except Exception:
                try:
                    os.killpg(os.getpgid(p.pid), signal.SIGKILL)
                except OSError:
                    try:
                        p.kill()
                    except OSError:
                        pass

    def _spawn(self, item, skip_frames=0.0):
        """curl [Stream] -> mpg123 -s -> aplay raw. Gibt Prozessliste zurück."""
        dec = ["mpg123", "-q", "-s", "-r", str(_SR), "-m"]
        if skip_frames:
            dec += ["-k", str(int(skip_frames))]
        c = None
        if item.startswith(("http://", "https://")):
            dec += ["-"]
            curl = ["curl", "-sSL", "-m", "86400", "--no-progress-meter", item]
            c = subprocess.Popen(curl, stdout=subprocess.PIPE,
                                 stderr=subprocess.DEVNULL, preexec_fn=_setsid)
            dec_stdin = c.stdout
        else:
            dec += [item]
            dec_stdin = None
        m = subprocess.Popen(dec, stdin=dec_stdin, stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, preexec_fn=_setsid)
        if dec_stdin:
            dec_stdin.close()
        a = subprocess.Popen(
            ["aplay", "-q", "-D", config.SPEAKER_DEVICE, "-t", "raw",
             "-r", str(_SR), "-c", "1", "-f", "S16_LE", "-"],
            stdin=m.stdout, stderr=subprocess.DEVNULL, preexec_fn=_setsid)
        m.stdout.close()
        procs = [x for x in (c, m, a) if x]
        with self._lock:
            if self._worker is threading.current_thread():
                self._procs = procs
        return procs, self._worker is threading.current_thread()

    def _run(self):
        """Worker: stueck fuer stueck; zwischen den Stuecken (und nach
        pause()) wartet er auf hold==0. Wird ein Stueck durch pause()
        getötet, ist a.wait() beendet – das Stueck wird NICHT
        weitergezählt, sondern nach resume() von der gemerkten Position
        neu gestartet (Datei) bzw. neu verbunden (Radio)."""
        while True:
            if self._stop_evt.is_set():
                return
            if not self._me():
                return                # durch _arm/stop ersetzt
            with self._lock:
                job = self._job
            if job is None:
                return
            items, source = job[0], job[1]
            if self._idx >= len(items):
                break
            # Auf freies Geraet warten (Pipeline/Web-TTS halten 'hold',
            # Web-UI 'web_hold'):
            waiting = False
            while self._hold > 0 or self._web_hold > 0:
                if self._stop_evt.is_set() or not self._me():
                    return
                if not waiting:
                    self._ev(f"Musik wartet auf das Geraet: {self._title}")
                    waiting = True
                time.sleep(0.25)
            item = items[self._idx]
            if len(items) > 1:
                t = re.sub(r"^\d+\s*[-.]\s*", "",
                           os.path.splitext(os.path.basename(item))[0])
                self._title = t
                self._source = source
            is_radio = item.startswith(("http://", "https://"))
            skip = 0
            if self._paused_elapsed:
                # Gesamtposition = zuvor gelaufene Teile + letzte Pause.
                # Datei: an dieser Position neu starten (-k Frames);
                # Radio: Live-Stream einfach neu verbinden.
                self._item_offset += self._paused_elapsed
                if not is_radio:
                    skip = self._item_offset * _SR / _FRAME_SAMPLES
                self._paused_elapsed = 0.0
            procs: list | None = None
            for attempt in range(_DEV_RETRIES):
                if not self._me():
                    return                # ersetzt -> naeher Worker spielt
                try:
                    procs, alive = self._spawn(item, skip)
                except OSError as e:
                    log.error("Player nicht gestartet: %s", e)
                    self._ev("mpg123/aplay nicht verfuegbar")
                    return
                if not alive:
                    self._kill_procs(procs)   # ersetzt, waehrend des Spawns
                    return
                time.sleep(0.5)
                if self._hold > 0:
                    # pause() kam zwischen Spawn und Check: neu warten,
                    # nichts weitzaehlen.
                    self._kill_procs(procs)
                    procs = None
                    break
                if all(p.poll() is None for p in procs):
                    break               # laeuft
                self._kill_procs(procs)
                procs = None
                if attempt < _DEV_RETRIES - 1:
                    time.sleep(1.0)     # TTS-aplay soll das Geraet loslassen
            if procs is None:
                if self._hold > 0:
                    continue            # zurueck zur Warte-Schleife
                self._ev(f"Wiedergabe fehlgeschlagen: {self._title}")
                return
            self._ev(("Radio: " + (self._title or "")) if source == "radio"
                     else f"Laueft: {self._title}")
            self._item_t0 = time.monotonic()
            rc = procs[-1].wait()
            self._kill_procs(procs)     # nur EIGENEN Baum (mpg123/curl)
            # pause() toetet zuerst mpg123; aplay sieht dann Pipe-EOF und
            # beendet sich mit rc=0, BEVOR sein SIGTERM ankommt. Der rc von
            # aplay ist also NICHT zuverlässig – massgeblich ist, ob irgendein
            # Prozess im Baum durch ein Signal starb (Unterbrechung).
            killed = any((p.returncode or 0) < 0 for p in procs)
            if not self._me():
                return                  # ersetzt: neuer Worker owns _procs
            with self._lock:
                self._procs = []
            if self._stop_evt.is_set():
                return
            if self._hold > 0 or self._web_hold > 0 or killed:
                # pause() hat den Baum getoetet -> dasselbe Stueck an der
                # gemerkten Position weiter (kein idx++, kein 'beendet').
                # hold kann schon wieder 0 sein (resume kam vor diesem Check)
                # -> continue faellt dann direkt durch die Warte-Schleife.
                # Kein Signal-Tod: Stueck lief von selbst zu Ende -> unten
                # normal weiterzaehlen, sonst wuerde es wieder von vorn
                # anfangen.
                continue
            self._idx += 1
            self._item_t0 = 0.0
            self._item_offset = 0.0
        self._ev("Wiedergabe beendet")
        with self._lock:
            if self._job is not None:
                self._job = None
                self._title, self._source = None, None


    # ── Pi-Bridge ──────────────────────────────────────────────
    def play_bridge_url(self, rtsp_url: str, title: str = "") -> str | None:
        """RTSP-Stream über Pi-Bridge abspielen.
        
        Startet Relay auf Pi (RTSP→HTTP), spielt über HTTP-Stream ab.
        Fehlerstring oder None.
        """
        import json as _json
        import urllib.request as _urllib
        
        if not config.BRIDGE_URL:
            return "Pi-Bridge nicht erreichbar"
        
        try:
            # Relay starten
            payload = _json.dumps({"url": rtsp_url, "title": title}).encode()
            req = _urllib.Request(
                f"{config.BRIDGE_URL}/relay",
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with _urllib.urlopen(req, timeout=10) as resp:
                data = _json.loads(resp.read())
        except Exception as e:
            log.error("Bridge-Relay-Start fehlgeschlagen: %s", e)
            return f"Pi-Bridge: Relay start fehlgeschlagen"
        
        relay_id = data.get("relay_id", "")
        playback_url = data.get("playback_url", "")
        
        if not playback_url:
            return "Bridge-Relay: Keine playback_url zurück"
        
        log.info("Bridge-Relay gestartet: %s", relay_id)
        
        # Relay-ID speichern für Stop
        self._bridge_relay_id = relay_id
        self._bridge_url = playback_url
        
        # Über normale play_url abspielen
        return self.play_url(playback_url, title=title)
    
    def stop_bridge_relay(self):
        """Bridge-Relay auf Pi beenden."""
        import json as _json
        import urllib.request as _urllib
        
        relay_id = getattr(self, "_bridge_relay_id", "")
        if not relay_id:
            return
        
        try:
            payload = _json.dumps({"relay_id": relay_id}).encode()
            req = _urllib.Request(
                f"{config.BRIDGE_URL}/stop_relay",
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with _urllib.urlopen(req, timeout=5) as resp:
                resp.read()
            log.info("Bridge-Relay beendet: %s", relay_id)
        except Exception as e:
            log.error("Bridge-Relay-Stop fehlgeschlagen: %s", e)
        finally:
            self._bridge_relay_id = None
            self._bridge_url = None


# Modul-Einzelinstanz (Web-Endpoints + Router greifen darauf zu)
player = MusicPlayer()
