"""Indian city search. Names and coordinates are from GeoNames cities5000 (CC-BY 4.0)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

_PATH = Path(__file__).resolve().parent / "data" / "india_cities.json"


@dataclass(frozen=True)
class City:
    name: str
    state: str
    lat: float
    lon: float
    population: int
    aliases: tuple[str, ...]


def _fold(value: str) -> str:
    return " ".join(value.casefold().split())


def _load() -> tuple[City, ...]:
    payload = json.loads(_PATH.read_text(encoding="utf-8"))
    cities: list[City] = []
    for row in payload.get("cities", []):
        name = str(row.get("n") or "").strip()
        state = str(row.get("s") or "").strip()
        if not name or not state:
            continue
        try:
            lat = float(row["y"])
            lon = float(row["x"])
            population = int(row.get("p") or 0)
        except (KeyError, TypeError, ValueError):
            continue
        aliases = tuple(str(item).strip() for item in row.get("a") or [] if str(item).strip())
        cities.append(City(name, state, lat, lon, population, aliases))
    return tuple(cities)


_CITIES = _load()


def search_cities(query: str, limit: int = 8) -> list[City]:
    folded = _fold(query)
    if len(folded) < 2 or limit < 1:
        return []
    ranked: list[tuple[int, int, str, City]] = []
    for city in _CITIES:
        rank = _rank(folded, city)
        if rank is None:
            continue
        ranked.append((rank, -city.population, city.name, city))
    ranked.sort()
    return [item[3] for item in ranked[:limit]]


def _rank(query: str, city: City) -> int | None:
    labels = (_fold(city.name), *(_fold(alias) for alias in city.aliases))
    best: int | None = None
    for index, label in enumerate(labels):
        if not label:
            continue
        if label == query:
            rank = 0
        elif label.startswith(query):
            rank = 2
        elif any(part.startswith(query) for part in label.split()):
            rank = 4
        elif query in label:
            rank = 5
        else:
            continue
        if index > 0 and rank == 2:
            rank = 3
        if best is None or rank < best:
            best = rank
    return best
