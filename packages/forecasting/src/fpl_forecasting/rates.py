"""Per-player event rates with empirical-Bayes shrinkage (§13 Bayesian updating, hierarchical
priors; §57).

For each rate (goal share, assist share, saves/90, defensive actions/90, cards/90, penalty
events/90, BPS residual/90) we compute recency-weighted sufficient statistics from
point-in-time history — a weighted "count" ``x`` and a weighted exposure ``E`` — and shrink
toward a prior mean ``m`` with strength ``κ`` (in exposure units):

    rate = (x + κ·m) / (E + κ)

This is the linear-Bayes (Bühlmann–Straub credibility) posterior mean under

    x/E | θ  has mean θ and variance φ·θ/E          (φ = 1 would be Poisson)
    θ        has mean m and variance c·m²            (c = squared coefficient of variation)

which gives κ = φ / (c·m). The prior mean ``m`` is position-specific (shares additionally depend
on log price within position, by weighted least squares). φ and c are estimated per rate and
position from a **split-half design**: each player's history is split into alternating matches
A/B, so sampling noise is independent between halves and

    E[(r_A − r_B)² · h] = φ·θ           with h = 1/(1/E_A + 1/E_B)
    E[(r_A − m)(r_B − m)] = c·m²

Neither needs a Poisson assumption (an xG blend is far smoother than goal counts; cards are
overdispersed). c is noisy for small groups (50-odd forwards), so per-position estimates are
shrunk toward the estimate pooled over positions (``pool_strength`` pseudo-players).
New players get the prior.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from fpl_domain.enums import POSITIONS
from fpl_storage.pit import PointInTimeView

HALF_LIFE_MATCHES = 12.0
MAX_ROWS = 60

# (name, numerator column, exposure kind)
RATES = (
    ("goal_share", "goal_blend", "team_xg_exposure"),
    ("assist_share", "assist_blend", "team_xg_exposure"),
    ("saves_p90", "saves", "minutes90"),
    ("dc_p90", "dc_actions", "dc_minutes90"),
    ("yellow_p90", "yellow_cards", "minutes90"),
    ("red_p90", "red_cards", "minutes90"),
    ("pen_miss_p90", "penalties_missed", "minutes90"),
    ("pen_save_p90", "penalties_saved", "minutes90"),
    ("og_p90", "own_goals", "minutes90"),
)


@dataclass
class RateConfig:
    goal_weight: float = 0.3  # finishing signal: blend = α·goals + (1−α)·xG
    half_life_matches: float = HALF_LIFE_MATCHES
    kappa_bounds: tuple[float, float] = (0.5, 500.0)
    min_half_exposure: float = 1.0
    pool_strength: float = 100.0  # pseudo-players pulling c toward the cross-position estimate


@dataclass
class RateModel:
    """Fitted priors (per position, price slope for shares) and shrinkage strengths."""

    config: RateConfig
    prior_intercept: dict[str, np.ndarray] = field(default_factory=dict)  # [4]
    prior_price_slope: dict[str, np.ndarray] = field(default_factory=dict)  # [4]
    phi: dict[str, np.ndarray] = field(default_factory=dict)  # [4] noise scale (1 = Poisson)
    cv2: dict[str, np.ndarray] = field(default_factory=dict)  # [4] between-player CV²
    n_split: dict[str, np.ndarray] = field(default_factory=dict)  # [4] players in split design

    def kappa(self, name: str, pos: np.ndarray, prior_mean: np.ndarray) -> np.ndarray:
        lo, hi = self.config.kappa_bounds
        denom = self.cv2[name][pos] * prior_mean
        k = np.where(denom > 0, self.phi[name][pos] / np.maximum(denom, 1e-12), hi)
        return np.clip(k, lo, hi)

    def prior_mean(self, name: str, pos: np.ndarray, log_price: np.ndarray) -> np.ndarray:
        m = self.prior_intercept[name][pos] + self.prior_price_slope[name][pos] * log_price
        return np.clip(m, 0.0, None)

    def posterior(
        self, stats: pd.DataFrame, pos: np.ndarray, log_price: np.ndarray
    ) -> pd.DataFrame:
        out = pd.DataFrame(index=stats.index)
        for name, num, expo in RATES:
            x = stats[num].to_numpy(dtype=float)
            e = stats[expo].to_numpy(dtype=float)
            m = self.prior_mean(name, pos, log_price)
            k = self.kappa(name, pos, m)
            out[name] = (np.nan_to_num(x) + k * m) / (np.nan_to_num(e) + k)
            out[f"{name}_exposure"] = np.nan_to_num(e)
        return out


def history_with_team_xg(view: PointInTimeView) -> pd.DataFrame:
    """PIT player-match history with the xG of the player's own side in each fixture."""
    hist = view.player_match()
    tm = view.team_match().set_index(["season", "fixture_id", "team_code"])["xg_for"]
    idx = pd.MultiIndex.from_frame(hist[["season", "fixture_id", "team_code"]])
    return hist.assign(team_xg=tm.reindex(idx).to_numpy())


