# Choix techniques

Ce document explique **pourquoi** Jarvis est construit ainsi. Pour l'installer et le lancer,
voir [start.md](start.md).

## Vue d'ensemble

```
« Ok Jarvis » (Silero + Whisper) +
bouton MICRO -------------------+--> Whisper : voix -> texte
                                            |
champ texte ----------------------------+---+
                                        v
          commande fréquente ou interdite ? -- oui --> outil direct (sans le modèle)
                                        | non
                                        v
          Agent : Qwen (Ollama, appels d'outils natifs)  <---------------+
                                        v                                |
          garde-fou de l'outil : autoriser / confirmer / refuser         |
                                        v                                |
          exécution ; le résultat repart au modèle ----------------------+
                                        v   (quand il n'appelle plus d'outil)
          réponse affichée + voix Piper
```

Tout est local : aucun LLM dans le cloud. Seules les recherches et pages web demandées passent par
internet.

---

## 1. Un agent plutôt qu'un classifieur

La première version faisait **classer** chaque phrase par le LLM dans une liste fermée d'actions
(`{"action": ..., "args": ...}`). Elle atteignait 99,3 % sur son benchmark, mais ne savait rien
faire hors de cette liste : fermer une application, lire ce qu'affiche une fenêtre, enchaîner
« ouvre Steam et dis-moi les jeux du moment », répondre à une question sur le PC.

Jarvis est maintenant un **agent** :
- le modèle reçoit la liste des outils (appels d'outils natifs d'Ollama) ;
- il en appelle un ou plusieurs, lit les résultats, recommence si besoin (12 étapes au plus) ;
- quand il n'appelle plus d'outil, son texte est la réponse.

**Commandes directes** (`jarvis/shortcuts.py`). Les phrases courtes et sans ambiguïté
(« pause », « chanson suivante », « monte le son », « volume à 30 », « mets un pouce », « plein écran »,
« ouvre ma bibliothèque Steam », « bonne nuit », « quelle heure est-il ? ») sont reconnues par des
expressions régulières et exécutées sans le modèle :
- **fiables à 100 %** : un modèle de 8B se trompe parfois sur les plus simples (« c'est trop fort »
  répondu « je baisse le volume » sans rien faire, « mets un pouce » pris pour « vidéo suivante ») ;
- **instantanées** (aucun appel au modèle) ;
- **la phrase entière doit correspondre** : « mets pause, attends 5 s et lance ma playlist » va au
  modèle, qui sait enchaîner. Formes acceptées : impératif, infinitif après « tu peux », « Jarvis »
  et « s'il te plaît » autour ;
- la commande passe par **les mêmes outils, garde-fous et historique** que si le modèle l'avait
  choisie (« plus fort » juste après marche toujours) ; si l'outil échoue (aucune vidéo YouTube
  affichée...), le modèle prend le relais ;
- les **demandes interdites** (« éteins l'ordinateur », « vide la corbeille », « désinstalle… »)
  sont refusées avant même d'atteindre le modèle, qui avait une fois tenté d'agir sur « passe
  l'écran en mode sombre dans les paramètres ». Le verbe doit ouvrir la phrase : « quels jeux sont
  installés ? » n'est pas « installe ».

C'est l'avantage de l'ancien classifieur (exact sur une liste fermée) sans sa limite : tout ce qui
n'est pas reconnu va à l'agent. 35 des 140 cas du benchmark passent ainsi : 98,6 % au
benchmark, contre 95,0 % pour le modèle seul (`--model-only`).

**Relances** quand le modèle n'agit pas : réponse « OK », annonce sans appel d'outil (« je lance… »,
« je clique dessus » après une simple lecture), « retiens que… » sans `remember`, ou « tu ne l'as pas
fait » / « ça ne marche pas » de l'utilisateur → une relance cachée lui demande d'appeler l'outil (ou
de refaire autrement). Ni l'annonce fausse ni la relance ne restent dans l'historique.

Décisions de l'utilisateur à l'origine de cette refonte :
- **accès libre, mais contrôlé** : lecture directe, confirmation pour ce qui modifie, interdits
  bloqués par le code (section 3) ;
- **messages WhatsApp et Discord** envoyés via les boutons de l'application, avec confirmation
  vocale et correction possible ;
