# mypianotool-backend

Service de reconnaissance optique de partitions (OMR) pour [MyPianoTool]. On lui
envoie une photo ou un scan, il renvoie du **MusicXML** — le format que la page
Partition sait déjà importer.

Le moteur est [Audiveris] : moteur OMR en Java, AGPL, le plus abouti des libres.

> **Changement du 4 octobre 2026 — oemer → Audiveris.** Le service a d'abord été
> écrit autour d'[oemer], pour sa tolérance supposée aux photos de travers. À
> l'essai, il n'a rien rendu d'exploitable. Audiveris demande une image plus
> propre et plus résolue, mais il transcrit. oemer reste dans le dépôt
> (`MPT_MOTEUR=oemer`, `requirements-oemer.txt`) : le moteur est derrière une
> interface, en changer n'a touché qu'un fichier — plus le Dockerfile, qui passe
> d'un runtime Python à un runtime Java.

[MyPianoTool]: ../mypianotool
[Audiveris]: https://github.com/Audiveris/audiveris
[oemer]: https://github.com/BreezeWhite/oemer

## Ce que le service fait, et ce qu'il ne fait pas

Il **prépare** l'image (orientation EXIF, HEIC d'iPhone, première page d'un PDF,
mise à l'échelle, égalisation du contraste), la passe à Audiveris, relit le
MusicXML produit et en tire un **compte rendu**.

Il ne connaît rien du modèle interne du site. La conversion reste du côté
d'Angular, dans `musicxml.import.ts`, déjà éprouvé sur le Canon et la Sonate au
clair de lune.

**La reconnaissance se trompe.** Sur une page nette et bien résolue d'une
partition imprimée, comptez 85 à 95 % de notes justes ; sur du manuscrit, sur une
photo prise de biais ou sur une page mal éclairée, Audiveris préfère souvent ne
rien rendre du tout plutôt que de deviner. C'est
pour cela que le compte rendu signale les **mesures dont les durées ne tombent pas
juste** : un moteur d'OMR ne donne pas de score de confiance, mais une mesure
fausse est presque toujours une mesure mal lue. Le scan fait le gros de la saisie ;
l'éditeur de la page Partition fait le reste.

## Démarrer

### Avec Docker — le chemin recommandé

C'est le plus simple : l'image compile Audiveris, pose les données de l'OCR et
embarque le service.

```bash
docker build -t mypianotool-backend .
docker run --rm -p 8077:8077 mypianotool-backend
```

Compter une dizaine de minutes et ~1,5 Go. Audiveris est **compilé depuis ses
sources** (tag `5.9.0`, réglable par `--build-arg VERSION_AUDIVERIS=…`) plutôt
qu'installé depuis un paquet : les paquets officiels sont des `.deb`/`.rpm`
amd64, inutilisables tels quels sur un Mac Apple Silicon, alors que la
compilation marche sur les deux architectures.

L'image part de `eclipse-temurin:25-jdk` : Audiveris 5.9 exige **Java 25**
(`gradle.properties`, `theMinJavaVersion`), et Temurin ne publie pas de variante
`-jre` pour cette version — le JDK sert donc aussi à l'exécution.

### En développement, sans Audiveris

Le moteur `factice` rend un MusicXML fixe en une seconde et demie. De quoi
développer l'interface sans JVM ni attente.

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt

MPT_MOTEUR=factice uvicorn app.main:app --port 8077 --reload
```

### Avec Audiveris installé à la main

Si vous avez déjà Audiveris sur la machine (paquet officiel, ou
`./gradlew :app:installDist` dans un clone du dépôt) :

```bash
pip install -r requirements.txt
export MPT_AUDIVERIS_CMD=/chemin/vers/Audiveris   # inutile s'il est dans le PATH
export TESSDATA_PREFIX=/chemin/vers/tessdata      # doit contenir eng.traineddata
uvicorn app.main:app --port 8077
```

`/api/sante` indique `moteurPret` et la version d'Audiveris : c'est le moyen le
plus rapide de vérifier que le service le trouve bien.

Vérification :

```bash
curl -s localhost:8077/api/sante | python3 -m json.tool
```

## L'API

Un scan est long ; la requête HTTP ne l'attend pas. On dépose, on interroge.

| Méthode | Route | Rôle |
|---|---|---|
| `GET` | `/api/sante` | état, moteur, modèles présents, formats lisibles, limites |
| `POST` | `/api/scans` | dépose une image (multipart, champ `image`) → `202` + identifiant |
| `GET` | `/api/scans/{id}` | état, progression, étape, compte rendu |
| `GET` | `/api/scans/{id}/musicxml` | le résultat |
| `GET` | `/api/scans/{id}/apercu` | l'image d'analyse, quand le moteur en produit une (oemer oui, Audiveris non) |
| `DELETE` | `/api/scans/{id}` | efface le travail et ses fichiers |

```bash
# Dépôt
curl -s -F "image=@partition.jpg" localhost:8077/api/scans
# → {"id":"4f2a…","etat":"en_attente","progression":0,…}

