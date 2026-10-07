"""Decision engine end to end on real forecasts (§18, §26, §29, §68–§72)."""

from __future__ import annotations

from dataclasses import replace

import pytest

from fpl_decision.engine import DecisionContext, recommend
from fpl_decision.inputs import player_table
from fpl_decision.render import render_markdown
from fpl_decision.scenarios import ScenarioSpec, perturb_forecast
from fpl_domain.rules import load_ruleset
from fpl_domain.squad import SquadPick
from fpl_domain.state import ManagerState, initial_chips
from fpl_forecasting.minutes import MinutesConfig
from fpl_forecasting.pipeline import forecast, train_forecast_models
from fpl_forecasting.walkforward import FeatureCache, cutoffs
from fpl_optimizer.milp import build_and_solve
from fpl_optimizer.problem import OptimizationProblem, load_optimizer_config
from fpl_simulation.engine import SimulationConfig
from fpl_storage.pit import PointInTimeView
from tests.fixtures_util import fixture_dataset

DS = fixture_dataset("2024-25", "2025-26")
RS = load_ruleset("2025-26")
GWS = (4, 5)


@pytest.fixture(scope="module")
def base():  # type: ignore[no-untyped-def]
    hist = cutoffs(DS, ["2024-25", "2025-26"])
    cut = next(c for c in hist if c.season == "2025-26" and c.gw == 4)
    cache = FeatureCache(DS, horizon=2)
    models = train_forecast_models(
        cache, cut, hist, minutes_config=MinutesConfig(n_estimators=30, min_child_samples=30)
    )
    fc = forecast(cache, cut, models, SimulationConfig(n_sims=400))
    pool = PointInTimeView(DS, cut.cutoff).player_pool("2025-26", 4)
    table = player_table(fc.summary, pool, GWS)
    empty = ManagerState(
        season="2025-26", gameweek=4, squad=(), bank=1000, free_transfers=1, chips=initial_chips(RS)
    )
    cfg = load_optimizer_config("default")
    cfg = cfg.model_copy(
        update={"stability": cfg.stability.model_copy(update={"perturbations": 6})}
    )
    sq = build_and_solve(
        OptimizationProblem(
            state=empty,
            ruleset=RS,
            players=table,
            gameweeks=GWS,
            config=cfg,
            initial_squad_mode=True,
        )
    ).plans[0]
    idx = table.index()
    picks = tuple(
        SquadPick(
            player_code=c,
            position=table.positions_map()[c],
            team_code=int(table.team[idx[c]]),
            purchase_price=int(table.price[idx[c]]),
        )
        for c in sq.squad
    )
    state = empty.model_copy(update={"squad": picks, "bank": sq.bank_after})
    feats = cache.get(cut).frame
    return fc, pool, table, state, cfg, sq, feats


def _ctx(fc, pool, state, cfg, feats):  # type: ignore[no-untyped-def]
    return DecisionContext(
        state=state,
        ruleset=RS,
        forecast=fc,
        players=player_table(fc.summary, pool, GWS, state),
        config=cfg,
        gameweeks=GWS,
        features=feats,
    )


def test_healthy_optimal_squad_holds_with_reasons(base) -> None:  # type: ignore[no-untyped-def]
    fc, pool, _, state, cfg, _, feats = base
    pkg = recommend(_ctx(fc, pool, state, cfg, feats), n_alternatives=2, run_scenarios=False)
    assert pkg.decision["action"] == "HOLD"
    assert pkg.chosen.label == "hold"
    assert pkg.explanation.refusal_reasons  # explains why no move cleared the thresholds
    assert not any("[" in r for r in pkg.explanation.refusal_reasons)  # names, not code lists
    assert not any("[" in b for b in pkg.explanation.constraints_binding)
    assert pkg.alternatives  # alternatives are still shown


