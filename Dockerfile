# syntax=docker/dockerfile:1
#
# Image finale : `scratch` + un binaire statique, et rien d'autre. Aucun paquet système à patcher,
# aucun interpréteur — c'est l'intérêt principal face à une image `python:*-alpine` (53 Mo,
# 38 paquets OS) déployée dans le namespace de chaque produit consommateur.
#
# `rustls` + `webpki-roots` compilent les certificats racines DANS le binaire : pas besoin d'y
# copier un bundle CA, contrairement à ce qu'imposerait la bibliothèque TLS du système.

# Version épinglée, pas `rust:1-alpine` : un tag flottant fait compiler l'image publiée par un
# compilateur que la CI n'a jamais éprouvé. Elle suit `rust-toolchain.toml` — `task msrv` le vérifie.
FROM rust:1.88-alpine AS builder
# musl-dev fournit le linker ; la cible par défaut de cette image est déjà x86_64-unknown-linux-musl,
# donc le binaire produit est statique.
RUN apk add --no-cache musl-dev
WORKDIR /src
# Les manifestes d'abord : une modification du seul code source réutilise le cache des dépendances.
COPY Cargo.toml Cargo.lock ./
RUN mkdir src && echo 'fn main() {}' > src/main.rs && cargo build --release --locked && rm -rf src
COPY src ./src
# `touch` : sans ça, cargo peut considérer le binaire factice comme à jour.
RUN touch src/main.rs && cargo build --release --locked

FROM scratch
COPY --from=builder /src/target/release/metabase-datasource-sync /metabase-datasource-sync
# `nobody`. Le chart repose son propre `runAsUser`, mais l'image ne doit pas inviter à tourner root.
USER 65534:65534
ENTRYPOINT ["/metabase-datasource-sync"]
