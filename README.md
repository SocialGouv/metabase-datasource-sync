# metabase-datasource-sync

Garde les **sources de données d'une instance Metabase** alignées sur des identifiants de base qui
**tournent** — credentials dynamiques Vault, IAM temporaire, rotation planifiée.

```
image : 1,8 Mo, FROM scratch, 0 paquet système
```

## Le problème

Metabase ne lit pas la connexion d'une source de données depuis son environnement : il la garde
**dans sa propre base applicative**, table `metabase_database`, champ `details` — **chiffré** dès
que `MB_ENCRYPTION_SECRET_KEY` est posée.

Quand les identifiants de la base interrogée tournent, cette connexion se périme, et aucun
mécanisme d'exploitation habituel ne la rattrape :

| Piste | Pourquoi elle échoue |
|---|---|
| Variable d'environnement | Metabase ne lit pas ses sources de données depuis l'environnement |
| Redémarrage du pod (Reloader, `kubectl rollout restart`) | même raison : l'information à corriger est en base |
| `UPDATE` SQL sur `details` | il faudrait reproduire le format chiffré de Metabase (AES-CBC-HMAC-SHA512, clé dérivée PBKDF2) — format interne non contractuel, dont la rupture **ne lève aucune erreur** : la source devient simplement illisible |
| Fichier de configuration (`MB_CONFIG_FILE_PATH`) | réservé aux offres **Pro et Enterprise** |

Reste l'API HTTP, seule voie où Metabase reste maître de son propre schéma. C'est ce que fait ce
programme.

## Ce qu'il fait

Un processus unique, qui :

1. lit les identifiants dans un ou plusieurs **Secrets Kubernetes montés en volume** — la seule
   forme qui voie la rotation, une variable d'environnement issue d'un Secret étant **figée au
   démarrage du pod** ;
2. s'authentifie sur Metabase avec un compte **administrateur** (mot de passe **relu dans un
   fichier** à chaque connexion, donc sa propre rotation ne demande pas de redémarrage) ;
3. **crée** la source si elle manque, la **réécrit** si son adressage ou son utilisateur a changé,
   **ne fait rien** sinon.

La boucle est *level-triggered* : elle repasse par l'API toutes les `RECONCILE_SECONDS` même sans
changement local, plutôt que de dépendre d'un front qui, manqué, laisserait une dérive
silencieuse. Un échec transitoire est journalisé et retenté ; un échec **durable** cesse de
rafraîchir un fichier de heartbeat, ce qu'une sonde de liveness transforme en redémarrage visible.

Aucun mot de passe n'est journalisé, y compris dans les corps d'erreur renvoyés par Metabase.

## Configuration

Tout passe par l'environnement.

| Variable | Défaut | Rôle |
|---|---|---|
| `MB_URL` | — (obligatoire) | URL de l'instance, joignable depuis ce processus |
| `MB_ADMIN_EMAIL` | — (obligatoire) | compte **administrateur** Metabase |
| `MB_ADMIN_PASSWORD_FILE` | — (obligatoire) | **chemin** du fichier portant son mot de passe |
| `POLL_SECONDS` | `30` | période de relecture des Secrets montés |
| `RECONCILE_SECONDS` | `600` | période de vérification complète contre l'API, même sans changement |
| `HTTP_TIMEOUT` | `30` | délai maximum d'un appel |
| `HEARTBEAT_FILE` | `/tmp/alive` | fichier touché à chaque cycle réussi |
| `CREATE_IF_MISSING` | `true` | créer la source si elle n'existe pas (voir l'avertissement) |
| `DS_COUNT` | `0` | nombre de sources |
| `DS<i>_NAME` | — | nom affiché dans Metabase ; sert de clé d'idempotence |
| `DS<i>_ENGINE` | `postgres` | moteur Metabase |
| `DS<i>_DIR` | — | répertoire où le Secret est monté |
| `DS<i>_KEYS` | `{}` | JSON : champ de `details` → nom du fichier dans `DS<i>_DIR` |
| `DS<i>_EXTRA` | `{}` | JSON : champs de `details` posés tels quels |

