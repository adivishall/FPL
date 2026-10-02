"""Alert rules (§74): materiality thresholds, de-duplication keys and exact price-risk maths."""

from __future__ import annotations

import itertools
from datetime import UTC, datetime, timedelta

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from fpl_domain.rules.model import SellingPriceRule
from fpl_domain.squad import selling_price
from fpl_notifications.rules import (
    DeadlineCfg,
    FixtureChangeCfg,
    InvalidationCfg,
    PlayerOutcome,
    PlayerStatus,
    PostGameweekCfg,
    PriceRiskCfg,
    Sale,
    SquadChangeCfg,
    deadline_reminder,
    fixture_changes,
    load_notification_config,
    post_gameweek_report,
    price_risk,
    recommendation_invalidation,
    squad_changes,
)

DL = datetime(2026, 9, 12, 10, 0, tzinfo=UTC)  # 11:00 BST


def test_config_loads_versioned() -> None:
    cfg = load_notification_config()
    assert cfg.config_ref.startswith("notifications/default@")
    assert cfg.deadline.lead_hours == (24.0, 2.0)


# ----------------------------------------------------------------------------- deadline


def test_deadline_windows_and_local_time() -> None:
    cfg = DeadlineCfg()
    assert deadline_reminder("2026-27", 4, DL, DL - timedelta(hours=30), cfg) == []
    a = deadline_reminder("2026-27", 4, DL, DL - timedelta(hours=20), cfg)
    assert len(a) == 1 and a[0].dedupe_key == "deadline:2026-27:4:24h"
    assert "11:00 BST" in a[0].body and a[0].severity == "info"
    b = deadline_reminder("2026-27", 4, DL, DL - timedelta(minutes=90), cfg, "Asia/Kolkata")
    assert b[0].dedupe_key == "deadline:2026-27:4:2h" and b[0].severity == "warning"
    assert "15:30 IST" in b[0].body  # the same official deadline in the manager's zone
    assert deadline_reminder("2026-27", 4, DL, DL + timedelta(minutes=1), cfg) == []
    with pytest.raises(ValueError, match="timezone-aware"):
        deadline_reminder("2026-27", 4, DL.replace(tzinfo=None), DL, cfg)


# ----------------------------------------------------------------------------- squad


def _st(**kw: object) -> PlayerStatus:
    base = {"start_prob": 0.9, "xp_next": 5.0, "xp": 20.0}
    return PlayerStatus(**{**base, **kw})  # type: ignore[arg-type]


def test_injury_is_material_and_dedupes_by_state() -> None:
    cfg = SquadChangeCfg()
    before = {1: _st(), 2: _st(xp_next=0.5, xp=0.8)}
    after = {
        1: _st(status="i", chance=0.0, news="Hamstring", xp_next=0.0, xp=6.0),
        2: _st(status="i", chance=0.0, xp_next=0.0, xp=0.0),  # ≤ 0.8 pts at stake: immaterial
    }
    a = squad_changes("s", 5, [1, 2], [1], before, after, cfg, {1: "Saka"})
    assert [x.evidence["player_code"] for x in a] == [1]
    assert a[0].severity == "critical" and "Hamstring" in a[0].body
    assert a[0].materiality == pytest.approx(14.0)  # max(1.0 × 5.0, 20 − 6)
    again = squad_changes("s", 5, [1, 2], [1], before, after, cfg)
    assert again[0].dedupe_key == a[0].dedupe_key
    worse = squad_changes(
        "s", 5, [1], [1], before, {1: _st(status="d", chance=50.0, xp_next=2.5, xp=10.0)}, cfg
    )
    assert worse[0].dedupe_key != a[0].dedupe_key and worse[0].severity == "warning"


def test_small_fluctuations_never_alert() -> None:
    cfg = SquadChangeCfg()
    before = {1: _st(chance=100.0)}
    after = {1: _st(status="d", chance=75.0, start_prob=0.8)}  # 25-pt drop but tiny xP change
    a = squad_changes("s", 5, [1], [1], before, {1: _st(start_prob=0.8)}, cfg)
    assert a == []
    b = squad_changes("s", 5, [1], [1], before, after, cfg)
    assert len(b) == 1 and b[0].materiality == pytest.approx(1.25)
    tight = cfg.model_copy(update={"min_points_impact": 2.0})
    assert squad_changes("s", 5, [1], [1], before, after, tight) == []


def test_role_change_and_unexpected_benching() -> None:
    cfg = SquadChangeCfg()
    a = squad_changes("s", 5, [1], [1], {1: _st()}, {1: _st(start_prob=0.4)}, cfg)
    assert a[0].evidence["subtype"] == "role_change" and a[0].severity == "warning"
    assert a[0].materiality == pytest.approx(0.5 * 5.0 / 0.9, rel=1e-3)
    benched = _st(recent_starts=(True, True, True, False))
    b = squad_changes("s", 5, [1], [1], {1: _st()}, {1: benched}, cfg)
    assert b[0].evidence["subtype"] == "unexpected_benching"
    rotated = _st(recent_starts=(True, False, True, False))  # rotation, not a surprise
    assert squad_changes("s", 5, [1], [1], {1: _st()}, {1: rotated}, cfg) == []


# ----------------------------------------------------------------------------- price risk


