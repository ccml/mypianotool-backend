# Déployer le service à côté du site

Le site tourne en Docker Compose derrière **Nginx Proxy Manager** (NPM), qui porte
le 80/443 et les certificats. Le service de reconnaissance vient s'ajouter comme un
quatrième conteneur sur le même réseau `apps-network`.

```
                    ┌─────────────────────┐
 Internet ─ 443 ──▶ │ nginx-proxy-manager │
                    └──────────┬──────────┘
                     /         │         \ /omr
                    ▼                     ▼
             mypianotool:4000      mypianotool-backend:8077
                    (site)            (reconnaissance)
                        apps-network
```

## 1. Construire et publier l'image

Comme pour le site, l'image part sur Docker Hub.

```bash
cd mypianotool-backend
docker buildx build --platform linux/amd64 \
  -t ccmlits/mypianotool-backend:0.1.0 --push .
```

**`--platform linux/amd64` n'est pas optionnel** si le serveur est un x86 et que
vous construisez depuis un Mac Apple Silicon. Vérifiez d'abord :

```bash
ssh serveur 'uname -m'      # x86_64 → amd64 ; aarch64 → arm64
```

Et prévoyez du temps : Audiveris est compilé depuis ses sources, et sous l'émulation
QEMU d'une construction croisée cela peut dépasser la demi-heure. Si c'est trop
long, construisez directement sur le serveur (`docker compose build`), à condition
qu'il ait la mémoire pour faire tourner Gradle.

Compter ~1,5 Go d'image. Vérifiez la place disponible avant de tirer.

## 2. Ajouter le service

Deux fichiers dans `srv_config/` :

- `mypianotool-backend/compose.yml` — le service, à copier tel quel ;
- `compose.yml` — un bloc `mypianotool-backend` de plus, sur `apps-network`.

Puis :

```bash
docker compose pull mypianotool-backend
docker compose up -d mypianotool-backend
docker compose logs -f mypianotool-backend
```

Au démarrage, le journal annonce le moteur et son état. Depuis un autre conteneur du
réseau, ou depuis l'hôte via `docker exec` :

```bash
docker exec mypianotool-backend \
  python3 -c "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:8077/api/sante').read().decode())"
```

`moteurPret` doit être `true` et `moteurVersion` ressembler à
`Audiveris 5.9.0:… · OCR Tesseract OCR, version 5.5.1`.

**Le service n'expose aucun port sur l'hôte, et c'est voulu** : il n'a aucune
authentification. Il ne doit être joignable que par le proxy, sur le réseau interne.

## 3. Router `/omr` dans Nginx Proxy Manager

Cette partie n'est pas dans un fichier : NPM garde sa configuration dans sa base
(`nginx-proxy-manager/data/`). Elle se fait dans l'interface, sur le port 81.

Ouvrez le *Proxy Host* du site, onglet **Custom locations**, et ajoutez :

| Champ | Valeur |
|---|---|
| Location | `/omr` |
| Scheme | `http` |
| Forward Hostname | `mypianotool-backend` |
| Forward Port | `8077` |

Puis, dans le petit engrenage de cette location (configuration avancée) :

```nginx
rewrite ^/omr/?(.*)$ /$1 break;
client_max_body_size 30m;
```

- Le **rewrite** retire le préfixe : NPM ne le fait pas tout seul, et sans lui le
  service recevrait `/omr/api/sante` au lieu de `/api/sante`.
- Le **client_max_body_size** laisse passer les photos. La limite du service est de
  25 Mo ; 30 Mo côté proxy lui laissent le soin de répondre `413` proprement plutôt
  que de faire couper nginx.

N'ajoutez pas de `proxy_pass` ici : NPM en écrit déjà un, et le doublon fait échouer
le rechargement d'nginx.

**Pas besoin d'allonger les délais.** C'est la contrepartie de l'API asynchrone :
le dépôt répond `202` tout de suite, le suivi est instantané, et la reconnaissance se
déroule côté serveur sans qu'aucune requête HTTP ne l'attende. Les 60 secondes par
défaut suffisent largement.

### Si le *rewrite* pose problème

