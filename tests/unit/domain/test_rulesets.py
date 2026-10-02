"""Ruleset configuration contract tests (ADR-0005, §41 #1, §51)."""

from __future__ import annotations

import copy

import pytest
import yaml
from pydantic import ValidationError

from fpl_domain.enums import ChipType, Position
from fpl_domain.rules import available_seasons, config_root, load_ruleset, parse_ruleset
from fpl_domain.rules.model import ChipFtPolicy
from fpl_domain.rules.schema import render_schema_yaml

SEASONS = ["2022-23", "2023-24", "2024-25", "2025-26", "2026-27"]


def test_every_backtest_season_has_a_ruleset() -> None:
    assert set(SEASONS) <= set(available_seasons())


@pytest.mark.parametrize("season", SEASONS)
def test_ruleset_loads_and_is_internally_consistent(season: str) -> None:
    rs = load_ruleset(season)
    assert rs.season == season
    assert rs.squad.size == 15
    assert rs.squad.positions.as_dict() == {
        Position.GK: 2,
        Position.DEF: 5,
        Position.MID: 5,
        Position.FWD: 3,
    }
    assert rs.squad.max_per_club == 3
    assert rs.transfers.hit_cost == 4
    assert len(rs.legal_formations()) == 8
    assert (3, 4, 3) in rs.legal_formations() and (5, 2, 3) in rs.legal_formations()
    assert (3, 6, 1) not in rs.legal_formations()


def test_committed_schema_matches_model() -> None:
    committed = (config_root() / "rules" / "schema.yaml").read_text(encoding="utf-8")
    assert committed == render_schema_yaml(), "run: uv run python -m fpl_domain.rules.schema"


def test_2026_27_rules_match_spec_section_51() -> None:
    rs = load_ruleset("2026-27")
    assert rs.transfers.max_banked_free_transfers == 5
    assert rs.transfers.free_transfers_per_gameweek == 1
    assert rs.transfers.top_ups == ()  # no AFCON top-up this season
    assert rs.transfers.chip_ft_policy is ChipFtPolicy.RETAIN
    by_type: dict[ChipType, list[tuple[int, int]]] = {}
    for c in rs.chips.catalogue:
        by_type.setdefault(c.type, []).append((c.first_gameweek, c.last_gameweek))
    for chip in (
        ChipType.WILDCARD,
        ChipType.FREE_HIT,
        ChipType.BENCH_BOOST,
        ChipType.TRIPLE_CAPTAIN,
    ):
        windows = sorted(by_type[chip])
        assert len(windows) == 2, f"two sets of {chip}"
        assert windows[0][1] == 19 and windows[1][0] == 20, "first set expires at GW19"
    dc = rs.scoring.defensive_contribution
    assert dc.enabled
    assert dc.by_position[Position.DEF].threshold == 10
    assert dc.by_position[Position.MID].threshold == 12
    assert Position.GK not in dc.by_position


def test_pre_2025_seasons_have_no_defensive_contribution() -> None:
    for season in ("2022-23", "2023-24", "2024-25"):
        assert not load_ruleset(season).scoring.defensive_contribution.enabled


def test_assistant_manager_is_declared_unsupported() -> None:
    rs = load_ruleset("2024-25")
    am = [c for c in rs.chips.catalogue if c.type is ChipType.ASSISTANT_MANAGER]
    assert len(am) == 1 and not am[0].supported
    assert all(c.type is not ChipType.ASSISTANT_MANAGER for c in rs.chips_valid_in(30))


def _raw(season: str = "2026-27") -> dict:
    path = config_root() / "rules" / f"{season}.yaml"
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_invalid_position_quota_rejected() -> None:
    data = _raw()
    data["squad"]["positions"]["MID"] = 6
    with pytest.raises(ValidationError, match="sum to 16"):
        parse_ruleset(data)


def test_unknown_field_rejected() -> None:
    data = _raw()
    data["transfers"]["secret_rule"] = 1
    with pytest.raises(ValidationError):
        parse_ruleset(data)


def test_chip_window_beyond_season_rejected() -> None:
    data = _raw()
    data["chips"]["catalogue"][4]["last_gameweek"] = 39
    with pytest.raises(ValidationError, match="beyond season length"):
        parse_ruleset(data)


def test_ruleset_hash_is_stable_and_content_sensitive() -> None:
    a = parse_ruleset(_raw())
    b = parse_ruleset(copy.deepcopy(_raw()))
    assert a.content_hash == b.content_hash
    data = _raw()
    data["transfers"]["hit_cost"] = 5
    assert parse_ruleset(data).content_hash != a.content_hash


def test_ruleset_is_immutable() -> None:
    rs = load_ruleset("2026-27")
    with pytest.raises(ValidationError):
        rs.transfers.hit_cost = 0  # type: ignore[misc]
