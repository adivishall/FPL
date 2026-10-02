"""Captaincy engine (§20, §63): captain choices compared on the *joint* simulation.

For each candidate captain among the starters, the whole lineup is re-scored on every simulated
sample with that armband (vice-captain fallback and auto-substitutions included), so the
comparison is paired: P(choice A beats choice B) is the share of samples where A's squad total
exceeds B's. Three profiles are reported without declaring one universally best:

* **expected** — highest mean squad points;
* **safe** — highest 25th percentile of squad points (downside protection);
* **high-variance** — highest probability that the captain's own (multiplied) return ≥ 13.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import numpy as np
import numpy.typing as npt

from fpl_domain.enums import ChipType, Position
from fpl_domain.rules import Ruleset
from fpl_domain.squad import Lineup
from fpl_simulation.lineup_eval import score_lineup

HAUL_THRESHOLD = 13


@dataclass(frozen=True)
class CaptainOption:
    player_code: int
    vice_captain: int
    mean_total: float
    p10_total: float
    p25_total: float
    p90_total: float
    captain_mean: float  # mean of the captain's multiplied return
    p_captain_haul: float  # P(multiplied return ≥ HAUL_THRESHOLD)
    p_captain_blank: float  # P(captain scores ≤ 2 before the multiplier)
    p_beats_expected_choice: float  # paired vs the expected-profile choice


@dataclass(frozen=True)
class CaptaincyAnalysis:
    options: tuple[CaptainOption, ...]
    expected: int
    safe: int
    high_variance: int
    beats_matrix: npt.NDArray[np.float64]  # [i, j] = P(total_i > total_j)
    chip: ChipType | None

    def option(self, code: int) -> CaptainOption:
        return next(o for o in self.options if o.player_code == code)


def analyse_captaincy(
    lineup: Lineup,
    positions: Mapping[int, Position],
    points: Mapping[int, npt.NDArray[np.integer]],
    minutes: Mapping[int, npt.NDArray[np.integer]],
    ruleset: Ruleset,
    candidates: Sequence[int] | None = None,
    chip: ChipType | None = None,
    top_k: int = 6,
) -> CaptaincyAnalysis:
    starters = list(lineup.starters)
    if candidates is None:
        means = {c: float(np.mean(points[c])) for c in starters}
        candidates = sorted(starters, key=lambda c: (-means[c], c))[:top_k]
    mult = (
        ruleset.chips.triple_captain_multiplier
        if chip is ChipType.TRIPLE_CAPTAIN
        else ruleset.captaincy.captain_multiplier
    )
    means_all = {c: float(np.mean(points[c])) for c in starters}
    totals: list[npt.NDArray[np.int64]] = []
    rows: list[tuple[int, int, npt.NDArray[np.int64], npt.NDArray[np.int64]]] = []
    for cand in candidates:
        if cand not in starters:
            raise ValueError(f"captain candidate {cand} is not a starter")
        vice = max((c for c in starters if c != cand), key=lambda c: (means_all[c], -c))
        lu = Lineup(starters=lineup.starters, bench=lineup.bench, captain=cand, vice_captain=vice)
        tot = score_lineup(lu, positions, points, minutes, ruleset, chip)
        cap_ret = np.where(np.asarray(minutes[cand]) > 0, mult * np.asarray(points[cand]), 0)
        totals.append(tot)
        rows.append((cand, vice, tot, cap_ret))
    tmat = np.vstack(totals)  # [K, S]
    beats = (tmat[:, None, :] > tmat[None, :, :]).mean(axis=2)
    mean_tot = tmat.mean(axis=1)
    exp_i = int(np.argmax(mean_tot))
    opts = []
    for i, (cand, vice, tot, cap_ret) in enumerate(rows):
        opts.append(
            CaptainOption(
                player_code=cand,
                vice_captain=vice,
                mean_total=float(tot.mean()),
                p10_total=float(np.percentile(tot, 10)),
                p25_total=float(np.percentile(tot, 25)),
                p90_total=float(np.percentile(tot, 90)),
                captain_mean=float(cap_ret.mean()),
                p_captain_haul=float((cap_ret >= HAUL_THRESHOLD).mean()),
                p_captain_blank=float((np.asarray(points[cand]) <= 2).mean()),
                p_beats_expected_choice=float(beats[i, exp_i]),
            )
        )
    p25 = np.array([o.p25_total for o in opts])
    haul = np.array([o.p_captain_haul for o in opts])
    # ties broken by mean (then by order) so profiles are deterministic
    safe_i = int(np.lexsort((-mean_tot, -p25))[0])
    hv_i = int(np.lexsort((-mean_tot, -haul))[0])
    return CaptaincyAnalysis(
        options=tuple(opts),
        expected=opts[exp_i].player_code,
        safe=opts[safe_i].player_code,
        high_variance=opts[hv_i].player_code,
        beats_matrix=beats,
        chip=chip,
    )
