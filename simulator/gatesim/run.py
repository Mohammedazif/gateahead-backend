"""Print what the app says at a gate, minute by minute, next to what the train is really doing.

    python -m gatesim                 # every scenario, summary + timelines
    python -m gatesim rider_on_express
    python -m gatesim --summary       # one line per scenario
"""

from __future__ import annotations

import sys
import argparse
import math
from dataclasses import dataclass

from gatesim.harness import CLEARANCE_MIN, LEAD_MIN, Look, run
from gatesim.scenarios import Scenario, scenarios

# Statuses that tell a person the gate is shut or about to shut.
DANGER = {"closed", "closing_soon"}
# Minutes after the engine reaches the gate that the app may still say closed.
GRACE = CLEARANCE_MIN
# API estimates are rounded to one decimal place; truth is continuous.
ROUNDING_MIN = 0.05


@dataclass
class Score:
    checks: int = 0
    missed: int = 0  # gate really closed or closing, app silent about the train
    wrong_open: int = 0  # gate really closed/closing, app says approaching/later and is late by more than the tolerance
    ghost: int = 0  # app says closed/closing well after the train passed, or long before it is near
    off: int = 0  # ETA wrong by more than the scenario tolerance while the train is within 30 min
    no_passed: int = 0  # train went over the gate 2.5-4.5 min ago, person at the gate, app shows nothing for it
    early_passed: int = 0  # app says "passed" before the train has reached the gate
    view_missed: int = 0  # gate really closed or closing, the tapped-gate view shows no train
    unresolved: int = 0  # stale stopped report silently dropped or given a countdown
    disagreement: int = 0  # crossing list and detail scene disagree
    invalid: int = 0  # a displayed estimate has no finite ETA or an invalid status
    worst: float = 0.0

    @property
    def ok(self) -> bool:
        return not (
            self.missed or self.view_missed or self.wrong_open or self.ghost or self.off or self.no_passed
            or self.early_passed or self.unresolved or self.disagreement or self.invalid
        )


def judge(look: Look, tolerance: float) -> list[str]:
    issues = []
    truth = look.true_status
    said = look.app_status
    valid_statuses = {None, "closed", "closing_soon", "approaching", "later", "awaiting_update"}
    for status, eta in ((said, look.app_eta), (look.scene_status, look.scene_eta)):
        if status not in valid_statuses or (status not in {None, "awaiting_update"}
                                           and (eta is None or not math.isfinite(eta))):
            issues.append("INVALID_ESTIMATE")
    allowed_error = tolerance + ROUNDING_MIN + (look.report_age if look.stopped_report_ahead else 0) + 1e-9
    if (look.report_ahead or look.stopped_report_ahead) and look.report_age >= 8:
        if said != "awaiting_update" or look.app_eta is not None or look.app_closes is not None or look.app_passed is not None:
            issues.append("STALE_COUNTDOWN")
        if look.viewed and (look.scene_status != "awaiting_update" or look.scene_eta is not None or look.scene_passed is not None):
            issues.append("STALE_VIEW")
        return issues
    awaiting = said == "awaiting_update" or (look.viewed and look.scene_status == "awaiting_update")
    awaiting_passage = (look.report_ahead or look.report_near_crossing) and look.true_eta is not None and look.true_eta <= 0
    if awaiting and not awaiting_passage:
        issues.append("UNEXPECTED_UNRESOLVED")
    if look.viewed and (said is not None or look.scene_status in {"closed", "closing_soon", "approaching"}) and (said != look.scene_status or
                        (look.app_eta is not None and look.scene_eta is not None
                         and abs(look.app_eta - look.scene_eta) > 0.15)):
        issues.append("DISAGREEMENT")
    if truth in DANGER and said is None and look.has_live_report:
        issues.append("MISSED")
    if look.viewed and truth in DANGER and look.scene_status is None and look.has_live_report:
        issues.append("VIEW_MISSED")
    elif (truth in DANGER and said not in DANGER and look.has_live_report and not awaiting_passage
          and (look.app_eta or 0) - (look.true_eta or 0) > allowed_error):
        issues.append("SAYS_OPEN")
    if said in DANGER and (
        look.true_eta is None or look.true_eta < -(GRACE + 1) or look.true_eta > 10 + tolerance
    ):
        issues.append("GHOST")
    if (
        look.app_eta is not None
        and look.true_eta is not None
        and -1 <= look.true_eta <= 30
        and abs(look.app_eta - look.true_eta) > allowed_error
    ):
        issues.append(f"OFF {look.app_eta - look.true_eta:+.1f}")
    pass_reports = (look.app_passed, look.scene_passed) if look.viewed else (look.app_passed,)
    reported = [age for age in pass_reports if age is not None]
    if (
        look.near_gate
        and look.has_live_report and not (look.report_ahead or look.report_near_crossing)
        and look.true_eta is not None
        and -4.5 <= look.true_eta <= -(GRACE + 0.5)
        and said is None
        and any(age is None for age in pass_reports)
    ):
        issues.append("NO_PASSED")
    if reported and (not look.has_live_report or look.report_ahead
                     or look.true_eta is None or look.true_eta > 1.5):
        issues.append("EARLY_PASSED")
    if any(not math.isfinite(age) or age < 0 for age in reported):
        issues.append("INVALID_ESTIMATE")
    elif reported and look.true_eta is not None and any(abs(age + look.true_eta) > allowed_error for age in reported):
        issues.append("INVALID_ESTIMATE")
    if len(reported) == 2 and abs(reported[0] - reported[1]) > 0.15:
        issues.append("DISAGREEMENT")
    return issues


