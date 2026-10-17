"""Audio: Aufnahme über persistenten `arecord` (16 kHz mono s16le) + aplay-Wiedergabe.

Warum CLI-Tools statt Python-Binding? Auf armv7l (Pi 3) gibt es keine
zuverlässigen PyALSA-/PortAudio-Wheels; alsa-utils ist im Image ohnehin drauf.

WICHTIG: Es gibt genau EINEN Leser am arecord-Strom (den Pipeline-Thread).
Nach jeder Wiedergabe wird der Stream per restart() geleert, damit während des
Sprechens aufgehäufte Frames (auch das eigene TTS vom Mic) nicht als neue
Sprache interpretiert werden.
"""
import array
import collections
import math
import subprocess
import time
import wave

from . import config


class MicStream:
    """Ein persistenter arecord-Prozess, von dem frame_bytes große Frames gelesen werden."""

    def __init__(self, frame_bytes):
        self.frame_bytes = frame_bytes
        self._proc = None
        self.start()

    def start(self):
        self._proc = subprocess.Popen(
            ["arecord", "-q", "-t", "raw", "-r", str(config.SAMPLE_RATE),
             "-c", "1", "-f", "S16_LE", "-D", config.MIC_DEVICE],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            bufsize=self.frame_bytes * 8,
        )

    def drain(self):
        """Buffer leeren – ALLE Pufferdaten verwerfen, ohne arecord zu
        restarten. 10-50ms statt 300-500ms beim restart().

        WICHTIG: arecord läuft permanent, die Pipe liefert NIE b'' (kein
        EOF) – ein while-True-Loop ohne Zeitlimit hängt ewig (seit v1.9.5
        im Cleanup-Thread / im Main-Loop -> 'folgefrage'-Hang). Deshalb
        festes Zeitlimit: ~0,1 s reichen, um den Ton-/TTS-Echo aus dem
        Puffer zu werfen; danach ist das Fenster ohnehin Geschichte."""
        if self._proc is None or self._proc.stdout is None:
            return
        deadline = time.monotonic() + 0.10
        while True:
            try:
                data = self._proc.stdout.read(self.frame_bytes * 16)
                if not data:
                    break
            except Exception:
                break
            if time.monotonic() >= deadline:
                break

    def restart(self):
        """Prozess neu starten – verwirft alles, was sich im Pipe-Buffer angestaut hat."""
        self.stop()
        self.start()

    def read(self):
        """Genau einen Frame blocking lesen; None bei Prozess-Ende."""
        buf = bytearray()
        while len(buf) < self.frame_bytes:
            try:
                chunk = self._proc.stdout.read(self.frame_bytes - len(buf))
            except (ValueError, OSError):
                return None
            if not chunk:
                return None
            buf.extend(chunk)
        return bytes(buf)

    def stop(self):
        if not self._proc:
            return
        try:
            self._proc.stdout.close()
        except Exception:
            pass
        try:
            self._proc.terminate()
            self._proc.wait(timeout=3)
        except Exception:
            try:
                self._proc.kill()
            except Exception:
                pass
        self._proc = None


# ── VAD ────────────────────────────────────────────────────────
class EnergyVad:
    """Adaptiver RMS-Schwellwert. Kein natives Dependency (armv7-freundlich).

    Die Geräuschuntergrenze wird aus Stummsegmenten gelernt; Sprache muss
    deutlich (3x) darüber liegen oder den Mindestpegel überschreiten.
    """

    def __init__(self):
        self.noise_floor = 150.0
        self._speech_seen = False

    def reset(self):
        self.__init__()

    @staticmethod
    def rms(frame):
        a = array.array("h")
        a.frombytes(frame[: len(frame) - len(frame) % 2])
        if not a:
            return 0.0
        return math.sqrt(sum(x * x for x in a) / len(a))

    def is_speech(self, frame):
        r = self.rms(frame)
        thresh = max(config.ENERGY_THRESH, self.noise_floor * 3.0)
        if r > thresh:
            self._speech_seen = True
            return True
        if not self._speech_seen:
            self.noise_floor = 0.97 * self.noise_floor + 0.03 * r
        return False


