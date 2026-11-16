"""Fenêtre principale de Jarvis (customtkinter).

L'interface ne fait qu'afficher : elle envoie les commandes au moteur et lit
ses événements toutes les 100 ms.
"""

from __future__ import annotations

import ctypes
import datetime
import queue
import tkinter as tk
from ctypes import wintypes

import customtkinter as ctk

from jarvis import APP_ID, __version__
from jarvis.engine import Engine, Event, State
from jarvis.ui import theme as t

_mutex = None  # gardé ouvert tant que Jarvis tourne


class JarvisApp(ctk.CTk):
    def __init__(self) -> None:
        super().__init__()
        ctk.set_appearance_mode("dark")
        self.title(t.TITLE)
        if t.ICON.exists():
            self.iconbitmap(str(t.ICON))
        self.geometry(t.WINDOW_SIZE)
        self.minsize(*t.WINDOW_MIN)
        self.configure(fg_color=t.BG_DARK)
        self.protocol("WM_DELETE_WINDOW", self._close)

        self._state = State.THINKING
        self._angle = 0.0
        self._build()

        self.engine = Engine()
        self._entry.configure(placeholder_text="Écrivez une commande ou dites « {} »...".format(
            self.engine.wake_name))
        self.engine.start()
        self.after(100, self._poll_events)
        self.after(50, self._animate)
        self._tick_clock()

    # ---- Construction ----

    def _build(self) -> None:
        header = ctk.CTkFrame(self, fg_color=t.BG_DARK, height=60)
        header.pack(fill="x", padx=t.PADDING, pady=(t.PADDING, 0))
        header.pack_propagate(False)
        ctk.CTkLabel(header, text="J.A.R.V.I.S", font=t.FONT_TITLE, text_color=t.ACCENT).pack()
        ctk.CTkLabel(header, text="Just A Rather Very Intelligent System",
                     font=t.FONT_SUBTITLE, text_color=t.TEXT_DIM).pack()
        self._clock = ctk.CTkLabel(header, text="", font=t.FONT_STATE, text_color=t.ACCENT_DIM)
        self._clock.place(relx=1.0, rely=0.0, anchor="ne")

        self._ring = tk.Canvas(self, width=100, height=100, bg=t.BG_DARK, highlightthickness=0)
        self._ring.pack(pady=(8, 0))
        self._state_label = ctk.CTkLabel(self, text="", font=t.FONT_STATE)
        self._state_label.pack()

        self._chat = ctk.CTkTextbox(self, font=t.FONT_CHAT, fg_color=t.BG_CHAT,
                                    text_color=t.TEXT_PRIMARY, wrap="word", state="disabled")
        self._chat.pack(fill="both", expand=True, padx=t.PADDING, pady=(8, 0))
        tags = {"user": t.ACCENT, "jarvis": t.TEXT_PRIMARY, "error": t.TEXT_ERROR,
                "info": t.TEXT_SYSTEM}
        for tag, color in tags.items():
            self._chat._textbox.tag_configure(tag, foreground=color)
            self._chat._textbox.tag_configure(tag + "_label", foreground=color, font=t.FONT_CHAT_BOLD)
        self._chat._textbox.tag_configure("step", foreground=t.TEXT_DIM, font=t.FONT_STEP)

        bar = ctk.CTkFrame(self, fg_color=t.BG_DARK, height=50)
        bar.pack(fill="x", padx=t.PADDING, pady=(8, 0))
        self._mic = ctk.CTkButton(bar, text="MICRO", width=70, height=36, font=t.FONT_BUTTON,
                                  fg_color=t.ACCENT_DIM, hover_color=t.ACCENT,
                                  text_color=t.BG_DARK, command=self._listen)
        self._mic.pack(side="left", padx=(0, 6))
        ctk.CTkButton(bar, text="ENVOYER", width=80, height=36, font=t.FONT_BUTTON,
                      fg_color=t.ACCENT, hover_color=t.ACCENT_GLOW, text_color=t.BG_DARK,
                      command=self._send).pack(side="right", padx=(6, 0))
        self._entry = ctk.CTkEntry(bar, font=t.FONT_CHAT, height=36, fg_color=t.BG_INPUT,
                                   border_color=t.ACCENT_DIM, border_width=1,
                                   placeholder_text="Écrivez une commande...")
        self._entry.pack(side="left", fill="x", expand=True)
        self._entry.bind("<Return>", lambda _: self._send())

        ctk.CTkLabel(self, text="Jarvis v{}  |  100 % local".format(__version__),
                     font=t.FONT_SMALL, text_color=t.TEXT_DIM).pack(pady=(4, 8))

    # ---- Événements du moteur ----

    def _poll_events(self) -> None:
        try:
            while True:
                self._on_event(self.engine.events.get_nowait())
        except queue.Empty:
            pass
        self.after(100, self._poll_events)

    def _on_event(self, event: Event) -> None:
        if event.kind == "state":
            self._state = event.data
            label, color, _ = t.STATES[self._state.value]
            self._state_label.configure(text=label, text_color=color)
            listening = self._state == State.LISTENING
            self._mic.configure(text="ÉCOUTE" if listening else "MICRO",
                                fg_color=t.SUCCESS if listening else t.ACCENT_DIM)
        elif event.kind == "user":
            self._write("Vous", event.data, "user")
        elif event.kind == "jarvis":
            self._write("Jarvis", event.data, "jarvis")
        elif event.kind in ("error", "info"):
            self._write("Système", event.data, event.kind)
        elif event.kind == "step":
            self._write_step(event.data)
        elif event.kind == "quit":
            self.after(1500, self._close)

    def _write(self, sender: str, message: str, tag: str) -> None:
        box = self._chat._textbox
        self._chat.configure(state="normal")
        box.insert("end", sender + " : ", tag + "_label")
        box.insert("end", message + "\n\n", tag)
        self._chat.configure(state="disabled")
        box.see("end")

    def _write_step(self, message: str) -> None:
        """Étape de l'agent (outil appelé, résultat) : petite ligne grise."""
        box = self._chat._textbox
        self._chat.configure(state="normal")
        box.insert("end", "    " + message + "\n", "step")
        self._chat.configure(state="disabled")
        box.see("end")

    # ---- Actions de l'utilisateur ----

    def _send(self) -> None:
        text = self._entry.get().strip()
        if text:
            self._entry.delete(0, "end")
            self._write("Vous", text, "user")
            self.engine.submit_text(text)

    def _listen(self) -> None:
        if self._state in (State.IDLE, State.SLEEPING):
            self.engine.listen()

    def _close(self) -> None:
        self.engine.stop()
        self.destroy()

    # ---- Animations ----

    def _tick_clock(self) -> None:
        self._clock.configure(text=datetime.datetime.now().strftime("%H:%M:%S"))
        self.after(1000, self._tick_clock)

    def _animate(self) -> None:
        _, color, speed = t.STATES[self._state.value]
        a, c = self._angle, self._ring
        c.delete("all")
        c.create_oval(8, 8, 92, 92, outline=t.ACCENT_DIM, width=1)
        c.create_arc(15, 15, 85, 85, start=a, extent=120, outline=color, width=2, style="arc")
        c.create_arc(15, 15, 85, 85, start=a + 180, extent=80, outline=color, width=2, style="arc")
        c.create_arc(22, 22, 78, 78, start=-a * 0.7, extent=90, outline=color, width=1.5, style="arc")
        c.create_oval(46, 46, 54, 54, fill=color, outline="")
        self._angle = (a + speed) % 360
        self.after(50, self._animate)


def _already_running() -> bool:
    """Un seul Jarvis à la fois (sinon deux micros et deux « Ok Jarvis ») : ramène la fenêtre déjà ouverte."""
    global _mutex
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    _mutex = kernel32.CreateMutexW(None, False, "Local\\" + APP_ID)
    if ctypes.get_last_error() != 183:  # ERROR_ALREADY_EXISTS
        return False
    user32 = ctypes.WinDLL("user32")
    user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
    user32.FindWindowW.restype = wintypes.HWND
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    hwnd = user32.FindWindowW(None, t.TITLE)
    if hwnd:
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        user32.SetForegroundWindow(hwnd)
    return True


def run() -> None:
    if _already_running():
        return
    # Identité propre dans la barre des tâches (icône et nom « Jarvis », regroupée avec le raccourci épinglé)
    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
    JarvisApp().mainloop()
