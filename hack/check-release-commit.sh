#!/bin/sh
# Un tag ne publie que ce que la CI a eprouve.
#
# `ci.yml` ne tourne pas sur un tag, et un ruleset sur `main` ne protege pas les tags : sans ce
# controle, `git tag v9.9.9 <n-importe-quel-commit> && git push --tags` publie une image que rien
# n-a verifiee. L-accord tag <-> version de Cargo.toml, lui, ne dit rien de la CI : il verifie que
# le binaire annonce la bonne version, pas qu-il compile.
#
# Deux conditions, et les deux sont necessaires :
#   1. le commit du tag est sur `main` -- il est donc passe par le ruleset (PR + checks requis) ;
#   2. les checks requis sont VERTS sur CE commit precis -- un bypass d-administrateur peut poser
#      un commit sur main sans les avoir payes.
#
# Quand plusieurs executions portent le meme nom (re-run), c-est la plus RECENTE qui decide, triee
# sur `started_at` : l-ordre de la reponse de l-API n-est pas un fait, la date en est un.
set -eu

sha="${1:?usage: check-release-commit.sh <sha>}"
repo="${GITHUB_REPOSITORY:-SocialGouv/metabase-datasource-sync}"
required="${RELEASE_REQUIRED_CHECKS:-test acceptance-reelle image}"

runs="$(gh api "repos/$repo/commits/$sha/check-runs?per_page=100" \
  --jq '.check_runs[] | [.started_at, .name, .status, .conclusion] | @tsv')"

if [ -z "$runs" ]; then
  echo "check-release-commit: aucune execution de CI sur $sha." >&2
  echo "Un tag se pose sur un commit deja eprouve : attendre que la CI de main finisse." >&2
  exit 1
fi

failed=0
for name in $required; do
  line="$(printf '%s\n' "$runs" | awk -F'\t' -v n="$name" '$2 == n' | sort | tail -1)"
  if [ -z "$line" ]; then
    echo "check-release-commit: le check requis '$name' n-a jamais tourne sur $sha." >&2
    failed=1
    continue
  fi
  status="$(printf '%s\n' "$line" | cut -f3)"
  conclusion="$(printf '%s\n' "$line" | cut -f4)"
  if [ "$status" != "completed" ]; then
    echo "check-release-commit: '$name' est encore en cours sur $sha (status $status)." >&2
    failed=1
  elif [ "$conclusion" != "success" ]; then
    echo "check-release-commit: '$name' a conclu '$conclusion' sur $sha." >&2
    failed=1
  fi
done

if [ "$failed" -ne 0 ]; then
  echo "Refus de publier : ce commit n-a pas ete eprouve par la CI." >&2
  exit 1
fi

echo "check-release-commit: $sha -- $required au vert."