- **Spotify** piloté par ses boutons (son API pourra venir plus tard) ;
- **« mets-toi en veille »** libère la carte graphique ;
- **tout en local**, pas de LLM cloud.

## 2. Les outils

Il y a 39 outils, dans `jarvis/tools/`. Chacun déclare :
- sa description et ses paramètres, envoyés au modèle ;
- s'il **lit** (le résultat repart au modèle pour qu'il réponde) ou s'il **agit** (son message,
  « J'ouvre Discord. », sert de réponse : le modèle répond « OK » et Jarvis dit le message de
  l'outil, sans reformulation qui pourrait inventer) ; un outil qui lit peut aussi agir (`ui_click`,
  `ui_type`, `ui_keys`, `wait`) : sinon « Je clique dessus. » après une simple lecture passait pour fait ;
- s'il renvoie du **contenu extérieur** (web, fenêtres, presse-papiers) ;
- si ses arguments sont **privés** (mémoire, texte tapé) : ils ne vont pas dans le journal ;
- un **garde-fou** éventuel, qui rend un verdict selon les arguments.

Choix par domaine :
- **Applications** : toutes celles du menu Démarrer (`Get-StartApps`, ~1 s au lancement), au lieu
  d'une liste blanche à tenir à jour. Les raccourcis « Désinstaller » ou « Réinitialiser » sont
  écartés. `config.yaml` n'apporte plus que des alias, et quelques outils absents du menu
  (Gestionnaire de périphériques : un `.msc` est ouvert par le shell, qui lance `mmc` avec
  l'élévation automatique de Windows ; Jarvis peut lire cette fenêtre mais n'y clique jamais).
  `open_app` attend la nouvelle fenêtre (5 s au plus) et la met au premier plan, pour que l'étape
  suivante lise la bonne fenêtre. Un jeu Steam installé passe par Steam ; un site connu (« ouvre
  YouTube ») ou une adresse (« lemonde.fr ») s'ouvre dans le navigateur, par nom exact seulement
  (« regedit » ressemblait trop à Reddit).
- **Onglets** : `close_tab` envoie Ctrl+W au navigateur le plus en avant (Brave, Chrome, Edge,
  Firefox...). Avant, « ferme cet onglet » donnait `close_app`, qui fermait tout le navigateur.
- **Piloter une application** : par **UI Automation** de Windows. `ui_read` liste les boutons,
  champs, onglets et textes de la fenêtre, chacun avec un numéro ; `ui_click` et `ui_type` visent
  un numéro. Jarvis vise donc un élément **par son nom**, jamais une coordonnée. L'ancienne
  version cliquait à des coordonnées approximatives, et un message pouvait partir au mauvais
  contact. Lire une fenêtre prend ~0,3 s. Les boutons Réduire / Agrandir / Fermer de la barre de
  titre ne sont pas listés : en tête de liste, « clique sur la deuxième vidéo » visait [2] = Agrandir
  (et « la quatrième » aurait fermé le navigateur).
