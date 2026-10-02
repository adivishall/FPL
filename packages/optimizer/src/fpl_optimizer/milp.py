"""Multi-gameweek squad / transfer / lineup / chip MILP solved with HiGHS (§61, ADR-0007).

Formulation (t = 0 … T−1 over the horizon; p over the candidate pool) — documented in full in
``docs/architecture/optimizer.md``:

Variables (§61.1)
  x[p,t]  persistent squad after GW t's transfers          y[p,t] / z[p,t]  bought / sold in GW t
  s[p,t]  starter        c[p,t] captain        v[p,t] vice-captain
  b[p,t,k] outfield bench slot k ∈ {1,2,3}                ch[k,t] chip instance k played in GW t
  q[p,t]  Free Hit squad (only where a Free Hit may be played); w[p,t] playing squad
  h[t]    paid transfers  f[t] free transfers available  u[t] unused free transfers
  bank[t] bank after GW t (tenths of £m)

Core constraints (§61.2): position quotas and club limit on x (and on q in a Free Hit week);
flow x[p,t] = x[p,t−1] + y[p,t] − z[p,t]; budget with true selling prices for owned players
(prices within the horizon are held at current values; ADR-0007); paid transfers
h ≥ transfers − f unless a Wildcard / Free Hit / unlimited week; free-transfer evolution
f[t+1] = min(cap, max(f[t] − n[t], 0) + 1), chip-week policy and scheduled top-ups (linearised
exactly with binaries, using that more free transfers never hurt the objective); legal XI,
captain/vice in the XI, 1 bench goalkeeper + 3 ordered outfield bench slots; one chip per GW;
Free Hit squad reverts (persistent squad frozen during the Free Hit week).

Objective (configurable, §61.3):
  Σ_t δ^t [ Σ a·s + (m−1)·a·c + w_v·a·v + Σ_k w_k·ev(bench_k) + w_gk·ev(bench GK)
            + BB/TC uplifts − hit_cost·h − λ_tr·n ]
  + v_ft·f[T] + v_bank·bank[T−1] + α·Σ ev[·,T−1]·x[·,T−1] − Σ v_chip·(chips used)
with a = ev − λ_risk·(ev − q10).
"""

from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import dataclass, field, replace
from typing import Any

import highspy
import numpy as np
from scipy import sparse

from fpl_domain.enums import POSITIONS, ChipType, Position
from fpl_domain.rules.model import ChipFtPolicy
from fpl_domain.squad import Lineup
from fpl_domain.state import ChipState, next_free_transfers
from fpl_optimizer.lineup import best_lineup
from fpl_optimizer.problem import OptimizationProblem

INF = highspy.kHighsInf
BIG_FT = 20


class OptimizationError(RuntimeError):
    pass


class _Model:
    """Thin sparse modelling layer over a HiGHS LP/MIP."""

    def __init__(self) -> None:
        self.lb: list[float] = []
        self.ub: list[float] = []
        self.cost: list[float] = []
        self.integer: list[bool] = []
        self.names: list[str] = []
        self.rows: list[tuple[dict[int, float], float, float]] = []
        self.const = 0.0

    def var(
        self, name: str, lb: float = 0.0, ub: float = 1.0, integer: bool = True, cost: float = 0.0
    ) -> int:
        self.lb.append(lb)
        self.ub.append(ub)
        self.cost.append(cost)
        self.integer.append(integer)
        self.names.append(name)
        return len(self.lb) - 1

    def add_cost(self, j: int, c: float) -> None:
        self.cost[j] += c

    def row(self, coefs: dict[int, float], lo: float = -INF, hi: float = INF) -> None:
        self.rows.append(({k: v for k, v in coefs.items() if v != 0.0}, lo, hi))

    def solve(
        self, settings: Any, start: dict[str, float] | None = None
    ) -> tuple[np.ndarray, float, str, dict[str, Any]]:
        n = len(self.lb)
        data, ri, ci = [], [], []
        lo, hi = [], []
        for r, (coefs, a, b) in enumerate(self.rows):
            for j, v in coefs.items():
                ri.append(r)
                ci.append(j)
                data.append(v)
            lo.append(a)
            hi.append(b)
        a_mat = sparse.csc_matrix((data, (ri, ci)), shape=(len(self.rows), n))
        lp = highspy.HighsLp()
        lp.num_col_ = n
        lp.num_row_ = len(self.rows)
        lp.col_cost_ = np.array([-c for c in self.cost])  # maximise
        lp.col_lower_ = np.array(self.lb)
        lp.col_upper_ = np.array(self.ub)
        lp.row_lower_ = np.array(lo)
        lp.row_upper_ = np.array(hi)
        lp.a_matrix_.format_ = highspy.MatrixFormat.kColwise
        lp.a_matrix_.start_ = a_mat.indptr
        lp.a_matrix_.index_ = a_mat.indices
        lp.a_matrix_.value_ = a_mat.data
        lp.integrality_ = [
            highspy.HighsVarType.kInteger if k else highspy.HighsVarType.kContinuous
            for k in self.integer
        ]
        h = highspy.Highs()
        h.setOptionValue("output_flag", False)
        h.setOptionValue("random_seed", settings.random_seed)
        h.setOptionValue("threads", settings.threads)
        h.setOptionValue("time_limit", settings.time_limit_seconds)
        h.setOptionValue("mip_rel_gap", settings.mip_rel_gap)
        h.setOptionValue("mip_feasibility_tolerance", 1e-7)
        h.passModel(lp)
        warm = False
        if start:
            # MIP start by variable name; the playing squad defaults to the persistent squad
            vec = []
            for name in self.names:
                if name in start:
                    vec.append(start[name])
                elif name.startswith("w["):
                    vec.append(start.get("x" + name[1:], 0.0))
                else:
                    vec.append(0.0)
            hs = highspy.HighsSolution()
            hs.col_value = vec
            hs.value_valid = True
            warm = h.setSolution(hs) == highspy.HighsStatus.kOk
        t0 = time.perf_counter()
        h.run()
        elapsed = time.perf_counter() - t0
        status = h.modelStatusToString(h.getModelStatus())
        info = h.getInfo()
        stats = {
            "solve_seconds": elapsed,
            "status": status,
            "mip_gap": float(getattr(info, "mip_gap", float("nan"))),
            "mip_node_count": int(getattr(info, "mip_node_count", 0)),
            "n_vars": n,
            "n_rows": len(self.rows),
            "n_integer": int(sum(self.integer)),
            "warm_start": warm,
        }
        if h.getInfo().primal_solution_status != 2:  # kSolutionStatusFeasible
            return np.array([]), float("nan"), status, stats
        sol = np.array(h.getSolution().col_value)
        obj = -float(h.getInfo().objective_function_value) + self.const
        return sol, obj, status, stats


