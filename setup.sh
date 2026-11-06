#!/usr/bin/env bash
# Create the venv and install deps. Never touches global python packages.
set -euo pipefail
cd "$(dirname "$0")"
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
mkdir -p cookies tmp logs
[ -f .env ] || { cp .env.example .env; echo "created .env, edit it (BOT_TOKEN, ALLOWED_USER_IDS)"; }
echo "done. next: put cookies in cookies/{twitter,instagram}.txt, then: .venv/bin/python bot.py"
