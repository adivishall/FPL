"""Scenario and counterfactual engine (§25, §69).

Scenarios perturb the *inputs* of the forecast, never its outputs: minutes and availability
scenarios change start/appearance probabilities, team-attack and fixture scenarios change the
fixture-level goal expectations or remove a fixture, and the joint simulation is re-run with the
same seed (paired with the base forecast). Price shocks change purchase prices (affordability);
the conservative scenario shrinks volatile expectations toward the position baseline.

Every perturbation is explicit, parameterised and recorded with the result — a scenario is a
labelled what-if, not a prediction.
"""

from __future__ import annotations

from dataclasses import fields, replace
from typing import Literal

import numpy as np
import numpy.typing as npt
from pydantic import BaseModel, ConfigDict

from fpl_domain.forecast import FixtureParams, PlayerFixtureParams
from fpl_forecasting.pipeline import Forecast, resimulate
from fpl_optimizer.problem import PlayerTable

ScenarioKind = Literal[
    "expected",
    "minutes_downside",
    "minutes_upside",
    "injury_shock",
    "team_attack_downside",
    "fixture_shock",
    "price_shock",
    "conservative",
]
SIMULATION_KINDS = {
    "minutes_downside",
    "minutes_upside",
    "injury_shock",
    "team_attack_downside",
    "fixture_shock",
}


class ScenarioSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    kind: ScenarioKind
    players: tuple[int, ...] = ()
    teams: tuple[int, ...] = ()
    gameweeks: tuple[int, ...] = ()  # empty = every simulated gameweek
    magnitude: float = 0.5
    description: str = ""


def _rows(params: PlayerFixtureParams, keep: npt.NDArray[np.bool_]) -> PlayerFixtureParams:
    return PlayerFixtureParams(**{f.name: getattr(params, f.name)[keep] for f in fields(params)})


def _fx_rows(fx: FixtureParams, keep: npt.NDArray[np.bool_]) -> FixtureParams:
    return FixtureParams(**{f.name: getattr(fx, f.name)[keep] for f in fields(fx)})


def _gw_mask(gw: npt.NDArray[np.int64], gameweeks: tuple[int, ...]) -> npt.NDArray[np.bool_]:
    return np.ones(len(gw), dtype=bool) if not gameweeks else np.isin(gw, gameweeks)


def perturb_forecast(fc: Forecast, spec: ScenarioSpec) -> Forecast:
    """Apply a simulation-level scenario and re-run the paired simulation."""
    if spec.kind not in SIMULATION_KINDS:
        return fc
    pl, fx = fc.players, fc.fixtures
    sel = np.isin(pl.player_code, spec.players) & _gw_mask(pl.gameweek, spec.gameweeks)
    if spec.kind == "minutes_downside":
        p_start = np.where(sel, pl.p_start * (1.0 - spec.magnitude), pl.p_start)
        pl = replace(pl, p_start=p_start)
    elif spec.kind == "minutes_upside":
        p_start = np.where(sel, np.maximum(pl.p_start, spec.magnitude), pl.p_start)
        sb = pl.start_buckets.copy()
        sb[sel] = 0.0
        sb[sel, -1] = 1.0  # plays the full match when starting
        pl = replace(pl, p_start=p_start, start_buckets=sb)
    elif spec.kind == "injury_shock":
        pl = replace(pl, p_start=np.where(sel, 0.0, pl.p_start), p_sub=np.where(sel, 0.0, pl.p_sub))
    elif spec.kind == "team_attack_downside":
        g = _gw_mask(fx.gameweek, spec.gameweeks)
        f = 1.0 - spec.magnitude
        mu_h = np.where(g & np.isin(fx.home_team, spec.teams), fx.mu_home * f, fx.mu_home)
        mu_a = np.where(g & np.isin(fx.away_team, spec.teams), fx.mu_away * f, fx.mu_away)
        fx = replace(fx, mu_home=mu_h, mu_away=mu_a)
    elif spec.kind == "fixture_shock":  # postponement: the teams' fixtures in those GWs vanish
        gone = _gw_mask(fx.gameweek, spec.gameweeks) & (
            np.isin(fx.home_team, spec.teams) | np.isin(fx.away_team, spec.teams)
        )
        dropped = fx.fixture_id[gone]
        fx = _fx_rows(fx, ~gone)
        pl = _rows(pl, ~np.isin(pl.fixture_id, dropped))
    return resimulate(fc, pl, fx, label=spec.name)


def perturb_table(table: PlayerTable, spec: ScenarioSpec) -> PlayerTable:
    """Table-level scenarios: price shocks and conservative shrinkage."""
    if spec.kind == "price_shock":
        price = table.price.copy()
        sel = np.isin(table.code, spec.players)
        price[sel] = price[sel] + round(spec.magnitude)
        return replace(table, price=price)
    if spec.kind == "conservative":
        ev = table.ev.copy()
        for k in range(4):
            m = table.position == k
            if m.any():
                base = ev[m].mean(axis=0, keepdims=True)
                ev[m] = (1 - spec.magnitude) * ev[m] + spec.magnitude * base
        q10 = None if table.q10 is None else np.minimum(table.q10, ev)
        return replace(table, ev=ev, q10=q10)
    return table


def stress_set(
    move_out: tuple[int, ...],
    move_in: tuple[int, ...],
    captain: int | None,
    first_gw: int,
    in_teams: tuple[int, ...] = (),
) -> list[ScenarioSpec]:
    """The default stress tests for a recommendation (§69)."""
    specs = [ScenarioSpec(name="expected", kind="expected", description="Base forecast.")]
    if move_in:
        specs += [
            ScenarioSpec(
                name="incoming_minutes_downside",
                kind="minutes_downside",
                players=move_in,
                magnitude=0.5,
                description="Incoming player(s) lose half their start probability.",
            ),
            ScenarioSpec(
                name="incoming_injury_now",
                kind="injury_shock",
                players=move_in,
                gameweeks=(first_gw,),
                description="Incoming player(s) ruled out this gameweek.",
            ),
        ]
        if in_teams:
            specs.append(
                ScenarioSpec(
                    name="incoming_team_attack_downside",
                    kind="team_attack_downside",
                    teams=in_teams,
                    magnitude=0.2,
                    description="Incoming player's team scores 20 % less.",
                ),
            )
            specs.append(
                ScenarioSpec(
                    name="incoming_fixture_postponed",
                    kind="fixture_shock",
                    teams=in_teams,
                    gameweeks=(first_gw,),
                    description="Incoming player's next fixture is postponed.",
                ),
            )
        specs.append(
            ScenarioSpec(
                name="incoming_price_rise",
                kind="price_shock",
                players=move_in,
                magnitude=2,
                description="Incoming player(s) rise £0.2m before the deadline.",
            ),
        )
    if move_out:
        specs.append(
            ScenarioSpec(
                name="outgoing_minutes_upside",
                kind="minutes_upside",
                players=move_out,
                magnitude=0.95,
                description="The player you sell starts and plays 90 minutes.",
            ),
        )
    if captain is not None:
        specs.append(
            ScenarioSpec(
                name="captain_injury_now",
                kind="injury_shock",
                players=(captain,),
                gameweeks=(first_gw,),
                description="Captain ruled out this gameweek.",
            ),
        )
    specs.append(
        ScenarioSpec(
            name="conservative",
            kind="conservative",
            magnitude=0.3,
            description="Expectations shrunk 30 % toward the position average.",
        ),
    )
    return specs