@dataclass
class GameweekPlan:
    gameweek: int
    squad: tuple[int, ...]  # persistent squad after the GW's transfers
    playing_squad: tuple[int, ...]  # differs from squad in a Free Hit week
    transfers_in: tuple[int, ...]
    transfers_out: tuple[int, ...]
    chip_id: str | None
    chip_type: ChipType | None
    lineup: Lineup
    paid_transfers: int
    hit_points: int
    free_transfers: int  # available at the start of the GW
    bank_after: int
    expected_points: float  # risk-neutral XI EV incl. armband (and BB/TC), excl. hits
    objective: float  # undiscounted per-GW objective contribution


@dataclass
class Solution:
    plans: list[GameweekPlan]
    objective: float
    status: str
    stats: dict[str, Any]
    free_transfers_end: int
    terms: dict[str, float] = field(default_factory=dict)
    values: dict[str, float] = field(default_factory=dict, repr=False)  # by variable name

    @property
    def first(self) -> GameweekPlan:
        return self.plans[0]

    def first_action(self) -> tuple[frozenset[int], frozenset[int]]:
        return frozenset(self.first.transfers_out), frozenset(self.first.transfers_in)

    @property
    def total_hits(self) -> int:
        return sum(p.hit_points for p in self.plans)


def _chip_ft_next(policy: ChipFtPolicy, per: int) -> tuple[str, int]:
    if policy is ChipFtPolicy.RETAIN:
        return "f", 0
    if policy is ChipFtPolicy.RETAIN_AND_ACCRUE:
        return "f", per
    return "const", per


