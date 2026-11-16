# Démo Jarvis : scénario (environ 2 min 50)

Une seule prise : le diaporama, puis Jarvis en direct, puis la dernière slide.
Le PowerPoint est `Jarvis_presentation.pptx` ; ce qu'il faut dire sur chaque slide est dans ses notes.

## Avant d'enregistrer (10 min)

1. **Notifications coupées** : Win+N, puis « Ne pas déranger ». Ferme ce qui ne sert pas à la démo.
2. **Casque avec micro** : sinon le micro capte la musique et la voix de Jarvis, et « Ok Jarvis » passe mal.
3. **Ouvre et connecte** Spotify, Discord et Paint fermé. Vérifie que le calendrier « Jarvis test » existe.
4. **Vérifie les connexions** (depuis `C:\delivery\2\EpyJarvis`) :
   ```powershell
   C:\delivery\2\Jarvis\.venv\Scripts\python.exe -m jarvis.tools.agenda status
   C:\delivery\2\Jarvis\.venv\Scripts\python.exe -m jarvis.tools.spotify status
   ```
5. **Lance Jarvis depuis le repo**, une minute avant (le chargement prend environ 25 s) :
   ```powershell
   cd C:\delivery\2\EpyJarvis
   C:\delivery\2\Jarvis\.venv\Scripts\pythonw.exe main.py
   ```
   L'icône du Bureau lance l'ancien dossier `Jarvis` : même code, mais lance depuis le repo pour montrer qu'il
   marche.
6. **Répète une fois** toutes les phrases ci-dessous, puis supprime l'événement de test (voir « Après »).
7. Ouvre `Jarvis_presentation.pptx` et lance le diaporama (F5).

## Enregistrer

- **Outil Capture d'écran** de Windows 11 : Win+Shift+R, sélectionne tout l'écran, active **le micro et le son
  du système** (sinon on n'entend pas la voix de Jarvis), puis Démarrer.
- Pendant le diaporama : Alt+Tab pour passer à Jarvis, Alt+Tab pour revenir à la dernière slide.
- Pour couper les attentes : **Clipchamp** (déjà installé sur Windows 11).

## Le déroulé

| Temps | Écran | Tu dis | Ce que doit faire Jarvis |
|---|---|---|---|
| 0:00 | Slide 1 | Présentation du projet (notes de la slide) | |
| 0:08 | Slides 2 à 6 | Une ou deux phrases par slide (notes) | |
| 1:00 | Slide 7 « Démo », puis Alt+Tab vers Jarvis | « Place à la démo. » | |
| 1:03 | Jarvis | « Ok Jarvis, ouvre Paint. » | Paint s'ouvre au premier plan |
| 1:09 | | « Ok Jarvis, ferme-le. » | Paint se ferme |
| 1:15 | | « Ok Jarvis, comment va mon PC ? » | Processeur, mémoire, carte graphique, à voix haute |
| 1:27 | | « Ok Jarvis, qu'est-ce que j'ai de prévu demain ? » *(facultatif si tu dépasses)* | Le planning de demain |
| 1:38 | | « Ok Jarvis, ajoute une séance de sport vendredi à 18 h dans mon calendrier Jarvis test. » | Créée directement, il dit la date, l'heure et le calendrier |
| 1:52 | | « Ok Jarvis, écris salut dans le salon général de Dan's office. » | Il ouvre le salon et **demande confirmation** en nommant le salon |
| 2:02 | | « Oui. » | Le message part, il l'annonce quand il apparaît |
| 2:12 | | « Ok Jarvis, éteins l'ordinateur. » | **Refus**, sans même appeler le modèle |
| 2:20 | | « Ok Jarvis, lance ma playlist phonk. » | La musique démarre sur Spotify |
| 2:30 | | « Ok Jarvis, mets pause, attends 5 secondes et passe à la suivante. » | Trois actions enchaînées |
| 2:42 | | « Ok Jarvis, pause. » puis « Ok Jarvis, bonne nuit. » | Musique en pause, puis Jarvis en veille |
| 2:48 | Alt+Tab vers la slide 8 | Conclusion (notes de la slide) | |

## Si ça rate pendant la prise

- **Phrase mal comprise** : répète-la, ou tape-la dans le champ texte de Jarvis (ça marche pareil).
- **Mauvaise destination à la confirmation** : réponds « non », rien n'est envoyé.
- **Spotify ne joue pas** : lance un morceau à la main une fois dans l'appli (il faut un appareil actif), puis
  redemande.
- **Jarvis ne réagit plus** : regarde son état dans la fenêtre (VEILLE : redis « Ok Jarvis »).

## Après

- « Ok Jarvis, supprime la séance de sport de vendredi dans mon calendrier Jarvis test. » puis « Oui. »
- Le journal de la démo est dans `jarvis.log` (ignoré par git, ne le publie pas).
