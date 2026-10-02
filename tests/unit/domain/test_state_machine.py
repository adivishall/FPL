"""Transfers, free-transfer rollover, hits, chips and Free Hit reversion (§51.1, §53.2, §77.1)."""

from __future__ import annotations

import random

import pytest

from fpl_domain.enums import ChipType, Position
from fpl_domain.errors import RuleViolation
from fpl_domain.rules import load_ruleset
from fpl_domain.state import (
    ChipStatus,
    GameweekDecision,
    Transfer,
    advance,
    apply_deadline,
    next_free_transfers,
    paid_transfer_count,
    state_violations,
)
from fpl_domain.validation import validate_plan
from tests.domain_util import RS, pool, prices, random_squad, state_for


def _swap(state, n: int, rng: random.Random, price_override: int | None = None):
    """n same-position transfers to unowned players that keep the club limit."""
    owned = set(state.codes)
    clubs = {}
    for p in state.squad:
        clubs[p.team_code] = clubs.get(p.team_code, 0) + 1
    out = []
    for p in rng.sample(list(state.squad), len(state.squad)):
        if len(out) == n:
            break
        cands = [
            r
            for r in pool()
            if r["position"] == p.position.value
            and r["player_code"] not in owned
            and (clubs.get(r["team_code"], 0) - (p.team_code == r["team_code"])) < 3
        ]
        if not cands:
            continue
        r = rng.choice(cands)
        owned.add(r["player_code"])
        clubs[p.team_code] -= 1
        clubs[r["team_code"]] = clubs.get(r["team_code"], 0) + 1
        out.append(
            Transfer(
                out_code=p.player_code,
                in_code=r["player_code"],
                in_position=Position(r["position"]),
                in_team_code=r["team_code"],
                in_price=price_override if price_override is not None else int(r["price"]),
            )
        )
    return tuple(out)


# ---------------------------------------------------------------- free transfers & hits


def test_free_transfers_bank_up_to_five() -> None:
    ft, seq = 1, []
    for gw in range(2, 9):
        ft = next_free_transfers(ft, 0, gw, None, RS)
        seq.append(ft)
    assert seq == [2, 3, 4, 5, 5, 5, 5]


def test_rollover_after_transfers_and_hits() -> None:
    assert next_free_transfers(3, 1, 10, None, RS) == 3
    assert next_free_transfers(3, 3, 10, None, RS) == 1
    assert next_free_transfers(1, 3, 10, None, RS) == 1  # hits don't create negative FT
    assert paid_transfer_count(1, 3, 10, None, RS) == 2
    assert paid_transfer_count(2, 2, 10, None, RS) == 0


def test_first_gameweek_unlimited_then_one_free_transfer() -> None:
    assert paid_transfer_count(0, 15, 1, None, RS) == 0
    assert next_free_transfers(0, 15, 1, None, RS) == 1


def test_wildcard_free_hit_retain_banked_transfers_2026_27() -> None:
    for chip in (ChipType.WILDCARD, ChipType.FREE_HIT):
        assert paid_transfer_count(1, 12, 10, chip, RS) == 0
        assert next_free_transfers(3, 12, 10, chip, RS) == 3


def test_pre_2024_chip_resets_and_max_two_banked() -> None:
    r = load_ruleset("2023-24")
    assert next_free_transfers(2, 0, 10, None, r) == 2
    assert next_free_transfers(2, 9, 10, ChipType.WILDCARD, r) == 1


def test_afcon_top_up_2025_26_and_world_cup_unlimited_2022_23() -> None:
    r25 = load_ruleset("2025-26")
    assert next_free_transfers(1, 1, 15, None, r25) == 5  # FTs for GW16 topped up to five
    r22 = load_ruleset("2022-23")
    assert paid_transfer_count(1, 9, 17, None, r22) == 0


# ---------------------------------------------------------------- deadlines


