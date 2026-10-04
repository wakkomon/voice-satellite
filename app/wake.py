"""Wake-Word-Erkennung: pvporcupine 1.9.5 (CPU, offline, ohne Access-Key).

Die Datei wake/computer_raspberry-pi.ppn ist byteidentisch zur im Wheel
bundelten Pi-Version. pvporcupine 1.9.5 waehlt die Raspberry-Pi-Shared-Lib
(self._library_path ueber CPU-part) automatisch; frame_length ist 512 Samples
bei 16 kHz = genau ein 32-ms-Frame unserer Aufnahme.

Aufruf-Vertrag: process(pcm_int16_list) pro Frame; Rueckgabe >= 0 == Treffer.
Wake und Aufnahme teilen denselben arecord-Stream und denselben Thread
(siehe main.Pipeline) – zwei Leser am selben FIFO wuerden sich gegenseitig
die Frames stehlen.
"""
import array
import logging
import os

import pvporcupine

log = logging.getLogger("wake")


class WakeWord:
    def __init__(self, ppn_path: str, sensitivity: float = 0.5):
        if not os.path.exists(ppn_path):
            raise FileNotFoundError(ppn_path)
        self._pp = pvporcupine.create(
            keyword_paths=[ppn_path],
            sensitivities=[sensitivity],
        )
        self.frame_length = self._pp.frame_length   # 512 samples @16k
        self.sample_rate = self._pp.sample_rate      # 16000
        log.info("Porcupine bereit: frame=%d rate=%d",
                 self.frame_length, self.sample_rate)

    def feed(self, pcm_bytes: bytes) -> bool:
        """Ein Frame (frame_length*2 Bytes, s16le) -> True bei Wake."""
        if len(pcm_bytes) < self.frame_length * 2:
            return False
        samples = array.array("h")
        samples.frombytes(pcm_bytes[: self.frame_length * 2])
        hit = self._pp.process(samples)
        return hit >= 0

    def close(self):
        try:
            self._pp.delete()
        except Exception:
            pass
