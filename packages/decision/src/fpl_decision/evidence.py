"""Structured evidence and explanations (§26, §68, ADR-0010).

Explanations are generated *only* from evidence objects carrying the numeric value, the
comparison baseline, the source snapshot and the model version (§68.1). A primary driver is the
rendering of an evidence item; there is no free-text reason without an evidence id behind it
("no unsupported reasons", M9 acceptance). A language model may later paraphrase these, but the
numbers remain the source of truth.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any, Literal

import numpy as np
import pandas as pd
from pydantic import BaseModel, ConfigDict

from fpl_domain.hashing import short_id

COMPONENT_LABELS = {
    "xp_appearance": "appearance points",
    "xp_goals": "goal points",
    "xp_assists": "assist points",
    "xp_clean_sheet": "clean-sheet points",
    "xp_goals_conceded": "goals-conceded deductions",
    "xp_saves": "save points",
    "xp_penalty_saves": "penalty-save points",
    "xp_penalty_misses": "penalty-miss deductions",
    "xp_yellow_cards": "yellow-card deductions",
    "xp_red_cards": "red-card deductions",
    "xp_own_goals": "own-goal deductions",
    "xp_defensive_contribution": "defensive-contribution points",
    "xp_bonus": "bonus points",
}


class Evidence(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    evidence_id: str
    feature: str
    label: str
    player_id: int | None
    value: float
    baseline: float | None
    direction: Literal["positive", "negative", "neutral"]
    unit: str
    gameweeks: tuple[int, ...]
    source_snapshot: str
    model_version: str
    importance: float = 0.0


def _ev(
    feature: str,
    label: str,
    player: int | None,
    value: float,
    baseline: float | None,
    unit: str,
    gws: Sequence[int],
    snapshot: str,
    model: str,
    higher_is_better: bool = True,
    importance: float = 0.0,
) -> Evidence:
    diff = value - (baseline if baseline is not None else 0.0)
    if abs(diff) < 1e-9:
        direction: Literal["positive", "negative", "neutral"] = "neutral"
    else:
        direction = "positive" if (diff > 0) == higher_is_better else "negative"
    payload = {"f": feature, "p": player, "g": list(gws), "s": snapshot, "v": round(value, 6)}
    return Evidence(
        evidence_id=short_id("ev", payload),
        feature=feature,
        label=label,
        player_id=player,
        value=float(value),
        baseline=None if baseline is None else float(baseline),
        direction=direction,
        unit=unit,
        gameweeks=tuple(int(g) for g in gws),
        source_snapshot=snapshot,
        model_version=model,
        importance=float(importance),
    )


def move_evidence(
    out_codes: Iterable[int],
    in_codes: Iterable[int],
    summary: pd.DataFrame,
    gameweeks: Sequence[int],
    snapshot: str,
    model_version: str,
    features: pd.DataFrame | None = None,
    price_probs: pd.DataFrame | None = None,
    names: dict[int, str] | None = None,
) -> list[Evidence]:
    """Evidence comparing incoming with outgoing players over the horizon."""
    outs, ins = list(out_codes), list(in_codes)
    s = summary[summary["gw"].isin(gameweeks)]
    comp_cols = [c for c in s.columns if c.startswith("xp_")]
    tot = s.groupby("player_code")[["mean", *comp_cols]].sum()

    def total(codes: list[int], col: str) -> float:
        return float(tot[col].reindex(codes).fillna(0.0).sum())

    items: list[Evidence] = []

    def name(c: int) -> str:
        return (names or {}).get(c, str(c))

    who_in = ", ".join(name(c) for c in ins) or "—"
    who_out = ", ".join(name(c) for c in outs) or "—"
    deltas = {c: total(ins, c) - total(outs, c) for c in comp_cols}
    norm = sum(abs(v) for v in deltas.values()) or 1.0
    for c, d in deltas.items():
        if abs(d) < 0.05:
            continue
        items.append(
            _ev(
                c,
                f"{COMPONENT_LABELS.get(c, c)} over {len(gameweeks)} GWs: {who_in} vs {who_out}",
                ins[0] if len(ins) == 1 else None,
                total(ins, c),
                total(outs, c),
                "points",
                gameweeks,
                snapshot,
                model_version,
                importance=abs(d) / norm,
            )
        )
    items.append(
        _ev(
            "expected_points",
            f"expected points over {len(gameweeks)} GWs: {who_in} vs {who_out}",
            ins[0] if len(ins) == 1 else None,
            total(ins, "mean"),
            total(outs, "mean"),
            "points",
            gameweeks,
            snapshot,
            model_version,
            importance=abs(total(ins, "mean") - total(outs, "mean")) / norm,
        )
    )
    first = s[s["gw"] == gameweeks[0]].set_index("player_code")
    for codes, side in ((ins, "in"), (outs, "out")):
        for c in codes:
            if c in first.index and "prob_start" in first.columns:
                ps = float(first.loc[c, "prob_start"])
                items.append(
                    _ev(
                        "start_probability",
                        f"P(start) GW{gameweeks[0]} — {name(c)} ({side})",
                        c,
                        ps,
                        None,
                        "probability",
                        gameweeks[:1],
                        snapshot,
                        model_version,
                    )
                )
    if features is not None and len(features):
        f = features[features["target_gw"].isin(gameweeks)]
        g = f.groupby("player_code")[["opp_xg_against_ewm", "opp_xg_for_ewm"]].mean()
        for codes, side in ((ins, "in"), (outs, "out")):
            for c in codes:
                if c in g.index:
                    items.append(
                        _ev(
                            "opponent_xg_conceded",
                            f"opponents' recent xG conceded per match — {name(c)} ({side})",
                            c,
                            float(g.loc[c, "opp_xg_against_ewm"]),
                            None,
                            "xG/match",
                            gameweeks,
                            snapshot,
                            "features-" + model_version,
                        )
                    )
        st = features[features["target_gw"] == gameweeks[0]].drop_duplicates("player_code")
        st = st.set_index("player_code")
        for c in outs:
            if c in st.index and not np.isnan(float(st.loc[c].get("chance_of_playing", np.nan))):
                items.append(
                    _ev(
                        "chance_of_playing",
                        f"official chance of playing — {name(c)} (out)",
                        c,
                        float(st.loc[c, "chance_of_playing"]),
                        100.0,
                        "%",
                        gameweeks[:1],
                        snapshot,
                        "fpl-live-status",
                    )
                )
    if price_probs is not None and len(price_probs):
        pp = price_probs.set_index("player_code")
        for codes, side in ((ins, "in"), (outs, "out")):
            for c in codes:
                if c in pp.index:
                    items.append(
                        _ev(
                            "price_rise_probability",
                            f"P(price rise before next deadline) — {name(c)} ({side})",
                            c,
                            float(pp.loc[c, "p_rise"]),
                            None,
                            "probability",
                            gameweeks[:1],
                            snapshot,
                            "price_change_lgbm-1.0.0",
                        )
                    )
    items.sort(key=lambda e: -e.importance)
    return items


class Driver(BaseModel):
    text: str
    evidence_ids: tuple[str, ...]


def primary_drivers(evidence: Sequence[Evidence], k: int = 4) -> list[Driver]:
    """Top-k evidence items by importance rendered as drivers — never text without evidence."""
    out = []
    for e in [e for e in evidence if e.importance > 0][:k]:
        delta = e.value - (e.baseline or 0.0)
        out.append(
            Driver(
                text=f"{e.label}: {e.value:.2f} vs {e.baseline:.2f} ({delta:+.2f} {e.unit})"
                if e.baseline is not None
                else f"{e.label}: {e.value:.2f} {e.unit}",
                evidence_ids=(e.evidence_id,),
            )
        )
    return out


def evidence_index(evidence: Sequence[Evidence]) -> dict[str, dict[str, Any]]:
    return {e.evidence_id: e.model_dump() for e in evidence}
