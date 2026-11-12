# GateAhead API

India-wide railway level-crossing alerts. This repository is the Python service: FastAPI and the Telegram bot. The website and the phone app are separate repositories.

Crossings and trains are fetched from the user's location. There is no hardcoded station, train, or gate catalog.

## What users do

1. `/setlocation` — Telegram GPS (where they are).
2. `/nearby` — OpenStreetMap crossings around them.
3. `/predict` — trains on that stretch (erail train list + RailYatri live status).
4. `/subscribe` — ping when a nearby cluster is about to close.
5. `/markgate` — GPS at a missing barrier. Rejected if not on a railway track.

## Run

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env   # set TELEGRAM_BOT_TOKEN
pytest
python -m gateahead.bot
```

HTTP API (same engine):

```bash
uvicorn gateahead.api.app:app --host 0.0.0.0 --port 8000
```

The website calls this API under `/api`.

## Layout

```
src/gateahead/     domain + Telegram + FastAPI
  osm.py           Overpass (crossings, stations, rails)
  verify.py        user pin checks
  erail.py         trains between two station codes
  live.py          RailYatri live position
  store.py         SQLite users / suggested gates
docs/IMPLEMENTATION_PLAN.md
```
