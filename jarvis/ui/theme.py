"""Couleurs, polices et libellés de l'interface."""

from pathlib import Path

TITLE = "J.A.R.V.I.S"
ICON = Path(__file__).with_name("jarvis.ico")  # fenêtre, barre des tâches et raccourcis

BG_DARK = "#0a0e17"
BG_PANEL = "#0f1623"
BG_INPUT = "#141c2b"
BG_CHAT = "#0c1220"
ACCENT = "#00d4ff"
ACCENT_DIM = "#0088aa"
ACCENT_GLOW = "#00e5ff"
TEXT_PRIMARY = "#e0e6f0"
TEXT_DIM = "#6b7a94"
TEXT_SYSTEM = "#4a6080"
TEXT_ERROR = "#ff4466"
SUCCESS = "#00ff88"
WARNING = "#ffaa00"

FONT = "Consolas"
FONT_TITLE = (FONT, 28, "bold")
FONT_SUBTITLE = (FONT, 11)
FONT_STATE = (FONT, 14, "bold")
FONT_CHAT = (FONT, 12)
FONT_CHAT_BOLD = (FONT, 12, "bold")
FONT_BUTTON = (FONT, 11, "bold")
FONT_SMALL = (FONT, 9)
FONT_STEP = (FONT, 10)

WINDOW_SIZE = "800x700"
WINDOW_MIN = (600, 500)
PADDING = 16

# état -> (libellé, couleur, vitesse de rotation de l'anneau)
STATES = {
    "idle": ("PRÊT", ACCENT_DIM, 0.5),
    "listening": ("À L'ÉCOUTE", SUCCESS, 2.0),
    "thinking": ("RÉFLEXION", WARNING, 3.0),
    "speaking": ("RÉPONSE", ACCENT_GLOW, 1.5),
    "sleeping": ("VEILLE", TEXT_SYSTEM, 0.15),
}
