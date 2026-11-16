# GateAhead simulator

A development-only test bench for the GateAhead backend. Its source is versioned here for reproducible testing; it is excluded from the installed Python application, website, and phone app.

It runs the backend's real prediction code (`/predict` and `/scene`) on a made-up 130 km railway line where the true position of every train is known each minute. Then it checks what the app would tell a person at a level crossing against what is really happening there.

## Run

The simulator is a development-only test bench stored with the backend source. Set up the backend environment first, then run these commands from its `simulator/` directory. It is excluded from the Python application package.

```bash
cd simulator
../.venv/bin/python -m gatesim --summary          # one PASS/FAIL line per scenario
../.venv/bin/python -m gatesim --summary --cache-live # real SQLite snapshot cache and simulated clock
../.venv/bin/python -m gatesim rider_on_express   # minute-by-minute timeline
../.venv/bin/python -m pytest -q                  # every scenario as a test
```

## What is simulated

- **The line** (`gatesim/world.py`): 15 stations, 12 level crossings, rail drawn as OpenStreetMap ways.
- **Fake services** that copy the real formats: Overpass (map), erail (trains between stations), and RailYatri live status and timetable. Live reports lag behind the train, refresh every few minutes, can freeze, and give positions in whole kilometres, like the real feed.
- **Trains** (`gatesim/scenarios.py`): an express, a later express, an all-stations MEMU, a night mail, a short passenger, and a train running 3 hours late.

## Scenarios

| Name | Situation |
|---|---|
| `rider_on_express` | You are on the train, checking the gate as it runs up to it and over it (the original field report) |
| `waiting_at_gate` | You stand at the gate while the train comes |
| `rider_pin_stays` | On the train, but your place was set once and never refreshed |
| `stale_feed` | Live status freezes while the train stands at a station |
| `feed_stalls_moving` | Live status freezes while the train is between stations |
| `late_express` | 35 minutes late, then an unplanned 7-minute wait just before the gate |
| `northbound_memu` | Train from the other direction |
| `gate_by_platform` | Gate 300 m past a platform where the train halts |
| `two_trains_cross` | Two trains from opposite sides a few minutes apart |
| `crossed_non_stop` | Train runs through a halt without stopping |
| `waiting_at_origin` | Train standing at its first station before it leaves |
| `terminates_short` | Train ends before reaching the gate |
| `three_hours_late` | Timetable says it passed hours ago, it is still coming |
| `after_midnight` | Train passes the gate around midnight |
| `stopped_feed_recovers` | A frozen stopped report becomes unresolved, then a new position resolves it |
| `express_skips_local_stops` | An express skips every local station and stops only at regional junctions |

Pytest runs every scenario both without cached live reports and with the real 90-second SQLite snapshot cache. A separate matrix checks all 12 crossings in both travel directions before, during, and after passage. HTTP workflow tests exercise the real API, report isolation, and prediction/scene agreement without opening sockets or contacting providers.

## What counts as wrong

The barrier is taken to come down 5 minutes before the train (`GATE_LEAD_MIN`) and to go up a minute after it.

Every train at the gate is judged each minute, not just the watched one.

| Flag | Meaning |
|---|---|
| `MISSED` | Barrier really down or coming down, the train list is silent |
| `VIEW_MISSED` | Same, in the tapped-gate view |
| `SAYS_OPEN` | Barrier really down or coming down, the app says approaching/later and is too late by more than the scenario allows |
| `GHOST` | App says closed/closing long after the train passed, or long before it is near |
| `OFF` | Train arrival time wrong by more than the scenario allows |
| `NO_PASSED` | Train went over 2.5 to 4.5 minutes ago and the app shows nothing for it |
| `EARLY_PASSED` | App says "passed" before the train has reached the gate |
| `STALE_COUNTDOWN` / `STALE_VIEW` | A stopped report at least eight minutes old loses its unresolved row, gets a countdown, or claims passage |
| `UNEXPECTED_UNRESOLVED` | A fresh moving report is incorrectly marked unresolved |
| `DISAGREEMENT` | A crossing shown in the list disagrees with its tapped scene |
| `INVALID_ESTIMATE` | A displayed estimate has no finite ETA or an invalid status |

The judge derives stopped-report state and effective age from the simulated provider payload and fetch clock. A fresh stopped estimate may differ from truth by the report age plus the scenario tolerance because departure is unconfirmed. At eight minutes, it must remain visible as unresolved without an ETA or passage note. A newly fetched position can resolve it. ETA comparisons allow 0.05 minutes for the API's one-decimal rounding. Mutation tests ensure that silently dropping a stopped train, inventing a countdown, or marking every train unresolved fails the judge.

## Adding a scenario

When a real trip shows something odd, add it to `scenarios()` in `gatesim/scenarios.py`: the trains, how the feed behaves, where the person is, which gate, and which minutes they check. Run it, read the timeline, and fix the backend until it passes.
