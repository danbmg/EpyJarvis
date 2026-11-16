"""Mot d'activation, écouté en continu.

- PhraseWakeWord (par défaut : « Ok Jarvis ») : aucun modèle openwakeword n'existe pour cette phrase. Le détecteur
  de voix Silero (processeur) découpe les prises de parole ; Whisper, déjà chargé pour les commandes, en fait un
  premier jet rapide. Dès que la phrase y est, l'anneau passe au vert et la commande est captée par le même flux
  micro (rien n'est perdu) : « Ok Jarvis, lance Spotify » d'une traite, ou « Ok Jarvis »... « lance Spotify ».
- WakeWord : un modèle openwakeword pré-entraîné (« Hey Jarvis »), plus léger, mais limité aux phrases existantes.
"""

from __future__ import annotations

import collections
import difflib
import logging
import queue
import re
import threading
import time
from typing import Callable

import numpy as np
import sounddevice as sd

from jarvis.tools import norm

log = logging.getLogger("jarvis.wakeword")

_RATE = 16000
_BLOCK = 1280      # 80 ms, taille attendue par openwakeword

# Prises de parole, en blocs de 80 ms
_PRE = 4           # gardés avant la voix (0,32 s) : le début de « Ok »
_PAUSE = 5         # de silence (0,4 s) : on cherche la phrase dans ce qui vient d'être dit
_MIN_VOICE = 4     # de voix au minimum pour transcrire (0,32 s : pas les clics ni la toux)
_CHECK = 25        # parole continue (conversation, chanson) : on cherche aussi toutes les 2 s...
_WINDOW = 38       # ... dans les 3 dernières secondes (1 s de recouvrement)
_END = 15          # de silence (1,2 s, comme le micro) qui terminent la commande
_FOLLOW = 62       # « Ok Jarvis » seul : 5 s pour commencer la commande (comme le micro)
_MAX = 250         # de la phrase à la fin de la commande (20 s)
_OK = {"ok", "okay", "oke", "okey", "oki", "okai", "ouke", "oukey"}


def after_phrase(text: str, phrase: str) -> str | None:
    """Ce qui suit la phrase d'activation dans `text` (« » si rien), ou None si elle n'y est pas.
    Tolère les graphies de Whisper : « OK, Jarvis », « Okay Jarvis », « O.K. Jarvis », « Oké Djarvis »."""
    text = re.sub(r"(?i)\bo\.\s?k\b\.?", "ok", text)
    words = [(m, _canon(m.group())) for m in re.finditer(r"[\w']+", text)]
    target = [_canon(w) for w in phrase.split()]
    for i in range(len(words) - len(target) + 1):
        if all(_close(words[i + k][1], t) for k, t in enumerate(target)):
            return text[words[i + len(target) - 1][0].end():].lstrip(" ,.;:!?-–…").strip()
    return None


def _canon(word: str) -> str:
    word = norm(word)
    return "ok" if word in _OK else word


def _close(word: str, target: str) -> bool:
    """« ok » exact ; « jarvis » approché (« Jarvi », « Djarvis », « Jervis »)."""
    return word == target or (len(target) >= 3 and difflib.SequenceMatcher(None, word, target).ratio() >= 0.75)


def _audio(blocks: list[np.ndarray]) -> np.ndarray:
    return np.concatenate(blocks).astype(np.float32) / 32768


