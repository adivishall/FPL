"""Property-based invariants for the rules engine (§36 'Property-based', §77.2)."""

from __future__ import annotations

import random

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from fpl_domain.enums import ChipType, Position
from fpl_domain.errors import RuleViolation
from fpl_domain.scoring import MatchEvents, score_match
from fpl_domain.squad import auto_substitute, formation, formation_is_legal
from fpl_domain.state import GameweekDecision, advance, apply_deadline, state_violations
from fpl_domain.validation import validate_plan
from tests.domain_util import RS, default_lineup, prices, random_squad, state_for
from tests.unit.domain.test_state_machine import _swap

SETTINGS = settings(max_examples=60, deadline=None, suppress_health_check=[HealthCheck.too_slow])


@SETTINGS
@given(
    seed=st.integers(0, 10**6),
    start_gw=st.integers(2, 30),
    plan=st.lists(
        st.tuples(
            st.integers(0, 4),
            st.sampled_from(
                [None, None, None, "wildcard", "free_hit", "bench_boost", "triple_captain"]
            ),
        ),
        min_size=1,
        max_size=6,
    ),
)
def test_random_decision_sequences_preserve_invariants(
    seed: int, start_gw: int, plan: list
) -> None:
    rng = random.Random(seed)
    state = state_for(
        random_squad(rng), gameweek=start_gw, ft=rng.randint(1, 5), bank=rng.randint(0, 150)
    )
    for n, chip_kind in plan:
        if state.gameweek >= RS.num_gameweeks:
            break
        chip_id = None
        if chip_kind:
            usable = [c for c in state.available_chips() if c.chip_type.value == chip_kind]
            chip_id = usable[0].chip_id if usable else None
        decision = GameweekDecision(transfers=_swap(state, n, rng), chip_id=chip_id)
        before = state
        try:
            out = apply_deadline(state, decision, prices(), RS)
        except RuleViolation:
            continue  # illegal decisions are rejected, never repaired
        # hits are exactly the ruleset penalty for paid transfers
        chip_type = out.chip.chip_type if out.chip else None
        if chip_type in (ChipType.WILDCARD, ChipType.FREE_HIT):
            assert out.hit_points == 0
        else:
            assert out.hit_points == 4 * max(0, len(decision.transfers) - before.free_transfers)
        assert out.bank_after >= 0
        state = advance(out, RS)
        assert state_violations(state, RS) == []
        assert 1 <= state.free_transfers <= RS.transfers.max_banked_free_transfers
        if chip_type is ChipType.FREE_HIT:
            assert state.squad == before.squad and state.bank == before.bank


@SETTINGS
@given(seed=st.integers(0, 10**6), k=st.integers(1, 4))
def test_validator_replay_matches_direct_application(seed: int, k: int) -> None:
    rng = random.Random(seed)
    s0 = state_for(random_squad(rng), gameweek=5, ft=2, bank=300)
    decisions, s = [], s0
    for _ in range(k):
        d = GameweekDecision(transfers=_swap(s, rng.randint(0, 2), rng))
        try:
            s = advance(apply_deadline(s, d, prices(), RS), RS)
        except RuleViolation:
            break
        decisions.append(d)
    rep = validate_plan(s0, decisions, prices(), RS)
    assert rep.valid
    if decisions:
        assert rep.final_state is not None
        assert set(rep.final_state.codes) == set(s.codes)
        assert rep.final_state.free_transfers == s.free_transfers


@SETTINGS
@given(seed=st.integers(0, 10**6), dnp=st.sets(st.integers(0, 14), max_size=8))
def test_autosubs_always_keep_a_legal_formation(seed: int, dnp: set[int]) -> None:
    squad = random_squad(random.Random(seed))
    lu = default_lineup(squad)
    pos = {p.player_code: p.position for p in squad}
    minutes = {p: (0 if i in dnp else 90) for i, p in enumerate(lu.players)}
    xi, subs = auto_substitute(lu, pos, minutes, RS)
    assert formation_is_legal(formation(xi, pos), RS)
    assert len(set(xi)) == 11
    assert sum(minutes[p] == 0 for p in xi) <= sum(minutes[p] == 0 for p in lu.starters)
    for out_p, in_p in subs:
        assert minutes[out_p] == 0 and minutes[in_p] > 0
        assert (pos[out_p] is Position.GK) == (pos[in_p] is Position.GK)


@settings(max_examples=200, deadline=None)
@given(
    pos=st.sampled_from(list(Position)),
    minutes=st.integers(1, 90),
    goals=st.integers(0, 3),
    assists=st.integers(0, 3),
)
def test_scoring_monotone_in_attacking_returns(
    pos: Position, minutes: int, goals: int, assists: int
) -> None:
    base = score_match(MatchEvents(minutes=minutes, goals=goals, assists=assists), pos, RS)
    more_g = score_match(MatchEvents(minutes=minutes, goals=goals + 1, assists=assists), pos, RS)
    more_a = score_match(MatchEvents(minutes=minutes, goals=goals, assists=assists + 1), pos, RS)
    assert more_g["total"] > base["total"] and more_a["total"] > base["total"]
