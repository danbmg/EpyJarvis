# Fonctionnalités et moyens utilisés

Pour chaque fonction : l'**API** utilisée s'il y en a une, sinon le moyen employé.
« Appli » veut dire que Jarvis pilote l'application de bureau par **UI Automation** : il lit la fenêtre et
clique ou écrit, comme toi.

## Le cœur, 100 % local

| Fonction | Moyen |
|---|---|
| Mot d'activation « Ok Jarvis » (« Ok Jarvis, lance Spotify » d'une traite) | détecteur de voix Silero + Whisper (local) |
| Voix → texte | faster-whisper `large-v3-turbo`, sur la carte graphique (local) |
| Réflexion, choix des outils | Qwen3 8B par **l'API locale d'Ollama** (`127.0.0.1:11434`) |
| Phrases fréquentes (« pause », « monte le son ») et interdits | Reconnues par le code, sans le modèle |
| Voix de Jarvis | Piper, voix `fr_FR-tom-medium` (local) |
| Mémoire (« retiens que… ») | Base SQLite locale (`jarvis_memory.db`) |

## Services

| Service | Ce que Jarvis fait | Moyen |
|---|---|---|
| **Spotify** | lancer une playlist, un artiste, un titre ; pause, reprise ; « qu'est-ce qui passe ? » | **Web API Spotify** (officielle, gratuite ; Premium requis pour piloter la lecture), connexion OAuth PKCE. L'interface de Spotify n'est pas lisible par UI Automation. |
| **Discord** | ouvrir un serveur, un salon ou une conversation ; lire ; envoyer en ton nom | **Appli**. Le serveur s'ouvre par Ctrl+K (`*nom`), le salon par son lien dans la barre de gauche ; le titre de la fenêtre confirme où l'on est. Aucune API autorisée n'écrit au nom d'un compte personnel (les « self-bots » sont interdits), et un bot écrirait sous son propre nom. |
| **WhatsApp** | envoyer à un contact ou un groupe ; dire qui a écrit | **Appli** : la recherche, la ligne du contact, puis le champ « Écrivez un message à… ». L'API WhatsApp est réservée aux comptes Business. |
| **Steam** | lancer un jeu, ouvrir la bibliothèque ou les téléchargements, jeux les plus joués, promos, détails d'un jeu | Fichiers locaux de Steam (jeux installés, temps de jeu), liens `steam://`, et **API publique du magasin** (`store.steampowered.com/api`, sans clé). |
| **YouTube** | lancer une vidéo (la 1re ou la n-ième), pouce, plein écran, avancer, sous-titres | La page de résultats est lue pour trouver la vidéo (sans clé d'API). Les raccourcis clavier de YouTube pilotent la lecture ; le pouce passe par UI Automation. |
| **Microsoft To Do** | ouvrir l'appli ou une liste ; ajouter une tâche ; la cocher ou la rouvrir ; dire ce qu'il reste à faire | **Appli** (interface Windows native, très lisible) : listes, tâches et cases à cocher se pilotent sans la souris, en 1 à 2 s. La tâche est retrouvée dans toutes les listes, même mal transcrite. Rien à configurer : les tâches se synchronisent avec ton compte Microsoft. L'API Microsoft Graph demanderait d'inscrire une application chez Microsoft. |
| **Google Agenda** | planning du jour ou de la semaine, créneaux libres, créer, renommer, déplacer, supprimer un événement, inviter des participants, choisir le calendrier | **API Google Calendar v3** (officielle, gratuite), sur tous les calendriers du compte. Connexion OAuth 2.0 PKCE une seule fois ; application Google Cloud publiée (en production) : le jeton n'expire pas. Jeton chiffré par DPAPI dans `%LOCALAPPDATA%\Jarvis`, renouvelé seul. Voir [Agenda](#agenda-google-agenda). |
| **Twitch** | regarder une chaîne | Adresse `twitch.tv/<chaîne>` ouverte dans le navigateur |
| **Web** | recherche, météo, actualité, lire une page, ouvrir un site | Recherche **DuckDuckGo** (page HTML, sans clé), puis lecture de la page ; sites connus ouverts dans le navigateur |

## Le PC

| Fonction | Moyen |
|---|---|
| Ouvrir, fermer, passer d'une appli à l'autre, fermer un onglet | Menu Démarrer lu au lancement, fenêtres Windows, processus (psutil) |
| Piloter n'importe quelle appli (lire, cliquer, écrire) | UI Automation |
| Volume, pause, suivant | Touches multimédia de Windows ; API Spotify si Spotify joue |
| « Comment va mon PC ? » | psutil, et `nvidia-smi` pour la carte graphique |
| « Il y a des mises à jour ? » | Windows Update, en lecture seule, et `winget upgrade` (liste seulement) |
| Questions sur les fichiers, le réseau, le matériel | PowerShell, script analysé avant exécution |
| Capture d'écran, presse-papiers, afficher le bureau | PowerShell / .NET, UI Automation, touches Windows |
| Minuteurs et rappels | Minuterie locale, annonce à voix haute (les rendez-vous datés vont dans Google Agenda) |
| Veille de Jarvis | Libère la carte graphique et n'écoute plus que « Ok Jarvis » |

## Envoi d'un message (Discord, WhatsApp)

1. Jarvis ouvre la conversation demandée.
2. Il pose **une seule question**, qui nomme la vraie destination : « J'envoie « salut » dans #général de
   Dan's office ? ». Si l'envoi rate ensuite, il réessaie sans redemander.
3. Il clique dans le champ, colle le texte et le relit. Il n'appuie sur Entrée que si le texte y est.
4. Il annonce l'envoi seulement quand le message apparaît dans la conversation.

**Destinataires possibles**

- **Discord** : tes amis et tes conversations, les membres des serveurs que vous partagez, et les salons de
  tous tes serveurs.
- **WhatsApp** : tes discussions, tes groupes, et les contacts sans discussion.
- **Noms mal transcrits** (« Alexe » pour Alex, « Mathieu » pour Matthieu) : Jarvis prend le nom le plus proche.
- **Homonymes** : il prend la conversation la plus récente. La confirmation affiche toujours le vrai nom :
  réponds « non » et précise si ce n'est pas la bonne personne.
- **Le message est écrit au destinataire** : « demande à Alex s'il a fini » donne « As-tu fini ton
  projet ? ».

## Agenda (Google Agenda)

**Dates** : le modèle passe les dates telles que tu les dis (« vendredi 18 h », « demain matin », « la semaine
prochaine ») ; le code les calcule (fuseau Europe/Paris, heure d'été comprise) et la réponse dit toujours la date
complète, l'heure et le calendrier. « Jeudi » : le prochain jeudi, aujourd'hui compris (le suivant si l'heure est
passée) ; « jeudi prochain » : après aujourd'hui ; durée par défaut 1 h.

**Confirmations** (décidées par le code) :

| Action | Question ? |
|---|---|
| Lire le planning, les créneaux libres, les calendriers | non |
| Créer un événement simple | non : il est créé, la réponse dit où et quand |
| Créer sur un créneau déjà pris (tous les calendriers comptent, sauf jours fériés et anniversaires) | oui : il signale le conflit et propose le premier créneau libre ; « oui », une autre heure, ou « ajoute-le quand même » |
| Créer avec des invités, inviter à un événement | oui : les adresses sont affichées, les invitations partent ensuite (`sendUpdates=all`) |
| Renommer, changer le lieu (événement à toi, sans invités) | non |
| Changer la durée, modifier un événement partagé ou récurrent | oui |
| Déplacer (jour, heure, calendrier) | oui, avec le conflit éventuel |
| Supprimer | oui, toujours ; une seule occurrence d'une série |

**Événement visé** : retrouvé par son nom et sa date dans tous les calendriers. Plusieurs possibles : Jarvis
demande lequel, sans rien toucher. Une modification n'écrase pas un changement fait entre-temps ailleurs
(`If-Match`).

**Adresses e-mail** : seulement celles que tu as dites ou écrites (dictées : « alex arobase gmail point com »).
Jarvis n'en invente aucune et n'en prend aucune dans une invitation lue ; elles ne sont ni retenues ni écrites
dans `jarvis.log`. Sans adresse (« avec Alex »), l'événement est créé sans invitation.

**Calendrier** : celui que tu nommes (« dans mon calendrier Ekipage »), sinon l'agenda principal
(`google_calendar.default_calendar` et `aliases` dans `config.yaml`). Les calendriers en lecture seule sont lus,
jamais modifiés. Si un calendrier ne répond pas, Jarvis le dit au lieu de répondre « rien de prévu ».

## Sécurité

Le code décide, pas le modèle :

- **lire** se fait directement ;
- **modifier** (envoyer, acheter, script qui change quelque chose) demande confirmation ;
- **interdit** (éteindre, supprimer, paramètres, mots de passe, installer) est refusé.

Détails dans [choices.md](choices.md).

## Secrets et données personnelles

| Fichier | Contenu | Protection |
|---|---|---|
| `%LOCALAPPDATA%\Jarvis\google_client.bin`, `google_token.bin` | client OAuth et jeton Google | **chiffrés DPAPI** : lisibles seulement par ton compte Windows, sur ce PC ; hors du projet |
| `secret_google_calendar.json` | client OAuth Google | en clair ; plus nécessaire au fonctionnement : à ranger hors du projet |
| `spotify_token.json` | jeton Spotify (piloter la lecture, lire les playlists) | **en clair** |
| `jarvis.log` | tes demandes et les réponses de Jarvis (300 caractères), donc parfois ton planning ou le texte d'un message ; adresses e-mail masquées ; arguments et résultats de l'outil agenda jamais écrits | en clair |
| `jarvis_memory.db` | ce que tu as demandé de retenir | en clair |

- **Usage personnel sur ce PC : sans risque particulier.** Le modèle, la voix et la transcription sont locaux ;
  aucun jeton Google n'apparaît dans le journal.
- **Protège ta session Windows** (mot de passe ou code PIN, BitLocker si possible) : DPAPI protège contre une copie
  des fichiers, pas contre quelqu'un ou un programme qui agit sous ta session.
- **`.gitignore`** exclut tous ces fichiers (et `.venv`). Il ne protège que Git : une copie du dossier ou un zip de
  sauvegarde les emporte. Les sauvegardes `Jarvis_backup_*.zip` en contiennent : **ne pas les partager**.
- **Dépôt public** : retirer aussi l'adresse du compte de `config.yaml` (`google_calendar.account`) et ne pas
  publier `CONTEXT.md` (contacts, serveurs, adresse).
- **Connexion Google** : le serveur de retour n'écoute que sur `127.0.0.1` (inaccessible du réseau), sur un port
  choisi au hasard, seulement pendant `login` (3 min au plus) ; `state` et PKCE empêchent qu'une réponse forgée ou
  un code intercepté servent. Passer l'appli en production ne rend aucune donnée publique : sans ton consentement,
  personne n'accède à ton agenda.