class WebRtcVad:
    """Optional (nur Desktop/x86 zum Testen); puffert auf 30-ms-Subframes."""

    def __init__(self):
        import webrtcvad
        self.vad = webrtcvad.Vad(config.VAD_AGGRESSIVEN)
        self._sub_bytes = int(config.SAMPLE_RATE * 0.030) * 2
        self._buf = bytearray()
        self._speech_seen = False

    def reset(self):
        self._buf.clear()
        self._speech_seen = False

    def is_speech(self, frame):
        self._buf.extend(frame)
        hit = False
        while len(self._buf) >= self._sub_bytes:
            sub = bytes(self._buf[: self._sub_bytes])
            del self._buf[: self._sub_bytes]
            try:
                if self.vad.is_speech(sub, config.SAMPLE_RATE):
                    hit = True
            except Exception:
                return False
        if hit:
            self._speech_seen = True
        return hit


def make_vad():
    if config.VAD_MODE == "webrtc":
        try:
            return WebRtcVad()
        except Exception as e:
            print(f"[audio] webrtcvad nicht verfügbar ({e}) – nutze Energie-VAD")
    return EnergyVad()


# ── Aufnahme nach dem Codewort ────────────────────────────────
class Recorder:
    def __init__(self, mic, vad, frame_bytes):
        self.mic = mic
        self.vad = vad
        self.frame_bytes = frame_bytes
        self.frame_sec = (frame_bytes // 2) / config.SAMPLE_RATE

    def _skip_wake_tail(self):
        for _ in range(max(0, int(config.WAKE_SKIP_SEC / self.frame_sec))):
            if self.mic.read() is None:
                return False
        return True

    def capture(self):
        """Wake-Rest verwerfen, dann Sprache bis Stille-Ende sammeln. -> pcm|None."""
        if not self._skip_wake_tail():
            return None
        return self._record(allow_wait=0.0)

    def capture_followup(self, wait_sec):
        """Follow-up-Modus: OHNE Wake-Word lauschen, bis Sprache kommt oder
        wait_sec Stille vergehen (FOLLOWUP_SEC). -> pcm | None."""
        return self._record(allow_wait=wait_sec)

    def _record(self, allow_wait):
        """Gemeinsames Herzstück: Sprache einsammeln bis Stille-Ende.
        allow_wait: wie lange vor dem ersten Sprach-Frame ohne Sprache
        gewartet wird (0 = wie bisher; Folgefrage wartet FOLLOWUP_SEC)."""
        preroll_len = max(1, int(config.PREROLL_SEC / self.frame_sec))
        preroll = collections.deque(maxlen=preroll_len)
        chunks = []
        started = False
        silence = 0.0
        waited = 0.0
        total = 0.0
        while True:
            frame = self.mic.read()
            if frame is None:
                break
            total += self.frame_sec
            if self.vad.is_speech(frame):
                if not started:
                    started = True
                    chunks.extend(preroll)
                    preroll.clear()
                silence = 0.0
            else:
                silence += self.frame_sec
                if not started:
                    waited += self.frame_sec
                    preroll.append(frame)
                    if allow_wait and waited >= allow_wait:
                        return None          # keine Folgefrage -> Follow-up zu
                    if not allow_wait and waited >= config.MAX_REC_SEC:
                        break                # Nach dem Wake-Word nichts gesagt
            if started:
                chunks.append(frame)
            if started and silence >= config.SILENCE_SEC:
                break
            if started and total >= config.MAX_REC_SEC:
                break
        if not started:
            return None
        pcm = b"".join(chunks)
        if len(pcm) / 2 / config.SAMPLE_RATE < config.MIN_REC_SEC:
            return None
        return pcm


# ── Utilities ──────────────────────────────────────────────────
def to_wav(pcm_bytes, rate=None):
    import io
    rate = rate or config.SAMPLE_RATE
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm_bytes)
    return buf.getvalue()


def play_wav(wav_bytes, device=None, blocking=True):
    """WAV -> aplay.

    Standard: blockiert bis fertig (Pipeline-Thread).
    blocking=False: startet aplay im Hintergrund und gibt sofort zurück.
    """
    device = device or config.SPEAKER_DEVICE
    p = subprocess.Popen(["aplay", "-q", "-D", device],
                         stdin=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        p.stdin.write(wav_bytes)
        p.stdin.close()
        if blocking:
            p.wait(timeout=180)
        else:
            p.poll()  # non-blocking status check
    except Exception:
        try:
            p.kill()
        except Exception:
            pass
