# SmartRail India — implementation plan

Branch **`v1`** is the working Telegram bot for the Mangalore–Kannur corridor. It used hardcoded `stations.json`, `trains.json`, and `gates.json`. That does not scale.

**`main`** is the product everyone uses: data is **fetched**, gates are **mapped from OSM + verified user pins**, trains are **discovered from the user’s location**.

Telegram stays the first client. Web and mobile call the same Python service.

---

## Goal

A person anywhere in India:

1. Shares GPS in Telegram.
2. Sees nearby level crossings (OSM, clustered).
3. Gets live “this crossing closes in N minutes” without typing a train number.
4. Can mark a missing gate while standing at it; fake pins are rejected.
5. Later opens a web map or app against the same API.

---

## What must never be hardcoded

| v1 | main |
|---|---|
| `stations.json` (~25 codes) | OSM `railway=station` + `ref` (IR code) around the user |
| `trains.json` (16 trains) | erail (or successor) trains **between the two nearest stations** |
| `gates.json` (146 crossings) | OSM `railway=level_crossing` around the user |
| Corridor km table | Live geometry + station ETAs |
| Easy-Rail wrapper | RailYatri / NTES live status (with **report age**) |

A city we have never visited must work if OSM has a crossing and a station `ref`.

---

## Architecture

```
Telegram / Web / Mobile
          │
          ▼
    FastAPI  (smartrail.api)
          │
          ▼
    SmartRail service
      ├─ OSM Overpass     crossings, stations, rail ways
      ├─ verify           pin vs track / duplicate / road
      ├─ erail            train numbers for this stretch
      ├─ RailYatri        live position + station ETAs
      ├─ predict          stale-aware ETA to a cluster
      └─ SQLite → later Postgres+PostGIS
```

One prediction engine. No copy-paste into Flutter.

### Query shape (not “poll India”)

```
user lat/lon
  → OSM crossings + stations + rails in ~2.5 km
  → two nearest stations with IR codes
  → trains between those codes (both directions)
  → live status only for that list (cap ~24)
  → ETA to the user’s crossing cluster
  → alert if ≤ 10 minutes
```

---

## User mapping (Telegram is enough)

Two different GPS pins:

| Command | Meaning |
|---|---|
| `/setlocation` + Share location | **I am here** (home / commute) |
| `/markgate` + Share location | **This coordinate is a barrier** |

Do not use one pin for both.

### Verify a user gate

On `/markgate`:

1. Overpass: rail ways, crossings, highways around the pin.
2. Distance to nearest rail **> 40 m** → reject (“stand at the gate”).
3. Existing OSM/user crossing **≤ 40 m** → subscribe to that one, do not clone.
4. On rail + road, no OSM node → save as `pending` candidate.
5. On rail, no road → weak candidate (footpath / unmanned).
6. Rate-limit: few pins per user per day.

`pending` is visible to that user. Promote to `public` when a second independent pin lands within 40 m, or after a simple review.

This is **crowdsourcing**, not ML training.

---

## Live data rules (learned on v1)

- Prefer **station ETA clocks** (`a_min` / `eta`) over “distance / 60 km/h”.
- Always keep **`status_as_of` age**. If NTES said Chandera 25 minutes ago with Trikarpur due 16:07, that train has **passed** — do not print “3 min”.
- Dead-reckon along the remaining route using age × speed as a fallback.
- Show `last seen X, as of N min ago` / `already passed your gates`.
- Cluster crossings within ~2 km so one train is one alert.

---

## Persistence

**Now (main):** SQLite (`data/smartrail.db`)

- `users` — location, subscription, crossing ids
- `crossings` — user candidates (`pending` / `public`)
- `alerts` — cooldown keys

**Next:** Postgres + PostGIS

- National OSM snapshot (monthly Overpass/HOTOSM import)
- `gist` index on gate points
- `station_code` on each gate (nearest IR station)
- `station_train` timetable index so we do not call erail on every predict

Redis: live train snapshots, 60–90 s TTL.

---

## Clients (order)

1. **Telegram (now)** — GPS, alerts, markgate. Prove two cities besides Thrikaripur.
2. **FastAPI** — already stubbed: `GET /gates`, `GET /predict`.
3. **Web map** — Leaflet, tap OSM gate, confirm. Linked from the bot.
4. **Mobile** — last. Push + background location. API client only.

Do not start Play Store while only one corridor works.

---

## Phased PRs on `main`

### PR1 — OSM nearby + markgate (this commit)

- Overpass around user
- `/setlocation` vs `/markgate`
- Pin verification
- SQLite
- Tests for parse + verify (no network)

### PR2 — Train discovery + predict

- Two nearest OSM station `ref`s
- erail trains between
- RailYatri live + stale ETAs
- Clustered `/predict` and `/subscribe`

### PR3 — Reliability

- Cache Overpass and live status
- Cap concurrency
- Fallback if Overpass is rate-limited
- Alert worker uses the same `predict_here`

### PR4 — National OSM import

- Batch load India `level_crossing` nodes
- Offline nearby for common areas
- Overpass only for misses / markgate

### PR5 — Station–train index

- Nightly timetable ingest
- Predict without erail scrape
- Swap live provider behind an interface if RailYatri breaks

### PR6 — Web map

- `/gates?lat=&lon=` on a map
- “This is my gate”
- Telegram login or deep link

### PR7 — Auth, rate limits, abuse

- Pin quotas
- Promote `pending` with second user
- Drop unused candidates

### PR8 — Mobile (only after PR2 works in a new state)

---

## Clean code rules

- No comments that restate names.
- No `enhanced_` / `ultimate_` module names.
- Providers (`osm`, `erail`, `live`) have no Telegram imports.
- Tests do not hit the network; parse fixtures.
- Secrets only in `.env`.
- New cities = data, not new Python files.

---

## Definition of done for “everyone can use it”

A user in a city **not** in v1’s JSON:

1. Shares GPS.
2. Sees OSM crossings (or is told to `/markgate`).
3. `/predict` lists real running trains or honestly says the live feed is empty/stale.
4. `/subscribe` later pings for ≤10 min, one message per cluster.

When that works in two states, add the web map. Not before.
