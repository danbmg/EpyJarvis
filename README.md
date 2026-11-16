# Jarvis

Assistant vocal local pour Windows, en français. Dites « Ok Jarvis », parlez : Jarvis est un
**agent**. Il enchaîne des outils (ouvrir une appli, lire une fenêtre, cliquer, chercher sur le web,
interroger le PC...), lit leurs résultats, puis vous répond.

La reconnaissance vocale, le modèle de langage (Qwen via Ollama) et la voix tournent sur votre PC.
Aucun LLM dans le cloud. Seules les recherches et pages web demandées, les commandes Spotify et les
opérations Google Agenda passent par internet (ni la voix ni les phrases ne sont envoyées).

## Ce qu'il sait faire

| Domaine | Exemples |
|---|---|
| Applications | « ouvre Paint », « ferme ça », « passe sur Discord », « quelles fenêtres sont ouvertes ? » |
| Piloter une appli | « lis-moi les derniers messages Discord », « qu'est-ce qu'il y a dans cette fenêtre ? » |
| Musique (Spotify par défaut) | « lance ma playlist phonk », « mets du Daft Punk », « lance The Mysterious Altar de Hastron », « qu'est-ce qui passe ? » |
| Enchaîner | « mets pause, attends 5 secondes et passe à la suivante » |
| Messages | « envoie coucou à Alex sur Discord », « écris salut dans le salon général de Dan's office », « ouvre le salon projets de Dan's office », « qu'est-ce qu'Alex m'a écrit ? », « dis à Maman sur WhatsApp que j'arrive », « j'ai des messages WhatsApp ? » (une confirmation vocale avant chaque envoi, qui nomme la vraie destination) |
| Steam | « lance Age of Empires 2 », « ouvre ma bibliothèque Steam », « y a-t-il des mises à jour sur Steam ? », « à quoi j'ai le plus joué ? », « des jeux coop en promo ? » |
| Son et musique | « monte le son », « mets le volume à 30 », « pause », « reprends », « chanson suivante » (Spotify même sur un autre appareil) |
| YouTube | « lance une vidéo de MrBeast » (la vidéo démarre), « lance la deuxième vidéo », « mets un pouce », « plein écran », « avance de 30 secondes », « sous-titres » |
| Web | « quel temps demain à Nice ? », « ouvre YouTube », « mets SkyyArt sur Twitch », « va sur github.com » |
| Tâches (Microsoft To Do) | « ajoute acheter du pain à ma todo », « j'ai fini la tâche hackathon », « rouvre la tâche stage », « qu'est-ce qu'il me reste à faire ? » |
| Agenda (Google Agenda) | « qu'est-ce que j'ai de prévu demain ? », « mes créneaux libres cette semaine », « ajoute une séance de sport vendredi à 18 h » (créée directement ; conflit signalé avec une autre heure proposée), « ajoute une réunion vendredi à 15 h avec alex@gmail.com », « déplace mon rendez-vous de jeudi à vendredi 15 h », « supprime ma réunion de lundi » (question avant d'inviter, déplacer ou supprimer) |
| Minuteurs | « minuteur de 10 minutes », « rappelle-moi dans 20 minutes de sortir le linge », « rappelle-moi à 18 h d'appeler maman » |
| PC | « comment va mon PC ? », « il y a des mises à jour ? », « combien de fichiers sur mon bureau ? » |
| Bureau | « affiche le bureau », « fais une capture d'écran », « lis le presse-papiers » |
| Mémoire | « retiens que mon code wifi est 1234 », « c'est quoi mon code wifi ? » |
| Jarvis | « mets-toi en veille » (libère la carte graphique), « quitte Jarvis » |

## Sécurité : accès libre, mais contrôlé

| Niveau | Exemples | Ce que fait Jarvis |
|---|---|---|
| Lecture | lire une fenêtre, l'état du PC, une page web, un script PowerShell `Get-*` | agit directement |
| Modification | envoyer un message, cliquer sur « Acheter », lancer ou copier via PowerShell, forcer une fermeture, inviter, déplacer ou supprimer un événement d'agenda | **demande confirmation** (« oui », « non », ou une correction) |
| Interdit | éteindre ou redémarrer, supprimer, paramètres et registre, mots de passe, installer ou désinstaller | **refuse**, quoi que dise le modèle |

Ces règles sont appliquées par le code, outil par outil, et non par le prompt ; les demandes interdites
(« éteins l'ordinateur », « vide la corbeille »...) sont même refusées avant d'atteindre le modèle. Détails dans
[docs/choices.md](docs/choices.md#3-sécurité--le-code-décide-pas-le-modèle).

Jetons, journal et mémoire : où ils sont, ce qui est chiffré, ce qu'il ne faut pas partager :
[docs/features.md](docs/features.md#secrets-et-données-personnelles).

## Démarrage rapide

Double-clic sur l'icône **Jarvis** du Bureau : Ollama est lancé s'il le faut, Jarvis dit « Jarvis Activated » et
écoute aussitôt. Raccourcis : `.\.venv\Scripts\python.exe -m jarvis.shortcut install`. Depuis un terminal :

```powershell
cd EpyJarvis
.\.venv\Scripts\python.exe main.py
```

Installation et dépannage : [docs/start.md](docs/start.md). Fonctionnalités et API utilisées : [docs/features.md](docs/features.md).

## Comment ça marche

```
« Ok Jarvis » / bouton MICRO -> Whisper (voix -> texte)
                                        |
                  champ texte ----------+
                                        v
         commande fréquente ou interdite ? (« pause », « monte le son »...)
             oui -> outil direct, sans le modèle (instantané)
             non -> v
         Agent : Qwen (Ollama) choisit un ou plusieurs outils  <------+
                                        v                             |
         garde-fous du code : autoriser / confirmer / refuser         |
                                        v                             |
         exécution, résultat renvoyé au modèle -----------------------+
                                        v   (quand il a fini)
         réponse affichée + voix Piper
```

## Structure

```
main.py                  point d'entrée (interface graphique)
config.example.yaml      réglages, alias d'applications (copie en config.yaml pour personnaliser)
jarvis/
  agent.py               boucle de l'agent : prompt, outils, confirmations, historique, relances
  shortcuts.py           commandes fréquentes et interdits reconnus sans le modèle
  engine.py              thread de travail : écoute -> agent -> réponse, veille, annonce de démarrage
  shortcut.py            raccourcis Bureau et menu Démarrer (application de bureau)
  llm.py                 client Ollama (bibliothèque standard)
  secret_store.py        jetons chiffrés par DPAPI (%LOCALAPPDATA%\Jarvis)
  tools/                 les 51 outils et leurs garde-fous
    __init__.py            Tool, Toolbox, verdicts autoriser / confirmer / refuser
    apps.py, windows.py    applications et fenêtres
    ui.py                  lecture et pilotage des fenêtres (UI Automation)
    shell.py               PowerShell analysé avant exécution
    spotify.py             Web API Spotify (connexion PKCE, lecture, playlists)
    messaging.py           Discord (serveurs, salons, privés : ouvrir, lire, envoyer) et WhatsApp
    youtube.py             pouce, plein écran, avancer, sous-titres de la vidéo affichée
    timers.py              minuteurs et rappels dits à voix haute
    todo.py                Microsoft To Do (listes, ajouter, cocher, rouvrir)
    agenda.py              Google Agenda : planning, créneaux libres, conflits, invitations, confirmations
    google_auth.py         connexion OAuth 2.0 PKCE (une fois), renouvellement du jeton
    google_calendar.py     client REST de l'API Google Calendar v3
    dates.py               dates dites en français -> dates précises (Europe/Paris)
    desktop.py, web.py, steam.py, system.py, memory.py
  voice/                 micro -> texte, texte -> voix, mot d'activation
  ui/                    fenêtre customtkinter
bench/                   benchmark de l'agent (cases.yaml + run.py)
tests/                   tests unitaires (sans Ollama ni micro)
docs/                    start.md (lancer), features.md (fonctions et API), demo.md (phrases à essayer), choices.md
```

## Tests et benchmark

```powershell
.\.venv\Scripts\python.exe -m unittest discover tests   # 233 tests, ~18 s
.\.venv\Scripts\python.exe -m bench.run                 # 176 cas, ~2 min, Ollama requis
.\.venv\Scripts\python.exe -m bench.run --model-only    # le modèle seul, sans les commandes directes
```

Score actuel (premier outil choisi, avec ses arguments) : **98,9 %** (174/176 dont agenda 17/18, le 05/11 ; ±3 points d'un lancement à l'autre) avec qwen3:8b et les
commandes directes (95,0 % pour le modèle seul), ~450 ms en moyenne sur une RTX 5060 Ti (84 % avec qwen2.5:7b seul). Le pilotage
d'une application en plusieurs étapes reste le point faible : voir
[docs/choices.md](docs/choices.md#4-le-modèle--qwen3-8b).
