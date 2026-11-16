"""Moteur de Jarvis : écoute -> agent (LLM + outils) -> réponse, dans un thread de travail.

L'interface envoie des commandes (texte ou « écoute ») et reçoit des événements
via des files d'attente : elle ne bloque jamais pendant un chargement ou une action.
"""

from __future__ import annotations

import enum
import logging
import queue
import re
import threading
import unicodedata
from dataclasses import dataclass
from typing import Any

from jarvis import shortcuts
from jarvis.agent import Agent, Reply
from jarvis.config import Config, setup_logging
from jarvis.llm import OllamaClient
from jarvis.tools import Result, Toolbox
from jarvis.tools import (agenda, apps, desktop, memory, messaging, shell, spotify, steam, system, timers, todo,
                          ui, web, youtube)

log = logging.getLogger("jarvis.engine")

_LISTEN = object()  # commande « écouter le micro »
_ANSWER = object()  # écouter la réponse à une confirmation (oui / non), sans « Ok Jarvis »
_WELCOME = object()  # écouter juste après l'annonce de démarrage, sans « Ok Jarvis »
_QUIET = object()    # « Ok Jarvis » sans commande à la suite : retour au repos
_ANSWER_WAIT = 8.0  # secondes pour commencer à répondre à une confirmation
_WELCOME_WAIT = 8.0  # secondes pour commencer à parler après « Jarvis Activated »
_SPOKEN_MAX = 400   # caractères prononcés au plus (~25 s) ; le reste est affiché


class State(enum.Enum):
    IDLE = "idle"
    LISTENING = "listening"
    THINKING = "thinking"
    SPEAKING = "speaking"
    SLEEPING = "sleeping"


@dataclass
class _Announce:
    text: str        # commande « dire ce texte » (minuteur terminé...), envoyée depuis un autre thread


@dataclass
class _Heard:
    text: str        # commande dite d'une traite après le mot d'activation (« Ok Jarvis, lance Spotify »)


@dataclass
class Event:
    kind: str        # "state", "user", "jarvis", "error", "info", "step", "quit"
    data: Any = None


