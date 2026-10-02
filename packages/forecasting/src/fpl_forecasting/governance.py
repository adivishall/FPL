"""Model governance: promotion gates, drift monitoring and retraining triggers (§34, §37, §71).

* **Promotion gates** — declarative checks from ``config/models/<model>.yaml``
  (``promotion_gates``) evaluated against a flat metrics dictionary produced by the walk-forward
  evaluation. A candidate is approved only if *every* gate passes; the full report (value,
  threshold, pass/fail per gate) is stored in the registry with the version.
* **Feature drift** — population stability index (PSI) of each feature between the training
  reference window and the current feature snapshot (quantile bins from the reference).
* **Residual / calibration drift** — recent RMSE relative to the evaluation RMSE, recent
  bias, and recent ECE of a key probability.
* **Retraining triggers** — periodic schedule *plus* material degradation or drift (§71), each
  reported with its reason so the decision to retrain is auditable.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Literal

import numpy as np
import pandas as pd
from pydantic import BaseModel, ConfigDict, Field

from fpl_forecasting import metrics as fm


class Gate(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    metric: str
    op: Literal["<=", ">=", "<", ">", "between"]
    threshold: float | None = None
    reference: str | None = Field(None, description="compare against another metric")
    margin: float = 0.0  # metric op (reference + margin)
    bounds: tuple[float, float] | None = None
    rationale: str = ""


class GateCheck(BaseModel):
    name: str
    metric: str
    value: float | None
    target: str
    passed: bool
    rationale: str = ""


class GateReport(BaseModel):
    passed: bool
    checks: list[GateCheck]
    config_ref: str | None = None


def evaluate_gates(
    metrics: Mapping[str, float], gates: Iterable[Gate], config_ref: str | None = None
) -> GateReport:
    checks: list[GateCheck] = []
    for g in gates:
        value = metrics.get(g.metric)
        if value is None or not np.isfinite(value):
            checks.append(
                GateCheck(
                    name=g.name,
                    metric=g.metric,
                    value=None,
                    target="metric missing",
                    passed=False,
                    rationale=g.rationale,
                )
            )
            continue
        if g.op == "between":
            if g.bounds is None:
                raise ValueError(f"gate {g.name}: 'between' needs bounds")
            lo, hi = g.bounds
            ok = lo <= value <= hi
            target = f"in [{lo}, {hi}]"
        else:
            if g.reference is not None:
                ref = metrics.get(g.reference)
                if ref is None:
                    checks.append(
                        GateCheck(
                            name=g.name,
                            metric=g.metric,
                            value=float(value),
                            target=f"reference {g.reference} missing",
                            passed=False,
                            rationale=g.rationale,
                        )
                    )
                    continue
                bound = float(ref) + g.margin
                target = f"{g.op} {g.reference} {g.margin:+g} (= {bound:.5f})"
            elif g.threshold is not None:
                bound = g.threshold
                target = f"{g.op} {bound}"
            else:
                raise ValueError(f"gate {g.name}: needs threshold or reference")
            ok = {
                "<=": value <= bound,
                ">=": value >= bound,
                "<": value < bound,
                ">": value > bound,
            }[g.op]
        checks.append(
            GateCheck(
                name=g.name,
                metric=g.metric,
                value=float(value),
                target=target,
                passed=bool(ok),
                rationale=g.rationale,
            )
        )
    return GateReport(passed=all(c.passed for c in checks), checks=checks, config_ref=config_ref)


# ---------------------------------------------------------------------------- drift


def psi(reference: pd.Series, current: pd.Series, bins: int = 10, eps: float = 1e-4) -> float:
    """Population stability index with quantile bins of the reference (missing = own bin)."""
    ref = pd.to_numeric(reference, errors="coerce").astype(float)
    cur = pd.to_numeric(current, errors="coerce").astype(float)
    r, c = ref.dropna().to_numpy(), cur.dropna().to_numpy()
    if len(r) == 0 or len(c) == 0:
        return float("nan")
    edges = np.unique(np.quantile(r, np.linspace(0, 1, bins + 1)[1:-1]))
    rb = np.bincount(np.searchsorted(edges, r, side="right"), minlength=len(edges) + 1)
    cb = np.bincount(np.searchsorted(edges, c, side="right"), minlength=len(edges) + 1)
    rp = np.append(rb, ref.isna().sum()) / len(ref)
    cp = np.append(cb, cur.isna().sum()) / len(cur)
    rp, cp = np.clip(rp, eps, None), np.clip(cp, eps, None)
    return float(np.sum((cp - rp) * np.log(cp / rp)))


def feature_drift(
    reference: pd.DataFrame, current: pd.DataFrame, features: Iterable[str], bins: int = 10
) -> pd.DataFrame:
    rows = [
        {"feature": f, "psi": psi(reference[f], current[f], bins)}
        for f in features
        if f in reference.columns and f in current.columns
    ]
    out = pd.DataFrame(rows)
    if out.empty:
        return out
    out["level"] = pd.cut(
        out["psi"], [-np.inf, 0.1, 0.25, np.inf], labels=["stable", "moderate", "major"]
    ).astype(str)
    return out.sort_values("psi", ascending=False).reset_index(drop=True)


class DriftThresholds(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    psi_major: float = 0.25
    max_major_features: int = 3
    rmse_ratio: float = 1.15
    abs_bias: float = 0.4
    ece: float = 0.05


class RetrainPolicy(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    every_gameweeks: int = 4
    drift: DriftThresholds = Field(default_factory=DriftThresholds)


class RetrainDecision(BaseModel):
    retrain: bool
    reasons: list[str]
    diagnostics: dict[str, float]


def residual_diagnostics(
    pred: np.ndarray, y: np.ndarray, prob: np.ndarray | None = None, event: np.ndarray | None = None
) -> dict[str, float]:
    d = {"rmse": fm.rmse(pred, y), "bias": fm.bias(pred, y), "n": float(len(y))}
    if prob is not None and event is not None:
        d["ece"] = fm.ece(prob, event)
    return d


def retraining_decision(
    policy: RetrainPolicy,
    gws_since_training: int,
    reference_rmse: float,
    recent: Mapping[str, float] | None = None,
    drift: pd.DataFrame | None = None,
) -> RetrainDecision:
    reasons: list[str] = []
    diag: dict[str, float] = {"gws_since_training": float(gws_since_training)}
    th = policy.drift
    if gws_since_training >= policy.every_gameweeks:
        reasons.append(f"schedule: {gws_since_training} GWs since training")
    if recent is not None:
        ratio = recent["rmse"] / reference_rmse if reference_rmse > 0 else float("nan")
        diag.update({"recent_rmse": recent["rmse"], "rmse_ratio": ratio, "bias": recent["bias"]})
        if ratio > th.rmse_ratio:
            reasons.append(f"degradation: RMSE ratio {ratio:.2f} > {th.rmse_ratio}")
        if abs(recent["bias"]) > th.abs_bias:
            reasons.append(f"residual drift: bias {recent['bias']:+.2f}")
        if "ece" in recent and recent["ece"] > th.ece:
            diag["ece"] = recent["ece"]
            reasons.append(f"calibration drift: ECE {recent['ece']:.3f} > {th.ece}")
    if drift is not None and not drift.empty:
        major = drift[drift["psi"] > th.psi_major]
        diag["n_major_drift_features"] = float(len(major))
        if len(major) > th.max_major_features:
            names = ", ".join(major["feature"].head(5))
            reasons.append(f"feature drift: {len(major)} features PSI > {th.psi_major} ({names})")
    return RetrainDecision(retrain=bool(reasons), reasons=reasons, diagnostics=diag)