Exemple pour une base PostgreSQL dont le Secret porte les clés `PG*` :

```sh
DS_COUNT=1
DS0_NAME='MonProduit'
DS0_DIR=/var/run/secrets/datasources/0
DS0_KEYS='{"host":"PGHOST","port":"PGPORT","dbname":"PGDATABASE","user":"PGUSER","password":"PGPASSWORD","ssl-mode":"PGSSLMODE"}'
DS0_EXTRA='{"ssl":true,"tunnel-enabled":false}'
```

Sur Kubernetes, le déploiement est fourni par un chart Helm `metabase-datasource-sync` qui pose
ces variables, monte les Secrets en volume et câble la sonde de liveness sur le heartbeat.
*(Pour la DNUM : il vit dans le dépôt de charts transverses, dossier
`dnum/metabase-datasource-sync`.)*

> ⚠️ **Les droits d'une source NEUVE sont ceux de Metabase, pas les vôtres.** Créer une source
> donne aux groupes Metabase existants les permissions que Metabase applique par défaut — elles
> peuvent autoriser les requêtes natives et les téléchargements. Chiffrer les identifiants ne
> cloisonne pas les données. Régler les permissions côté Metabase, et vérifier avec un compte
> non-admin. Pour garder ce provisioning manuel : `CREATE_IF_MISSING=false`.

## Ce qu'il refuse de faire

- **Écrire dans une base réservée** de Metabase (`is_sample`, `is_audit`, `is_attached_dwh`).
- **Choisir entre deux candidates.** Une source est retrouvée par son nom, à défaut par
  `(moteur, host, port, dbname)`. Si plusieurs correspondent, il refuse bruyamment plutôt que de
  réécrire la connexion d'une source qui ne lui appartient pas.
- **Renommer une source existante.** Le nom affiché est celui de l'utilisateur ; les questions
  référencent l'identifiant, pas le nom.
- **Écraser des réglages qu'il ne possède pas.** `is_on_demand`, `cache_ttl`, `schedules` et
  consorts sont relus de l'existant et renvoyés tels quels — un `PUT` partiel les remettrait à
  leur défaut.

## Développement

```sh
cargo test                     # tests unitaires
cargo build                    # binaire de développement
python3 tests/acceptance.py    # acceptation contre un faux Metabase (rapide)
python3 tests/real_metabase.py # acceptation contre un VRAI Metabase (cf. ci-dessous)
docker build -t metabase-datasource-sync .
```

`real_metabase.py` attend une instance Metabase et un PostgreSQL joignables ; la CI les fournit en
services. En local :

```sh
docker network create mds-test
docker run -d --name mds-pg --network mds-test -e POSTGRES_PASSWORD=postgres \
  -e POSTGRES_DB=app -p 55432:5432 postgres:16-alpine
docker run -d --name mds-metabase --network mds-test -p 3000:3000 \
  -e MB_ENCRYPTION_SECRET_KEY=une-cle-de-test-suffisamment-longue metabase/metabase:v0.63.15
PG_HOST_FOR_METABASE=mds-pg PG_LOCAL_PORT=55432 python3 tests/real_metabase.py
```

La **suite d'acceptation** décrit un comportement, pas une implémentation : elle pilote le binaire
par son environnement et ses fichiers, et vérifie l'état d'un faux Metabase qui reproduit les
comportements du vrai susceptibles de piéger un client naïf (fusion de `details`, remise à zéro
des champs absents d'un `PUT`, throttling des connexions). Elle peut viser n'importe quelle
implémentation :

```sh
SYNC_CMD=/chemin/vers/une/autre/implementation python3 tests/acceptance.py
```

C'est ainsi qu'a été prouvée la parité de ce binaire avec l'implémentation Python d'origine
(0.1.0) : **les 41 mêmes assertions, le même journal**.