# Suivi
curl -s localhost:8077/api/scans/4f2a…
# → {"etat":"en_cours","progression":62,"etape":"Têtes de notes",…}

# Résultat
curl -s localhost:8077/api/scans/4f2a…/musicxml -o partition.musicxml
```

États : `en_attente` → `en_cours` → `termine` ou `echoue`.

Le compte rendu, une fois terminé :

```json
{
  "parties": 2, "mesures": 32, "notes": 418, "accords": 46,
  "silences": 21, "liaisons": 3, "alterations": 12,
  "chiffrage": "4/4", "armure": -1,
  "mesuresSuspectes": [7, 12, 23], "mesuresSuspectesTotal": 3,
  "avertissements": []
}
```

Codes d'erreur : `400` requête mal formée, `413` fichier trop lourd, `415` fichier
illisible comme image, `429` quota par IP, `503` file saturée.

## Formats acceptés

JPEG, PNG, TIFF, BMP, WebP, HEIC (iPhone), et la première page d'un PDF. Au delà de
3508 px de côté l'image est réduite ; en deçà de 1800 px elle est agrandie et le
résultat le signale — à cette résolution, la reconnaissance est peu fiable.

3508 px, c'est la hauteur d'une page A4 scannée à 300 dpi. Ce n'est pas un chiffre
rond par hasard : Audiveris commence par mesurer l'interligne des portées et en
déduit tout le reste de son analyse, il lui faut une quinzaine de pixels. C'est la
différence la plus visible avec oemer, qui se contentait de 2480 px.

## Configuration

Tout passe par l'environnement, préfixe `MPT_`.

| Variable | Défaut | Rôle |
|---|---|---|
| `MPT_MOTEUR` | `audiveris` | `audiveris`, `oemer` ou `factice` |
| `MPT_AUDIVERIS_CMD` | *(le PATH)* | chemin du lanceur Audiveris |
| `MPT_AUDIVERIS_MEMOIRE` | `3g` | `-Xmx` de la JVM |
| `MPT_PORT` | `8077` | port d'écoute |
| `MPT_ORIGINES` | `https://localhost:4200,http://localhost:4200` | origines CORS, séparées par des virgules |
| `MPT_DOSSIER_TRAVAUX` | `/tmp/mypianotool-omr` | où vivent les fichiers temporaires |
| `MPT_TAILLE_MAX_MO` | `25` | taille maximale d'un envoi |
| `MPT_LARGEUR_MIN` / `MPT_LARGEUR_MAX` | `1800` / `3508` | bornes de redimensionnement |
| `MPT_TRAVAUX_SIMULTANES` | `1` | scans en parallèle (la reconnaissance sature déjà un cœur) |
| `MPT_FILE_MAX` | `8` | au delà, `503` |
| `MPT_DELAI_MOTEUR_S` | `900` | au delà, le scan est tué |
| `MPT_DUREE_VIE_MIN` | `60` | rétention d'un résultat avant effacement |
| `MPT_LIMITE_IP_PAR_HEURE` | `20` | quota par adresse ; `0` désactive |
| `MPT_FACTICE_DUREE_S` | `1.5` | durée simulée du moteur factice |

## Tests

```bash
pytest                                # ou, sans pytest, fichier par fichier :
python tests/test_analyse.py          # compte rendu tiré du MusicXML
python tests/test_image.py            # préparation de l'image
python tests/test_moteur_audiveris.py # commande, résultat, messages d'échec
python tests/test_moteur.py           # oemer et lecture des .mxl
python tests/test_api.py              # lance un vrai serveur en moteur factice
```