def _brute_force(buys, sales, bank, rise, fall):  # type: ignore[no-untyped-def]
    outcomes = []
    for c in buys:
        outcomes.append([(1, rise[c]), (-1, fall[c]), (0, 1 - rise[c] - fall[c])])
    for s in sales:
        r, f = rise[s.player_code], fall[s.player_code]
        sp = selling_price(s.purchase_price, s.current_price, SellingPriceRule.HALF_PROFIT_FLOOR)
        lo = selling_price(
            s.purchase_price, s.current_price - 1, SellingPriceRule.HALF_PROFIT_FLOOR
        )
        hi = selling_price(
            s.purchase_price, s.current_price + 1, SellingPriceRule.HALF_PROFIT_FLOOR
        )
        outcomes.append([(sp - lo, f), (sp - hi, r), (0, 1 - r - f)])
    p = 0.0
    for combo in itertools.product(*outcomes):
        if sum(d for d, _ in combo) > bank:
            q = 1.0
            for _, pr in combo:
                q *= pr
            p += q
    return p


@settings(max_examples=60, deadline=None)
@given(
    rise=st.lists(st.floats(0, 0.6), min_size=4, max_size=4),
    fall=st.lists(st.floats(0, 0.4), min_size=4, max_size=4),
    bank=st.integers(0, 2),
    purchase=st.integers(40, 80),
    delta=st.integers(-3, 5),
)
def test_price_risk_matches_brute_force(rise, fall, bank, purchase, delta) -> None:  # type: ignore[no-untyped-def]
    r = dict(enumerate(rise))
    f = dict(enumerate(fall))
    sales = [Sale(2, purchase, purchase + delta), Sale(3, purchase + 5, purchase + 5)]
    expected = _brute_force([0, 1], sales, bank, r, f)
    out = price_risk("s", 3, {0: 60, 1: 70}, sales, bank, r, f, PriceRiskCfg(min_probability=0.0))
    assert out[0].evidence["p_unaffordable"] == pytest.approx(expected, abs=1e-12)


def test_price_risk_threshold() -> None:
    r, f = {7: 0.7}, {7: 0.0}
    hi = price_risk("s", 3, {7: 80}, [], 0, r, f, PriceRiskCfg(), names={7: "Palmer"})
    assert hi and hi[0].severity == "critical" and "Palmer 70%" in hi[0].body
    assert price_risk("s", 3, {7: 80}, [], 1, r, f, PriceRiskCfg()) == []  # buffer covers it
    assert price_risk("s", 3, {}, [], 0, r, f, PriceRiskCfg()) == []


# ----------------------------------------------------------------------------- fixtures


def test_fixture_change_materiality() -> None:
    team_of = {1: 10, 2: 10, 3: 20}
    before = {(10, 7): 1, (20, 7): 1, (10, 8): 1}
    after = {(10, 7): 0, (20, 7): 1, (10, 8): 2}  # postponed into a double
    xpm = {1: 5.0, 2: 3.0, 3: 4.0}
    a = fixture_changes(
        "s", "rec_x", [7, 8], [1, 2, 3], team_of, before, after, xpm, FixtureChangeCfg()
    )
    assert len(a) == 1 and a[0].materiality == pytest.approx(16.0)
    assert {c["gameweek"] for c in a[0].evidence["changes"]} == {7, 8}
    assert (
        fixture_changes("s", "rec_x", [7, 8], [3], team_of, before, after, xpm, FixtureChangeCfg())
        == []
    )


# ----------------------------------------------------------------------------- invalidation


def test_invalidation_threshold_and_weak_move() -> None:
    cfg = InvalidationCfg()
    assert recommendation_invalidation("s", 3, "rec_1", "TRANSFER", 2.0, 0.7, 3.0, "x", cfg) == []
    a = recommendation_invalidation("s", 3, "rec_1", "TRANSFER", 1.0, 0.7, 3.5, "hold", cfg)
    assert a[0].dedupe_key == "invalid:rec_1" and a[0].materiality == pytest.approx(2.5)
    weak = recommendation_invalidation("s", 3, "rec_1", "TRANSFER", -0.4, 0.42, 0.0, "hold", cfg)
    assert weak and "42%" in weak[0].body
    hold = recommendation_invalidation("s", 3, "rec_1", "HOLD", 0.0, 0.0, 1.0, "x", cfg)
    assert hold == []


# ----------------------------------------------------------------------------- post-GW


def test_post_gameweek_explains_misses() -> None:
    outs = [
        PlayerOutcome(1, True, 2, 7.0, 1.0, 15.0, 0.95, 2, 90, {"goals": 0}),
        PlayerOutcome(2, True, 1, 4.0, 1.0, 9.0, 0.9, 0, 0),
        PlayerOutcome(3, True, 1, 3.5, 1.0, 8.0, 0.9, 15, 90, {"goals": 2, "bonus": 3}),
        PlayerOutcome(4, False, 0, 1.0, 0.0, 2.0, 0.3, 1, 10),
    ]
    a = post_gameweek_report("s", 6, outs, 55.0, 38.0, 74.0, 49, PostGameweekCfg(), {1: "Haaland"})
    assert a[0].dedupe_key == "postgw:s:6" and "inside" in a[0].body
    assert "Haaland (C)" in a[0].body and "did not play (forecast P(play) 90%)" in a[0].body
    assert "hauled 15" in a[0].body and "goals 2" in a[0].body
    assert "4:" not in a[0].body  # an immaterial bench miss is not listed
