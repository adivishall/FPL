"""Point-in-time team strength model (§14, §56.2).

For a match between home team i and away team j::

    log μ_home = c + h + att_i − def_j
    log μ_away = c       + att_j − def_i

fitted by maximising a recency-weighted Poisson (quasi-)likelihood of a blended target
``y = α·goals + (1−α)·xG`` (xG stabilises the noisy goal signal) plus Gaussian priors on the
ratings. Teams without recent Premier League history (promoted sides) get a prior shifted by the
configured promoted-team offsets. Posterior uncertainty comes from the Laplace approximation
(inverse Hessian at the MAP).

Inputs come exclusively from ``PointInTimeView.team_match()`` so every fit is time-causal.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd
from pydantic import BaseModel, ConfigDict
from scipy.optimize import minimize

from fpl_domain.config import load_versioned_config

MODEL_NAME = "team_strength"


class TeamStrengthConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    half_life_days: float = 120.0
    window_days: float = 500.0
    goals_weight: float = 0.35
    prior_sd: float = 0.35
    promoted_attack_prior: float = -0.25
    promoted_defence_prior: float = -0.20
    home_advantage_prior: float = 0.12
    home_advantage_sd: float = 0.10
    min_matches_for_history: int = 5


@dataclass
class TeamStrength:
    """Fitted ratings at one cutoff."""

    cutoff: pd.Timestamp
    teams: list[int]
    attack: np.ndarray
    defence: np.ndarray
    attack_sd: np.ndarray
    defence_sd: np.ndarray
    intercept: float
    home_advantage: float
    cov: np.ndarray
    n_matches: int
    config: TeamStrengthConfig
    promoted: set[int] = field(default_factory=set)

    def _idx(self, team: int) -> int | None:
        try:
            return self.teams.index(team)
        except ValueError:
            return None

    def rating(self, team: int) -> tuple[float, float, float, float]:
        """(attack, defence, attack_sd, defence_sd); unseen teams get the promoted prior."""
        i = self._idx(team)
        if i is None:
            c = self.config
            return c.promoted_attack_prior, c.promoted_defence_prior, c.prior_sd, c.prior_sd
        return (
            float(self.attack[i]),
            float(self.defence[i]),
            float(self.attack_sd[i]),
            float(self.defence_sd[i]),
        )

    def expected_goals(self, home: int, away: int, neutral: bool = False) -> tuple[float, float]:
        ah, dh, _, _ = self.rating(home)
        aa, da, _, _ = self.rating(away)
        h = 0.0 if neutral else self.home_advantage
        return (
            float(np.exp(self.intercept + h + ah - da)),
            float(np.exp(self.intercept + aa - dh)),
        )

    def log_mu_sd(self, team: int, opponent: int) -> float:
        """Delta-method SD of log μ for ``team`` scoring against ``opponent``."""
        i, j = self._idx(team), self._idx(opponent)
        n = len(self.teams)
        grad = np.zeros(2 * n + 2)
        grad[-2] = 1.0  # intercept
        var_extra = 0.0
        if i is not None:
            grad[i] = 1.0
        else:
            var_extra += self.config.prior_sd**2
        if j is not None:
            grad[n + j] = -1.0
        else:
            var_extra += self.config.prior_sd**2
        return float(np.sqrt(max(grad @ self.cov @ grad + var_extra, 0.0)))

    def table(self) -> pd.DataFrame:
        return pd.DataFrame(
            {
                "team_code": self.teams,
                "attack": self.attack,
                "defence": self.defence,
                "attack_sd": self.attack_sd,
                "defence_sd": self.defence_sd,
            }
        ).assign(home_advantage=self.home_advantage, cutoff=self.cutoff)


def _team_rows(team_match: pd.DataFrame) -> pd.DataFrame:
    """One row per match (home perspective) from the per-team match table."""
    h = team_match[team_match["was_home"]].copy()
    return pd.DataFrame(
        {
            "fixture_id": h["fixture_id"].to_numpy(),
            "season": h["season"].to_numpy(),
            "kickoff_at": h["kickoff_at"].to_numpy(),
            "home": h["team_code"].astype(int).to_numpy(),
            "away": h["opponent_team_code"].astype(int).to_numpy(),
            "hg": h["goals_for"].astype(float).to_numpy(),
            "ag": h["goals_against"].astype(float).to_numpy(),
            "hxg": pd.to_numeric(h["xg_for"], errors="coerce").astype(float).to_numpy(),
            "axg": pd.to_numeric(h["xg_against"], errors="coerce").astype(float).to_numpy(),
        }
    )


def fit_team_strength(
    team_match: pd.DataFrame,
    cutoff: pd.Timestamp,
    config: TeamStrengthConfig | None = None,
    current_teams: list[int] | None = None,
) -> TeamStrength:
    cfg = config or TeamStrengthConfig()
    m = _team_rows(team_match)
    m = m[pd.to_datetime(m["kickoff_at"], utc=True) <= cutoff]
    age = (cutoff - pd.to_datetime(m["kickoff_at"], utc=True)).dt.total_seconds() / 86400.0
    keep = age <= cfg.window_days
    m, age = m[keep.to_numpy()], age[keep]
    teams = sorted(set(m["home"]) | set(m["away"]) | set(current_teams or []))
    n = len(teams)
    if n == 0:
        raise ValueError("no teams to fit")
    idx = {t: k for k, t in enumerate(teams)}
    counts = pd.concat([m["home"], m["away"]]).value_counts()
    promoted = {t for t in teams if counts.get(t, 0) < cfg.min_matches_for_history}

    a = cfg.goals_weight
    yh = np.where(np.isnan(m["hxg"]), m["hg"], a * m["hg"] + (1 - a) * m["hxg"])
    ya = np.where(np.isnan(m["axg"]), m["ag"], a * m["ag"] + (1 - a) * m["axg"])
    w = 0.5 ** (age.to_numpy() / cfg.half_life_days)
    hi = np.array([idx[t] for t in m["home"]], dtype=int)
    ai = np.array([idx[t] for t in m["away"]], dtype=int)

    prior_att = np.array([cfg.promoted_attack_prior if t in promoted else 0.0 for t in teams])
    prior_def = np.array([cfg.promoted_defence_prior if t in promoted else 0.0 for t in teams])
    prec = 1.0 / cfg.prior_sd**2
    hprec = 1.0 / cfg.home_advantage_sd**2
    # Identification: ratings are only defined up to a common shift (att_i + k, def_j + k leave
    # every μ unchanged). Soft sum-to-zero constraints (strong quadratic penalties) identify them,
    # so reported per-team SDs describe identified contrasts, not the arbitrary common level.
    ident = 1e4
    base = (
        np.log(max(np.average(np.concatenate([yh, ya]), weights=np.concatenate([w, w])), 0.1))
        if len(m)
        else np.log(1.35)
    )

    def unpack(th: np.ndarray) -> tuple[np.ndarray, np.ndarray, float, float]:
        return th[:n], th[n : 2 * n], th[2 * n], th[2 * n + 1]

    def objective(th: np.ndarray) -> tuple[float, np.ndarray]:
        att, dfn, c, hadv = unpack(th)
        eta_h = c + hadv + att[hi] - dfn[ai]
        eta_a = c + att[ai] - dfn[hi]
        mu_h, mu_a = np.exp(eta_h), np.exp(eta_a)
        nll = np.sum(w * (mu_h - yh * eta_h)) + np.sum(w * (mu_a - ya * eta_a))
        nll += 0.5 * prec * (np.sum((att - prior_att) ** 2) + np.sum((dfn - prior_def) ** 2))
        nll += 0.5 * hprec * (hadv - cfg.home_advantage_prior) ** 2
        nll += 0.5 * ident * (att.sum() ** 2 + dfn.sum() ** 2)
        rh, ra = w * (mu_h - yh), w * (mu_a - ya)
        g_att = (
            np.bincount(hi, rh, n)
            + np.bincount(ai, ra, n)
            + prec * (att - prior_att)
            + ident * att.sum()
        )
        g_def = (
            -np.bincount(ai, rh, n)
            - np.bincount(hi, ra, n)
            + prec * (dfn - prior_def)
            + ident * dfn.sum()
        )
        g_c = rh.sum() + ra.sum()
        g_h = rh.sum() + hprec * (hadv - cfg.home_advantage_prior)
        return float(nll), np.concatenate([g_att, g_def, [g_c, g_h]])

    th0 = np.concatenate(
        [
            prior_att - prior_att.mean(),
            prior_def - prior_def.mean(),
            [base, cfg.home_advantage_prior],
        ]
    )
    res = minimize(
        objective, th0, jac=True, method="L-BFGS-B", options={"maxiter": 500, "gtol": 1e-8}
    )
    th = res.x
    att, dfn, c, hadv = unpack(th)

    # Laplace approximation: Hessian of the negative log posterior at the MAP,
    # H = Xᵀ diag(w·μ) X + prior precision, with X the design matrix of both score equations.
    k = 2 * n + 2
    mu_h = np.exp(c + hadv + att[hi] - dfn[ai])
    mu_a = np.exp(c + att[ai] - dfn[hi])
    rows = len(mu_h)
    x = np.zeros((2 * rows, k))
    r = np.arange(rows)
    x[r, hi] = 1.0
    x[r, n + ai] = -1.0
    x[r, 2 * n] = 1.0
    x[r, 2 * n + 1] = 1.0
    x[rows + r, ai] = 1.0
    x[rows + r, n + hi] = -1.0
    x[rows + r, 2 * n] = 1.0
    wm = np.concatenate([w * mu_h, w * mu_a])
    hess = (x * wm[:, None]).T @ x
    hess[np.arange(2 * n), np.arange(2 * n)] += prec
    hess[:n, :n] += ident
    hess[n : 2 * n, n : 2 * n] += ident
    hess[2 * n + 1, 2 * n + 1] += hprec
    hess[2 * n, 2 * n] += 1e-6
    cov = np.linalg.pinv(hess)
    sd = np.sqrt(np.clip(np.diag(cov), 0.0, None))
    return TeamStrength(
        cutoff=cutoff,
        teams=teams,
        attack=att,
        defence=dfn,
        attack_sd=sd[:n],
        defence_sd=sd[n : 2 * n],
        intercept=float(c),
        home_advantage=float(hadv),
        cov=cov,
        n_matches=len(m),
        config=cfg,
        promoted=promoted,
    )


def config_for_season(season: str) -> TeamStrengthConfig:
    """Configuration a forecast/backtest for ``season`` may use (rolling-origin selection)."""
    data = load_versioned_config("models", "team_strength").data
    merged = {**data["default"], **data.get("by_target_season", {}).get(season, {})}
    return TeamStrengthConfig(**merged)
