"""Synthèse vocale hors ligne avec Piper. La voix est téléchargée au premier lancement."""

from __future__ import annotations

import logging
from pathlib import Path

import numpy as np
import sounddevice as sd

log = logging.getLogger("jarvis.tts")


class TextToSpeech:
    def __init__(self, voice: str = "fr_FR-tom-medium", voices_dir: Path = Path("voices")) -> None:
        self.voice_name = voice
        self.voices_dir = voices_dir
        self._voice = None

    def load(self) -> None:
        from piper import PiperVoice
        from piper.download_voices import download_voice

        model = self.voices_dir / (self.voice_name + ".onnx")
        if not model.exists():
            log.info("Téléchargement de la voix %s...", self.voice_name)
            self.voices_dir.mkdir(parents=True, exist_ok=True)
            download_voice(self.voice_name, self.voices_dir)
        self._voice = PiperVoice.load(model)
        log.info("Voix %s chargée.", self.voice_name)

    def speak(self, text: str) -> None:
        if not text.strip():
            return
        chunks = list(self._voice.synthesize(text))
        audio = np.concatenate([c.audio_float_array for c in chunks])
        sd.play(audio, chunks[0].sample_rate)
        sd.wait()
