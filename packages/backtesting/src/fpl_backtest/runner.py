"""Walk-forward season replay (§27, §70; protocol in docs/BACKTEST_PROTOCOL.md).

For each decision gameweek t of a season:

1. **Freeze information** at the decision cutoff (deadline − buffer): every input is read through
   ``PointInTimeView`` and the forecast's provenance must satisfy max_source_available_at ≤ cutoff
   (asserted; any violation aborts the backtest).
2. **Forecast** with models trained only on data available at a training cutoff ≤ t (retrained
   every ``retrain_every`` gameweeks), and derive the point baselines' predictions from the same
   feature rows.
3. **Decide**: each strategy maps (its own manager state, the information at t) to a gameweek
   decision; strategies differ only in forecast and/or decision policy, never in information.
4. **Apply** the decision through the domain state machine with the prices at the cutoff (an
   illegal decision is recorded and replaced by HOLD — it counts against the validity rate).
5. **Score** with the *actual* outcomes of gameweek t (official points, minutes → automatic
   substitutions, captain / vice, chips) minus hits; record diagnostics; **advance** the state.

All strategies start from the same initial squad (chosen at the GW1 cutoff by the initial-squad
optimiser on the engine's forecast), so differences come from weekly decisions.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Protocol

import pandas as pd

from fpl_decision.chips import plan_chips
from fpl_decision.engine import DecisionContext, recommend
from fpl_decision.inputs import player_table
from fpl_domain.enums import POSITIONS, ChipType, Position
from fpl_domain.errors import RuleViolation
from fpl_domain.rules import Ruleset, load_ruleset
from fpl_domain.squad import Lineup, SquadPick, gameweek_points
from fpl_domain.state import (
    GameweekDecision,
    ManagerState,
    Transfer,
    advance,
    apply_deadline,
    initial_chips,
)
from fpl_features.labels import realized_player_fixture
from fpl_forecasting.baselines import all_baselines
from fpl_forecasting.pipeline import Forecast, ForecastModels, forecast, train_forecast_models
from fpl_forecasting.walkforward import Cutoff, FeatureCache, cutoffs, training_table
from fpl_optimizer.lineup import best_lineup
from fpl_optimizer.milp import OptimizationError, Solution, build_and_solve
from fpl_optimizer.pool import candidate_pool
from fpl_optimizer.problem import (
    OptimizationProblem,
    OptimizerConfig,
    PlayerTable,
)
from fpl_simulation.engine import SimulationConfig
from fpl_storage.dataset import CanonicalDataset
from fpl_storage.pit import PointInTimeView

# ----------------------------------------------------------------------------- information


@dataclass
class GwInfo:
    """Everything known at one decision cutoff (shared by all strategies)."""

    season: str
    gw: int
    cutoff: pd.Timestamp
    ruleset: Ruleset
    forecast: Forecast
    pool: pd.DataFrame
    prices: dict[int, int]
    positions: dict[int, Position]
    teams: dict[int, int]
    features: pd.DataFrame
    tables: dict[str, PlayerTable]  # forecast name → table over the planning horizon
    gameweeks: tuple[int, ...]
    max_source_available_at: pd.Timestamp | None


def _registry(ds: CanonicalDataset, season: str) -> tuple[dict[int, Position], dict[int, int]]:
    p = ds["players"]
    p = p[p["season"] == season]
    pos = {int(c): Position(v) for c, v in zip(p["player_code"], p["position"], strict=True)}
    team = {int(c): int(t) for c, t in zip(p["player_code"], p["team_code"], strict=True)}
    return pos, team


def gw_info(
    cache: FeatureCache,
    cut: Cutoff,
    models: ForecastModels,
    history: Sequence[Cutoff],
    horizon: int,
    sim: SimulationConfig,
    owned_codes: Sequence[int],
) -> GwInfo:
    ds = cache.ds
    view = PointInTimeView(ds, cut.cutoff)
    fc = forecast(cache, cut, models, sim)
    max_av = fc.provenance.get("max_source_available_at")
    if max_av not in (None, "None") and pd.Timestamp(max_av) > cut.cutoff:
        raise AssertionError(f"lookahead: source data {max_av} after cutoff {cut.cutoff}")
    pool = view.player_pool(cut.season, cut.gw)
    pos, team = _registry(ds, cut.season)
    # the point-in-time pool is authoritative for team (mid-season moves) and position
    for c, t, p in zip(pool["player_code"], pool["team_code"], pool["position"], strict=True):
        if pd.notna(t):
            team[int(c)] = int(t)
        if isinstance(p, str):
            pos[int(c)] = Position(p)
    # owned players missing from the pool (e.g. left the league) keep their last known price
    prices = {
        int(c): int(p)
        for c, p in zip(pool["player_code"], pool["price"], strict=True)
        if pd.notna(p)
    }
    obs = view.price_observations(cut.season).sort_values("observed_at")
    last = obs.groupby("player_code")["price"].last()
    extra = []
    for c in owned_codes:
        if c not in prices and c in last.index:
            prices[c] = int(last[c])
            extra.append(
                {
                    "player_code": c,
                    "team_code": team.get(c),
                    "position": pos[c].value,
                    "price": prices[c],
                }
            )
    if extra:
        pool = pd.concat([pool, pd.DataFrame(extra)], ignore_index=True)
    gws = tuple(g for g in range(cut.gw, cut.gw + horizon) if g <= 38)
    ff = cache.get(cut).frame
    tables = {"mc": player_table(fc.summary, pool, gws)}
    train = training_table(cache, cut, history)
    keys = ff[["player_code", "target_gw"]]
    for b in all_baselines():
        b.fit(train)
        pred = keys.assign(mean=b.predict(ff).to_numpy(float), p10=0.0).rename(
            columns={"target_gw": "gw"}
        )
        summ = pred.groupby(["player_code", "gw"], as_index=False)[["mean", "p10"]].sum()
        tables[b.name] = player_table(summ, pool, gws)
    return GwInfo(
        season=cut.season,
        gw=cut.gw,
        cutoff=cut.cutoff,
        ruleset=load_ruleset(cut.season),
        forecast=fc,
        pool=pool,
        prices=prices,
        positions=pos,
        teams=team,
        features=ff,
        tables=tables,
        gameweeks=gws,
        max_source_available_at=None if max_av in (None, "None") else pd.Timestamp(max_av),
    )


# ----------------------------------------------------------------------------- strategies


@dataclass
class StrategyOutput:
    decision: GameweekDecision
    expected_points: float | None = None  # strategy's own expectation for this GW (if any)
    planned_next: tuple[tuple[int, ...], tuple[int, ...]] | None = None  # (outs, ins) for t+1
    notes: list[str] = field(default_factory=list)


class Strategy(Protocol):
    name: str

    def decide(self, state: ManagerState, info: GwInfo) -> StrategyOutput: ...


def _transfers(outs: Sequence[int], ins: Sequence[int], info: GwInfo) -> tuple[Transfer, ...]:
    o = sorted(outs, key=lambda c: (POSITIONS.index(info.positions[c]), c))
    i = sorted(ins, key=lambda c: (POSITIONS.index(info.positions[c]), c))
    return tuple(
        Transfer(
            out_code=a,
            in_code=b,
            in_position=info.positions[b],
            in_team_code=info.teams[b],
            in_price=info.prices[b],
        )
        for a, b in zip(o, i, strict=True)
    )


def _lineup_for(
    codes: Sequence[int],
    table: PlayerTable,
    info: GwInfo,
    cfg: OptimizerConfig,
    chip: ChipType | None = None,
) -> Lineup:
    idx = table.index()
    ev = {c: float(table.ev[idx[c], 0]) if c in idx else 0.0 for c in codes}
    return best_lineup(
        list(codes), info.positions, ev, ev, cfg.objective, info.ruleset, chip
    ).lineup


def _problem(
    state: ManagerState, info: GwInfo, table: PlayerTable, cfg: OptimizerConfig, horizon: int
) -> OptimizationProblem:
    h = min(horizon, len(info.gameweeks))
    t = replace(
        table, ev=table.ev[:, :h].copy(), q10=None if table.q10 is None else table.q10[:, :h].copy()
    )
    pool = candidate_pool(t, state.codes, cfg.pool, discount=cfg.objective.discount)
    return OptimizationProblem(
        state=state, ruleset=info.ruleset, players=pool, gameweeks=info.gameweeks[:h], config=cfg
    )


def _from_solution(sol: Solution, info: GwInfo) -> GameweekDecision:
    f = sol.first
    return GameweekDecision(
        transfers=_transfers(f.transfers_out, f.transfers_in, info),
        chip_id=f.chip_id,
        lineup=f.lineup,
    )


@dataclass
class OptimizerStrategy:
    """Forecast table × MILP over a horizon (no paired-gain thresholds)."""

    name: str
    forecast_name: str
    horizon: int
    config: OptimizerConfig

    def decide(self, state: ManagerState, info: GwInfo) -> StrategyOutput:
        prob = _problem(state, info, info.tables[self.forecast_name], self.config, self.horizon)
        sol = build_and_solve(prob)
        nxt = None
        if len(sol.plans) > 1:
            nxt = (sol.plans[1].transfers_out, sol.plans[1].transfers_in)
        return StrategyOutput(_from_solution(sol, info), sol.plans[0].expected_points, nxt)


@dataclass
class HoldStrategy:
    """Never transfers; picks XI / captain from the forecast each week."""

    name: str
    config: OptimizerConfig
    forecast_name: str = "mc"

    def decide(self, state: ManagerState, info: GwInfo) -> StrategyOutput:
        lu = _lineup_for(state.codes, info.tables[self.forecast_name], info, self.config)
        return StrategyOutput(GameweekDecision(lineup=lu))


@dataclass
class EngineStrategy:
    """The full decision engine: paired thresholds vs HOLD, optional chip planner."""

    name: str
    config: OptimizerConfig
    use_chips: bool = True
    n_alternatives: int = 2

    def decide(self, state: ManagerState, info: GwInfo) -> StrategyOutput:
        ctx = DecisionContext(
            state=state,
            ruleset=info.ruleset,
            forecast=info.forecast,
            players=info.tables["mc"],
            config=self.config,
            gameweeks=info.gameweeks,
            features=info.features,
        )
        pkg = recommend(
            ctx,
            n_alternatives=self.n_alternatives,
            run_stability=False,
            run_scenarios=False,
            run_chips=False,
        )
        chosen = pkg.chosen
        steps = chosen.timeline
        nxt = (
            (tuple(steps[1].transfers_out), tuple(steps[1].transfers_in))
            if len(steps) > 1
            else None
        )
        lineup = Lineup(
            starters=tuple(pkg.lineup["starters"]),
            bench=tuple(pkg.lineup["bench"]),
            captain=pkg.lineup["captain"],
            vice_captain=pkg.lineup["vice_captain"],
        )
        decision = GameweekDecision(
            transfers=_transfers(chosen.sells, chosen.buys, info),
            chip_id=chosen.chip,
            lineup=lineup,
        )
        notes = [f"action {pkg.decision['action']}"]
        if self.use_chips and chosen.chip is None:
            prob = _problem(state, info, info.tables["mc"], self.config, len(info.gameweeks))
            base = _solution_for(prob, chosen.sells, chosen.buys)
            plans = plan_chips(
                prob, base, info.forecast, self.config.decision.min_prob_positive, sensitivity=False
            )
            now = [c for c in plans if c.recommendation == "play now"]
            if now:
                pick = max(now, key=lambda c: c.value_now or 0.0)
                notes.append(f"chip {pick.chip_id}: {pick.reason}")
                ctype = ChipType(pick.chip_type)
                if ctype in (ChipType.WILDCARD, ChipType.FREE_HIT):
                    p2 = replace(
                        prob,
                        forced_chips={pick.chip_id: info.gw},
                        chip_options={pick.chip_id: (info.gw,)},
                    )
                    decision = _from_solution(build_and_solve(p2), info)
                else:
                    lu = _lineup_for(
                        base.first.playing_squad, info.tables["mc"], info, self.config, ctype
                    )
                    decision = GameweekDecision(
                        transfers=decision.transfers, chip_id=pick.chip_id, lineup=lu
                    )
        return StrategyOutput(decision, pkg.decision["expected_points"], nxt, notes)


def _solution_for(prob: OptimizationProblem, sells: Sequence[int], buys: Sequence[int]) -> Solution:
    hold = not sells and not buys
    pref = replace(
        prob.preferences,
        hold_first_gw=hold,
        forced_out=frozenset() if hold else frozenset(sells),
        forced_in=frozenset() if hold else frozenset(buys),
    )
    return build_and_solve(replace(prob, preferences=pref))


# ----------------------------------------------------------------------------- replay


@dataclass
class GwRecord:
    strategy: str
    season: str
    gw: int
    points: int  # after hits
    raw_points: int
    hit_points: int
    transfers: int
    chip: str | None
    captain: int
    vice_captain: int
    starters: list[int]
    bench: list[int]
    captain_points: int
    bench_points: int
    hindsight_lineup_points: int  # best lineup of the playing squad with actual outcomes
    expected_points: float | None
    valid: bool
    runtime_s: float
    data_age_hours: float | None
    transfers_out: list[int]
    transfers_in: list[int]
    planned_next: list[list[int]] | None
    notes: list[str]


def _actual(ds: CanonicalDataset, season: str, gw: int) -> tuple[dict[int, int], dict[int, int]]:
    pf = realized_player_fixture(ds, season, [gw])
    g = pf.groupby("player_code")[["points", "minutes"]].sum()
    return (g["points"].astype(int).to_dict(), g["minutes"].astype(int).to_dict())


def _hindsight_best(
    codes: Sequence[int],
    positions: dict[int, Position],
    points: dict[int, int],
    minutes: dict[int, int],
    rs: Ruleset,
    cfg: OptimizerConfig,
    chip: ChipType | None,
) -> int:
    ev = {c: float(points.get(c, 0)) for c in codes}
    lu = best_lineup(list(codes), positions, ev, ev, cfg.objective, rs, chip).lineup
    return _points(
        gameweek_points(lu, positions, points, minutes, rs, chip.value if chip else None)
    )


def _points(result: dict[str, object]) -> int:
    v = result["points"]
    assert isinstance(v, int)
    return v


def initial_squad(info: GwInfo, cfg: OptimizerConfig, budget: int = 1000) -> ManagerState:
    empty = ManagerState(
        season=info.season,
        gameweek=info.gw,
        squad=(),
        bank=budget,
        free_transfers=0,
        chips=initial_chips(info.ruleset),
    )
    t = info.tables["mc"]
    prob = OptimizationProblem(
        state=empty,
        ruleset=info.ruleset,
        players=t,
        gameweeks=info.gameweeks,
        config=cfg,
        initial_squad_mode=True,
    )
    plan = build_and_solve(prob).plans[0]
    picks = tuple(
        SquadPick(
            player_code=c,
            position=info.positions[c],
            team_code=info.teams[c],
            purchase_price=info.prices[c],
        )
        for c in plan.squad
    )
    return empty.model_copy(update={"squad": picks, "bank": plan.bank_after, "lineup": plan.lineup})


def run_season(
    ds: CanonicalDataset,
    season: str,
    strategies: Sequence[Strategy],
    history_seasons: Sequence[str],
    horizon: int = 5,
    retrain_every: int = 4,
    sim: SimulationConfig | None = None,
    gameweeks: Sequence[int] | None = None,
    cache_root: Any = None,
    progress: Callable[[str], None] | None = None,
    initial_cfg: OptimizerConfig | None = None,
) -> pd.DataFrame:
    """Replay ``season`` for every strategy; one record per strategy × gameweek."""
    sim = sim or SimulationConfig(n_sims=1000)
    cache = FeatureCache(ds, horizon, cache_root)
    hist = cutoffs(ds, [*history_seasons, season])
    season_cuts = [c for c in hist if c.season == season]
    if gameweeks is not None:
        season_cuts = [c for c in season_cuts if c.gw in set(gameweeks)]
    states: dict[str, ManagerState] = {}
    records: list[GwRecord] = []
    models: ForecastModels | None = None
    trained_gw = -99
    for cut in season_cuts:
        if models is None or cut.gw - trained_gw >= retrain_every:
            models = train_forecast_models(cache, cut, hist)
            trained_gw = cut.gw
        owned = sorted({c for s in states.values() for c in s.codes})
        info = gw_info(cache, cut, models, hist, horizon, sim, owned)
        if not states:
            start = initial_squad(info, initial_cfg or OptimizerConfig())
            states = {s.name: start for s in strategies}
        pts_map, min_map = _actual(ds, season, cut.gw)
        for strat in strategies:
            state = states[strat.name]
            t0 = time.perf_counter()
            notes: list[str] = []
            try:
                out = strat.decide(state, info)
                res = apply_deadline(state, out.decision, info.prices, info.ruleset)
                valid = True
            except (RuleViolation, OptimizationError) as exc:
                notes.append(f"invalid decision replaced by HOLD: {exc}")
                cfg = getattr(strat, "config", OptimizerConfig())
                lu = _lineup_for(state.codes, info.tables["mc"], info, cfg)
                out = StrategyOutput(GameweekDecision(lineup=lu))
                res = apply_deadline(state, out.decision, info.prices, info.ruleset)
                valid = False
            runtime = time.perf_counter() - t0
            chip_t = res.chip.chip_type if res.chip else None
            lu_opt = res.lineup or out.decision.lineup
            assert lu_opt is not None
            lu = lu_opt
            sc = gameweek_points(
                lu, info.positions, pts_map, min_map, info.ruleset, chip_t.value if chip_t else None
            )
            raw = _points(sc)
            playing = tuple(p.player_code for p in res.playing_squad)
            best = _hindsight_best(
                playing,
                info.positions,
                pts_map,
                min_map,
                info.ruleset,
                getattr(strat, "config", OptimizerConfig()),
                chip_t,
            )
            age = None
            if info.max_source_available_at is not None:
                age = (info.cutoff - info.max_source_available_at).total_seconds() / 3600
            records.append(
                GwRecord(
                    strategy=strat.name,
                    season=season,
                    gw=cut.gw,
                    points=raw - res.hit_points,
                    raw_points=raw,
                    hit_points=res.hit_points,
                    transfers=len(res.transfers),
                    chip=res.chip.chip_id if res.chip else None,
                    captain=lu.captain,
                    vice_captain=lu.vice_captain,
                    starters=list(lu.starters),
                    bench=list(lu.bench),
                    captain_points=pts_map.get(lu.captain, 0),
                    bench_points=sum(pts_map.get(c, 0) for c in lu.bench),
                    hindsight_lineup_points=best,
                    expected_points=out.expected_points,
                    valid=valid,
                    runtime_s=runtime,
                    data_age_hours=age,
                    transfers_out=[t.out_code for t in res.transfers],
                    transfers_in=[t.in_code for t in res.transfers],
                    planned_next=None
                    if out.planned_next is None
                    else [list(out.planned_next[0]), list(out.planned_next[1])],
                    notes=notes + out.notes,
                )
            )
            if cut.gw < info.ruleset.num_gameweeks:
                states[strat.name] = advance(res, info.ruleset)
        if progress:
            last = {r.strategy: r.points for r in records if r.gw == cut.gw}
            progress(f"{season} GW{cut.gw}: {last}")
    return pd.DataFrame([r.__dict__ for r in records])


def default_strategies(cfg: OptimizerConfig, horizon: int = 5) -> list[Strategy]:
    return [
        EngineStrategy("engine", cfg, use_chips=True),
        EngineStrategy("engine_no_chips", cfg, use_chips=False),
        OptimizerStrategy("single_gw_mc", "mc", 1, cfg),
        OptimizerStrategy("simple_xp", "ppg_availability", 1, cfg),
        OptimizerStrategy("form", "recent_form", 1, cfg),
        OptimizerStrategy("fpl_style_heuristic", "fpl_style", 1, cfg),
        HoldStrategy("hold", cfg),
    ]
