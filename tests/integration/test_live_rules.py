"""The versioned 2026-27 ruleset agrees with the official game settings (§51, ADR-0005).

`bootstrap-static` publishes the game's own configuration (`game_settings`,
`game_config.scoring`, `chips`). This test fetches it live and compares every rule it exposes
with `config/rules/2026-27.yaml`. Marked `network`: it needs access to fantasy.premierleague.com.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from fpl_domain.config import load_versioned_config
from fpl_domain.enums import Position
from fpl_domain.rules import load_ruleset
from fpl_ingestion.sources.fpl_api import FplApiClient

pytestmark = [pytest.mark.network, pytest.mark.integration]

POS = {"GKP": Position.GK, "DEF": Position.DEF, "MID": Position.MID, "FWD": Position.FWD}
CHIP = {
    "wildcard": "wildcard",
    "freehit": "free_hit",
    "bboost": "bench_boost",
    "3xc": "triple_captain",
}


@pytest.fixture(scope="module")
def official() -> dict[str, Any]:
    client = FplApiClient.from_config(load_versioned_config("", "sources").data)
    raw, _ = client.bootstrap_static()  # the unchanged payload, as captured
    payload: dict[str, Any] = json.loads(raw.content)
    return payload


def test_squad_budget_and_transfer_rules(official: dict[str, Any]) -> None:
    rs, gs = load_ruleset("2026-27"), official["game_settings"]
    assert rs.squad.size == gs["squad_squadsize"]
    assert rs.lineup.starters == gs["squad_squadplay"]
    assert rs.squad.max_per_club == gs["squad_team_limit"]
    assert rs.squad.initial_budget == gs["squad_total_spend"]
    assert (
        gs["transfers_sell_on_fee"] == 0.5
        and rs.pricing.selling_price_rule.value == "half_profit_floor"
    )
    assert gs["element_sell_at_purchase_price"] is False
    per = rs.transfers.free_transfers_per_gameweek
    assert rs.transfers.max_banked_free_transfers == per + gs["max_extra_free_transfers"]


def test_chip_catalogue_and_windows(official: dict[str, Any]) -> None:
    rs = load_ruleset("2026-27")
    ours = sorted((c.type.value, c.first_gameweek, c.last_gameweek) for c in rs.chips.catalogue)
    theirs = sorted(
        (CHIP[c["name"]], int(c["start_event"]), int(c["stop_event"])) for c in official["chips"]
    )
    assert ours == theirs


def test_scoring_table(official: dict[str, Any]) -> None:
    sc, s = official["game_config"]["scoring"], load_ruleset("2026-27").scoring
    for k, pos in POS.items():
        assert s.goal.get(pos) == sc["goals_scored"][k], k
        assert s.clean_sheet.points.get(pos) == sc["clean_sheets"][k], k
        assert s.goals_conceded.points.get(pos) == sc["goals_conceded"][k], k
        dc = s.defensive_contribution.by_position.get(pos)
        assert (dc.points if dc else 0) == sc["defensive_contribution"][k], k
    assert s.assist == sc["assists"]
    assert s.saves.points == sc["saves"]
    assert (s.penalty_save, s.penalty_miss) == (sc["penalties_saved"], sc["penalties_missed"])
    assert (s.yellow_card, s.red_card, s.own_goal) == (
        sc["yellow_cards"],
        sc["red_cards"],
        sc["own_goals"],
    )
    a = s.appearance
    assert (a.points_below_threshold, a.points_at_or_above_threshold) == (
        sc["short_play"],
        sc["long_play"],
    )