class PhraseWakeWord:
    def __init__(self, on_wake: Callable[[str], None], on_detect: Callable[[], None],
                 transcribe: Callable[[np.ndarray, str | None], str], phrase: str = "ok jarvis",
                 threshold: float = 0.5) -> None:
        self.on_wake = on_wake          # la commande dite (« » : rien après la phrase)
        self.on_detect = on_detect      # phrase reconnue : Jarvis écoute la commande
        self.transcribe = transcribe    # (audio float32 16 kHz, phrase d'exemple du premier jet ou None) -> texte
        self.phrase = phrase
        self.hint = phrase.title() + "."  # « Ok Jarvis. »
        self.threshold = threshold      # détecteur de voix : plus haut = ignore davantage bruits et musique
        self._paused = threading.Event()
        self._resumed_at = 0.0
        self._running = False
        self._thread: threading.Thread | None = None
        self._reset()

    def start(self) -> None:
        from openwakeword.vad import VAD

        self._vad = VAD()
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True, name="wakeword")
        self._thread.start()
        log.info("Mot d'activation actif (« %s », détecteur de voix + Whisper).", self.phrase)

    def stop(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=2)

    def pause(self) -> None:
        self._paused.set()

    def resume(self) -> None:
        self._resumed_at = time.monotonic()  # le son capté pendant la pause (Jarvis qui parle) est jeté
        self._paused.clear()

    def _loop(self) -> None:
        blocks: queue.Queue[tuple[float, np.ndarray]] = queue.Queue()  # rien ne se perd pendant une transcription

        def on_audio(indata, frames, time_info, status) -> None:
            blocks.put((time.monotonic(), indata[:, 0].copy()))

        with sd.InputStream(samplerate=_RATE, channels=1, dtype="int16", blocksize=_BLOCK, callback=on_audio):
            while self._running:
                try:
                    at, block = blocks.get(timeout=0.5)
                except queue.Empty:
                    continue
                if self._paused.is_set() or at < self._resumed_at:
                    self._reset()
                    continue
                try:
                    self._feed(block, self._vad.predict(block, frame_size=640) >= self.threshold)
                except Exception:
                    log.exception("Échec de l'analyse du mot d'activation")
                    self._reset()

    def _reset(self) -> None:
        self._pre: collections.deque[np.ndarray] = collections.deque(maxlen=_PRE)
        self._seg: list[np.ndarray] = []
        self._voiced = self._silent = self._since_check = self._found_at = self._spoke = 0
        self._found: str | None = None  # la suite entendue au premier jet, une fois la phrase reconnue

    def _feed(self, block: np.ndarray, speech: bool) -> None:
        """Un bloc de 80 ms et l'avis du détecteur de voix."""
        if not self._seg:
            if speech:
                self._seg, self._voiced, self._silent, self._since_check = [*self._pre, block], 1, 0, 0
            else:
                self._pre.append(block)
            return
        self._seg.append(block)
        self._silent = 0 if speech else self._silent + 1

        if self._found is None:  # pas encore la phrase
            self._voiced += speech
            self._since_check += 1
            pause = self._silent >= _PAUSE
            if pause or self._since_check >= _CHECK:
                if self._voiced >= _MIN_VOICE:
                    self._check()
                if self._found is None:
                    if pause:
                        self._reset()
                    else:
                        self._seg = self._seg[-_WINDOW:]
            return

        self._spoke += speech  # la commande, après la phrase
        waiting = not self._found and not self._spoke  # « Ok Jarvis » seul : on attend la commande
        if self._silent >= (_FOLLOW if waiting else _END) or len(self._seg) >= _MAX:
            self._fire()
            self._reset()

    def _check(self) -> None:
        window = self._seg[-_WINDOW:]
        self._since_check = 0
        rest = after_phrase(self.transcribe(_audio(window), self.hint), self.phrase)
        if rest is not None:
            self._seg, self._found, self._found_at, self._spoke = window, rest, len(window), 0
            log.info("Mot d'activation détecté (« %s »).", self.phrase)
            self.on_detect()

    def _fire(self) -> None:
        """Fin de la commande : relue avec soin (faisceau de 5, phrase d'exemple de la config)."""
        if self._found:      # « Ok Jarvis, lance Spotify » d'une traite : tout est relu
            text = self.transcribe(_audio(self._seg), None)
            rest = after_phrase(text, self.phrase)
            command = text if rest is None else rest
        elif self._spoke:    # « Ok Jarvis »... puis la commande : seulement ce qui suit la phrase
            command = self.transcribe(_audio(self._seg[self._found_at:]), None)
        else:
            command = ""
        log.info("Commande après « %s » : %s", self.phrase, command or "(rien)")
        self.on_wake(command)


class WakeWord:
    def __init__(self, on_wake: Callable[[], None], model: str = "hey_jarvis",
                 threshold: float = 0.5, cooldown: float = 3.0) -> None:
        self.on_wake = on_wake
        self.model_name = model
        self.threshold = threshold
        self.cooldown = cooldown
        self._paused = threading.Event()
        self._running = False
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        from openwakeword.model import Model
        from openwakeword.utils import download_models

        download_models(model_names=[self.model_name])  # ne retélécharge pas s'ils existent
        self._model = Model(wakeword_models=[self.model_name], inference_framework="onnx")
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True, name="wakeword")
        self._thread.start()
        log.info("Mot d'activation actif (%s, seuil %.2f).", self.model_name, self.threshold)

    def stop(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=2)

    def pause(self) -> None:
        self._paused.set()

    def resume(self) -> None:
        self._model.reset()
        self._paused.clear()

    def _loop(self) -> None:
        last = 0.0
        with sd.InputStream(samplerate=_RATE, channels=1, dtype="int16", blocksize=_BLOCK) as stream:
            while self._running:
                block, _ = stream.read(_BLOCK)  # on lit même en pause pour vider le tampon
                if self._paused.is_set():
                    continue
                score = max(self._model.predict(block[:, 0]).values())
                if score >= self.threshold and time.monotonic() - last > self.cooldown:
                    last = time.monotonic()
                    log.info("Mot d'activation détecté (score %.2f).", score)
                    self._model.reset()
                    self.on_wake()