def test_transfer_hits_budget_and_purchase_price() -> None:
    rng = random.Random(4)
    st = state_for(random_squad(rng), ft=1, bank=1000)
    tr = _swap(st, 3, rng)
    out = apply_deadline(st, GameweekDecision(transfers=tr), prices(), RS)
    assert out.paid_transfers == 2 and out.hit_points == 8
    bought = {p.player_code: p.purchase_price for p in out.playing_squad}
    assert all(bought[t.in_code] == t.in_price for t in tr)
    nxt = advance(out, RS)
    assert nxt.gameweek == 3 and nxt.free_transfers == 1
    assert state_violations(nxt, RS) == []


def test_insufficient_budget_rejected() -> None:
    rng = random.Random(5)
    st = state_for(random_squad(rng), bank=0)
    tr = _swap(st, 1, rng, price_override=200)
    with pytest.raises(RuleViolation, match="negative_bank"):
        apply_deadline(st, GameweekDecision(transfers=tr), prices(), RS)


def test_illegal_transfers_rejected() -> None:
    rng = random.Random(6)
    st = state_for(random_squad(rng))
    (t,) = _swap(st, 1, rng)
    with pytest.raises(RuleViolation, match="not_owned"):
        apply_deadline(
            st, GameweekDecision(transfers=(t.model_copy(update={"out_code": -1}),)), prices(), RS
        )
    with pytest.raises(RuleViolation, match="in_owned"):
        apply_deadline(
            st,
            GameweekDecision(
                transfers=(t.model_copy(update={"in_code": st.squad[1].player_code}),)
            ),
            prices(),
            RS,
        )
    wrong_pos = t.model_copy(
        update={"in_position": Position.GK if t.in_position is not Position.GK else Position.FWD}
    )
    with pytest.raises(RuleViolation, match="position_quota"):
        apply_deadline(st, GameweekDecision(transfers=(wrong_pos,)), prices(), RS)


def test_chip_windows_one_use_and_expiry() -> None:
    rng = random.Random(7)
    st = state_for(random_squad(rng), gameweek=19)
    out = apply_deadline(st, GameweekDecision(chip_id="bench_boost_1"), prices(), RS)
    nxt = advance(out, RS)
    assert nxt.chip("bench_boost_1").status is ChipStatus.USED
    assert nxt.chip("bench_boost_1").used_gameweek == 19
    assert nxt.chip("wildcard_1").status is ChipStatus.EXPIRED  # first set expires after GW19
    assert nxt.chip("wildcard_2").status is ChipStatus.AVAILABLE
    with pytest.raises(RuleViolation, match="chip_unavailable"):
        apply_deadline(nxt, GameweekDecision(chip_id="bench_boost_1"), prices(), RS)
    with pytest.raises(RuleViolation, match="chip_unavailable"):
        apply_deadline(
            state_for(random_squad(rng), gameweek=5),
            GameweekDecision(chip_id="wildcard_2"),
            prices(),
            RS,
        )


def test_free_hit_reverts_exactly() -> None:
    rng = random.Random(8)
    st = state_for(random_squad(rng), gameweek=6, ft=2, bank=600)
    tr = _swap(st, 6, rng)
    out = apply_deadline(st, GameweekDecision(transfers=tr, chip_id="free_hit_1"), prices(), RS)
    assert out.hit_points == 0
    assert {p.player_code for p in out.playing_squad} != set(st.codes)
    nxt = advance(out, RS)
    assert nxt.squad == st.squad and nxt.bank == st.bank and nxt.lineup == st.lineup
    assert nxt.free_transfers == 2  # retained under 2026-27 rules


def test_validate_plan_replays_multi_gameweek_sequences() -> None:
    rng = random.Random(9)
    st = state_for(random_squad(rng), gameweek=3, ft=1, bank=500)
    d1 = GameweekDecision(transfers=_swap(st, 1, rng))
    rep = validate_plan(
        st, [d1, GameweekDecision(), GameweekDecision(chip_id="wildcard_1")], prices(), RS
    )
    assert rep.valid, rep.violations
    assert [s.free_transfers_at_start for s in rep.steps] == [1, 1, 2]
    assert rep.final_state is not None and rep.final_state.gameweek == 6
    bad = validate_plan(st, [GameweekDecision(chip_id="wildcard_2")], prices(), RS)
    assert not bad.valid and "chip_unavailable" in bad.violations[0]
