#!/usr/bin/env bash
# Записать/обновить одну переменную в .env (без слешей в команде — удобно для веб-консоли).
# Использование: bash set-env.sh KEY VALUE
# Пример:        bash set-env.sh API_KEY abcdef123
cd "$(dirname "$0")"
key="$1"; val="$2"
if [ -z "$key" ]; then
  echo "Использование: bash set-env.sh KEY VALUE"
  exit 1
fi
touch .env
grep -v "^${key}=" .env > .env.tmp || true
mv .env.tmp .env
echo "${key}=${val}" >> .env
echo "Записано: ${key}=${val}"
