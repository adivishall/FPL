"""Re-optimisation triggers (§62.1) and head-to-head league analysis (§24)."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from fpl_decision.league import head_to_head, plan_choice_vs_rival, template_gaps
from fpl_decision.triggers import PlanInputs, reoptimization_triggers
from fpl_domain.enums import Position
from fpl_domain.rules import load_ruleset
from fpl_domain.squad import Lineup
from fpl_simulation.engine import SimulationResult


def _inputs(**kw):  # type: ignore[no-untyped-def]
    base = {
        "ruleset_version": "2026-27.1",
        "finalized_gameweeks": frozenset({1, 2}),
        "fixtures": {3: frozenset({21, 22}), 4: frozenset({31})},
        "status": {10: ("a", None), 20: ("a", None)},
        "prices": {10: 60, 20: 75, 30: 80},
    }
    base.update(kw)
    return PlanInputs(**base)


def test_no_change_no_trigger() -> None:
    assert reoptimization_triggers(_inputs(), _inputs(), [10], [30], [3, 4]) == []


def test_each_kind_of_change_is_reported() -> None:
    new = _inputs(
        ruleset_version="2026-27.2",
        finalized_gameweeks=frozenset({1, 2, 3}),
        fixtures={3: frozenset({21}), 4: frozenset({31, 41})},
        status={10: ("d", 50.0), 20: ("a", None)},
        prices={10: 61, 20: 75, 30: 83},
    )
    tr = reoptimization_triggers(
        _inputs(), new, owned=[10], planned_buys=[30], horizon=[3, 4], bank_after_plan=2
    )
    kinds = [t.kind for t in tr]
    assert kinds.count("fixture_change") == 2
    assert {
        "rules_change",
        "gameweek_finalised",
        "availability_change",
        "price_change",
        "affordability",
    } <= set(kinds)
    assert any(t.player_code == 30 and t.old == "80" and t.new == "83" for t in tr)


RS = load_ruleset("2026-27")
POS = [Position.GK] * 2 + [Position.DEF] * 5 + [Position.MID] * 5 + [Position.FWD] * 3


def _sim(points: np.ndarray) -> SimulationResult:
    s, p = points.shape
    pts = points.reshape(s, p, 1).astype(np.int16)
    mins = np.full((s, p, 1), 90, dtype=np.int16)
    return SimulationResult(
        player_codes=np.arange(1, p + 1),
        gameweeks=np.array([5]),
        points=pts,
        minutes=mins,
        starts=np.ones((s, p, 1), dtype=np.int8),
        has_fixture=np.ones((p, 1), bool),
        components={},
        events={},
        seed=0,
        n_sims=s,
        ruleset_version=RS.ruleset_version,
    )


def test_head_to_head_probabilities() -> None:
    rng = np.random.default_rng(0)
    # 17 players: 1..15 shared squad template, 16 = my differential MID, 17 = rival's MID
    pts = np.full((2000, 17), 2)
    pts[:, 15] = np.where(rng.random(2000) < 0.5, 10, 2)  # my differential: boom/bust
    pts[:, 16] = 5  # rival's steady pick
    sim = _sim(pts)
    positions = {i + 1: POS[i] for i in range(15)} | {16: Position.MID, 17: Position.MID}
    base = (1, 3, 4, 5, 8, 9, 10, 11, 13, 14)
    mine = Lineup(starters=(*base, 16), bench=(2, 6, 7, 12), captain=13, vice_captain=8)
    riv = Lineup(starters=(*base, 17), bench=(2, 6, 7, 12), captain=13, vice_captain=8)
    h = head_to_head(sim, mine, {"rival": riv}, 5, positions, RS)[0]
    assert h.my_differentials == [16] and h.rival_differentials == [17]
    assert h.probability_ahead == pytest.approx(0.5, abs=0.03)  # 10 > 5 half the time
    assert h.gap_mean == pytest.approx(0.5 * 5 + 0.5 * -3, abs=0.2)
    table = plan_choice_vs_rival(
        {"a": np.full(100, 60.0), "b": np.r_[np.full(50, 50.0), np.full(50, 80.0)]},
        np.full(100, 62.0),
    )
    assert list(table["plan"]) == ["b", "a"]
    assert table.set_index("plan").loc["a", "probability_ahead"] == 0.0


def test_template_gaps() -> None:
    own = pd.Series({1: 55.0, 2: 31.0, 3: 12.0, 4: 40.0})
    df = template_gaps({1}, own)
    assert list(df["player_code"]) == [4, 2]
