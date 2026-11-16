"""Reconnaissance vocale : micro -> texte avec faster-whisper (GPU si possible, sinon CPU)."""

from __future__ import annotations

import importlib.util
import logging
import os
import time
from typing import Callable

import numpy as np
import sounddevice as sd

log = logging.getLogger("jarvis.stt")
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

_RATE = 16000
_BLOCK = 1600            # 0,1 s
_START_TIMEOUT = 5.0     # abandonne si personne ne parle dans ce délai


def _add_cuda_dlls() -> None:
    """Rend visibles les DLL CUDA installées par pip (nvidia-cublas-cu12, nvidia-cudnn-cu12)."""
    for package in ("nvidia.cublas", "nvidia.cudnn"):
        try:
            spec = importlib.util.find_spec(package)
        except ModuleNotFoundError:
            continue
        for location in (spec.submodule_search_locations or []) if spec else []:
            bin_dir = os.path.join(location, "bin")
            if os.path.isdir(bin_dir):
                os.add_dll_directory(bin_dir)
                os.environ["PATH"] = bin_dir + os.pathsep + os.environ["PATH"]


class SpeechToText:
    def __init__(self, model: str = "large-v3-turbo", device: str = "cuda", language: str = "fr",
                 prompt: str | None = None, silence_threshold: float = 0.008,
                 silence_duration: float = 1.2, max_duration: float = 15.0) -> None:
        self.model_name = model
        self.device = device
        self.language = language
        self.prompt = prompt
        self.silence_threshold = silence_threshold
        self.silence_duration = silence_duration
        self.max_duration = max_duration
        self._model = None

    def _model_path(self) -> str:
        """Dossier du modèle : celui déjà téléchargé (démarrage hors ligne), sinon téléchargement."""
        from faster_whisper.utils import download_model

        try:
            return download_model(self.model_name, local_files_only=True)
        except Exception:
            log.info("Whisper '%s' absent du cache : téléchargement.", self.model_name)
            return download_model(self.model_name)

    def load(self) -> None:
        from faster_whisper import WhisperModel

        path = self._model_path()
        if self.device == "cuda":
            _add_cuda_dlls()
            try:
                self._model = WhisperModel(path, device="cuda", compute_type="float16")
                segments, _ = self._model.transcribe(np.zeros(_RATE, dtype=np.float32))
                list(segments)  # force un vrai calcul pour vérifier que CUDA fonctionne
                log.info("Whisper '%s' chargé sur GPU.", self.model_name)
                return
            except Exception as exc:
                log.warning("GPU indisponible pour Whisper (%s), passage sur CPU.", exc)
        self._model = WhisperModel(path, device="cpu", compute_type="int8")
        log.info("Whisper '%s' chargé sur CPU.", self.model_name)

    def listen(self, start_timeout: float | None = None, stop: Callable[[], bool] | None = None) -> str:
        """Enregistre jusqu'à un silence après la parole, puis transcrit. Abandonne si personne ne parle
        avant `start_timeout` secondes, ou si `stop()` devient vrai avant la parole (réponse tapée au clavier)."""
        if self._model is None:
            self.load()

        timeout = _START_TIMEOUT if start_timeout is None else start_timeout
        blocks: list[np.ndarray] = []
        speech_started = False
        silent_time = 0.0
        start = time.monotonic()
        with sd.InputStream(samplerate=_RATE, channels=1, dtype="float32", blocksize=_BLOCK) as stream:
            while time.monotonic() - start < self.max_duration:
                block, _ = stream.read(_BLOCK)
                blocks.append(block[:, 0].copy())
                if np.abs(block).mean() >= self.silence_threshold:
                    speech_started, silent_time = True, 0.0
                else:
                    silent_time += _BLOCK / _RATE
                if speech_started and silent_time >= self.silence_duration:
                    break
                if not speech_started and (time.monotonic() - start > timeout or (stop and stop())):
                    return ""

        text = self.transcribe(np.concatenate(blocks))
        log.info("Entendu : %s", text)
        return text

    def transcribe(self, audio: np.ndarray, hint: str | None = None) -> str:
        """audio : float32, 16 kHz. hint : premier jet rapide du mot d'activation, avec cette seule phrase pour
        exemple (« Ok Jarvis. » : sans elle, Whisper écrit souvent « Ok, j'ai revu »)."""
        quick = hint is not None
        segments, _ = self._model.transcribe(
            audio, language=self.language, beam_size=1 if quick else 5,
            initial_prompt=hint if quick else self.prompt, vad_filter=not quick)
        return " ".join(s.text.strip() for s in segments).strip()
