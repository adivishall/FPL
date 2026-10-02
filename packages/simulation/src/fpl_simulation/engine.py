"""Joint fixture-level Monte Carlo simulation of FPL points (§12, §59, ADR-0006).

For each fixture and each of ``n_sims`` samples:

1. minutes per player (start → minutes bucket → minute; else substitute appearance); exactly
   one starting goalkeeper per team is drawn from the team's goalkeepers;
2. team goals ~ Poisson(μ) with goal times ~ U(0, 90);
3. each goal is an opponent own goal with probability ``own_goal_rate``; otherwise the scorer is
   drawn among players **on the pitch at that minute** ∝ goal share, and (with probability
   ``p_assisted``) an assister among on-pitch team-mates ∝ assist share;
4. goals conceded / clean sheets per player from their own on-pitch interval;
5. saves, defensive actions (negative binomial), cards and penalty events from per-90 rates
   scaled by minutes (and opponent strength where meaningful);
6. BPS from a learned linear map + player offset + noise; bonus 3/2/1 to the top three;
7. points from the versioned ruleset (``fpl_domain.scoring``); double gameweeks summed.

Common random numbers: every random draw comes from a stream keyed by
``(seed, fixture_id, player_code)`` (or ``(seed, fixture_id, side)`` for team-level draws), so a
scenario that changes one player's parameters leaves all other draws unchanged. This gives
paired comparisons between plans and scenarios (§25, §69).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
import numpy.typing as npt

from fpl_domain.forecast import (
    START_BUCKETS,
    SUB_BUCKETS,
    FixtureParams,
    GlobalParams,
    PlayerFixtureParams,
)
from fpl_domain.rules.model import Ruleset
from fpl_domain.scoring import COMPONENTS, ScoringTables, score_components

F = npt.NDArray[np.float64]
I = npt.NDArray[np.int64]  # noqa: E741

N_PLAYER_UNIFORMS = 12
N_TEAM_DRAWS = 6
GK = 0


@dataclass(frozen=True)
class SimulationConfig:
    n_sims: int = 2000
    seed: int = 20260828
    max_goals: int = 10
    max_n_sims: int = 20_000  # hard cap (§80 cost control)

    def __post_init__(self) -> None:
        if not 1 <= self.n_sims <= self.max_n_sims:
            raise ValueError(f"n_sims must be in [1, {self.max_n_sims}]")


@dataclass
class SimulationResult:
    player_codes: I  # [P] sorted
    gameweeks: I  # [G] sorted
    points: npt.NDArray[np.int16]  # [S, P, G]
    minutes: npt.NDArray[np.int16]  # [S, P, G]
    has_fixture: npt.NDArray[np.bool_]  # [P, G]
    components: dict[str, npt.NDArray[np.float32]]  # mean points per component [P, G]
    events: dict[str, npt.NDArray[np.float32]]  # mean event counts [P, G]
    seed: int
    n_sims: int
    ruleset_version: str
    model_versions: dict[str, str] = field(default_factory=dict)

    def index_of(self, player_code: int) -> int:
        i = int(np.searchsorted(self.player_codes, player_code))
        if i >= len(self.player_codes) or self.player_codes[i] != player_code:
            raise KeyError(player_code)
        return i

    def gw_index(self, gameweek: int) -> int:
        return int(np.searchsorted(self.gameweeks, gameweek))

    def summary(self) -> dict[str, npt.NDArray[np.float64]]:
        """Per player × GW distribution summary (§59.2 output contract)."""
        p = self.points.astype(np.float64)
        q = np.percentile(p, [10, 25, 50, 75, 90], axis=0)
        mins = self.minutes.astype(np.float64)
        mq = np.percentile(mins, [10, 50, 90], axis=0)
        return {
            "mean": p.mean(axis=0),
            "std": p.std(axis=0),
            "p10": q[0],
            "p25": q[1],
            "p50": q[2],
            "p75": q[3],
            "p90": q[4],
            "prob_2_plus": (p >= 2).mean(axis=0),
            "prob_6_plus": (p >= 6).mean(axis=0),
            "prob_10_plus": (p >= 10).mean(axis=0),
            "prob_15_plus": (p >= 15).mean(axis=0),
            "expected_minutes": mins.mean(axis=0),
            "minutes_p10": mq[0],
            "minutes_p50": mq[1],
            "minutes_p90": mq[2],
            "prob_play": (mins > 0).mean(axis=0),
        }


def _rng(seed: int, *keys: int) -> np.random.Generator:
    return np.random.default_rng(np.random.SeedSequence([seed & 0xFFFFFFFF, *keys]))


def _categorical(u: F, probs: F) -> I:
    """Inverse-CDF categorical draw: u [S, n], probs [n, k] → index [S, n]."""
    cdf = np.cumsum(probs, axis=1)
    cdf[:, -1] = 1.0 + 1e-12
    return (u[..., None] > cdf[None, :, :]).sum(axis=-1).astype(np.int64)


def _bucket_minutes(idx: I, u: F, buckets: tuple[tuple[int, int], ...]) -> I:
    lo = np.array([b[0] for b in buckets])[idx]
    hi = np.array([b[1] for b in buckets])[idx]
    return lo + np.floor(u * (hi - lo + 1)).astype(np.int64)


def _poisson_ppf(u: F, lam: F, kmax: int = 40) -> I:
    """Vectorised Poisson inverse CDF (exact up to ``kmax``)."""
    lam = np.broadcast_to(lam, u.shape)
    pmf = np.exp(-lam)
    cdf = pmf.copy()
    k = np.zeros(u.shape, dtype=np.int64)
    for j in range(1, kmax + 1):
        k += (u > cdf).astype(np.int64)
        pmf = pmf * lam / j
        cdf = cdf + pmf
    return k


def _negbin_ppf(u: F, mean: F, size: F, kmax: int = 60) -> I:
    mean = np.broadcast_to(mean, u.shape)
    size = np.broadcast_to(size, u.shape)
    p = size / (size + mean)
    pmf = np.power(p, size)
    cdf = pmf.copy()
    k = np.zeros(u.shape, dtype=np.int64)
    for j in range(1, kmax + 1):
        k += (u > cdf).astype(np.int64)
        pmf = pmf * (j - 1 + size) / j * (1 - p)
        cdf = cdf + pmf
    return k


def _normal(u1: F, u2: F) -> F:
    return np.sqrt(-2.0 * np.log(np.clip(u1, 1e-12, 1.0))) * np.cos(2 * np.pi * u2)


def _select(weights: F, u: F) -> tuple[I, npt.NDArray[np.bool_]]:
    """Weighted choice along the last axis; returns (index, has_candidate)."""
    total = weights.sum(axis=-1)
    ok = total > 0
    cdf = np.cumsum(weights, axis=-1) / np.where(ok, total, 1.0)[..., None]
    idx = (u[..., None] > cdf).sum(axis=-1)
    return np.minimum(idx, weights.shape[-1] - 1).astype(np.int64), ok


def simulate(
    players: PlayerFixtureParams,
    fixtures: FixtureParams,
    glob: GlobalParams,
    ruleset: Ruleset,
    config: SimulationConfig | None = None,
) -> SimulationResult:
    cfg = config or SimulationConfig()
    s, gmax = cfg.n_sims, cfg.max_goals
    tables = ScoringTables.from_ruleset(ruleset)
    bps = glob.bps

    codes = np.unique(players.player_code)
    gws = np.unique(fixtures.gameweek) if len(fixtures) else np.unique(players.gameweek)
    p_idx = np.searchsorted(codes, players.player_code)
    g_idx = np.searchsorted(gws, players.gameweek)
    points = np.zeros((s, len(codes), len(gws)), dtype=np.int16)
    minutes_out = np.zeros((s, len(codes), len(gws)), dtype=np.int16)
    has_fixture = np.zeros((len(codes), len(gws)), dtype=bool)
    comp_sum = {c: np.zeros((len(codes), len(gws)), dtype=np.float64) for c in COMPONENTS}
    ev_names = (
        "minutes",
        "goals",
        "assists",
        "clean_sheets",
        "goals_conceded",
        "saves",
        "bonus",
        "dc_actions",
        "starts",
    )
    ev_sum = {e: np.zeros((len(codes), len(gws)), dtype=np.float64) for e in ev_names}

    fx_row = {int(f): k for k, f in enumerate(fixtures.fixture_id)}
    for fid in np.unique(players.fixture_id):
        if int(fid) not in fx_row:
            raise ValueError(f"players reference fixture {fid} without fixture params")
        k = fx_row[int(fid)]
        rows = np.flatnonzero(players.fixture_id == fid)
        home_team, away_team = int(fixtures.home_team[k]), int(fixtures.away_team[k])
        mu = {True: float(fixtures.mu_home[k]), False: float(fixtures.mu_away[k])}
        n = len(rows)
        pos = players.position[rows]
        is_home = players.is_home[rows].astype(bool)
        team = players.team_code[rows]
        if np.any((team != home_team) & (team != away_team)):
            raise ValueError(f"fixture {fid}: player team not in fixture")

        # ---- per-player uniforms (CRN keyed by player)
        u = np.empty((N_PLAYER_UNIFORMS, s, n))
        for j, r in enumerate(rows):
            u[:, :, j] = _rng(cfg.seed, int(fid), int(players.player_code[r])).random(
                (N_PLAYER_UNIFORMS, s)
            )
        (u_start, u_bucket, u_within, u_sub, u_saves, u_dc, u_y, u_r, u_pm, u_ps, u_b1, u_b2) = u

        # ---- minutes
        started = u_start < players.p_start[rows][None, :]
        for side in (True, False):  # exactly one starting GK per team
            gk = np.flatnonzero((pos == GK) & (is_home == side))
            started[:, gk] = False
            if len(gk):
                pr = players.p_start[rows][gk]
                tot = pr.sum()
                probs = pr / max(tot, 1.0)  # leftover mass = unmodelled goalkeeper starts
                u_gk = _rng(cfg.seed, int(fid), 1_000_003 + int(side)).random(s)
                cdf = np.cumsum(probs)
                choice = (u_gk[:, None] > cdf[None, :]).sum(axis=1)
                for c_i, gk_col in enumerate(gk):
                    started[:, gk_col] = choice == c_i
        sb = _categorical(u_bucket, players.start_buckets[rows])
        m_start = _bucket_minutes(sb, u_within, START_BUCKETS)
        subbed_on = (~started) & (u_sub < players.p_sub[rows][None, :])
        subb = _categorical(u_bucket, players.sub_buckets[rows])
        m_sub = _bucket_minutes(subb, u_within, SUB_BUCKETS)
        mins = np.where(started, m_start, np.where(subbed_on, m_sub, 0))
        on_from = np.where(started, 0, 90 - mins).astype(np.float64)
        on_to = np.where(started, mins, 90).astype(np.float64)
        on_to = np.where(mins > 0, on_to, -1.0)

        goals = np.zeros((s, n), dtype=np.int64)
        assists = np.zeros((s, n), dtype=np.int64)
        own_goals = np.zeros((s, n), dtype=np.int64)
        conceded = np.zeros((s, n), dtype=np.int64)
        for side in (True, False):
            tr = _rng(cfg.seed, int(fid), 2_000_003 + int(side)).random((N_TEAM_DRAWS, s, gmax))
            u_g, u_time, u_scorer, u_assisted, u_assister, u_og = tr
            lam = mu[side]
            n_goals = np.minimum(_poisson_ppf(u_g[:, 0], np.full(s, lam), gmax), gmax)
            valid = np.arange(gmax)[None, :] < n_goals[:, None]  # [S, G]
            tau = u_time * 90.0
            mine = is_home == side
            on = (tau[:, :, None] >= on_from[:, None, :]) & (tau[:, :, None] < on_to[:, None, :])
            is_og = valid & (u_og < glob.own_goal_rate)
            # scorer among own on-pitch players
            w_sc = on * (players.goal_share[rows] * mine)[None, None, :]
            sc, ok_sc = _select(w_sc, u_scorer)
            scored = valid & ~is_og & ok_sc
            np.add.at(goals, (np.nonzero(scored)[0], sc[scored]), 1)
            # assister among on-pitch team-mates excluding the scorer
            w_as = on * (players.assist_share[rows] * mine)[None, None, :]
            w_as[np.arange(s)[:, None], np.arange(gmax)[None, :], sc] = 0.0
            asr, ok_as = _select(w_as, u_assister)
            assisted = scored & (u_assisted < glob.p_assisted) & ok_as
            np.add.at(assists, (np.nonzero(assisted)[0], asr[assisted]), 1)
            # own goals by on-pitch opponents
            w_og = on * (players.og_propensity[rows] * ~mine)[None, None, :]
            ogp, ok_og = _select(w_og, u_scorer)
            og_done = is_og & ok_og
            np.add.at(own_goals, (np.nonzero(og_done)[0], ogp[og_done]), 1)
            # every goal of this side is conceded by opponents on the pitch at that time
            conceded += np.einsum(
                "sg,sgn->sn", valid.astype(np.int64), (on & ~mine[None, None, :]).astype(np.int64)
            )

        clean = (mins >= tables.cs_min_minutes) & (conceded == 0)
        frac = mins / 90.0
        opp_mu = np.where(is_home, mu[False], mu[True])
        opp_factor = opp_mu / glob.league_mu
        lam_saves = players.saves_p90[rows] * frac * opp_factor**glob.saves_opp_elasticity
        saves = np.where(pos == GK, _poisson_ppf(u_saves, lam_saves), 0)
        dc_mean = players.dc_p90[rows] * frac * opp_factor**glob.dc_opp_elasticity
        dc_actions = np.where(
            (mins > 0) & (pos != GK),
            _negbin_ppf(u_dc, dc_mean + 1e-9, players.dc_dispersion[rows]),
            0,
        )
        yellow = ((mins > 0) & (u_y < 1 - np.exp(-players.yellow_p90[rows] * frac))).astype(int)
        red = ((mins > 0) & (u_r < 1 - np.exp(-players.red_p90[rows] * frac))).astype(int)
        pen_miss = ((mins > 0) & (u_pm < 1 - np.exp(-players.pen_miss_p90[rows] * frac))).astype(
            int
        )
        pen_save = (
            (mins > 0) & (pos == GK) & (u_ps < 1 - np.exp(-players.pen_save_p90[rows] * frac))
        ).astype(int)

        # ---- BPS and bonus
        bps_val = (
            bps.intercept[pos]
            + bps.per_minute[pos] * mins
            + bps.full_match[pos] * (mins >= 60)
            + bps.goal[pos] * goals
            + bps.assist * assists
            + bps.clean_sheet[pos] * clean
            + bps.goals_conceded[pos] * conceded
            + bps.saves * saves
            + bps.dc_action[pos] * dc_actions
            + bps.yellow * yellow
            + bps.red * red
            + bps.own_goal * own_goals
            + bps.pen_miss * pen_miss
            + bps.pen_save * pen_save
            + players.bps_offset[rows] * frac
            + bps.sigma[pos] * _normal(u_b1, u_b2)
        )
        bps_val = np.where(mins > 0, bps_val, -np.inf)
        bonus = np.zeros((s, n), dtype=np.int64)
        if n >= 1:
            top = np.argsort(-bps_val, axis=1)[:, :3]
            for rank, award in enumerate(ruleset.scoring.bonus.awards[: min(3, n)]):
                col = top[:, rank]
                eligible = np.isfinite(bps_val[np.arange(s), col])
                bonus[np.arange(s)[eligible], col[eligible]] = award

        comps = score_components(
            tables,
            pos,
            mins,
            goals,
            assists,
            clean.astype(int),
            conceded,
            saves,
            pen_save,
            pen_miss,
            yellow,
            red,
            own_goals,
            bonus,
            dc_actions,
        )
        total = np.sum([comps[c] for c in COMPONENTS], axis=0)
        pi, gi = p_idx[rows], g_idx[rows]
        at_idx: Any = (slice(None), pi, gi)
        np.add.at(points, at_idx, total.astype(np.int16))
        np.add.at(minutes_out, at_idx, mins.astype(np.int16))
        has_fixture[pi, gi] = True
        for c in COMPONENTS:
            np.add.at(comp_sum[c], (pi, gi), comps[c].mean(axis=0))
        for e, arr in (
            ("minutes", mins),
            ("goals", goals),
            ("assists", assists),
            ("clean_sheets", clean),
            ("goals_conceded", conceded),
            ("saves", saves),
            ("bonus", bonus),
            ("dc_actions", dc_actions),
            ("starts", started),
        ):
            np.add.at(ev_sum[e], (pi, gi), np.asarray(arr, dtype=np.float64).mean(axis=0))

    return SimulationResult(
        player_codes=codes,
        gameweeks=gws,
        points=points,
        minutes=minutes_out,
        has_fixture=has_fixture,
        components={c: v.astype(np.float32) for c, v in comp_sum.items()},
        events={e: v.astype(np.float32) for e, v in ev_sum.items()},
        seed=cfg.seed,
        n_sims=s,
        ruleset_version=ruleset.ruleset_version,
        model_versions=dict(glob.model_versions),
    )
