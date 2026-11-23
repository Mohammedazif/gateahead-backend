"""Indian city search. Names and coordinates are from GeoNames cities5000 (CC-BY 4.0)."""

from __future__ import annotations

import json
import unicodedata
from dataclasses import dataclass
from pathlib import Path

_PATH = Path(__file__).resolve().parent / "data" / "india_cities.json"
_LOCAL_ALIASES = {
    "Bengaluru": ("ಬೆಂಗಳೂರು",),
    "Chennai": ("சென்னை",),
    "Delhi": ("दिल्ली", "ਦਿੱਲੀ"),
    "Hyderabad": ("హైదరాబాద్", "حیدرآباد"),
    "Kannur": ("കണ്ണൂർ",),
    "Kochi": ("കൊച്ചി",),
    "Kolkata": ("কলকাতা", "कोलकाता"),
    "Kozhikode": ("കോഴിക്കോട്",),
    "Mumbai": ("मुंबई",),
    "New Delhi": ("नई दिल्ली",),
    "Payyanur": ("പയ്യന്നൂർ",),
    "Pune": ("पुणे",),
    "Thiruvananthapuram": ("തിരുവനന്തപുരം",),
}


@dataclass(frozen=True)
class City:
    name: str
    state: str
    lat: float
    lon: float
    population: int
    aliases: tuple[str, ...]


def _fold(value: str) -> str:
    folded = unicodedata.normalize("NFKC", value).casefold()
    return " ".join("".join(char if unicodedata.category(char)[0] in "LMN" else " "
                            for char in folded).split())


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
        cities.append(City(name, state, lat, lon, population, aliases + _LOCAL_ALIASES.get(name, ())))
    return tuple(cities)


_CITIES = _load()
_SEARCHABLE = tuple((city, (_fold(city.name), *map(_fold, city.aliases))) for city in _CITIES)


def search_cities(query: str, limit: int = 8) -> list[City]:
    folded = _fold(query)
    if not 2 <= len(folded) <= 100 or limit < 1 or not any(char.isalpha() for char in folded):
        return []
    ranked = []
    compact = folded.replace(" ", "")
    for city, labels in _SEARCHABLE:
        rank = _rank(folded, compact, labels)
        if rank is not None:
            ranked.append((rank, city))
    if not ranked and len(compact) >= 4:
        ranked = [(6, city) for city, labels in _SEARCHABLE
                  if any(_one_edit(compact, label.replace(" ", "")) for label in labels)]
    ranked.sort(key=lambda item: (item[0], -item[1].population, item[1].name, item[1].state))
    return [city for _, city in ranked[:limit]]


def _rank(query: str, compact: str, labels: tuple[str, ...]) -> int | None:
    best: int | None = None
    for index, label in enumerate(labels):
        if not label:
            continue
        if label == query:
            rank = 0
        elif label.replace(" ", "") == compact:
            rank = 1
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


def _one_edit(first: str, second: str) -> bool:
    if abs(len(first) - len(second)) > 1:
        return False
    if len(first) > len(second):
        first, second = second, first
    for index, (a, b) in enumerate(zip(first, second)):
        if a != b:
            if len(first) < len(second):
                return first[index:] == second[index + 1:]
            return first[index + 1:] == second[index + 1:] or (
                first[index:index + 2] == second[index:index + 2][::-1]
                and first[index + 2:] == second[index + 2:]
            )
    return len(first) != len(second)
