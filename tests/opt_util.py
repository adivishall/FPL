"""Synthetic tiny leagues for optimiser tests (test helper)."""

from __future__ import annotations

import numpy as np

from fpl_domain.enums import POSITIONS
from fpl_domain.rules import load_ruleset
from fpl_domain.squad import SquadPick
from fpl_domain.state import ManagerState, initial_chips
from fpl_optimizer.problem import (
    ObjectiveWeights,
    OptimizationProblem,
    OptimizerConfig,
    PlayerTable,
    Preferences,
)

QUOTA = (2, 5, 5, 3)


def tiny_league(
    seed: int,
    extras: tuple[int, int, int, int] = (1, 1, 1, 1),
    gameweek: int = 10,
    horizon: int = 2,
    season: str = "2026-27",
    free_transfers: int = 1,
    bank: int | None = None,
    weights: ObjectiveWeights | None = None,
    chip_options: dict[str, tuple[int, ...]] | None = None,
    forced_chips: dict[str, int] | None = None,
    preferences: Preferences | None = None,
    n_teams: int = 8,
) -> OptimizationProblem:
    rng = np.random.default_rng(seed)
    rs = load_ruleset(season)
    codes, pos, team, price, owned_flags = [], [], [], [], []
    code = 100
    slot = 0
    for k, (q, e) in enumerate(zip(QUOTA, extras, strict=True)):
        for j in range(q + e):
            codes.append(code)
            code += 1
            pos.append(k)
            if j < q:  # owned: spread over clubs (≤ 2 per club)
                team.append(1 + slot % n_teams)
                slot += 1
            else:
                team.append(int(rng.integers(1, n_teams + 1)))
            price.append(int(rng.integers(40, 110)))
            owned_flags.append(j < q)
    n = len(codes)
    ev = rng.gamma(2.0, 1.6, (n, horizon))
    ev[rng.random((n, horizon)) < 0.1] = 0.0  # some blanks / injuries
    q10 = np.clip(ev - rng.gamma(2.0, 1.0, (n, horizon)), 0.0, None)
    table = PlayerTable(
        code=np.array(codes, dtype=np.int64),
        position=np.array(pos, dtype=np.int64),
        team=np.array(team, dtype=np.int64),
        price=np.array(price, dtype=np.int64),
        ev=ev,
        q10=q10,
    )
    picks = []
    for c, p, t, pr, o in zip(codes, pos, team, price, owned_flags, strict=True):
        if o:
            purchase = int(np.clip(pr + rng.integers(-6, 7), 39, 130))
            picks.append(
                SquadPick(
                    player_code=c, position=POSITIONS[p], team_code=t, purchase_price=purchase
                )
            )
    state = ManagerState(
        season=season,
        gameweek=gameweek,
        squad=tuple(picks),
        bank=int(rng.integers(0, 25)) if bank is None else bank,
        free_transfers=free_transfers,
        chips=initial_chips(rs),
    )
    cfg = OptimizerConfig(objective=weights or ObjectiveWeights())
    return OptimizationProblem(
        state=state,
        ruleset=rs,
        players=table,
        gameweeks=tuple(range(gameweek, gameweek + horizon)),
        config=cfg,
        preferences=preferences or Preferences(),
        chip_options=chip_options or {},
        forced_chips=forced_chips or {},
    )