Repli sans réécriture : un **sous-domaine** dédié (`omr.exemple.be`) avec son propre
Proxy Host vers `mypianotool-backend:8077`, racine comprise. Il faut alors :

- renseigner `MPT_ORIGINES=https://exemple.be` dans le compose (on passe en
  requêtes d'origine croisée, le service doit autoriser le site) ;
- mettre `apiScan: 'https://omr.exemple.be'` dans `environment.prod.ts` ;
- un certificat de plus.

C'est plus de pièces, mais rien à réécrire.

## 4. Pointer le site vers le service

Dans `mypianotool/src/environments/environment.prod.ts` :

```ts
export const environment = {
  production: true,
  apiScan: '/omr'
};
```

Tant que cette valeur est vide, la fenêtre « Scanner » dit que le service est
indisponible et le reste de la page fonctionne normalement — c'était le réglage
d'attente.

**Il faut donc republier aussi l'image du site** (`ccmlits/mypianotool:2.7.1`) :
l'adresse du service est compilée dans le bundle Angular.

## 5. Vérifier depuis l'extérieur

```bash
curl -s https://exemple.be/omr/api/sante | python3 -m json.tool
```

Puis, dans le site, la fenêtre « Scanner » doit proposer de choisir une photo au
lieu d'annoncer un service indisponible.

## Le relais de partage — ce qui change, et ce qui ne change pas

Rien à ajouter au déploiement. La route `/omr` déjà décrite ci-dessus porte
`/api/relais` comme elle porte `/api/scans`, et `client_max_body_size 30m`
couvre largement les 8 Mo du relais. Pas de volume, pas de port, pas de
sous-domaine.

Deux choses restent à faire, et elles sont faciles à oublier :

1. **Republier les deux images.** Le relais est dans l'image du backend, et
   `apiBackend` est compilé dans le bundle Angular du site.
2. **Rien d'autre.** En particulier, `mem_limit: 4g` n'a pas à bouger : le
   relais tient cent partitions en mémoire pour 64 Mo au plus, à comparer aux
   3 Go que la JVM d'Audiveris demande sur une page dense.

Une fois en place, la vérification tient en trois commandes depuis l'extérieur :

```bash
curl -s https://VOTRE-DOMAINE/omr/api/sante | python3 -m json.tool
# → "relais": {"actif": true, "ttlS": 180, …}

printf 'coucou' | curl -s -X POST https://VOTRE-DOMAINE/omr/api/relais \
  -H 'X-Code: 4271' --data-binary @-
# → {"code":"4271","jeton":"…","octets":6,"resteS":180}

curl -s https://VOTRE-DOMAINE/omr/api/relais/4271    # → coucou
curl -s -o /dev/null -w '%{http_code}\n' \
     https://VOTRE-DOMAINE/omr/api/relais/4271       # → 404, usage unique
```

Si le dépôt répond `404`, c'est la réécriture du préfixe `/omr` qui manque — le
même symptôme, et le même remède, que pour `/api/scans`.

## Ce qu'il faut surveiller

- **La mémoire.** Un scan ouvre une JVM à 3 Go. `mem_limit: 4g` empêche le conteneur
  d'emporter le serveur, mais si la machine n'a que 4 Go au total, baissez
  `MPT_AUDIVERIS_MEMOIRE` à `1500m` et attendez-vous à des échecs sur les pages
  denses — le service les signale proprement.
- **Le processeur.** Un cœur occupé pendant une à plusieurs minutes par page, et un
  seul scan à la fois (`MPT_TRAVAUX_SIMULTANES=1`). Sur un petit VPS, le site reste
  réactif parce que c'est un autre conteneur, mais tout ralentit.
- **Rien n'est persisté.** Pas de volume, pas de sauvegarde : les scans vivent une
  heure et disparaissent avec le conteneur. Une mise à jour de l'image ne perd rien.
- **Watchtower** est activé par le label, comme sur les autres services : l'image se
  mettra à jour toute seule si vous publiez un `:latest`. Avec un tag figé
  (`0.1.0`), il ne fera rien — c'est le même choix que pour le site.
