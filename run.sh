#!/bin/sh
set -eu

cd "$(dirname "$0")"

if [ ! -f .env ]; then
  echo "Copy .env.example to .env and enter your Kayo/Binge credentials first." >&2
  exit 1
fi

if [ ! -x .venv/bin/python ]; then
  python3 -m venv .venv
  .venv/bin/pip install -r requirements.txt
fi

exec .venv/bin/python gateway.py