`test_api.py` démarre uvicorn en sous-processus et tape dessus en HTTP : multipart,
codes de statut, en-têtes, CORS, quota. C'est ce que verra le navigateur.

`test_moteur_audiveris.py` fait tourner le vrai code de transcription contre un
**faux Audiveris** : un script shell qui imite ses lignes de journal et écrit un
`.xml`. Le sous-processus, la progression et la lecture du résultat sont donc
éprouvés sans JVM. Ce qu'aucun de ces tests ne dit, c'est si la reconnaissance
est bonne : cela, seule une vraie photo le dira.

## Pourquoi ces choix

- **Starlette plutôt que FastAPI.** Cinq routes, un corps multipart, aucun schéma à
  valider. FastAPI n'apporterait ici que des dépendances. Le remplacement, si le
  besoin vient, ne touche que `app/main.py`.
- **Audiveris en sous-processus.** Il est écrit en Java : il n'y a pas d'autre
  chemin depuis Python. L'avantage secondaire reste le même qu'avec oemer — un
  scan qui s'éternise ou qui explose en mémoire se tue sans emporter le serveur.
- **La progression est honnête, donc grossière.** Audiveris ne journalise pas ses
  vingt étapes en mode batch. Plutôt qu'inventer des étapes jamais observées, le
  service n'annonce que les quelques lignes qu'Audiveris écrit vraiment, et fait
  avancer la barre avec le temps entre deux.
- **Un seul processus, état en mémoire.** Il n'y a pas de base : un scan vit une
  heure et meurt. Pour tenir plus de charge, augmenter `MPT_TRAVAUX_SIMULTANES`, ou
  mettre plusieurs conteneurs derrière un répartiteur à session persistante — un
  identifiant de scan appartient au processus qui l'a créé.
- **Le moteur est derrière une interface** (`app/moteurs/base.py`). Le passage
  d'oemer à Audiveris n'a touché qu'un fichier de moteur, trois valeurs par défaut
  et le Dockerfile. Passer à un service commercial coûterait le même prix.

## Mise en production

Le service n'a **aucune authentification** : il n'est pas fait pour être exposé
directement. Le mettre derrière le reverse proxy du site, sur un chemin dédié
(`/omr`), ce qui règle aussi le CORS et le certificat. Voir
[`doc/integration-angular.md`](doc/integration-angular.md).

Prévoir du processeur **et de la mémoire** : une page occupe un cœur pendant une à
plusieurs minutes, et la JVM demande jusqu'à 3 Go (`MPT_AUDIVERIS_MEMOIRE`).
Rien n'est persisté, rien n'est journalisé du contenu des partitions ; les fichiers
déposés sont effacés après `MPT_DUREE_VIE_MIN` minutes.

## Licences

Ce service est à vous. **Audiveris est sous licence AGPL v3** : tel qu'il est
utilisé ici — un programme séparé, appelé en sous-processus, qui communique par
fichiers — il n'impose rien au code de MyPianoTool. Si vous deviez un jour l'
intégrer autrement (sa bibliothèque dans votre propre processus), la question se
poserait. oemer, lui, est sous licence MIT. Les partitions scannées restent
la propriété de leurs ayants droit : la règle du catalogue de MyPianoTool — aucune
édition tierce embarquée dans le site — vaut aussi ici, un scan reste chez
l'utilisateur qui l'a fait.

## Structure

```
app/
  main.py              routes HTTP, CORS, limites de taille
  travaux.py           file d'attente, cycle de vie d'un scan, ménage
  image.py             préparation de l'image (EXIF, HEIC, PDF, échelle, contraste)
  analyse.py           compte rendu tiré du MusicXML (mesures suspectes)
  config.py            configuration par variables d'environnement
  moteurs/
    base.py            l'interface
    audiveris_moteur.py  Audiveris en sous-processus, échecs traduits en clair
    oemer_moteur.py      l'ancien moteur, conservé
    factice.py           moteur de développement et de test
scripts/
  telecharger-modeles.py   (modèles d'oemer uniquement)
doc/
  integration-angular.md
tests/
```