class Engine:
    wake_name = "Ok Jarvis"  # le mot d'activation, tel qu'affiché

    def __init__(self, config_path: str | None = None) -> None:
        self.cfg = cfg = Config(config_path)
        setup_logging(cfg.get("logging.level", "INFO"), cfg.path("logging.file", "jarvis.log"))
        self.wake_name = (cfg.get("wakeword.phrase", "ok jarvis")
                          or cfg.get("wakeword.model", "hey_jarvis").replace("_", " ")).title()

        self.events: queue.Queue[Event] = queue.Queue()
        self._commands: queue.Queue[Any] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._sleeping = False
        self._sleep_requested = False
        self._quit_requested = False

        self.llm = OllamaClient(cfg.get("llm.model", "qwen3:8b"),
                                cfg.get("llm.base_url", "http://127.0.0.1:11434"),
                                cfg.get("llm.timeout", 60), cfg.get("llm.keep_alive", "30m"),
                                cfg.get("llm.num_ctx", 16384), cfg.get("llm.think"))
        self.catalog = apps.AppCatalog(cfg.get("apps", []))
        self.memory = memory.Memory(cfg.path("memory.db_path", "jarvis_memory.db"))
        self.timers = timers.Timers(lambda text: self._commands.put(_Announce(text)))
        self.toolbox = self._build_toolbox()
        self.agent = Agent(self.llm, self.toolbox, self._on_agent_event, self.memory.facts, shortcuts.match)
        self.stt = None
        self.tts = None
        self.wakeword = None

    def _build_toolbox(self) -> Toolbox:
        box = Toolbox()
        spotify_client = spotify.client_from_config(self.cfg)
        apps.register(box, self.catalog)
        ui.register(box, ui.Navigator())
        desktop.register(box, spotify_client)
        web.register(box)
        youtube.register(box)
        steam.register(box)
        timers.register(box, self.timers)
        system.register(box, self._request_sleep, self._request_quit)
        memory.register(box, self.memory)
        shell.register(box)
        messaging.register(box)
        todo.register(box)
        if spotify_client:
            spotify.register(box, spotify_client)
        # Google Agenda : seulement s'il est configuré. Les adresses à inviter doivent venir de l'utilisateur.
        calendar = agenda.from_config(self.cfg, heard=lambda: self.agent.heard())
        if calendar:
            agenda.register(box, calendar)
        return box

    # ---- API appelée depuis l'interface ----

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True, name="engine")
        self._thread.start()

    def stop(self) -> None:
        self._commands.put(None)
        if self.wakeword:
            self.wakeword.stop()

    def submit_text(self, text: str) -> None:
        if text.strip():
            self._commands.put(text.strip())

    def listen(self) -> None:
        self._commands.put(_LISTEN)

    # ---- Thread de travail ----

    def _emit(self, kind: str, data: Any = None) -> None:
        self.events.put(Event(kind, data))

    def _set_state(self, state: State) -> None:
        self._emit("state", state)

    def _idle(self) -> None:
        self._set_state(State.SLEEPING if self._sleeping else State.IDLE)

    def _run(self) -> None:
        import uiautomation as auto

        with auto.UIAutomationInitializerInThread():  # l'automatisation d'interface exige COM par thread
            self._set_state(State.THINKING)
            self._load_components()
            self._welcome()
            self._idle()

            while (command := self._commands.get()) is not None:
                try:
                    self._dispatch(command)
                except Exception as exc:
                    log.exception("Erreur inattendue")
                    self._emit("error", "Erreur inattendue : {}".format(exc))
                self._idle()

    def _dispatch(self, command: Any) -> None:
        if command is _LISTEN:
            self._handle_voice()
        elif command is _WELCOME:
            self._handle_voice(_WELCOME_WAIT, "Dis « {} » quand tu veux me parler.".format(self.wake_name)
                               if self.wakeword else "Clique sur MICRO ou écris ta demande quand tu veux.")
        elif command is _ANSWER:
            if self.agent.waiting_answer:  # sinon, déjà répondu au clavier entre-temps
                self._handle_voice(_ANSWER_WAIT, "Pas de réponse entendue : dis « {}, oui » ou « non », ou écris-le."
                                   .format(self.wake_name))
        elif isinstance(command, _Heard):
            self._emit("user", command.text)
            self._handle_text(command.text, by_voice=True)
        elif command is _QUIET:
            pass  # la boucle repasse à l'état de repos
        elif isinstance(command, _Announce):
            self._say(command.text)
        else:
            self._handle_text(command, by_voice=False)

    def _load_components(self) -> None:
        """Charge les briques lourdes une par une ; une brique en panne n'empêche pas les autres."""
        cfg = self.cfg
        if cfg.get("llm.autostart", True) and not self.llm.is_up():
            self._emit("info", "Démarrage d'Ollama...")
            self.llm.start_server()
        error = self.llm.check()
        if error:
            self._emit("error", error)
        else:
            # Le modèle se charge dans la carte graphique pendant que le micro et la voix se chargent.
            self._emit("info", "Chargement du modèle {}...".format(self.llm.model))
            threading.Thread(target=self._warm_up, daemon=True, name="warm-up").start()

        try:
            self.catalog.load_start_menu()
        except Exception as exc:
            log.warning("Menu Démarrer illisible : %s", exc)

        try:
            from jarvis.voice.stt import SpeechToText
            self.stt = SpeechToText(
                cfg.get("stt.model", "large-v3-turbo"), cfg.get("stt.device", "cuda"),
                cfg.get("stt.language", "fr"), cfg.get("stt.prompt"),
                cfg.get("stt.silence_threshold", 0.008), cfg.get("stt.silence_duration", 1.2),
                cfg.get("stt.max_duration", 15.0))
            self.stt.prompt = self._stt_prompt()
            self.stt.load()
        except Exception as exc:
            log.exception("Reconnaissance vocale indisponible")
            self._emit("error", "Micro indisponible : {}".format(exc))
            self.stt = None

        if cfg.get("tts.enabled", True):
            try:
                from jarvis.voice.tts import TextToSpeech
                self.tts = TextToSpeech(cfg.get("tts.voice", "fr_FR-tom-medium"),
                                        cfg.path("tts.voices_dir", "voices"))
                self.tts.load()
            except Exception as exc:
                log.exception("Synthèse vocale indisponible")
                self._emit("error", "Voix indisponible : {}".format(exc))
                self.tts = None

        if cfg.get("wakeword.enabled", True) and self.stt:
            try:
                from jarvis.voice.wakeword import PhraseWakeWord, WakeWord
                phrase = cfg.get("wakeword.phrase", "ok jarvis")
                if phrase:  # « Ok Jarvis » : détecteur de voix + Whisper
                    self.wakeword = PhraseWakeWord(self._on_wake, lambda: self._set_state(State.LISTENING),
                                                   self.stt.transcribe, phrase,
                                                   cfg.get("wakeword.vad_threshold", 0.5))
                else:       # modèle openwakeword (« Hey Jarvis »)
                    self.wakeword = WakeWord(self.listen, cfg.get("wakeword.model", "hey_jarvis"),
                                             cfg.get("wakeword.threshold", 0.5),
                                             cfg.get("wakeword.cooldown", 3.0))
                self.wakeword.start()
            except Exception as exc:
                log.exception("Mot d'activation indisponible")
                self._emit("error", "Mot d'activation indisponible : {}".format(exc))
                self.wakeword = None

    def _on_wake(self, command: str) -> None:
        """Commande dite après « Ok Jarvis » (thread du micro) ; « » : rien dit, Jarvis revient au repos."""
        self._commands.put(_Heard(command) if command else _QUIET)

    def _warm_up(self) -> None:
        try:
            self.llm.warm_up()
        except (ConnectionError, RuntimeError) as exc:
            log.warning("Préchargement du modèle échoué : %s", exc)

    def _welcome(self) -> None:
        """Annonce de démarrage (« Jarvis Activated »), puis le micro s'ouvre : on parle sans « Ok Jarvis »."""
        cfg = self.cfg
        phrase = cfg.get("assistant.startup_phrase")
        if phrase:
            log.info("Annonce de démarrage : %s", phrase)
            self._say(phrase, spoken=cfg.get("assistant.startup_speech", phrase))
        self._emit("jarvis", cfg.get("assistant.greeting", "Jarvis en ligne."))
        if cfg.get("assistant.listen_on_start", True) and self.stt:
            self._commands.put(_WELCOME)

    def _stt_prompt(self) -> str | None:
        """Phrase d'exemple de config.yaml, plus le nom des jeux installés : Whisper les écrit mieux
        (« Age of Empires II » plutôt que « âge des empires »)."""
        prompt = self.cfg.get("stt.prompt") or ""
        try:
            games = [g["name"] for g in steam.installed_games()][:20]
        except OSError:
            games = []
        return " ".join(filter(None, [prompt, "Jeux : {}.".format(", ".join(games)) if games else ""])) or None

    def _handle_voice(self, wait: float | None = None, silence: str | None = None) -> None:
        """wait : secondes pour commencer à parler (par défaut, le délai du micro) ; silence : message affiché si
        personne ne parle. Une phrase tapée pendant l'écoute la coupe."""
        if not self.stt:
            self._emit("error", "Le micro n'est pas disponible.")
            return
        self._set_state(State.LISTENING)
        self._pause_wakeword(True)
        try:
            text = self.stt.listen(wait, stop=self._text_waiting)
        finally:
            self._pause_wakeword(False)
        if text:
            self._emit("user", text)
            self._handle_text(text, by_voice=True)
        elif silence and not self._text_waiting():
            self._emit("info", silence)

    def _text_waiting(self) -> bool:
        """Une phrase tapée attend dans la file : elle passe avant l'écoute du micro."""
        with self._commands.mutex:
            return any(isinstance(c, str) for c in self._commands.queue)

    def _handle_text(self, text: str, by_voice: bool) -> None:
        if self._sleeping:
            self._sleeping = False
            self._emit("info", "Je me réveille...")
        self._set_state(State.THINKING)
        try:
            reply = self.agent.handle(text)
        except ConnectionError:
            reply = Reply("Je n'arrive pas à joindre Ollama. Est-il lancé ?", ok=False)
        # Une question de confirmation : seule la 1re ligne est dite (le détail, comme le script, reste à l'écran).
        self._say(reply.text, ok=reply.ok, spoken=reply.text.splitlines()[0] if reply.question else speech(reply.text))

        if reply.question and self.stt:
            # Écoute la réponse sans attendre « Ok Jarvis », que la demande ait été dite ou tapée.
            self._commands.put(_ANSWER)
        if self._quit_requested:
            self._emit("quit")
        elif self._sleep_requested:
            self._sleep_requested = False
            self._sleeping = True
            try:
                self.llm.unload()
            except (ConnectionError, RuntimeError):
                pass
            self._emit("info", "En veille. Dites « {} » pour me réveiller.".format(self.wake_name))

    def _say(self, text: str, ok: bool = True, spoken: str | None = None) -> None:
        """Affiche le texte, puis prononce `spoken` (par défaut la première ligne)."""
        self._emit("jarvis" if ok else "error", text)
        spoken = text.splitlines()[0] if spoken is None and text.strip() else spoken
        if not self.tts or not spoken:
            return
        self._set_state(State.SPEAKING)
        self._pause_wakeword(True)
        try:
            self.tts.speak(spoken)
        except Exception:
            log.exception("Échec de la synthèse vocale")
        finally:
            self._pause_wakeword(False)
        self._set_state(State.THINKING)

    def _on_agent_event(self, kind: str, text: str) -> None:
        if kind == "notice":
            self._say(text)
        else:
            self._emit("step", text)

    def _request_sleep(self) -> Result:
        self._sleep_requested = True
        return Result(True, "Je me mets en veille.")

    def _request_quit(self) -> Result:
        self._quit_requested = True
        return Result(True, self.cfg.get("assistant.farewell", "À bientôt."))

    def _pause_wakeword(self, paused: bool) -> None:
        if self.wakeword:
            self.wakeword.pause() if paused else self.wakeword.resume()


ZERO_WIDTH = chr(0x200D) + chr(0xFE0F)  # liant d'émojis et sélecteur de variante, invisibles


def speech(text: str, limit: int = _SPOKEN_MAX) -> str:
    """Texte à prononcer : sans émojis, lignes jointes, sans puces ni numéros, coupé en fin de phrase."""
    text = "".join(c for c in text if unicodedata.category(c) != "So" and c not in ZERO_WIDTH
                   and not "\U0001F3FB" <= c <= "\U0001F3FF")  # émojis, liants et teintes de peau
    parts = []
    for line in text.splitlines():
        line = re.sub(r"\s+([.,])", r"\1", " ".join(re.sub(r"^\s*(?:[-•]|\d+[.)])\s+", "", line).split()))
        if line:
            parts.append(line if line[-1] in ".!?:;," else line + ".")
    text = " ".join(parts)
    if len(text) <= limit:
        return text
    cut = max(text.rfind(end, 0, limit) for end in (". ", "! ", "? "))
    return text[:cut + 1] if cut > 0 else text[:limit].rsplit(" ", 1)[0] + "..."
