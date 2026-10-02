"""Manager-state reconstruction from public endpoints (§6.1, §86.1). Payloads are SYNTHETIC."""

from __future__ import annotations

import json
import random
from datetime import UTC, datetime

from fpl_domain.enums import ChipType, Position
from fpl_domain.rules import load_ruleset
from fpl_domain.state import ChipStatus, state_violations
from fpl_ingestion.manager_sync import reconstruct_state
from fpl_ingestion.sources import fpl_api_schemas as s
from tests.domain_util import random_squad
from tests.fixtures_util import API_DIR

RS = load_ruleset("2026-27")
BOOT = s.BootstrapStatic.model_validate(json.loads((API_DIR / "bootstrap-static.json").read_text()))
BY_CODE = {e.code: e for e in BOOT.elements}
T0 = datetime(2026, 8, 1, tzinfo=UTC)


def _picks(codes: list[int]) -> s.EntryPicks:
    elems = [BY_CODE[c] for c in codes]
    gks = [e for e in elems if e.element_type == 1]
    out = [e for e in elems if e.element_type != 1]
    order = [gks[0], *out[:10], gks[1], *out[10:]]  # 1 GK + 10 outfield start; GK2 bench first
    picks = [
        s.ApiPick(
            element=e.id, position=i + 1, multiplier=1, is_captain=i == 10, is_vice_captain=i == 9
        )
        for i, e in enumerate(order)
    ]
    hist = s.ApiHistoryEvent(
        event=1,
        points=50,
        total_points=50,
        bank=0,
        value=1000,
        event_transfers=0,
        event_transfers_cost=0,
        points_on_bench=0,
    )
    return s.EntryPicks(active_chip=None, picks=picks, entry_history=hist)


def _legal_outfield_order(codes: list[int]) -> list[int]:
    """Order codes so that GK, 3 DEF, 2 MID, 1 FWD start (legal for any 4 remaining)."""
    pos = {c: Position.from_element_type(BY_CODE[c].element_type) for c in codes}
    gk = [c for c in codes if pos[c] is Position.GK]
    d = [c for c in codes if pos[c] is Position.DEF]
    m = [c for c in codes if pos[c] is Position.MID]
    f = [c for c in codes if pos[c] is Position.FWD]
    return gk + d[:4] + m[:4] + f[:2] + d[4:] + m[4:] + f[2:]


def test_reconstruction_replays_free_transfers_chips_and_free_hit() -> None:
    rng = random.Random(11)
    base = [p.player_code for p in random_squad(rng)]
    fh_squad = [p.player_code for p in random_squad(rng, exclude=set(base))]
    # Synthetic season (gw, transfers, recorded hit cost):
    # GW1 create; GW2 roll (FT 1→2); GW3 4 transfers with 2 FT → 2 paid = -8;
    # GW4 wildcard; GW5 roll; GW6 free hit; GW7 one transfer.
    hist_rows = [(1, 0, 0), (2, 0, 0), (3, 4, 8), (4, 9, 0), (5, 0, 0), (6, 11, 0), (7, 1, 0)]
    current = [
        s.ApiHistoryEvent(
            event=g,
            points=50,
            total_points=50 * g,
            rank=None,
            overall_rank=1000 + g,
            bank=15,
            value=1000,
            event_transfers=n,
            event_transfers_cost=c,
            points_on_bench=0,
        )
        for g, n, c in hist_rows
    ]
    chips = [
        s.ApiChipPlay(name="wildcard", time=T0, event=4),
        s.ApiChipPlay(name="freehit", time=T0, event=6),
    ]
    history = s.EntryHistory(current=current, chips=chips)
    entry = s.ApiEntry(id=1234, name="Synthetic XI", started_event=1)
    bought = base[-1]
    transfers = [
        s.ApiTransfer(
            element_in=BY_CODE[bought].id,
            element_in_cost=41,
            element_out=1,
            element_out_cost=40,
            entry=1234,
            event=3,
            time=T0,
        ),
        s.ApiTransfer(
            element_in=BY_CODE[fh_squad[0]].id,
            element_in_cost=99,
            element_out=2,
            element_out_cost=40,
            entry=1234,
            event=6,
            time=T0,
        ),  # FH: ignored
    ]
    squads = {
        5: _legal_outfield_order(base),
        6: _legal_outfield_order(fh_squad),
        7: _legal_outfield_order(base),
    }
    rep = reconstruct_state(
        entry,
        history,
        lambda gw: _picks(squads[gw]),
        transfers,
        BOOT,
        RS,
        next_gameweek=8,
        as_of=T0,
    )
    st = rep.state
    assert rep.warnings == []
    assert [r["ft_start"] for r in rep.replay] == [0, 1, 2, 1, 1, 2, 2]
    assert st.free_transfers == 2 and st.bank == 15 and st.gameweek == 8
    assert set(st.codes) == set(base)
    assert st.pick(bought).purchase_price == 41
    e0 = BY_CODE[base[0]]
    assert st.pick(base[0]).purchase_price == e0.now_cost - (e0.cost_change_start or 0)
    assert st.chip("wildcard_1").status is ChipStatus.USED
    assert st.chip("wildcard_1").used_gameweek == 4
    assert st.chip("free_hit_1").used_gameweek == 6
    assert st.chip("wildcard_2").status is ChipStatus.AVAILABLE
    assert state_violations(st, RS) == []


def test_free_hit_in_last_gameweek_reverts_to_previous_picks() -> None:
    rng = random.Random(12)
    base = [p.player_code for p in random_squad(rng)]
    fh = [p.player_code for p in random_squad(rng, exclude=set(base))]
    current = [
        s.ApiHistoryEvent(
            event=g,
            points=1,
            total_points=g,
            bank=0,
            value=1000,
            event_transfers=0 if g != 3 else 12,
            event_transfers_cost=0,
            points_on_bench=0,
        )
        for g in (1, 2, 3)
    ]
    history = s.EntryHistory(
        current=current, chips=[s.ApiChipPlay(name="freehit", time=T0, event=3)]
    )
    squads = {2: _legal_outfield_order(base), 3: _legal_outfield_order(fh)}
    rep = reconstruct_state(
        s.ApiEntry(id=1, name="x"),
        history,
        lambda gw: _picks(squads[gw]),
        [],
        BOOT,
        RS,
        next_gameweek=4,
        as_of=T0,
    )
    assert set(rep.state.codes) == set(base)
    assert rep.state.free_transfers == 2  # FT retained through the Free Hit (2026-27 rules)


def test_hit_cost_disagreement_is_reported_not_hidden() -> None:
    rng = random.Random(13)
    base = [p.player_code for p in random_squad(rng)]
    current = [
        s.ApiHistoryEvent(
            event=1,
            points=1,
            total_points=1,
            bank=0,
            value=1000,
            event_transfers=0,
            event_transfers_cost=0,
            points_on_bench=0,
        ),
        s.ApiHistoryEvent(
            event=2,
            points=1,
            total_points=2,
            bank=0,
            value=1000,
            event_transfers=1,
            event_transfers_cost=4,
            points_on_bench=0,
        ),
    ]
    rep = reconstruct_state(
        s.ApiEntry(id=1, name="x"),
        s.EntryHistory(current=current, chips=[]),
        lambda gw: _picks(_legal_outfield_order(base)),
        [],
        BOOT,
        RS,
        next_gameweek=3,
        as_of=T0,
    )
    assert any("GW2" in w and "disagree" in w for w in rep.warnings)
    assert rep.state.chip("triple_captain_1").chip_type is ChipType.TRIPLE_CAPTAIN