def sufficient_stats(hist: pd.DataFrame, positions: pd.Series, config: RateConfig) -> pd.DataFrame:
    """Recency-weighted counts and exposures per player from PIT match history.

    ``hist`` must contain ``team_xg`` (team xG of the player's side in that fixture).
    """
    if hist.empty:
        return pd.DataFrame()
    h = hist.sort_values(["player_code", "kickoff_at"], ascending=[True, False]).copy()
    h["idx"] = h.groupby("player_code").cumcount()
    h = h[h["idx"] < MAX_ROWS]
    w = 0.5 ** (h["idx"] / config.half_life_matches)
    mins90 = h["minutes"].astype(float) / 90.0
    a = config.goal_weight
    xg = h["xg"].astype("Float64").astype(float)
    xa = h["xa"].astype("Float64").astype(float)
    goals = h["goals"].astype(float)
    ast = h["assists"].astype(float)
    has_x = xg.notna()
    team_xg = h["team_xg"].astype(float)
    use = (has_x & team_xg.notna()).to_numpy(bool).astype(float)
    pos = h["player_code"].map(positions).astype(object)
    cbi = h["cbi"].astype("Float64").astype(float)
    tck = h["tackles"].astype("Float64").astype(float)
    rec = h["recoveries"].astype("Float64").astype(float)
    dca = cbi + tck + np.where(pos.isin(["MID", "FWD"]).to_numpy(bool), rec, 0.0)
    has_dc = (dca.notna() & (pos != "GK")).to_numpy(bool).astype(float)
    cols = {
        "goal_blend": (a * goals + (1 - a) * xg.fillna(goals)) * w * use,
        "assist_blend": (a * ast + (1 - a) * xa.fillna(ast)) * w * use,
        "team_xg_exposure": team_xg.fillna(0.0) * mins90 * w * use,
        "saves": h["saves"].astype(float) * w,
        "dc_actions": dca.fillna(0.0) * w * has_dc,
        "dc_minutes90": mins90 * w * has_dc,
        "yellow_cards": h["yellow_cards"].astype(float) * w,
        "red_cards": h["red_cards"].astype(float) * w,
        "penalties_missed": h["penalties_missed"].astype(float) * w,
        "penalties_saved": h["penalties_saved"].astype(float) * w,
        "own_goals": h["own_goals"].astype(float) * w,
        "minutes90": mins90 * w,
    }
    df = pd.DataFrame(cols)
    half_a = (h["idx"].to_numpy() % 2 == 0).astype(float)
    for c in list(cols):
        df[f"{c}__a"] = df[c] * half_a
        df[f"{c}__b"] = df[c] * (1.0 - half_a)
    df["player_code"] = h["player_code"].to_numpy()
    return df.groupby("player_code").sum()


def _wls(x: np.ndarray, y: np.ndarray, w: np.ndarray) -> tuple[float, float]:
    if len(x) < 5 or w.sum() <= 0:
        return (float(np.average(y, weights=w)) if w.sum() > 0 else 0.0), 0.0
    xm = np.average(x, weights=w)
    ym = np.average(y, weights=w)
    var = np.average((x - xm) ** 2, weights=w)
    slope = float(np.average((x - xm) * (y - ym), weights=w) / var) if var > 1e-9 else 0.0
    return float(ym - slope * xm), slope


def fit_rate_model(
    stats: pd.DataFrame,
    positions: pd.Series,
    log_price: pd.Series,
    config: RateConfig | None = None,
) -> RateModel:
    """Fit priors (mean by position, price slope for shares), φ and c by split halves."""
    cfg = config or RateConfig()
    model = RateModel(config=cfg)
    pos_of = positions.reindex(stats.index)
    lp_all = log_price.reindex(stats.index).fillna(log_price.median()).to_numpy()
    for name, num, expo in RATES:
        icpt, slope = np.zeros(4), np.zeros(4)
        phi, cv2, n = np.ones(4), np.zeros(4), np.zeros(4)
        cov_sum, m2_sum = np.zeros(4), np.zeros(4)
        for k, p in enumerate(POSITIONS):
            sel = (pos_of == p.value).to_numpy()
            x = stats[num].to_numpy(float)[sel]
            e = stats[expo].to_numpy(float)[sel]
            ok = e > 0.05
            if ok.sum() < 5:
                icpt[k] = float(x.sum() / e.sum()) if e.sum() > 0 else 0.0
                continue
            r = x[ok] / e[ok]
            if name in ("goal_share", "assist_share"):
                icpt[k], slope[k] = _wls(lp_all[sel][ok], r, e[ok])
            else:
                icpt[k] = float(x[ok].sum() / e[ok].sum())
            m = np.clip(icpt[k] + slope[k] * lp_all[sel], 0.0, None)
            xa, xb = (
                stats[f"{num}__a"].to_numpy(float)[sel],
                stats[f"{num}__b"].to_numpy(float)[sel],
            )
            ea, eb = (
                stats[f"{expo}__a"].to_numpy(float)[sel],
                stats[f"{expo}__b"].to_numpy(float)[sel],
            )
            both = (ea >= cfg.min_half_exposure) & (eb >= cfg.min_half_exposure) & (m > 0)
            n[k] = both.sum()
            if n[k] < 10:
                continue
            ra, rb, mb = xa[both] / ea[both], xb[both] / eb[both], m[both]
            h = 1.0 / (1.0 / ea[both] + 1.0 / eb[both])
            phi[k] = max(float(np.mean((ra - rb) ** 2 * h) / np.mean(mb)), 1e-6)
            cov_sum[k] = float(np.sum((ra - mb) * (rb - mb)))
            m2_sum[k] = float(np.sum(mb**2))
            cv2[k] = max(cov_sum[k] / m2_sum[k], 0.0)
        pooled = max(cov_sum.sum() / m2_sum.sum(), 0.0) if m2_sum.sum() > 0 else 0.0
        cv2 = (n * cv2 + cfg.pool_strength * pooled) / (n + cfg.pool_strength)
        model.prior_intercept[name] = icpt
        model.prior_price_slope[name] = slope
        model.phi[name] = phi
        model.cv2[name] = cv2
        model.n_split[name] = n
    return model