def test_injured_starter_triggers_a_justified_transfer(base) -> None:  # type: ignore[no-untyped-def]
    fc, pool, table, state, cfg, sq, feats = base
    idx = table.index()
    star = max(sq.lineup.starters, key=lambda c: table.ev[idx[c], 0])
    hurt = perturb_forecast(fc, ScenarioSpec(name="injury", kind="injury_shock", players=(star,)))
    ctx = _ctx(hurt, pool, state, cfg, feats)
    pkg = recommend(ctx, n_alternatives=2)
    d = pkg.decision
    assert d["action"] in ("TRANSFER", "HIT")
    assert star in d["transfers_out"]
    assert pkg.chosen.passes_thresholds and pkg.chosen.valid
    assert d["optimality"] == pkg.chosen.optimality == "proven optimal"
    assert pkg.chosen.gain_horizon.mean >= cfg.decision.min_gain
    assert d["p10"] <= d["p50"] <= d["p90"]
    # every driver is backed by an evidence object that exists in the package
    ids = {e.evidence_id for e in pkg.evidence}
    assert pkg.explanation.primary_drivers
    for drv in pkg.explanation.primary_drivers:
        assert set(drv["evidence_ids"]) <= ids
    assert set(pkg.explanation.supporting_evidence) == ids
    # HOLD counterfactual, timeline, scenarios, stability, reproducibility ids
    assert pkg.explanation.hold_counterfactual["timeline"]
    assert [s.gameweek for s in pkg.chosen.timeline] == list(GWS)
    names = {s.name for s in pkg.scenarios}
    assert {"expected", "incoming_minutes_downside", "incoming_injury_now", "conservative"} <= names
    assert pkg.stability is not None and pkg.stability.label in ("stable", "fragile")
    assert pkg.optimizer_run_id.startswith("opt_") and pkg.decision_id.startswith("dec_")
    assert pkg.captaincy is not None and pkg.captaincy.expected in pkg.lineup["starters"]
    # chip planner: every available chip valued with a recommendation and a reason
    assert {c.chip_id for c in pkg.chips} >= {"bench_boost_1", "triple_captain_1"}
    for c in pkg.chips:
        assert c.recommendation in ("play now", "wait", "keep") and c.reason
        for w in c.by_gameweek:
            assert (
                w.uplift_p10 <= w.uplift_mean <= w.uplift_p90 + 1e-9 or w.uplift_p10 <= w.uplift_p90
            )
    tc = next(c for c in pkg.chips if c.chip_id == "triple_captain_1")
    assert all(w.uplift_mean >= 0 for w in tc.by_gameweek)  # TC can only add points
    # deterministic: same inputs → same decision id
    again = recommend(ctx, n_alternatives=2, run_scenarios=False, run_stability=False)
    assert again.decision_id == pkg.decision_id
    # §68.2 sections rendered from the package only
    md = render_markdown(pkg)
    for section in (
        "## Decision",
        "## Expected effect",
        "## Why",
        "## What could go wrong",
        "## If you do nothing",
        "## Alternatives",
        "## Assumptions",
        "## Reproducibility",
    ):
        assert section in md
    assert pkg.decision_id in md and pkg.explanation.primary_drivers[0]["evidence_ids"][0] in md
    assert "Optimiser: the chosen plan is proven optimal." in md
    # the package serialises (API contract)
    js = pkg.model_dump(mode="json")
    assert js["decision"]["action"] == d["action"]


def test_parallel_solves_change_nothing(base) -> None:  # type: ignore[no-untyped-def]
    # stability perturbations and chip weeks solved by worker processes: the same package
    fc, pool, table, state, cfg, sq, feats = base
    idx = table.index()
    star = max(sq.lineup.starters, key=lambda c: table.ev[idx[c], 0])
    hurt = perturb_forecast(fc, ScenarioSpec(name="injury", kind="injury_shock", players=(star,)))
    ctx = _ctx(hurt, pool, state, cfg, feats)
    serial = recommend(ctx, n_alternatives=2, run_scenarios=False)
    parallel = recommend(replace(ctx, workers=3), n_alternatives=2, run_scenarios=False)
    assert serial.stability is not None and serial.chips
    volatile = {"timings", "generated_at"}
    assert parallel.model_dump(exclude=volatile) == serial.model_dump(exclude=volatile)


def test_profiles_change_thresholds_not_constraints(base) -> None:  # type: ignore[no-untyped-def]
    fc, pool, _, state, _, _, feats = base
    cons = load_optimizer_config("conservative")
    cons = cons.model_copy(
        update={"stability": cons.stability.model_copy(update={"perturbations": 3})}
    )
    pkg = recommend(_ctx(fc, pool, state, cons, feats), n_alternatives=1, run_scenarios=False)
    assert pkg.config_refs["optimizer"].startswith("optimizer/conservative@")
    assert any("E[gain] ≥ 1.0 pts" in a and "≥ 0.65" in a for a in pkg.assumptions)
