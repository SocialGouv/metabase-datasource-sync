#!/usr/bin/env python3
"""Le plancher de Rust déclaré est vrai, et il est déclaré partout pareil.

Deux moitiés, parce qu'aucune ne suffit seule.

1. **Les trois déclarations s'accordent** — `Cargo.toml` (`rust-version`),
   `rust-toolchain.toml` (`channel`) et le `Dockerfile` (image de build). Laissées libres, elles
   dérivent en silence : le contrat déclaré cesse d'être celui qui compile, et le binaire publié
   cesse d'être celui que la CI a éprouvé.

2. **Le plancher déclaré couvre l'arbre verrouillé** — aucune dépendance ne réclame plus récent.
   Cette moitié n'est PAS redondante avec l'épinglage du canal : mesuré sur ce dépôt, un canal
   1.85 compile sans broncher un arbre où `icu_*` et `time` déclarent 1.88. Avec le resolver v2
   (édition 2021), le `rust-version` d'une dépendance est indicatif — cargo ne le fait respecter
   que pour NOTRE paquet. Sans ce contrôle, une montée de plancher chez une dépendance passe donc
   inaperçue jusqu'au jour où elle utilise vraiment une nouveauté du langage.
"""

import json
import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
SELF = "metabase-datasource-sync"


def version_tuple(v):
    # Complété à trois composantes : sans ça `1.88.0` serait « plus récent » que `1.88`, une
    # comparaison de tuples de longueurs différentes départageant sur la longueur.
    parts = [int(x) for x in re.findall(r"\d+", v)[:3]]
    return tuple(parts + [0] * (3 - len(parts)))


def minor(v):
    return ".".join(v.split(".")[:2])


def declared(path, pattern, what):
    text = (ROOT / path).read_text(encoding="utf-8")
    m = re.search(pattern, text, re.M)
    if not m:
        sys.exit(f"check-msrv: {path} ne déclare pas {what}")
    return m.group(1)


def check_declarations():
    cargo = declared("Cargo.toml", r'^rust-version = "([^"]+)"', "rust-version")
    toolchain = declared("rust-toolchain.toml", r'^channel = "([^"]+)"', "channel")
    docker = declared("Dockerfile", r"^FROM rust:([0-9][0-9.]*)-alpine", "FROM rust:<X.Y>-alpine")

    if len({minor(cargo), minor(toolchain), minor(docker)}) != 1:
        print("check-msrv: les versions de Rust divergent.", file=sys.stderr)
        print(f"  Cargo.toml           rust-version = {cargo}", file=sys.stderr)
        print(f"  rust-toolchain.toml  channel      = {toolchain}", file=sys.stderr)
        print(f"  Dockerfile           FROM rust:{docker}-alpine", file=sys.stderr)
        print(
            "Monter le plancher se fait aux TROIS endroits : sinon le contrat déclaré n'est plus\n"
            "celui qui compile, et le binaire publié n'est plus celui que la CI a éprouvé.",
            file=sys.stderr,
        )
        sys.exit(1)

    return cargo


def check_dependencies(cargo):
    run = subprocess.run(
        ["cargo", "metadata", "--format-version", "1", "--locked"],
        capture_output=True,
        text=True,
        cwd=ROOT,
    )
    if run.returncode != 0:
        print("check-msrv: `cargo metadata` a échoué :", file=sys.stderr)
        print(run.stderr.strip(), file=sys.stderr)
        sys.exit(1)

    floors = [
        (p["name"], p["version"], p["rust_version"])
        for p in json.loads(run.stdout)["packages"]
        if p.get("rust_version") and p["name"] != SELF
    ]
    if not floors:
        print("check-msrv: aucune dépendance ne déclare de rust-version — contrôle sans objet.")
        return

    floors.sort(key=lambda f: version_tuple(f[2]), reverse=True)
    top_name, top_version, top_floor = floors[0]

    if version_tuple(top_floor) > version_tuple(cargo):
        blocking = [f for f in floors if version_tuple(f[2]) > version_tuple(cargo)]
        print(
            f"check-msrv: {len(blocking)} dépendance(s) réclament plus récent que "
            f"rust-version = {cargo}.",
            file=sys.stderr,
        )
        for name, version, floor in blocking[:10]:
            print(f"  {floor:9} {name} {version}", file=sys.stderr)
        print(
            f"Monter rust-version à {minor(top_floor)} aux trois endroits, ou verrouiller ces\n"
            "dépendances à une version plus ancienne. Le canal épinglé ne le dira pas tout seul :\n"
            "avec le resolver v2, le rust-version d'une dépendance est indicatif.",
            file=sys.stderr,
        )
        sys.exit(1)

    print(
        f"check-msrv: Rust {minor(cargo)} — les trois déclarations s'accordent, et couvrent "
        f"l'arbre verrouillé (le plus exigeant : {top_name} {top_version}, {top_floor})."
    )


def main():
    check_dependencies(check_declarations())


if __name__ == "__main__":
    main()