def build_and_solve(
    prob: OptimizationProblem,
    extra_cuts: list[tuple[dict[tuple[str, int, int], float], float]] | None = None,
    start: Solution | None = None,
) -> Solution:
    """Build the MILP for ``prob`` and solve it. ``extra_cuts`` are rows Σ coef·var ≤ rhs over
    named first-GW transfer variables, e.g. {("y", p, 0): 1, ...} — used for top-N no-good cuts.
    """
    rs, st, pl, cfg = prob.ruleset, prob.state, prob.players, prob.config
    obj = cfg.objective
    m = _Model()
    n_pl, n_gw = pl.n, pl.horizon
    gws = prob.gameweeks
    idx = pl.index()
    owned = np.zeros(n_pl, dtype=bool)
    for c in st.codes:
        owned[idx[c]] = True
    if prob.initial_squad_mode:
        owned[:] = False
    prices = {int(c): int(p) for c, p in zip(pl.code, pl.price, strict=True)}
    sell_val = st.selling_values(prices, rs) if not prob.initial_squad_mode else {}
    sale = np.array(
        [
            sell_val.get(int(c), int(pl.price[i])) if owned[i] else int(pl.price[i])
            for i, c in enumerate(pl.code)
        ],
        dtype=float,
    )
    buy = pl.price.astype(float)
    ev = pl.ev
    a = ev - obj.risk_aversion * pl.downside()
    disc = np.array([obj.discount**t for t in range(n_gw)])
    pos = pl.position
    gk = pos == 0
    pref = prob.preferences
    quotas = rs.squad.positions
    lo_l, hi_l = rs.lineup.min_per_position, rs.lineup.max_per_position
    cap_mult = rs.captaincy.captain_multiplier
    tc_mult = rs.chips.triple_captain_multiplier
    per = rs.transfers.free_transfers_per_gameweek
    cap_ft = rs.transfers.max_banked_free_transfers
    hit_cost = rs.transfers.hit_cost
    bank0 = float(st.bank)
    big_ev = float(np.abs(ev).max() * 4 + 10)

    # ---------------------------------------------------------------- chips
    chip_vars: dict[tuple[str, int], int] = {}
    chip_state: dict[str, ChipState] = {c.chip_id: c for c in st.chips}
    allowed: dict[str, set[int]] = defaultdict(set)
    for cid, wks in prob.chip_options.items():
        allowed[cid] |= set(wks)
    for cid, wk in prob.forced_chips.items():
        allowed[cid].add(wk)
    for cid, weeks in allowed.items():
        if cid not in chip_state:
            raise OptimizationError(f"chip {cid} not held by the manager")
        cs = chip_state[cid]
        if cs.chip_type is ChipType.ASSISTANT_MANAGER:
            raise OptimizationError("assistant manager chip is not supported (ADR-0001 #14)")
        for t, g in enumerate(gws):
            if (g in weeks and cs.usable_in(g)) or (prob.forced_chips.get(cid) == g):
                if (
                    not (cs.first_gameweek <= g <= cs.last_gameweek)
                    or cs.status.value != "available"
                ):
                    raise OptimizationError(f"{cid} cannot be played in GW {g}")
                chip_vars[(cid, t)] = m.var(f"chip[{cid},{t}]")
    by_type: dict[tuple[ChipType, int], list[int]] = defaultdict(list)
    for (cid, t), j in chip_vars.items():
        by_type[(chip_state[cid].chip_type, t)].append(j)
    for cid in allowed:
        js = [j for (c2, _), j in chip_vars.items() if c2 == cid]
        if not js:
            continue
        if cid in prob.forced_chips:
            t_f = gws.index(prob.forced_chips[cid])
            m.row({chip_vars[(cid, t_f)]: 1.0}, 1.0, 1.0)
        m.row(dict.fromkeys(js, 1.0), -INF, 1.0)
        val = obj.chip_values.get(chip_state[cid].chip_type.value, 0.0)
        if val:
            for j in js:
                m.add_cost(j, -val)
    if rs.chips.one_chip_per_gameweek:
        for t in range(n_gw):
            js = [j for (_, tt), j in chip_vars.items() if tt == t]
            if len(js) > 1:
                m.row(dict.fromkeys(js, 1.0), -INF, 1.0)

    def chip_sum(ct: ChipType, t: int) -> dict[int, float]:
        return dict.fromkeys(by_type.get((ct, t), []), 1.0)

    fh_possible = [bool(by_type.get((ChipType.FREE_HIT, t))) for t in range(n_gw)]

    # ---------------------------------------------------------------- squad, transfers
    x = np.zeros((n_pl, n_gw), dtype=int)
    y = np.zeros((n_pl, n_gw), dtype=int)
    z = np.zeros((n_pl, n_gw), dtype=int)
    for t in range(n_gw):
        for p in range(n_pl):
            x[p, t] = m.var(f"x[{p},{t}]")
            yub = 0.0 if (owned[p] or int(pl.code[p]) in pref.banned) else 1.0
            y[p, t] = m.var(f"y[{p},{t}]", ub=yub)
            z[p, t] = m.var(f"z[{p},{t}]")
            if int(pl.code[p]) in pref.locked:
                m.row({x[p, t]: 1.0}, 1.0, 1.0)
    for p in range(n_pl):
        if not owned[p]:
            m.row({int(y[p, t]): 1.0 for t in range(n_gw)}, -INF, 1.0)
            m.row({int(z[p, t]): 1.0 for t in range(n_gw)}, -INF, 1.0)
    for t in range(n_gw):
        for k, posn in enumerate(POSITIONS):
            m.row(
                {int(x[p, t]): 1.0 for p in np.flatnonzero(pos == k).tolist()},
                quotas.get(posn),
                quotas.get(posn),
            )
        for team in np.unique(pl.team):
            members = np.flatnonzero(pl.team == team).tolist()
            if len(members) > rs.squad.max_per_club:
                m.row({int(x[p, t]): 1.0 for p in members}, -INF, rs.squad.max_per_club)
        for p in range(n_pl):
            coefs: dict[int, float] = {int(x[p, t]): 1.0, int(y[p, t]): -1.0, int(z[p, t]): 1.0}
            if t > 0:
                coefs[int(x[p, t - 1])] = -1.0
                m.row(coefs, 0.0, 0.0)
            else:
                m.row(coefs, float(owned[p]), float(owned[p]))
            m.row({int(y[p, t]): 1.0, int(z[p, t]): 1.0}, -INF, 1.0)
            if t == 0 and not owned[p]:
                m.row({int(z[p, 0]): 1.0}, 0.0, 0.0)

    # first-GW preferences
    for code in pref.forced_out:
        m.row({int(z[idx[code], 0]): 1.0}, 1.0, 1.0)
    for code in pref.forced_in:
        m.row({int(y[idx[code], 0]): 1.0}, 1.0, 1.0)
    if pref.hold_first_gw:
        for p in range(n_pl):
            m.row({int(y[p, 0]): 1.0, int(z[p, 0]): 1.0}, 0.0, 0.0)

    # bank
    bank = [m.var(f"bank[{t}]", 0.0, INF, integer=False) for t in range(n_gw)]
    for t in range(n_gw):
        coefs = {bank[t]: 1.0}
        for p in range(n_pl):
            coefs[int(z[p, t])] = -sale[p]
            coefs[int(y[p, t])] = buy[p]
        if t > 0:
            coefs[bank[t - 1]] = -1.0
            m.row(coefs, 0.0, 0.0)
        else:
            m.row(coefs, bank0, bank0)

    # transfers count, hits, free transfers
    n_expr = [{int(y[p, t]): 1.0 for p in range(n_pl)} for t in range(n_gw)]
    made = [st.transfers_made if t == 0 else 0 for t in range(n_gw)]
    f = [m.var("f[0]", float(st.free_transfers), float(st.free_transfers), integer=True)]
    for t in range(1, n_gw + 1):
        f.append(m.var(f"f[{t}]", 0.0, float(BIG_FT), integer=True))
    h = []
    policy_kind, policy_val = _chip_ft_next(rs.transfers.chip_ft_policy, per)
    for t, g in enumerate(gws):
        top = rs.transfers.top_up_for(g)
        unlimited_const = (
            (g == 1 and rs.transfers.first_gameweek_unlimited)
            or (top is not None and top.unlimited)
            or (prob.initial_squad_mode and t == 0)
        )
        free_chip = {**chip_sum(ChipType.WILDCARD, t), **chip_sum(ChipType.FREE_HIT, t)}
        ht = m.var(f"h[{t}]", 0.0, 15.0 + made[t], integer=True)
        h.append(ht)
        if not unlimited_const:
            # h ≥ n + made − f − BIG·(wc + fh)
            coefs = {ht: 1.0, f[t]: 1.0, **{j: -v for j, v in n_expr[t].items()}}
            for j in free_chip:
                coefs[j] = 30.0
            m.row(coefs, float(made[t]), INF)
        if pref.max_transfers_per_gw is not None:
            m.row(n_expr[t], -INF, float(pref.max_transfers_per_gw))
        # Free Hit week: persistent squad frozen
        fh_js = chip_sum(ChipType.FREE_HIT, t)
        if fh_js:
            m.row(
                {**{int(y[p, t]): 1.0 for p in range(n_pl)}, **dict.fromkeys(fh_js, 15.0)},
                -INF,
                15.0,
            )
            m.row(
                {**{int(z[p, t]): 1.0 for p in range(n_pl)}, **dict.fromkeys(fh_js, 15.0)},
                -INF,
                15.0,
            )
        # next free transfers
        nxt = f[t + 1]
        top_next = rs.transfers.top_up_for(g + 1)
        g_bin = None
        if top_next is not None and top_next.set_free_transfers_to is not None:
            g_bin = m.var(f"ftop[{t}]")
            # g = 1 ⇒ nxt ≤ K (the top-up level); g = 0 ⇒ the regular rules below bind
            m.row(
                {nxt: 1.0, g_bin: float(BIG_FT)},
                -INF,
                float(top_next.set_free_transfers_to) + BIG_FT,
            )
        relax = {g_bin: -float(BIG_FT) * 2} if g_bin is not None else {}
        # Upper bounds only (more free transfers never hurt, so they bind at the optimum).
        # Every '≤' below is relaxed when the top-up branch (g_bin = 1) supplies the bound.
        if (g == 1 and rs.transfers.first_gameweek_unlimited) or (
            prob.initial_squad_mode and t == 0
        ):
            first = rs.transfers.initial_free_transfers_after_first_gameweek
            m.row({nxt: 1.0, **relax}, -INF, float(min(first, cap_ft)))
        else:
            chip_flag = dict(free_chip)
            unlimited_top = top is not None and top.unlimited
            # normal rule: nxt ≤ u + per, with u ≤ max(f − n − made, 0)
            u = m.var(f"u[{t}]", 0.0, float(BIG_FT), integer=True)
            wb = m.var(f"ub[{t}]")
            m.row({u: 1.0, f[t]: -1.0, **n_expr[t], wb: -float(BIG_FT)}, -INF, -float(made[t]))
            m.row({u: 1.0, wb: float(BIG_FT)}, -INF, float(BIG_FT))
            if unlimited_top:
                chip_coefs = None  # whole week behaves like a chip week
            else:
                chip_coefs = {j: -float(BIG_FT) * 2 for j in chip_flag}
                m.row({nxt: 1.0, u: -1.0, **chip_coefs, **relax}, -INF, float(per))
            if chip_flag or unlimited_top:
                # chip-week policy, active when a WC/FH chip is played (or always if unlimited)
                act = {} if unlimited_top else {j: float(BIG_FT) * 2 for j in chip_flag}
                rhs_extra = 0.0 if unlimited_top else float(BIG_FT) * 2
                if policy_kind == "f":
                    m.row(
                        {nxt: 1.0, f[t]: -1.0, **act, **relax}, -INF, float(policy_val) + rhs_extra
                    )
                else:
                    m.row({nxt: 1.0, **act, **relax}, -INF, float(policy_val) + rhs_extra)
            m.row({nxt: 1.0, **relax}, -INF, float(cap_ft))

    if pref.max_hits_total is not None:
        m.row(dict.fromkeys(h, 1.0), -INF, float(pref.max_hits_total))

    # ---------------------------------------------------------------- lineup, Free Hit squad
    s = np.zeros((n_pl, n_gw), dtype=int)
    cpt = np.zeros((n_pl, n_gw), dtype=int)
    vc = np.zeros((n_pl, n_gw), dtype=int)
    bo = {}
    play = np.zeros((n_pl, n_gw), dtype=int)  # variable index of the playing squad indicator
    q = {}
    terms_idx: dict[str, list[tuple[int, float]]] = defaultdict(list)
    for t in range(n_gw):
        d = disc[t]
        fh_js = chip_sum(ChipType.FREE_HIT, t)
        if fh_possible[t]:
            prev_x = [int(x[p, t - 1]) for p in range(n_pl)] if t > 0 else None
            for p in range(n_pl):
                q[(p, t)] = m.var(f"q[{p},{t}]")
                if owned[p]:
                    # originally-owned players can join the Free Hit squad only if still held
                    if prev_x is None:
                        pass
                    else:
                        m.row({q[(p, t)]: 1.0, prev_x[p]: -1.0}, -INF, 0.0)
                wv = m.var(f"w[{p},{t}]", integer=False)
                play[p, t] = wv
                fh_c = dict.fromkeys(fh_js, 1.0)
                m.row({wv: 1.0, int(x[p, t]): -1.0, **dict.fromkeys(fh_c, -1.0)}, -INF, 0.0)
                m.row({wv: 1.0, q[(p, t)]: -1.0, **fh_c}, -INF, 1.0)
                m.row({wv: 1.0, int(x[p, t]): -1.0, **fh_c}, 0.0, INF)
                m.row({wv: 1.0, q[(p, t)]: -1.0, **dict.fromkeys(fh_c, -1.0)}, -1.0, INF)
            for k, posn in enumerate(POSITIONS):
                members = np.flatnonzero(pos == k).tolist()
                m.row(
                    {
                        **{q[(p, t)]: 1.0 for p in members},
                        **{j: -float(quotas.get(posn)) for j in fh_js},
                    },
                    0.0,
                    0.0,
                )
            for team in np.unique(pl.team):
                members = np.flatnonzero(pl.team == team).tolist()
                if len(members) > rs.squad.max_per_club:
                    m.row({q[(p, t)]: 1.0 for p in members}, -INF, rs.squad.max_per_club)
            # budget: Σ cost·q ≤ bank_{t−1} + Σ sale·x_{t−1}. No big-M is needed: when the Free
            # Hit is not played q ≡ 0 and the right-hand side is non-negative.
            coefs = {q[(p, t)]: sale[p] for p in range(n_pl)}
            if t > 0:
                coefs[bank[t - 1]] = -1.0
                for p in range(n_pl):
                    coefs[int(x[p, t - 1])] = coefs.get(int(x[p, t - 1]), 0.0) - sale[p]
                m.row(coefs, -INF, 0.0)
            else:
                m.row(coefs, -INF, bank0 + float(sale[owned].sum()))
            for p in range(n_pl):
                if int(pl.code[p]) in pref.banned and not owned[p]:
                    m.row({q[(p, t)]: 1.0}, 0.0, 0.0)
        else:
            play[:, t] = x[:, t]

        for p in range(n_pl):
            s[p, t] = m.var(f"s[{p},{t}]", cost=d * a[p, t])
            cpt[p, t] = m.var(f"c[{p},{t}]", cost=d * (cap_mult - 1) * a[p, t])
            vc[p, t] = m.var(f"v[{p},{t}]", cost=d * obj.vice_weight * a[p, t])
            terms_idx["starters"].append((int(s[p, t]), d * a[p, t]))
            terms_idx["captain"].append((int(cpt[p, t]), d * (cap_mult - 1) * a[p, t]))
            terms_idx["vice"].append((int(vc[p, t]), d * obj.vice_weight * a[p, t]))
            m.row({int(s[p, t]): 1.0, int(play[p, t]): -1.0}, -INF, 0.0)
            m.row({int(cpt[p, t]): 1.0, int(s[p, t]): -1.0}, -INF, 0.0)
            m.row({int(vc[p, t]): 1.0, int(s[p, t]): -1.0}, -INF, 0.0)
            m.row({int(cpt[p, t]): 1.0, int(vc[p, t]): 1.0}, -INF, 1.0)
        m.row({int(s[p, t]): 1.0 for p in range(n_pl)}, rs.lineup.starters, rs.lineup.starters)
        m.row({int(cpt[p, t]): 1.0 for p in range(n_pl)}, 1.0, 1.0)
        m.row({int(vc[p, t]): 1.0 for p in range(n_pl)}, 1.0, 1.0)
        for k, posn in enumerate(POSITIONS):
            m.row(
                {int(s[p, t]): 1.0 for p in np.flatnonzero(pos == k).tolist()},
                lo_l.get(posn),
                hi_l.get(posn),
            )
        # bench: 3 ordered outfield slots + the bench goalkeeper
        bb_js = chip_sum(ChipType.BENCH_BOOST, t)
        n_slots = rs.lineup.bench_outfield
        for kslot in range(n_slots):
            slot_vars = {}
            for p in np.flatnonzero(~gk).tolist():
                bo[(p, t, kslot)] = m.var(f"b[{p},{t},{kslot}]")
                slot_vars[bo[(p, t, kslot)]] = 1.0
            m.row(slot_vars, 1.0, 1.0)
            bv = m.var(
                f"bv[{t},{kslot}]", -INF, INF, integer=False, cost=d * obj.bench_weights[kslot]
            )
            terms_idx["bench"].append((bv, d * obj.bench_weights[kslot]))
            m.row(
                {bv: 1.0, **{bo[(p, t, kslot)]: -ev[p, t] for p in np.flatnonzero(~gk).tolist()}},
                -INF,
                0.0,
            )
            if bb_js:
                m.row({bv: 1.0, **dict.fromkeys(bb_js, big_ev)}, -INF, big_ev)
        for p in np.flatnonzero(~gk).tolist():
            m.row(
                {
                    **{bo[(p, t, k)]: 1.0 for k in range(n_slots)},
                    int(play[p, t]): -1.0,
                    int(s[p, t]): 1.0,
                },
                0.0,
                0.0,
            )
        bvg = m.var(f"bvg[{t}]", -INF, INF, integer=False, cost=d * obj.bench_gk_weight)
        terms_idx["bench"].append((bvg, d * obj.bench_gk_weight))
        m.row(
            {
                bvg: 1.0,
                **{int(play[p, t]): -ev[p, t] for p in np.flatnonzero(gk).tolist()},
                **{int(s[p, t]): ev[p, t] for p in np.flatnonzero(gk).tolist()},
            },
            -INF,
            0.0,
        )
        if bb_js:
            m.row({bvg: 1.0, **dict.fromkeys(bb_js, big_ev)}, -INF, big_ev)
            for p in range(n_pl):
                bbx = m.var(f"bbx[{p},{t}]", 0.0, 1.0, integer=False, cost=d * a[p, t])
                terms_idx["bench_boost"].append((bbx, d * a[p, t]))
                m.row({bbx: 1.0, int(play[p, t]): -1.0, int(s[p, t]): 1.0}, -INF, 0.0)
                m.row({bbx: 1.0, **dict.fromkeys(bb_js, -1.0)}, -INF, 0.0)
        tc_js = chip_sum(ChipType.TRIPLE_CAPTAIN, t)
        if tc_js:
            for p in range(n_pl):
                tcx = m.var(
                    f"tcx[{p},{t}]",
                    0.0,
                    1.0,
                    integer=False,
                    cost=d * (tc_mult - cap_mult) * a[p, t],
                )
                terms_idx["triple_captain"].append((tcx, d * (tc_mult - cap_mult) * a[p, t]))
                m.row({tcx: 1.0, int(cpt[p, t]): -1.0}, -INF, 0.0)
                m.row({tcx: 1.0, **dict.fromkeys(tc_js, -1.0)}, -INF, 0.0)
        # costs of transfers
        m.add_cost(h[t], -d * hit_cost)
        terms_idx["hits"].append((h[t], -d * hit_cost))
        if obj.transfer_penalty:
            for j in n_expr[t]:
                m.add_cost(j, -d * obj.transfer_penalty)
                terms_idx["transfer_penalty"].append((j, -d * obj.transfer_penalty))
    # sunk hits already taken this GW are not part of the decision
    paid_before = max(0, st.transfers_made - st.free_transfers)
    m.const += disc[0] * hit_cost * paid_before

    # terminal values
    m.add_cost(f[n_gw], obj.free_transfer_value)
    terms_idx["free_transfers_end"].append((f[n_gw], obj.free_transfer_value))
    if obj.bank_value_per_tenth:
        m.add_cost(bank[n_gw - 1], obj.bank_value_per_tenth)
        terms_idx["bank_end"].append((bank[n_gw - 1], obj.bank_value_per_tenth))
    if obj.terminal_squad_weight:
        for p in range(n_pl):
            m.add_cost(int(x[p, n_gw - 1]), obj.terminal_squad_weight * ev[p, n_gw - 1])
            terms_idx["terminal_squad"].append(
                (int(x[p, n_gw - 1]), obj.terminal_squad_weight * ev[p, n_gw - 1])
            )

    for cut, rhs in extra_cuts or []:
        coefs = {}
        for (kind, p, t), v in cut.items():
            coefs[int({"y": y, "z": z}[kind][p, t])] = v
        m.row(coefs, -INF, rhs)

    sol, objective, status, stats = m.solve(cfg.solver, start.values if start else None)
    if sol.size == 0:
        raise OptimizationError(f"no feasible solution ({status})")
    stats["status"] = status

    # ---------------------------------------------------------------- extraction
    def on(j: int) -> bool:
        return bool(sol[j] > 0.5)

    plans: list[GameweekPlan] = []
    # Free transfers are only upper-bounded in the MILP (more never hurts), so when they do not
    # bind they can be slack; the reported path is recomputed with the domain rule.
    ft_now = st.free_transfers
    for t, g in enumerate(gws):
        squad = tuple(int(pl.code[p]) for p in range(n_pl) if on(int(x[p, t])))
        playing = tuple(int(pl.code[p]) for p in range(n_pl) if sol[int(play[p, t])] > 0.5)
        chip_id = next((cid for (cid, tt), j in chip_vars.items() if tt == t and on(j)), None)
        chip_type = chip_state[chip_id].chip_type if chip_id else None
        if chip_type is ChipType.FREE_HIT:
            prev = (
                {int(pl.code[p]) for p in range(n_pl) if on(int(x[p, t - 1]))}
                if t > 0
                else {int(pl.code[p]) for p in np.flatnonzero(owned).tolist()}
            )
            t_in = tuple(sorted(set(playing) - prev))
            t_out = tuple(sorted(prev - set(playing)))
        else:
            t_in = tuple(sorted(int(pl.code[p]) for p in range(n_pl) if on(int(y[p, t]))))
            t_out = tuple(sorted(int(pl.code[p]) for p in range(n_pl) if on(int(z[p, t]))))
        starters = [p for p in range(n_pl) if on(int(s[p, t]))]
        starters_codes = tuple(
            int(pl.code[p]) for p in sorted(starters, key=lambda p: (pos[p], pl.code[p]))
        )
        gk_bench = [
            int(pl.code[p])
            for p in range(n_pl)
            if gk[p] and sol[int(play[p, t])] > 0.5 and not on(int(s[p, t]))
        ]
        out_bench = []
        for kslot in range(rs.lineup.bench_outfield):
            pk = next(p for p in np.flatnonzero(~gk).tolist() if on(bo[(p, t, kslot)]))
            out_bench.append(int(pl.code[pk]))
        cap = next(int(pl.code[p]) for p in range(n_pl) if on(int(cpt[p, t])))
        vice = next(int(pl.code[p]) for p in range(n_pl) if on(int(vc[p, t])))
        lineup = Lineup(
            starters=starters_codes,
            bench=tuple(gk_bench + out_bench),
            captain=cap,
            vice_captain=vice,
        )
        hits = round(sol[h[t]])
        paid_this = hits - (paid_before if t == 0 else 0)
        exp_pts = float(sum(ev[p, t] for p in starters))
        ci = idx[cap]
        mult = tc_mult if chip_type is ChipType.TRIPLE_CAPTAIN else cap_mult
        exp_pts += (mult - 1) * ev[ci, t]
        if chip_type is ChipType.BENCH_BOOST:
            exp_pts += sum(ev[idx[c], t] for c in gk_bench + out_bench)
        plans.append(
            GameweekPlan(
                gameweek=g,
                squad=tuple(sorted(squad)),
                playing_squad=tuple(sorted(playing)),
                transfers_in=t_in,
                transfers_out=t_out,
                chip_id=chip_id,
                chip_type=chip_type,
                lineup=lineup,
                paid_transfers=max(paid_this, 0),
                hit_points=max(paid_this, 0) * hit_cost,
                free_transfers=ft_now,
                bank_after=round(sol[bank[t]]),
                expected_points=exp_pts,
                objective=0.0,
            )
        )
        if prob.initial_squad_mode and t == 0:
            ft_now = min(rs.transfers.initial_free_transfers_after_first_gameweek, cap_ft)
        else:
            n_made = len(t_in) if chip_type is not ChipType.FREE_HIT else 0
            ft_now = next_free_transfers(ft_now, n_made + made[t], g, chip_type, rs)
    terms = {k: float(sum(sol[j] * c for j, c in v)) for k, v in terms_idx.items()}
    chip_cost = sum(
        sol[j] * obj.chip_values.get(chip_state[cid].chip_type.value, 0.0)
        for (cid, _), j in chip_vars.items()
    )
    if chip_cost:
        terms["chip_opportunity_cost"] = -float(chip_cost)
    result = Solution(
        plans=plans,
        objective=objective,
        status=status,
        stats=stats,
        free_transfers_end=ft_now,
        terms=terms,
        values={n: float(v) for n, v in zip(m.names, sol, strict=True) if v != 0.0},
    )
    if status != "Optimal":
        _polish(prob, result, round(sol[f[n_gw]]))
    return result


