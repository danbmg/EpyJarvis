# Lancer Jarvis

## Prérequis

Windows 10 ou 11, un micro, **Python 3.12**, **[Ollama](https://ollama.com/download)**. Une carte NVIDIA est
conseillée.

## Installation (une fois)

```powershell
git clone git@github.com:danbmg/EpyJarvis.git
cd EpyJarvis
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pip install nvidia-cublas-cu12 "nvidia-cudnn-cu12==9.*"   # carte NVIDIA seulement
ollama pull qwen3:8b
copy config.example.yaml config.yaml                                                # réglages personnels
.\.venv\Scripts\python.exe -m jarvis.tools.spotify login                                 # Spotify
.\.venv\Scripts\python.exe -m jarvis.tools.agenda login                                  # Google Agenda
```

**Configuration** : `config.yaml` (copie de `config.example.yaml`, jamais versionnée) reçoit tes réglages.
Sans lui, Jarvis lit `config.example.yaml` : tout marche sauf Spotify et l'agenda.

- **Spotify** : crée ta propre application sur developer.spotify.com (redirect URI
  `http://127.0.0.1:8765/callback`) et mets son Client ID dans `spotify.client_id`.
- **Google Agenda** : crée un client OAuth « Application de bureau » dans Google Cloud (API Google Calendar
  activée), télécharge son JSON sous le nom `secret_google_calendar.json` à la racine du projet (ignoré par git)
  et mets ton adresse Gmail dans `google_calendar.account`.
- La voix Piper (`voices/`) et le modèle Whisper sont téléchargés au premier lancement.

**Google Agenda** : la connexion se fait une seule fois, avec le compte de `config.yaml`
(`google_calendar.account`). Le navigateur s'ouvre ; si Google affiche « application non validée », clique sur
« Paramètres avancés » puis « Accéder à Jarvis » (normal pour une appli personnelle publiée sans validation
Google), et **coche les deux permissions**. Le fichier JSON du client OAuth est alors importé chiffré dans
`%LOCALAPPDATA%\Jarvis` avec le jeton.

- L'application Google Cloud est **publiée (en production) depuis le 05/11/2026** : le jeton n'expire pas, il se
  renouvelle seul. `... -m jarvis.tools.agenda status` doit dire « Connexion sans date d'expiration ».
- `secret_google_calendar.json` n'est plus nécessaire au fonctionnement : range-le **hors du projet** plutôt que de
  le supprimer (c'est la seule copie en clair du secret client ; la copie chiffrée ne marche que sur ce compte
  Windows).
- `... logout` révoque l'accès chez Google et efface le jeton : il faudra refaire `login`.

## Lancer

**Double-clique sur l'icône « Jarvis »** du Bureau (ou tape « Jarvis » dans le menu Démarrer). La fenêtre
s'ouvre sans console ; Jarvis lance Ollama s'il ne tourne pas (`ollama serve`, sans fenêtre), charge tout, dit
**« Jarvis Activated »**, puis le micro s'ouvre 8 s : parle directement. Ensuite, dis **« Ok Jarvis »** :
l'anneau passe au vert, puis dis ta demande (ou tout d'une traite : « Ok Jarvis, lance Spotify »). Tu peux aussi
cliquer sur MICRO ou taper ta demande. Relancer l'icône ramène la fenêtre déjà ouverte (un seul Jarvis à la fois).

Créer ou supprimer les raccourcis (Bureau + menu Démarrer, déjà faits sur ce PC) :

```powershell
.\.venv\Scripts\python.exe -m jarvis.shortcut install    # ou : remove, status
```

Pour l'épingler à la barre des tâches : clic droit sur « Jarvis » dans le menu Démarrer > Épingler à la barre
des tâches. Réglages du démarrage (`config.yaml`) : `assistant.startup_phrase`, `assistant.startup_speech`
(prononciation), `assistant.listen_on_start`, `llm.autostart`.

Depuis un terminal (avec les journaux à l'écran) :

```powershell
cd EpyJarvis
.\.venv\Scripts\python.exe main.py
```

Le chargement prend environ 25 s (quelques minutes la première fois : téléchargement de Whisper et de la
voix).

## En cas de problème

| Symptôme | Solution |
|---|---|
| « Ollama ne répond pas » | Lance Ollama ; Jarvis le retrouve à la demande suivante. |
| « Modèle 'qwen3:8b' absent » | `ollama pull qwen3:8b` |
| « Spotify n'est pas connecté » | `.\.venv\Scripts\python.exe -m jarvis.tools.spotify login` |
| « Google Agenda n'est pas connecté » (jamais connecté, accès révoqué, mot de passe Google changé) | `.\.venv\Scripts\python.exe -m jarvis.tools.agenda login` (un jeton refusé par Google est effacé tout seul) |
| `status` annonce une date d'expiration | Jeton obtenu quand l'application Google Cloud était en mode « Test » (7 jours) : vérifie dans Google Cloud > « Audience » qu'elle est « En production », puis `login` à nouveau. |
| « Google Agenda est injoignable » | Connexion internet ; le reste de Jarvis fonctionne sans. |
| Pas de carte NVIDIA | Dans `config.yaml` : `stt.device: cpu` et `stt.model: small` |

Les détails sont dans `jarvis.log`. Ce que Jarvis sait faire : [features.md](features.md).
