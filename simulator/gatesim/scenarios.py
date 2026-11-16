"""Situations the app has to get right at a level crossing.

Each scenario names one gate, one train to watch, where the person is, and the minutes they check.
Every other train in the list is still on the line, so the app can confuse them.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from gatesim.harness import Where, at_gate, minutes_between, pinned, riding
from gatesim.world import GATE_BY_NODE, Call, Feed, SimTrain


def express(start_delay: float = 0.0, delays: dict[str, float] | None = None) -> SimTrain:
    """16601 southbound. Leaves Elamkulam 10:26, passes School Road gate (km 29.4) about 10:29."""
    return SimTrain(
        "16601",
        "Coastal Express",
        [
            Call("NPJ", 600, 600),
            Call("PZH", 615, 617),
            Call("ELM", 625, 626),
            Call("CRK", 632, 632, halts=False),
            Call("VLR", 639, 640),
            Call("MWJ", 649, 654),
            Call("PNM", 668, 669),
            Call("TKR", 677, 678),
            Call("MNR", 693, 694),
            Call("CHV", 701, 702),
            Call("SGJ", 720, 720),
        ],
        start_delay=start_delay,
        delays=delays or {},
    )


def later_express() -> SimTrain:
    """16603 southbound, about two hours behind 16601 at the same gate."""
    return SimTrain(
        "16603",
        "Malabar Express",
        [
            Call("NPJ", 725, 725),
            Call("PZH", 740, 742),
            Call("ELM", 750, 751),
            Call("VLR", 764, 765),
            Call("MWJ", 774, 779),
            Call("SGJ", 845, 845),
        ],
    )


def memu_north(start: int = 560) -> SimTrain:
    """66602 northbound all-stations MEMU from Midway Junction. The default start passes School Road gate about 10:05."""
    codes = [("MWJ", 0, 0), ("VLR", 11, 12), ("CRK", 20, 21), ("ELM", 29, 30), ("PZH", 40, 41), ("KDV", 51, 52), ("NPJ", 62, 62)]
    return SimTrain(
        "66602",
        "Midway - Northpur MEMU",
        [Call(code, start + arrive, start + depart) for code, arrive, depart in codes],
        southbound=False,
    )


def night_mail() -> SimTrain:
    """12605 southbound, passes School Road gate just after midnight."""
    return SimTrain(
        "12605",
        "Night Mail",
        [
            Call("NPJ", 1410, 1410),
            Call("PZH", 1425, 1427),
            Call("ELM", 1435, 1436),
            Call("VLR", 1449, 1450),
            Call("MWJ", 1459, 1464),
            Call("SGJ", 1530, 1530),
        ],
    )


def short_passenger() -> SimTrain:
    """56611 southbound passenger that ends at Elamkulam, 3 km short of School Road gate."""
    return SimTrain(
        "56611",
        "Northpur - Elamkulam Passenger",
        [Call("NPJ", 590, 590), Call("KDV", 602, 603), Call("PZH", 613, 614), Call("ELM", 624, 624)],
    )


def fog_express() -> SimTrain:
    """12609 southbound, timetabled past School Road gate at 07:29, running three hours late."""
    return SimTrain(
        "12609",
        "Fog Express",
        [
            Call("NPJ", 420, 420),
            Call("PZH", 435, 437),
            Call("ELM", 445, 446),
            Call("VLR", 459, 460),
            Call("MWJ", 469, 474),
            Call("SGJ", 540, 540),
        ],
        start_delay=180,
    )


@dataclass
class Scenario:
    name: str
    story: str
    trains: list[SimTrain]
    watch: str
    gate: int
    where: Where
    minutes: list[float]
    feeds: dict[str, Feed] = field(default_factory=dict)
    # Feed lag the app cannot see through; ETA error within this many minutes counts as right.
    tolerance: float = 3.0


def scenarios() -> list[Scenario]:
    fast = Feed(lag=2, refresh=3)
    gate = GATE_BY_NODE[9004]
    rider = express()
    found = [
        Scenario(
            "rider_on_express",
            "You are on 16601 and keep checking School Road gate as the train runs up to it and over it.",
            [rider, later_express(), memu_north(540)],
            "16601",
            9004,
            riding(rider),
            minutes_between("10:05", "10:34"),
            {"16601": fast},
        ),
        Scenario(
            "waiting_at_gate",
            "You are standing at School Road gate while 16601 comes down from Northpur.",
            [express(), later_express(), memu_north(540)],
            "16601",
            9004,
            at_gate(gate),
            minutes_between("10:00", "10:34"),
            {"16601": fast},
        ),
        Scenario(
            "rider_pin_stays",
            "You are on 16601. Your place was set once at 10:19, 9 km before the gate, and never refreshed.",
            [rider, later_express()],
            "16601",
            9004,
            pinned(riding(rider), 600 + 19),
            minutes_between("10:19", "10:34"),
            {"16601": fast},
        ),
        Scenario(
            "stale_feed",
            "Live status stops updating at 10:15 while 16601 sits at Puzhakkal. The train keeps running on time.",
            [express(), later_express()],
            "16601",
            9004,
            at_gate(gate),
            minutes_between("10:12", "10:34"),
            {"16601": Feed(lag=1, refresh=1, stall_from=615, stall_to=700)},
            tolerance=4.0,
        ),
        Scenario(
            "late_express",
            "16601 leaves Northpur 35 minutes late, then stands 7 extra minutes at Elamkulam, 3 km before the gate."
            " No feed can know that extra wait before it happens, so the ETA may run up to 8 minutes early until it does.",
            [express(35, {"ELM": 42}), later_express()],
            "16601",
            9004,
            at_gate(gate),
            minutes_between("10:36", "11:16"),
            {"16601": fast},
            tolerance=8.0,
        ),
        Scenario(
            "northbound_memu",
            "You wait at School Road gate for the northbound MEMU coming up from Cherukara.",
            [memu_north(), express(), later_express()],
            "66602",
            9004,
            at_gate(gate),
            minutes_between("09:40", "10:20"),
            {"66602": fast},
        ),
        Scenario(
            "gate_by_platform",
            "Station Road gate is 300 m past the Vellur platform. 16601 halts at Vellur first.",
            [express(), later_express()],
            "16601",
            9007,
            at_gate(GATE_BY_NODE[9007]),
            minutes_between("10:20", "10:44"),
            {"16601": fast},
        ),
        Scenario(
            "two_trains_cross",
            "16601 comes from the north and the MEMU from the south, a few minutes apart, at School Road gate.",
            [express(), memu_north(600), later_express()],
            "16601",
            9004,
            at_gate(gate),
            minutes_between("10:05", "10:34"),
            {"16601": fast, "66602": fast},
        ),
        Scenario(
            "feed_stalls_moving",
            "Live status freezes at 10:19 while 16601 is between Puzhakkal and Elamkulam, like a 45-minute-old RailYatri report.",
            [express(), later_express()],
            "16601",
            9004,
            at_gate(gate),
            minutes_between("10:15", "10:40"),
            {"16601": Feed(lag=1, refresh=1, stall_from=619, stall_to=700)},
            tolerance=4.0,
        ),
        Scenario(
            "crossed_non_stop",
            "Mill Road gate is past Cherukara, a halt 16601 runs through without stopping (feed status T).",
            [express(), later_express()],
            "16601",
            9006,
            at_gate(GATE_BY_NODE[9006]),
            minutes_between("10:15", "10:40"),
            {"16601": fast},
        ),
        Scenario(
            "waiting_at_origin",
            "Harbour Road gate is 3 km out of Northpur. 16601 stands at the origin until it leaves at 10:00.",
            [express(), memu_north(540), later_express()],
            "16601",
            9001,
            at_gate(GATE_BY_NODE[9001]),
            minutes_between("09:35", "10:08"),
            {"16601": fast},
        ),
        Scenario(
            "terminates_short",
            "A passenger ends at Elamkulam and never reaches School Road gate. The app must not close the gate for it.",
            [short_passenger(), express(), later_express()],
            "56611",
            9004,
            at_gate(gate),
            minutes_between("10:00", "10:30"),
            {"56611": fast, "16601": fast},
        ),
        Scenario(
            "three_hours_late",
            "12609 was due at School Road gate at 07:29 and is running three hours late.",
            [fog_express(), express(), later_express()],
            "12609",
            9004,
            at_gate(gate),
            minutes_between("10:10", "10:34"),
            {"12609": fast, "16601": fast},
        ),
        Scenario(
            "after_midnight",
            "Night Mail passes School Road gate at about 00:00 while you wait from 23:35.",
            [night_mail()],
            "12605",
            9004,
            at_gate(gate),
            minutes_between("23:35", "+00:06"),
            {"12605": fast},
        ),
        Scenario(
            "stopped_feed_recovers",
            "A stopped report freezes at Puzhakkal, becomes unresolved, then a new moving position resolves it.",
            [express(), later_express()],
            "16601",
            9004,
            at_gate(gate),
            minutes_between("10:12", "10:36"),
            {"16601": Feed(lag=1, refresh=1, stall_from=615, stall_to=630)},
            tolerance=4.0,
        ),
        Scenario(
            "express_skips_local_stops",
            "An express stops at the regional junctions but skips every station beside School Road gate.",
            [SimTrain("16512", "Regional Express", [Call("NPJ", 600, 600), Call("MWJ", 650, 651),
                                                       Call("SGJ", 721, 721)])],
            "16512",
            9004,
            at_gate(gate),
            minutes_between("10:12", "10:35"),
            {"16512": fast},
        ),
    ]
    return found