def _polish(prob: OptimizationProblem, sol: Solution, ft_end_var: int) -> None:
    """Repair a non-optimal (time-limited) incumbent without changing any transfer decision:
    re-solve each gameweek's lineup exactly for the chosen playing squad (the incumbent's
    lineup need not be optimal) and replace the free-transfer term by the true end count (the
    variable may be slack). The objective is corrected by the same amounts, so it stays equal to
    an independent recomputation (ADR-0007)."""
    pl, w = prob.players, prob.config.objective
    idx = pl.index()
    pos = pl.positions_map()
    down = pl.downside()
    gain = 0.0
    for t, plan in enumerate(sol.plans):
        ev = {c: float(pl.ev[idx[c], t]) for c in plan.playing_squad}
        val = {
            c: float(pl.ev[idx[c], t] - w.risk_aversion * down[idx[c], t])
            for c in plan.playing_squad
        }
        best = best_lineup(plan.playing_squad, pos, ev, val, w, prob.ruleset, plan.chip_type)
        mine = _lineup_objective(plan, ev, val, w, prob, pos)
        if best.value > mine + 1e-9:
            gain += w.discount**t * (best.value - mine)
            plan.lineup = best.lineup
            plan.expected_points = best.expected_points
    gain += w.free_transfer_value * (sol.free_transfers_end - ft_end_var)
    sol.objective += gain
    sol.stats["polished_gain"] = gain


