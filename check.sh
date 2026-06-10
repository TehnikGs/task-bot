#!/usr/bin/env bash
# Проверка, достаёт ли сервер нужные сервисы. Запуск: bash check.sh
# Код 200/3xx/401/404 = доступно (сервер получил ответ).
# 000 или TIMEOUT = заблокировано/недоступно -> нужен прокси.
echo "Проверяю доступ с этого сервера (10 сек на каждый)..."
echo ""
for url in https://api.telegram.org https://api.groq.com https://github.com; do
  code=$(curl -s -m 10 -o /dev/null -w "%{http_code}" "$url")
  rc=$?
  if [ "$rc" != "0" ] || [ "$code" = "000" ]; then
    echo "  $url  ->  НЕДОСТУПНО (нужен прокси)"
  else
    echo "  $url  ->  OK (код $code)"
  fi
done
echo ""
echo "Если Telegram и Groq показали OK — прокси НЕ нужен."
