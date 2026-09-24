# metabase-datasource-sync

Un binaire Rust unique qui réconcilie les **sources de données Metabase** avec des identifiants de
base qui tournent, par l'API HTTP — la seule voie possible, `details` étant chiffré en base. Le
*quoi* et le *pourquoi* sont dans le [README](README.md) ; ce fichier porte l'état du dépôt, ses
décisions verrouillées et ses pièges.

## Comment lancer

Tout est dans le `Taskfile.yml` : `devbox run -- task` liste les cibles. Rien n'est recopié ici —
une commande qui vit à deux endroits diverge.

La porte de la CI est `task check`. Elle se lance **telle quelle**, sans `| head`, sans `| grep` :
un tuyau fermé tue le run en route et le code de retour lu ensuite n'est plus le sien.

## Conventions du dépôt

- **Français** : commentaires, messages de commit (conventional commits), noms d'étapes de CI,
  messages du programme. C'est la convention établie, elle prime.
- `--locked` sur chaque invocation de cargo. Le `Dockerfile` construit avec `--locked` : un
  `Cargo.lock` en retard sur la version de `Cargo.toml` casse le build de l'image, pas la CI.

## Décisions verrouillées

**Une seule version de Rust pour trois usages.** `rust-toolchain.toml` la nomme ; `Cargo.toml`
(`rust-version`) déclare le même plancher ; le `Dockerfile` construit depuis la même image. Ces
trois-là doivent s'accorder, et `task msrv` refuse le contraire.

Ce qui l'a décidé : le plancher réel de l'arbre verrouillé est **1.88** (les crates `icu_*` 2.3 et
`time` 0.3 le déclarent), la CI installait « ce que rustup donnait », donc plus récent, et
`Cargo.toml` annonçait **1.75**. Trois nombres libres, un contrat faux, et tout au vert.

Conséquence assumée : le dépôt compile avec son plancher, pas avec le stable du jour — il ne voit
donc pas les diagnostics d'un compilateur plus récent, et chaque montée de plancher amènera sa
petite vague de clippy. En échange, aucun outil ne change sous les pieds : une montée de
compilateur est une PR, comme une montée de dépendance.

**Ce que l'épinglage ne fait pas, et l'expérience qui l'a montré.** Un canal 1.85 compile sans
broncher un arbre où `icu_*` 2.3 et `time` 0.3 déclarent `rust-version = 1.88` : avec le resolver
v2 (édition 2021), cargo ne fait respecter que le `rust-version` de NOTRE paquet ; celui d'une
dépendance est indicatif. L'épinglage seul laisserait donc une montée de plancher chez une
dépendance passer inaperçue jusqu'à ce qu'elle utilise vraiment une nouveauté du langage.

C'est pourquoi `task msrv` a **deux moitiés** : les trois déclarations s'accordent, *et* le
plancher déclaré couvre l'arbre verrouillé (`cargo metadata`). Les cinq mutants correspondants ont
été vus rouges — un par déclaration, le tag flottant `rust:1`, et les trois abaissées ensemble
(seule la seconde moitié peut mordre sur celui-là).

**Renovate fait bouger les trois d'un seul tenant, sur UNE datasource.** Grouper ne suffit pas, et
c'est un dry run qui l'a montré : le manager natif `rust-toolchain` lit le canal par la datasource
`rust-version`, qui **retarde** — le 23/09/2026 elle proposait 1.97.1 quand Rust 1.98.1 était
publié et que `docker` proposait 1.98. Groupés mais sur deux datasources, les trois tombaient sur
une seule branche avec deux versions, et `task msrv` rougissait par construction. Le canal et
`rust-version` passent donc par le manager maison de `.github/renovate.json5`, sur `docker` comme
le `FROM` ; le manager natif est éteint. Ne pas le rallumer « pour simplifier ».

**Un tag ne publie que la version qu'il annonce.** `docker-release.yaml` refuse de publier quand
`vX.Y.Z` ne correspond pas au `version` de `Cargo.toml` — un `--version` qui ment ne se découvre
qu'en diagnostic, au pire moment.

