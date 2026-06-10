#!/usr/bin/env bash
# Установка/обновление бота на сервере. Запуск: bash setup.sh
# Секретов тут НЕТ — токен и ключ берутся из .env (создаётся отдельно).
set -e
cd "$(dirname "$0")"

echo ">> Пересоздаю виртуальное окружение..."
rm -rf .venv
python3 -m venv .venv
.venv/bin/pip install --upgrade pip -q
echo ">> Ставлю зависимости..."
.venv/bin/pip install -q -r requirements.txt

echo ">> Устанавливаю службу systemd..."
cp deploy/task-bot.service /etc/systemd/system/task-bot.service
systemctl daemon-reload
systemctl enable task-bot
systemctl restart task-bot

sleep 2
echo ">> Статус:"
systemctl --no-pager --lines=10 status task-bot || true
echo ""
echo ">> Готово. Логи в реальном времени: journalctl -u task-bot -f"