def _lineup_objective(
    plan: GameweekPlan,
    ev: dict[int, float],
    val: dict[int, float],
    w: Any,
    prob: OptimizationProblem,
    pos: dict[int, Position],
) -> float:
    lu, rs = plan.lineup, prob.ruleset
    ct = plan.chip_type
    mult = (
        rs.chips.triple_captain_multiplier
        if ct is ChipType.TRIPLE_CAPTAIN
        else rs.captaincy.captain_multiplier
    )
    v = sum(val[c] for c in lu.starters) + (mult - 1) * val[lu.captain]
    v += w.vice_weight * val[lu.vice_captain]
    gk_bench = [c for c in lu.bench if pos[c] is Position.GK]
    out_bench = [c for c in lu.bench if c not in gk_bench]
    if ct is ChipType.BENCH_BOOST:
        v += sum(val[c] for c in lu.bench)
    else:
        v += sum(wk * ev[c] for wk, c in zip(w.bench_weights, out_bench, strict=False))
        v += w.bench_gk_weight * sum(ev[c] for c in gk_bench)
    return float(v)


def solve_with_chips(prob: OptimizationProblem) -> Solution:
    """Optimise with chip choice open, warm-started from the no-chip optimum so the incumbent is
    never worse than not playing a chip (chip-open MILPs are much harder; ADR-0007)."""
    if not prob.chip_options or prob.forced_chips:
        return build_and_solve(prob)
    base = build_and_solve(replace(prob, chip_options={}))
    return build_and_solve(prob, start=base)


def plan_positions(prob: OptimizationProblem) -> dict[int, Position]:
    return prob.players.positions_map()
