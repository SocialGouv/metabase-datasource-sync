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

1. **Les tags échappent à la CI.** `ci.yml` ne tourne pas sur un tag, et un ruleset sur `main` ne
   protège pas les tags : un `vX.Y.Z` posé sur n'importe quel commit publie une image que rien n'a
   éprouvée. L'accord tag ↔ `Cargo.toml` ne dit rien de la CI. À régler : éprouver les checks sur
   les tags, ou vérifier l'ascendance du tag sur un commit mergé et vert — plus une procédure de
   release écrite, la version ayant toujours été bumpée par poussée directe.
2. **Pas de ruleset sur `main`.** À poser : PR obligatoire, `test` + `acceptance-reelle` + `image`
   requis, fraîcheur de la base (`strict`) — le dépôt a zéro PR à ce jour, donc aucune raison de
   commencer plus laxiste.
3. **Pas de Renovate.** Le dépôt doit être **ajouté** à l'installation de l'App
   `socialgouv-renovate` (`repository_selection: selected`) et ses deux secrets créés. La conf
   voudra le manager `cargo` natif plus un manager maison pour l'image Metabase épinglée dans
   `ci.yml` et pour l'image de build du `Dockerfile`.
4. **Les actions ne sont pas épinglées par SHA.** À faire avec Renovate, qui sait les maintenir —
   les épingler à la main sans lui donnerait des versions figées pour toujours.
5. **Pas d'intégration iterion.** Dans cet ordre : un premier verdict vert, **puis**
   `revi/review` requis, **puis** l'observer bloquer une révision neuve, **puis** armer
   l'automerge — jamais l'inverse. Un gate mal aligné avec un automerge armé est un trou, pas une
   demi-mesure.
