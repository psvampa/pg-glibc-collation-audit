#!/usr/bin/env bash
# Uso: ./audit-locale-diff.sh <tag_viejo> <tag_nuevo>
# Ejemplo: ./audit-locale-diff.sh glibc-2.28 glibc-2.34
#
# Lista, para cualquier par de tags de glibc, qué locales de localedata/locales/
# tuvieron cambios de contenido, y si la tabla maestra de collation
# (iso14651_t1_common, de la que heredan casi todos los locales) cambió.
set -euo pipefail
OLD=$1
NEW=$2

if [ ! -d glibc ]; then
  git clone --filter=blob:none --no-checkout https://github.com/bminor/glibc.git
fi
cd glibc

git diff --name-only "$OLD..$NEW" -- localedata/locales/ | sort > /tmp/changed_locales.txt

echo "Total de locales con cambios de contenido: $(wc -l < /tmp/changed_locales.txt)"
echo "---"
echo "¿Cambió la tabla maestra de collation (afecta a casi todos por herencia)?"
git diff --stat "$OLD..$NEW" -- localedata/locales/iso14651_t1_common
echo "---"
echo "Lista completa en /tmp/changed_locales.txt"