- **Messages Discord et WhatsApp** (`messaging.py`) : des outils dédiés, pas `ui_type`. Le modèle
  tapait dans la conversation affichée (le mauvais salon), puis disait « envoyé » malgré l'échec.
  - **Discord** : la navigation rapide (Ctrl+K) cherche la personne ou le salon ; ses résultats se
    lisent « general, Salon textuel, night owls », donc le bon serveur est choisi (un salon vocal
    est écarté). Le champ « Envoyer un message à @Alex » / « dans #general » confirme ensuite la
    destination : si ce n'est pas celle demandée, rien n'est envoyé. `discord_read` lit les derniers
    messages (auteur : bouton du profil ; texte : `message-content`).
  - **WhatsApp** (application WebView2) : la recherche filtre la liste des discussions ; la ligne qui
    commence par le nom du contact est cliquée (pas un message qui le cite). `whatsapp_unread` lit la
    liste sans ouvrir de discussion : aucun accusé de lecture n'est envoyé.
  - Le texte est **collé** (les deux applis refusent l'écriture directe), et chaque envoi est
    **confirmé à voix haute** avant : « J'envoie « coucou » à Alex sur Discord ? ».
  - Une réponse qui dit « le message a été envoyé » ou « j'ai mis pause » sans outil réussi est
    traitée comme une annonce fausse : le modèle est relancé.
- **Spotify : par sa Web API, pas par son interface.** Constaté le 3 novembre 2026 : lancé au
  démarrage de Windows, Spotify n'expose à UI Automation que le cadre de son moteur Chromium
  (barre d'onglets, « Omnibox Popup »), aucune playlist ni bouton Lecture. L'API est de toute façon
  plus fiable qu'une suite de clics, surtout avec un modèle 7B. Choix :
  - **connexion OAuth PKCE** : l'application développeur « Jarvis (local) » (créée par l'utilisateur
    via ChatGPT) n'a pas besoin de son secret ; seul le jeton de l'utilisateur est gardé, dans
    `spotify_token.json`. Jeton renouvelé automatiquement ;
  - **4 outils** : `spotify_play` (playlist, artiste, album ou titre ; vide = reprendre),
    `spotify_pause`, `spotify_now_playing`, `spotify_playlists` ;
  - **les playlists de l'utilisateur d'abord** : « ma playlist phonk » trouve sa playlist « Phonk »
    avant de chercher dans tout Spotify ; sinon un artiste, une playlist ou un album au nom exact,
    sinon le premier titre trouvé ;
  - **appareil** : l'appareil actif (comme Spotify le fait lui-même), sinon ce PC ; si Spotify
    n'est ouvert nulle part, Jarvis l'ouvre et attend qu'il apparaisse (15 s) ;
  - suivant / précédent restent sur les touches multimédia (`media`) ;
  - la recherche n'utilise pas `market=from_token` : ce paramètre exige le droit
    `user-read-private`, et Spotify répondait « 403 Insufficient client scope » ;
  - avec les playlists de l'utilisateur, seuls comptent le nom exact, le début du nom ou une faute
    proche : une playlist « work » ne doit pas répondre à « mets un son de work hard ».
- **Attendre** : `wait` (30 s au plus), pour « mets pause, attends 5 secondes et... ».
- **Steam** : la bibliothèque est lue sur le disque (`libraryfolders.vdf`, manifestes, temps de
  jeu dans `localconfig.vdf`), plus de liste de jeux dans `config.yaml`. Le magasin passe par les
  API publiques de `store.steampowered.com`. Les noms dits sont rapprochés des vrais : chiffres
  romains (« Age of Empires 2 » = « II »), sigles (« AoE 2 », « CS2 »), et un autre numéro n'est
  pas le même jeu (« Age of Empires 4 » ne lance pas le II) ; si la recherche du magasin tombe sur
  un jeu installé, il est lancé. `steam_open` affiche une page de l'application Steam
  (bibliothèque, téléchargements et mises à jour, magasin) par les liens `steam://`.
- **Web** : `web_search` interroge DuckDuckGo (version HTML, sans clé d'API) pour que Jarvis
  réponde lui-même ; `browser_search` ouvre les résultats dans le navigateur. `youtube_play` lance
  directement la première vraie vidéo (lue dans la page de résultats, sans clé d'API : pas les Shorts
  ni la publicité) ; `twitch_watch` ouvre le live d'une chaîne. Avant eux, « lance une vidéo de
  MrBeast » n'ouvrait que la page de résultats.
- **PowerShell** : pour tout ce qu'aucun outil ne couvre (fichiers, réseau, matériel...).
  Limite vue en réel : le modèle invente parfois des commandes (`Get-WindowsUpdateHistory`), ou un
  script qui ne prouve rien (« pilotes OK » en ne gardant que les périphériques en état OK).
- **Système** : `system_health` (psutil, `nvidia-smi`, santé des disques) et `check_updates`
  (Windows Update, pilotes compris, et winget, **sans rien installer**). Les mises à jour des jeux
  dans les launchers (Steam, Battle.net, Epic...) ne sont pas vues : pas d'API simple.

## 3. Sécurité : le code décide, pas le modèle

Le prompt rappelle les interdits, mais la garantie est dans le code : chaque outil sensible a un
garde-fou qui rend **autoriser**, **confirmer** ou **refuser**. Un refus est renvoyé au modèle
(« REFUSÉ par la sécurité de Jarvis : ... ») pour qu'il l'explique.

| Domaine | Lecture directe | Confirmation | Refus |
|---|---|---|---|
| PowerShell | `Get-*`, `Measure-*`, `ipconfig`... | lancer, copier, arrêter un processus, modifier le presse-papiers | supprimer, éteindre, `Set-*`, registre, comptes, installer, code dynamique, redirection vers un fichier, `-Force`, objets COM qui exécutent (`InvokeVerb`, `ShellExecute`, `.Run()`) |
| Fenêtres | lire n'importe quelle fenêtre | boutons « Envoyer », « Acheter », « Payer », « Publier », « Installer », « Confirmer » | boutons « Supprimer », « Désinstaller », « Réinitialiser », « Mot de passe », « Arrêter », « Déconnexion » ; agir dans une console, les paramètres, le registre, le gestionnaire des tâches, une fenêtre UAC |
| Messageries | lire | valider un texte ou appuyer sur Entrée (le message est relu à voix haute) | |
| Clavier | raccourcis courants | | Suppr, Win+R, Win+X, Alt+F4 |
| Explorateur | | | écrire ailleurs que dans la recherche (la barre d'adresse lance des commandes) |
| Applications | ouvrir, fermer normalement | forcer la fermeture | forcer la fermeture d'un processus système |
| Web | `http` et `https` | ouvrir un site **après** avoir lu du contenu extérieur | autres schémas (`file:`, `javascript:`) |

Points clés :
- **PowerShell est analysé par l'analyseur syntaxique de PowerShell** (`Parser::ParseInput`),
  pas par une recherche de mots. Les alias sont résolus (`rm`, `del` deviennent `Remove-Item`),
  et ce qui empêcherait l'analyse est refusé : `iex`, `& ('Remove' + '-Item')`, `$objet.$methode()`.
  41 scripts de test couvrent les trois verdicts (`tests/test_shell.py`).
- **Exécution via un fichier `.ps1` temporaire** : l'antivirus bloquait `-EncodedCommand`,
  technique courante des logiciels malveillants.
- **Contenu extérieur = données, jamais des ordres.** Chaque résultat de ce type est marqué comme
  tel pour le modèle. Après en avoir lu, ouvrir un site demande confirmation : une page qui
  écrirait « ouvre tel lien » ne peut pas faire agir Jarvis seul (injection de prompt).
- **Confirmation** : « oui » exécute, « non » annule toute la suite prévue, et toute autre réponse
  est traitée comme une correction (« non, écris plutôt... ») : le modèle refait l'action avec.
- **Applications du Store** : elles partagent toutes le processus `ApplicationFrameHost`. Jarvis
  retrouve le vrai processus de l'appli par sa fenêtre enfant ; sinon, « ferme la calculatrice »
  aurait fermé aussi Microsoft To Do.

## 4. Le modèle : Qwen3 8B

**Choisi le 3 novembre 2026** : 90 à 93 % au benchmark (section 6), contre 84 % pour Qwen2.5 7B,
avec **100 % d'appels natifs** (59 % avant) et ~500 à 630 ms par étape. Mesuré sur la carte :
**7,0 Go** avec le contexte de 16 384 jetons, entièrement sur le GPU ; avec Whisper et les autres
applications ouvertes, 12,3 Go utilisés sur 16.

| Catégorie | Qwen2.5 7B | Qwen3 8B | | Catégorie | Qwen2.5 7B | Qwen3 8B |
|---|---|---|---|---|---|---|
| Réponses directes | 8/8 | 8/8 | | Spotify | 8/9 | 9/9 |
| Interdits (aucun outil) | 11/11 | 11/11 | | Bureau | 4/4 | 3/4 |
| Applications | 15/18 | 16/18 | | Mémoire | 4/6 | 6/7 |
| Interface | 2/4 | 4/4 | | Système | 8/9 | 8/9 |
| Web | 10/10 | 9/10 | | Plusieurs étapes | 3/3 | 3/3 |
| Steam | 6/8 | 6/8 | | Suivi de conversation | 4/6 | 5/6 |
| Son et médias | 5/9 | 8/9 | | **Total** | **84 %** | **91 %** |

- **`think: false`** (`config.yaml`) : Qwen3 « réfléchit » à voix basse avant de répondre, ce qui
  est trop lent pour la voix. Jarvis coupe cette réflexion à chaque requête.
- **Écarté : Qwen3.5 9B** (6,6 Go). Réflexion coupée : 34 %, il n'appelle presque aucun outil
  (0/18 sur les applications). Réflexion active : excellent (17/18 applications, 8/9 son, 7/9
  Spotify), mais **4 à 6 s par étape, jusqu'à 16 s** : trop lent pour la voix. Qwen3 8B, lui, fait
  aussi bien sans réflexion (la réflexion ne lui apporte qu'un point, pour 3 s de plus par étape).
- **Prénom inventé** : à « comment je m'appelle ? », Qwen3 8B invente un prénom presque à chaque
  fois (« Lucas », « Jules »), ou le prend dans le titre de la fenêtre au premier plan (« InMoov »),
  malgré les consignes. Le code écarte toute réponse « tu t'appelles X » si X n'est pas dans la
  mémoire (section 5) et répond qu'il ne connaît pas le prénom.
- **Demandes en plusieurs parties** : avec la règle « OK » d'origine, il s'arrêtait après la
  première action (« mets pause, attends 5 secondes et lance ma playlist »). La règle dit
  maintenant de répondre « OK » seulement quand **toutes** les actions demandées sont faites.
- **Offres d'aide** : il termine presque chaque réponse par « Tu veux que je fasse quelque chose
  d'autre ? » (essai réel du 3 novembre, 20 h 29), pénible à l'oral. Le prompt l'interdit, et le
  code retire en plus les phrases finales d'offre générique (« autre chose », « si tu as besoin »,
  « dis-le-moi »...) : le modèle ne les suit pas toujours, et elles ne restent pas non plus dans
  l'historique, qu'il imiterait. Une vraie question (« la version II ou IV ? ») est gardée.
- **« Tu peux… ? »** : « est-ce que tu peux ouvrir Steam ? » recevait « oui, Steam est déjà
  ouvert » sans rien faire. Le prompt précise que c'est une demande d'action.
- **Action de remplacement** : « mets un pouce à cette vidéo » donnait `media` « suivante », l'outil
  le plus proche. Le prompt et la description de `media` l'interdisent ; il le fait encore au
  benchmark (un outil YouTube dédié reste à faire).
- **Ratés restants de Qwen3 8B** : « OK » sans outil sur des demandes indirectes (« j'ai envie de
  coder », « c'est trop fort », « bonne nuit Jarvis ») — dans l'agent réel, il est alors relancé ;
  `open_app` au lieu de `steam_launch` pour un jeu (« lance Rocket League »). Un appel écrit en
  texte avec une majuscule (`Clipboard_write {...}`), compté raté, est désormais récupéré.

### Avant : Qwen2.5 7B

Le premier modèle de l'agent. **Limites constatées en essais réels** (3 novembre 2026), qui ont
motivé les parades ci-dessous (toujours actives) :
- Il écrit souvent ses appels **en texte** au lieu du format d'outil : seuls 59 % des appels du
  benchmark sont natifs. Formats vus : `volume up`, `ui_click {"element": 2}`,
  `ui_click(element=5)`, `open_url("github.com")`.
- Il **annonce** parfois l'action sans la faire (« Je ferme Discord. »), ou répond « OK » d'emblée.
  Dans une vraie conversation, l'erreur s'entretient : il recopie ses annonces des tours précédents.
- Il glisse l'appel à la fin d'une phrase : « Je vais lancer SkyyArt. `steam_launch("SkyyArt")` ».
- Il invente des valeurs hors liste (`media` avec `pause` au lieu de `play_pause`).
- **Piloter une application en plusieurs étapes n'est pas fiable** : dans la Calculatrice, après
  avoir lu la fenêtre, il explique à l'utilisateur où cliquer au lieu de cliquer.

**Parades dans le code** :
- Les appels écrits en texte sont récupérés, seulement pour des outils qui existent : message
  fait uniquement d'appels (`volume up`, `ui_click(element=5)`...), ou appel glissé dans une
  phrase sous une forme stricte (`nom(...)`, `nom {...}`, `{"name": ...}`). Les valeurs sont lues
  avec `json` ou `ast.literal_eval` : rien n'est jamais exécuté. Les garde-fous s'appliquent comme
  d'habitude.
- « OK » ou annonce d'action (« je vais ouvrir... », « je lance... ») sans aucun outil réussi : le
  modèle est relancé une fois (« rien n'a été fait »). S'il recommence, Jarvis répond « Je n'ai pas
  réussi à le faire : aucune action n'a été exécutée », **jamais** « C'est fait ». Une question
  (« quel jeu veux-tu que je lance ? ») n'est pas une annonce.
- Valeurs de liste fermée vérifiées, avec des alias (`pause` → `play_pause`, `promos` → `specials`) ;
  une valeur inconnue renvoie au modèle les valeurs possibles.
- L'interface montre toujours ce qui s'est passé : outil inconnu, arguments invalides, ou
  « aucun outil utilisé ». Le journal note le résultat de chaque outil.
- **Ni les annonces fausses, ni la relance, ni les tours ratés ne restent dans l'historique.** Dans
  un essai réel, après quelques échecs gardés en mémoire, le modèle a fini par répondre « je ne peux
  pas lancer PalWorld sur ton ordinateur » : il imitait ses propres échecs. Le prompt rappelle aussi
  qu'il agit vraiment sur le PC.
- Cliquer sur un numéro inconnu (fenêtre pas encore lue) ne fait rien : Jarvis relit la fenêtre et
  renvoie la liste au modèle.
- Le prompt interdit « OK » avant tout appel d'outil. Trois variantes de cette règle ont été
  mesurées (79 à 82 % à heure fixe) : sans elle, les appels natifs montent à 87 %, mais le score
  ne bouge pas.

**Suite** : si Qwen3 8B ne suffit pas pour piloter une application en plusieurs étapes, essayer
un 14B (`bench.run --model qwen3:14b`). En Q4 (~9 Go plus le contexte), il tient sur la carte avec
Whisper, mais de justesse.

## 5. Mémoire de conversation

- Les **6 derniers tours** sont gardés, **10 minutes** au plus : « ouvre Discord », puis
  « ferme-le ».
- Les tours précédents gardent **leurs appels d'outils**, avec des résultats tronqués (1 500
  caractères pour le dernier tour, 200 pour les plus anciens). Résumés en « question → réponse »,
  ils apprenaient au modèle à répondre sans agir : sur le benchmark, le suivi de conversation est
  passé de 1 cas sur 6 à 4 sur 6 (un des six cas a aussi été remplacé entre-temps).
- **Piège du template Qwen d'Ollama** : un message de l'assistant n'affiche ses appels d'outils
  que si son texte est vide. Jarvis enregistre donc ces messages avec un texte vide. Sinon le
  modèle ne voit plus ses appels passés.
- Chaque demande porte la **date, l'heure et la fenêtre au premier plan**, marquées « Contexte » :
  « ferme ça » sait de quoi on parle.
- **Ce que l'utilisateur a demandé de retenir est dans le prompt** (30 informations au plus, les
  plus récentes). Sans cela, à « comment je m'appelle ? », le modèle répondait sans appeler
  `recall`, et inventait (« tu t'appelles Lucas »). Le prompt précise que ce qui n'y est pas, il ne
  le sait pas, et qu'il ne doit pas le déduire des titres de fenêtres. Comme cela ne suffit pas pour
  le prénom, le code remplace toute réponse « tu t'appelles X » dont X n'est pas en mémoire.
- **`num_ctx: 16384`** : le prompt et les outils font déjà ~3 700 jetons (mesuré), et une
  fenêtre lue peut en ajouter des milliers : les 4 096 par défaut d'Ollama seraient dépassés dès
  la première lecture.

## 6. Un benchmark pour l'agent

`bench/run.py` envoie chaque phrase de `bench/cases.yaml` au modèle et examine son **premier**
message, sans rien exécuter (`Agent.first_step`). 140 cas en 15 catégories : réponses directes,
interdits (aucun outil attendu), applications, interface, web, Steam, son, Spotify, bureau,
mémoire, système, minuteurs, messages, demandes en plusieurs étapes, suivi de conversation (avec un historique d'appels
d'outils, comme dans l'agent réel). Un cas peut simuler la fenêtre au premier plan. La mémoire
est simulée (prénom Dan, anniversaire de maman le 12 mars) : le score ne dépend pas de la vraie base.

Il mesure le score par catégorie, la latence, et la **part d'appels natifs**. Les commandes directes
comptent dans le score, comme dans Jarvis (`--model-only` mesure le modèle seul).

Limites, pour lire le score honnêtement :
- seul le premier choix est mesuré, pas une tâche complète en plusieurs étapes ;
- le prompt contient l'heure : le score varie d'environ ±3 points d'un lancement à l'autre ;
- les cas ont été écrits par la même personne que le prompt : il faut ajouter de vraies demandes
  mal traitées, tirées de `jarvis.log` ;
- les anciens résultats (`bench/results/*.json`, classifieur) ne sont pas comparables.

## 7. Pas de mode secours sans Ollama

Sans Ollama, Jarvis ne comprend rien : il le dit clairement au démarrage et à chaque demande.
Maintenir en parallèle un analyseur à expressions régulières coûterait cher pour un résultat
bien inférieur.

## 8. Voix

- **Reconnaissance : faster-whisper `large-v3-turbo` sur GPU.** En français, il transcrit
  correctement « mon code wifi est 4821 » ou « Counter Strike 2 » (`small` écrivait « Jervis »),
  en quelques dixièmes de seconde. `stt.prompt` oriente Whisper vers le vocabulaire attendu ; le nom des jeux installés y est ajouté
  au démarrage. Les
  bibliothèques CUDA sont installées par pip ; si CUDA échoue, Jarvis passe sur le CPU.
  L'enregistrement s'arrête après 1,2 s de silence, et abandonne si personne ne parle pendant 5 s.
- **Démarrage hors ligne** : le modèle Whisper est d'abord cherché dans le cache local, et
  téléchargé seulement s'il manque.
- **Synthèse : Piper**, hors ligne, voix `fr_FR-tom-medium`. edge-tts envoyait chaque réponse
  aux serveurs de Microsoft (et renvoyait une erreur 403).
- **Ce qui est dit** : la réponse entière, sans puces ni émojis (Qwen3 en met malgré la consigne,
  ils restent affichés), coupée en fin de phrase vers 400
  caractères (~25 s) ; le reste est affiché. Pour une question de confirmation, seule la première
  ligne est dite : le détail (le script PowerShell, par exemple) reste à l'écran.
- **Mot d'activation : « Ok Jarvis » (depuis le 15/11/2026, avant : openwakeword `hey_jarvis`).** Aucun
  modèle openwakeword n'existe pour « Ok Jarvis », et en entraîner un demande des Go de données et des
  heures. Le détecteur de voix Silero (processeur, fourni avec openwakeword) découpe les prises de parole ;
  le Whisper déjà chargé en fait un premier jet (faisceau de 1, ~0,27 s) avec « Ok Jarvis. » pour seul
  exemple : sans lui, « Ok Jarvis » devenait « Ok, j'ai revu » (8/8 détectés avec, 0 faux réveil sur
  « Ok, j'ai vu », « Ok, d'accord », du bruit). La phrase est cherchée à chaque pause de 0,4 s, et toutes
  les 2 s en parole continue. Dès qu'elle est reconnue, l'anneau passe au vert et la commande est captée
  par le même flux micro : rien n'est perdu, même dite d'une traite (« Ok Jarvis, lance Spotify »).
  Coût : ~0,7 s entre « Jarvis » et l'anneau vert (openwakeword : instantané), une transcription par prise
  de parole entendue. `wakeword.phrase: ""` revient à openwakeword. La détection est en pause pendant
  l'écoute et pendant que Jarvis parle, pour qu'il ne se réveille pas lui-même.
- **Après une question, Jarvis écoute aussitôt** la réponse, sans « Ok Jarvis ».

## 9. Veille

« Mets-toi en veille » décharge le modèle de la carte graphique (`keep_alive: 0`), et Jarvis
n'écoute plus que « Ok Jarvis ». La demande suivante le réveille ; le modèle se recharge en
quelques secondes. Utile pour jouer avec toute la mémoire graphique.

## 10. Son et musique : touches multimédia virtuelles, et l'API Spotify

Jarvis simule les touches « volume + », « volume − », « muet », « lecture/pause », « suivant » et
« précédent » d'un clavier multimédia.

**Pause, reprise, suivant, précédent** (`media`, classe `Media`) : Jarvis demande d'abord à l'API Spotify
ce qui joue. Si Spotify joue, c'est l'API qui agit : la musique peut tourner sur un **autre appareil**
(un autre PC du compte), que les touches de ce PC n'atteignent pas, et c'était la cause des « pause »
sans effet. « Pause » ne relance jamais la musique (la touche lecture/pause, elle, bascule). Sinon,
les touches agissent sur le lecteur de ce PC (YouTube, VLC...) ; « reprends » relance ce qui a été mis
en pause. **La musique va à Spotify par défaut** (« lance The Mysterious Altar de Hastron ») ; YouTube
seulement si l'utilisateur parle de vidéo, de clip, de YouTube ou d'internet.

- **Aucune dépendance**, et cela fonctionne avec **n'importe quel lecteur** (Spotify, YouTube dans
  le navigateur, VLC...), puisque Windows envoie ces touches au lecteur actif.
- Une pression vaut 2 % : « monte le son » fait 5 pressions, soit +10 %.
- « Volume à 30 » : 50 pressions vers le bas (retour à 0), puis 15 vers le haut, à ±2 % près.
- Limite : Jarvis ne lit pas le volume actuel (`pycaw` le permettrait).

**YouTube** (`youtube_control`) : la vidéo affichée est trouvée par le titre de la fenêtre du
navigateur (« … - YouTube - Brave ») ; plein écran, avancer, reculer et sous-titres sont les
raccourcis de YouTube (f, l, j, c) envoyés à cet onglet. Le pouce n'a pas de raccourci : le bouton
« J'aime cette vidéo… » est cherché par UI Automation et cliqué, sauf s'il est déjà enfoncé.
« Lance la deuxième vidéo » après une recherche relance la recherche et ouvre la 2e
(`youtube_play`, `rank=2`) : plus sûr qu'un clic dans la page.

**Minuteurs et rappels** (`set_timer`) : un `threading.Timer` par rappel ; à l'heure dite, le
texte passe par la file de commandes du moteur et Jarvis le dit, même en veille. Ils ne survivent
pas à un redémarrage de Jarvis, et sont limités à 24 h.

## 11. Architecture du code

- **Deux threads** : la fenêtre ne fait qu'afficher ; un thread de travail fait tout le reste
  (chargements, écoute, agent, outils, voix). Ils communiquent par des files d'attente, donc la
  fenêtre ne se fige jamais. UI Automation exige une initialisation COM dans ce thread.
- **Chargement brique par brique** : si le micro ou la voix sont en panne, le reste fonctionne
  (une demande tapée au clavier, par exemple). L'erreur s'affiche dans la fenêtre.
- **Mémoire SQLite**, compatible avec la base de la première version, utilisable depuis les deux
  threads.
- **Noms approchants** : « discorde » trouve Discord, « spotifaille » trouve Spotify, et « wifi »
  retrouve la clé « code wifi ». Cela compense les erreurs de transcription.

## 12. Petits détails qui comptent

- **`127.0.0.1` au lieu de `localhost`** pour joindre Ollama. Sous Windows, `localhost` tente
  d'abord l'IPv6, sur lequel Ollama n'écoute pas, et perd **2 secondes par requête** (2 069 ms
  contre 15 ms).
- **`keep_alive: 30m`** : le modèle reste en mémoire graphique entre deux demandes. Il est aussi
  préchargé au démarrage, pour que la première demande ne prenne pas 20 s.
- **Erreurs PowerShell séparées de la sortie** : le modèle reçoit le message court de chaque
  erreur et la consigne de corriger son script. Avant, une erreur passait pour un résultat, et le
  modèle inventait une explication.
- **Chemins relatifs au dossier du projet** (logs, mémoire, voix) : Jarvis fonctionne quel que
  soit le dossier de lancement, ce qui est indispensable au démarrage automatique.
- **Python 3.12** : toutes les dépendances (ctranslate2, onnxruntime, piper-tts) y ont des
  paquets Windows précompilés.

## Pistes suivantes

1. **Essais réels avec Qwen3 8B**, surtout le pilotage d'applications (Calculatrice) ; s'il bloque,
   passer un 14B au benchmark (`qwen3:14b`).
2. **Messages** : vérifier en réel l'envoi Discord et WhatsApp (la navigation et la recherche ont été
   testées sans rien envoyer).
3. **Benchmark de tâches complètes** : exécuter des scénarios sans risque (Calculatrice,
   « ce qui passe sur Spotify ») de bout en bout, pour mesurer le pilotage en plusieurs étapes.
4. **Moins d'outils par demande** : n'envoyer au modèle que les outils du domaine de la phrase
   (49 aujourd'hui), pour réduire les confusions.
5. **Spotify** : file d'attente par l'API, choix de l'appareil à la voix.
6. **Volume exact** avec `pycaw`.
7. **Accents des commandes Windows** (`ipconfig`...) mal décodés dans les sorties PowerShell.