**La suite d'acceptation est un contrat de comportement, pas un test d'implémentation.** Elle
pilote le binaire par son environnement et ses fichiers, et `SYNC_CMD` lui fait viser n'importe
quelle implémentation. Elle doit rester verte : c'est elle qui a prouvé la parité avec
l'implémentation Python d'origine (41 assertions, même journal).

**Ce que le programme refuse de faire** — bases réservées de Metabase, arbitrage entre deux
candidates, renommage d'une source existante, écrasement de réglages qu'il ne possède pas. Ces
refus sont la fonctionnalité ; ne pas les « assouplir » pour faire passer un cas.

## Trous connus

Le dépôt entre dans la boucle d'auto-maintenance (épique iterion **#1585**, ticket **#1597**).
Dans l'ordre, et l'ordre est contraint :

1. **Renovate est configuré, mais ne peut pas encore tourner.** `.github/renovate.json5` et le
   workflow sont en place ; il manque, hors du dépôt : l'**ajout** du dépôt à l'installation de
   l'App `socialgouv-renovate` (`repository_selection: selected`) et ses **deux secrets**
   (`RENOVATE_APP_ID`, `RENOVATE_APP_PRIVATE_KEY`). D'ici là le run hebdomadaire échoue à l'étape
   du jeton — bruyamment, c'est voulu. Les images de `services:` (Metabase, PostgreSQL) sont lues
   par le manager `github-actions` natif : aucun manager maison n'est nécessaire pour elles.
2. **Les actions ne sont pas épinglées par SHA.** `helpers:pinGitHubActionDigests` est absent
   exprès : il réécrit `.github/workflows/**`, que l'App qui livre l'alignement ne peut pas encore
   toucher (iterion **#1595**, décision en attente). L'activer avant ferait naître des PR que
   personne ne peut aligner.
3. **Pas d'intégration iterion.** Dans cet ordre : un premier verdict vert, **puis**
   `revi/review` requis, **puis** l'observer bloquer une révision neuve, **puis** armer
   l'automerge — jamais l'inverse. Un gate mal aligné avec un automerge armé est un trou, pas une
   demi-mesure.

## `main` est protégé

Ruleset `23870489`, actif : PR obligatoire (0 approbation requise — c'est la CI qui juge),
`test` + `acceptance-reelle` + `image` requis, **`strict`** (la branche doit être à jour avant
merge), suppression et *non-fast-forward* interdits. Seul bypass : le rôle **admin**. Le dépôt a
`allow_update_branch: true`, sans quoi `strict` coûterait un rebase manuel à chaque PR.

`strict` plutôt qu'une merge queue : le dépôt n'avait aucune PR avant celle-ci, donc deux PR
simultanées — le cas que la queue protège — n'arrivent pas. Coût assumé : chaque rebase produit un
nouveau head, donc une nouvelle exécution de la CI.

`revi/review` n'y est **pas** encore, et c'est l'ordre du ticket #1597 : un premier verdict vert
d'abord, le rendre requis ensuite, l'observer bloquer une révision neuve, et seulement après armer
l'automerge.

## Le chemin de release est fermé

Il l'était : `ci.yml` ne tourne pas sur un tag et une protection de `main` ne protège pas les
tags, donc un `git tag` sur n'importe quel commit publiait une image que rien n'avait éprouvée.
Le job `tag-eprouve` refuse désormais un tag dont le commit n'est pas sur `main`, ou dont les
checks requis ne sont pas verts **sur ce commit précis** — un bypass d'administrateur pouvant
poser sur `main` un commit qui ne les a pas payés.

Le garde ne concerne **que les tags** : une poussée sur `main` produit un commit neuf (squash)
dont la CI démarre au même instant, et exiger ses checks bloquerait chaque merge.

Quand plusieurs exécutions portent le même nom (re-run), c'est la plus **récente** qui décide,
triée sur `started_at` : l'ordre de la réponse de l'API n'est pas un fait, la date en est un.

La procédure, avec le `task release:check` qui rejoue le contrôle en local, est dans le
[README](README.md#publier-une-version).
