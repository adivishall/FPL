"""Promotion gates, drift statistics and retraining triggers (§71)."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from fpl_forecasting.governance import (
    Gate,
    RetrainPolicy,
    evaluate_gates,
    feature_drift,
    psi,
    retraining_decision,
)
from fpl_forecasting.model_config import minutes_spec, points_spec, price_spec


def test_gates_compare_to_thresholds_references_and_bounds() -> None:
    m = {"rmse": 2.0, "rmse_base": 2.1, "cov": 0.8, "ece": 0.03}
    gates = [
        Gate(name="a", metric="rmse", op="<=", reference="rmse_base"),
        Gate(name="b", metric="rmse", op="<=", reference="rmse_base", margin=-0.2),
        Gate(name="c", metric="cov", op="between", bounds=(0.75, 0.9)),
        Gate(name="d", metric="ece", op="<=", threshold=0.02),
        Gate(name="e", metric="missing", op="<", threshold=1.0),
    ]
    rep = evaluate_gates(m, gates)
    assert [c.passed for c in rep.checks] == [True, False, True, False, False]
    assert not rep.passed
    assert rep.checks[4].target == "metric missing"
    assert evaluate_gates(m, gates[:1] + gates[2:3]).passed


def test_gate_without_target_is_a_config_error() -> None:
    with pytest.raises(ValueError, match="threshold or reference"):
        evaluate_gates({"x": 1.0}, [Gate(name="x", metric="x", op="<")])


def test_psi_detects_shift_and_missingness() -> None:
    rng = np.random.default_rng(0)
    ref = pd.Series(rng.normal(0, 1, 5000))
    assert psi(ref, pd.Series(rng.normal(0, 1, 5000))) < 0.02
    assert psi(ref, pd.Series(rng.normal(1.0, 1, 5000))) > 0.25
    cur = pd.Series(rng.normal(0, 1, 5000))
    cur[:2000] = np.nan
    assert psi(ref, cur) > 0.25
    df = feature_drift(pd.DataFrame({"a": ref}), pd.DataFrame({"a": ref + 2}), ["a", "zz"])
    assert list(df["feature"]) == ["a"] and df.loc[0, "level"] == "major"


def test_retraining_triggers_report_reasons() -> None:
    pol = RetrainPolicy(every_gameweeks=4)
    quiet = retraining_decision(pol, 1, 2.0, {"rmse": 2.05, "bias": 0.1, "ece": 0.01})
    assert not quiet.retrain and quiet.reasons == []
    sched = retraining_decision(pol, 4, 2.0)
    assert sched.retrain and sched.reasons[0].startswith("schedule")
    degr = retraining_decision(pol, 1, 2.0, {"rmse": 2.5, "bias": 0.6, "ece": 0.09})
    assert len(degr.reasons) == 3
    drift = pd.DataFrame({"feature": list("abcd"), "psi": [0.3, 0.4, 0.5, 0.6]})
    assert retraining_decision(pol, 1, 2.0, drift=drift).reasons[0].startswith("feature drift")


def test_model_configs_parse_and_gates_are_well_formed() -> None:
    for spec, vc in (points_spec(), minutes_spec(), price_spec()):
        names = [g.name for g in spec.promotion_gates]
        assert names and len(names) == len(set(names))
        for g in spec.promotion_gates:
            assert (g.threshold is not None) or (g.reference is not None) or (g.bounds is not None)
        assert vc.ref.startswith("models/")
    ps, _ = points_spec()
    assert ps.training.horizons == (0, 1, 2, 3, 4)
