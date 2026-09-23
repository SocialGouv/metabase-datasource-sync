#!/bin/sh
# L'image `scratch` démarre et refuse une configuration incomplète. Une image qui ne peut pas se
# lancer échouerait sinon au premier déploiement, pas ici.
#
# La sortie du conteneur est capturée avant d'être examinée : un `docker run | grep` ferme le tuyau
# et le code de retour lu ensuite est celui de `grep`, pas celui qu'on croit lire.
set -eu

img="${1:?usage: check-image.sh <image>}"

out="$(docker run --rm "$img" 2>&1 || true)"
printf '%s\n' "$out"

if ! printf '%s\n' "$out" | grep -q "CONFIGURATION INVALIDE"; then
  echo "check-image: l'image n'a pas refusé une configuration vide — attendu « CONFIGURATION INVALIDE »" >&2
  exit 1
fi

echo "check-image: $img démarre et refuse une configuration incomplète."
