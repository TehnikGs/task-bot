#!/usr/bin/env bash
# Записать прокси для Telegram в .env (без слешей в самой команде — удобно для веб-консоли).
# Использование: bash set-proxy.sh HOST PORT [USER PASS]
# Пример:        bash set-proxy.sh 1.2.3.4 1080 myuser mypass
cd "$(dirname "$0")"
host="$1"; port="$2"; user="$3"; pass="$4"
if [ -z "$host" ] || [ -z "$port" ]; then
  echo "Использование: bash set-proxy.sh HOST PORT [USER PASS]"
  exit 1
fi
touch .env
# убрать прежнюю строку прокси, если была, сохранив остальное
grep -v '^TELEGRAM_PROXY=' .env > .env.tmp || true
mv .env.tmp .env
if [ -n "$user" ]; then
  echo "TELEGRAM_PROXY=socks5://${user}:${pass}@${host}:${port}" >> .env
else
  echo "TELEGRAM_PROXY=socks5://${host}:${port}" >> .env
fi
echo "Записано в .env:"
grep '^TELEGRAM_PROXY=' .env
