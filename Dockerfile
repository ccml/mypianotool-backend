# syntax=docker/dockerfile:1
#
# Image du service de reconnaissance de partitions : Audiveris (Java) + le
# service HTTP (Python) dans le même conteneur.
#
#   docker build -t mypianotool-backend .
#   docker run --rm -p 8077:8077 mypianotool-backend
#
# Compter une dizaine de minutes et ~1,5 Go. Audiveris est compilé depuis ses
# sources plutôt qu'installé depuis un paquet : les paquets officiels sont des
# .deb/.rpm amd64, inutilisables tels quels sur un Mac Apple Silicon, alors que
# la compilation marche sur les deux architectures.

ARG VERSION_AUDIVERIS=5.9.0
# Audiveris 5.9 exige Java 25 (gradle.properties, theMinJavaVersion). Temurin ne
# publie pas de variante `-jre` pour 25 : le JDK sert donc aussi à l'exécution.
ARG IMAGE_JAVA=eclipse-temurin:25-jdk

# ---------------------------------------------------------------------------
# 1. Compilation d'Audiveris
# ---------------------------------------------------------------------------
FROM ${IMAGE_JAVA} AS audiveris

ARG VERSION_AUDIVERIS

RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /build
RUN git clone --depth 1 --branch ${VERSION_AUDIVERIS} \
      https://github.com/Audiveris/audiveris.git .

# `installDist` produit une distribution complète : lanceur + toutes les
# dépendances. Le dossier produit porte le nom du sous-projet (« app ») et non
# celui du lanceur (« Audiveris ») : le build d'Audiveris ne renomme que le
# script, par `applicationName` dans la configuration de CreateStartScripts.
# Plutôt que de parier sur ce nom, on prend le dossier produit, quel qu'il soit,
# et on échoue bruyamment s'il n'y a pas de lanceur dedans.
RUN ./gradlew --no-daemon :app:installDist \
 && set -eu; \
    dist="$(find app/build/install -mindepth 1 -maxdepth 1 -type d | head -1)"; \
    if [ -z "$dist" ]; then echo "installDist n'a produit aucune distribution"; exit 1; fi; \
    echo "Distribution trouvée : $dist"; \
    mkdir -p /opt/audiveris && cp -a "$dist/." /opt/audiveris/; \
    if [ ! -x /opt/audiveris/bin/Audiveris ]; then \
      echo "Lanceur introuvable. Contenu de bin/ :"; ls -l /opt/audiveris/bin || true; exit 1; \
    fi; \
    rm -rf ~/.gradle

# ---------------------------------------------------------------------------
# 2. Service
# ---------------------------------------------------------------------------
FROM ${IMAGE_JAVA} AS service

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_BREAK_SYSTEM_PACKAGES=1

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      python3 python3-pip python3-venv \
      libgomp1 curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*

RUN pip3 install --no-cache-dir \
      "starlette>=0.47,<2" \
      "uvicorn[standard]>=0.30" \
      "python-multipart>=0.0.9" \
      "pillow>=10.3" \
      "pillow-heif>=0.16" \
      "pypdfium2>=4.30"

# --- Audiveris -------------------------------------------------------------
COPY --from=audiveris /opt/audiveris /opt/audiveris
RUN ln -s /opt/audiveris/bin/Audiveris /usr/local/bin/Audiveris

# Données de l'OCR. Audiveris embarque Tesseract par JavaCPP mais pas ses
# langues : sans `eng.traineddata`, l'étape TEXTS échoue. Le dépôt et la version
# sont ceux que le projet référence (gradle.properties : theTessdataTag).
ARG TESSDATA_TAG=4.1.0
ENV TESSDATA_PREFIX=/opt/tessdata
RUN mkdir -p ${TESSDATA_PREFIX} \
 && for langue in eng fra; do \
      curl -fsSL -o ${TESSDATA_PREFIX}/$langue.traineddata \
        https://github.com/tesseract-ocr/tessdata/raw/${TESSDATA_TAG}/$langue.traineddata; \
    done

# Contrôle à la construction : si le lanceur, la JVM ou l'OCR sont mal posés,
# autant le savoir ici plutôt qu'au premier scan d'un utilisateur.
# `-batch -help` plutôt que `-version` : cette dernière option n'existe qu'à
# partir de la 5.10. Avant d'afficher son aide, Audiveris journalise son
# environnement, dont la ligne « OCR Engine » — c'est elle qu'on vient lire.
RUN JAVA_OPTS=-Djava.awt.headless=true Audiveris -batch -help 2>&1 | tee /tmp/audiveris-env.txt \
 && grep -q "Audiveris:" /tmp/audiveris-env.txt \
 || { echo "Audiveris ne démarre pas dans cette image."; exit 1; }
RUN grep -q "OCR Engine:.*[Tt]esseract" /tmp/audiveris-env.txt \
 || { echo "ATTENTION : Audiveris ne voit pas son OCR."; grep "OCR" /tmp/audiveris-env.txt || true; exit 1; }

# --- application -----------------------------------------------------------
WORKDIR /service
COPY app /service/app

RUN useradd --create-home --uid 10001 partition \
 && mkdir -p /var/lib/mypianotool-omr \
 && chown -R partition:partition /var/lib/mypianotool-omr /service
USER partition

ENV MPT_MOTEUR=audiveris \
    MPT_PORT=8077 \
    MPT_DOSSIER_TRAVAUX=/var/lib/mypianotool-omr \
    MPT_AUDIVERIS_CMD=/usr/local/bin/Audiveris \
    MPT_AUDIVERIS_MEMOIRE=3g \
    HOME=/home/partition

EXPOSE 8077

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD python3 -c "import urllib.request,os,sys; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('MPT_PORT','8077')+'/api/sante',timeout=4).status==200 else 1)"

# Un seul processus, délibérément : la file de travaux vit en mémoire. Audiveris
# prend déjà un cœur et jusqu'à 3 Go par page — donner plus de travailleurs à
# uvicorn ne ferait que mettre la machine à genoux.
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${MPT_PORT:-8077} --workers 1 --timeout-keep-alive 65"]