def score(looks: list[Look], tolerance: float) -> Score:
    """Judge the watched train and every other train on the line at each check."""
    result = Score()
    for look in [item for check in looks for item in (check, *check.rest)]:
        result.checks += 1
        issues = judge(look, tolerance)
        result.missed += "MISSED" in issues
        result.view_missed += "VIEW_MISSED" in issues
        result.wrong_open += "SAYS_OPEN" in issues
        result.ghost += "GHOST" in issues
        result.no_passed += "NO_PASSED" in issues
        result.early_passed += "EARLY_PASSED" in issues
        result.unresolved += any(item in issues for item in ("STALE_COUNTDOWN", "STALE_VIEW", "UNEXPECTED_UNRESOLVED"))
        result.disagreement += "DISAGREEMENT" in issues
        result.invalid += "INVALID_ESTIMATE" in issues
        if any(item.startswith("OFF") for item in issues):
            result.off += 1
            result.worst = max(result.worst, abs((look.app_eta or 0) - (look.true_eta or 0)))
    return result


def _num(value: float | None) -> str:
    return "  -  " if value is None else f"{value:5.1f}"


def _gate(eta: float | None, up_after: float = GRACE) -> str:
    """Minutes until the barrier comes down, 'down' while it is, 'up' once the train has cleared."""
    if eta is None:
        return "  -  "
    if eta < -up_after:
        return "  up "
    left = eta - LEAD_MIN
    return " down" if left <= 0 else f"{left:5.1f}"


def _gone(look: Look) -> str:
    ago = look.app_passed if look.app_passed is not None else look.scene_passed
    return "" if ago is None else f"passed {ago:.0f}m ago"


def timeline(scenario: Scenario, looks: list[Look]) -> str:
    lines = [
        f"== {scenario.name}: {scenario.story}",
        f"Barrier down {LEAD_MIN:.0f} min before the train. 'barrier' = minutes until it comes down.",
        "       ----------- really -----------   ------------ app says ------------   gate view",
        "time   train  barrier  status           train  barrier  status              status         | passed note      | issues / other trains here",
    ]
    for look in looks:
        issues = judge(look, scenario.tolerance)
        for other in look.rest:
            issues += [f"{other.train}:{item}" for item in judge(other, scenario.tolerance)]
        others = ", ".join(f"{n} {s} {e:.0f}m" for n, e, s in look.others)
        app_barrier = _gate(look.app_eta, GRACE)
        lines.append(
            f"{look.clock}  {_num(look.true_eta)}  {_gate(look.true_eta)}    {look.true_status:<14}   "
            f"{_num(look.app_eta)}  {app_barrier}    {str(look.app_status or '-'):<16}    "
            f"{str(look.scene_status or '-'):<14} | {_gone(look):<16} | {' '.join(issues)}{'  ' if issues and others else ''}{others}"
        )
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("names", nargs="*", help="Scenario names (default: all)")
    parser.add_argument("--summary", action="store_true", help="Print one result per scenario")
    parser.add_argument("--cache-live", action="store_true", help="Use the real 90-second shared snapshot cache")
    args = parser.parse_args(argv)
    available = scenarios()
    unknown = set(args.names) - {item.name for item in available}
    if unknown:
        parser.error(f"Unknown scenario(s): {', '.join(sorted(unknown))}")
    chosen = [item for item in available if not args.names or item.name in args.names]
    failures = 0
    rows = []
    for scenario in chosen:
        looks = run(scenario.trains, scenario.feeds, scenario.watch, scenario.gate, scenario.where, scenario.minutes,
                    cache_live=args.cache_live)
        result = score(looks, scenario.tolerance)
        failures += not result.ok
        rows.append(
            f"{'PASS' if result.ok else 'FAIL'}  {scenario.name:<18} checks={result.checks:<4} missed={result.missed:<3}"
            f" says_open={result.wrong_open:<3} ghost={result.ghost:<3} eta_off={result.off:<3}"
            f" no_passed={result.no_passed:<3} early_passed={result.early_passed:<3} view_missed={result.view_missed:<3} worst_off={result.worst:.1f}m"
            f" unresolved={result.unresolved:<3} disagree={result.disagreement:<3}"
            f" invalid={result.invalid:<3}"
        )
        if not args.summary:
            print(timeline(scenario, looks))
            print()
    print("\n".join(rows))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
