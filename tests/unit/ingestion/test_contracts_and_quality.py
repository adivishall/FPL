"""Data contracts and quality gates (§55.1, §55.2, §36 'Data quality')."""

from __future__ import annotations

import pandas as pd
import pytest

from fpl_domain.enums import Severity
from fpl_ingestion.contracts import MERGED_GW, ColumnSpec, DataContract
from fpl_ingestion.quality import SeasonFrames, run_quality_gates
from tests.fixtures_util import validated_frames


def codes(frames: SeasonFrames, severity: Severity | None = None) -> set[str]:
    return {i.code for i in frames.issues if severity is None or i.severity is severity}


def test_real_excerpt_passes_gates_with_expected_upstream_anomaly() -> None:
    f = validated_frames("2025-26")
    assert f.gate_passed
    assert "duplicate_exact" in codes(f, Severity.WARNING)  # known upstream duplicate rows
    assert not codes(f, Severity.ERROR), [i.message for i in f.issues]
    assert not f.merged_gw.duplicated(["fixture", "element"]).any()


@pytest.mark.parametrize("season", ["2024-25", "2026-27"])
def test_other_excerpts_are_clean(season: str) -> None:
    f = validated_frames(season)
    assert f.gate_passed
    assert not codes(f, Severity.ERROR)


def _contract() -> DataContract:
    return DataContract(
        "t",
        "1",
        "thing",
        (
            ColumnSpec("id", "int", min=1),
            ColumnSpec("x", "float", min=0, max=1),
            ColumnSpec("kind", "str", allowed=frozenset({"a", "b"})),
            ColumnSpec("opt", "int", required=False, nullable=True),
        ),
        unique_key=("id",),
    )


def test_contract_detects_range_allowed_and_missing_optional() -> None:
    df = pd.DataFrame({"id": [1, 2, 3], "x": [0.5, 2.0, -1.0], "kind": ["a", "c", "b"]})
    out, issues = _contract().validate(df)
    got = {i.code: i.count for i in issues}
    assert got == {"range_violation": 2, "allowed_values": 1}
    assert "opt" in out.columns and out["opt"].isna().all()


def test_missing_required_column_is_critical() -> None:
    _, issues = _contract().validate(pd.DataFrame({"id": [1], "kind": ["a"]}))
    assert any(
        i.code == "schema_missing_column" and i.severity is Severity.CRITICAL for i in issues
    )


def test_exact_duplicates_deduplicated_but_conflicts_are_critical() -> None:
    df = pd.DataFrame({"id": [1, 1, 2, 2], "x": [0.1, 0.1, 0.2, 0.3], "kind": ["a"] * 4})
    _, issues = _contract().validate(df)
    by = {i.code: i for i in issues}
    assert by["duplicate_exact"].severity is Severity.WARNING
    assert by["duplicate_conflicting"].severity is Severity.CRITICAL


def test_type_coercion_failure_is_critical() -> None:
    _, issues = _contract().validate(pd.DataFrame({"id": ["x"], "x": [0.1], "kind": ["a"]}))
    assert any(i.code == "schema_type" for i in issues)


def test_xp_column_is_declared_high_leakage() -> None:
    assert MERGED_GW.spec("xP").leakage == "high"


# ---------------------------------------------------------------- fault injection


def _fresh() -> SeasonFrames:
    f = validated_frames("2025-26")
    return SeasonFrames(
        f.season,
        f.merged_gw.drop(columns=["team_id"]).copy(),
        f.fixtures.copy(),
        f.teams.copy(),
        f.players_raw.copy(),
        [],
    )


def test_unknown_fixture_reference_quarantines() -> None:
    f = _fresh()
    f.merged_gw.loc[f.merged_gw.index[0], "fixture"] = 99_999
    out = run_quality_gates(f)
    assert not out.gate_passed and "ref_missing_fixture" in codes(out, Severity.CRITICAL)


def test_score_mismatch_is_detected() -> None:
    f = _fresh()
    fid = int(f.fixtures.loc[f.fixtures["finished"].astype(bool), "id"].iloc[0])
    f.fixtures.loc[f.fixtures["id"] == fid, "team_h_score"] += 1
    assert "score_reconciliation" in codes(run_quality_gates(f), Severity.ERROR)


def test_extra_starter_is_detected() -> None:
    f = _fresh()
    idx = f.merged_gw[(f.merged_gw["starts"] == 0)].index[0]
    f.merged_gw.loc[idx, "starts"] = 1
    assert "starters_not_eleven" in codes(run_quality_gates(f), Severity.ERROR)


def test_position_conflict_with_registry_is_detected() -> None:
    f = _fresh()
    i = f.merged_gw.index[f.merged_gw["position"] == "MID"][0]
    f.merged_gw.loc[i, "position"] = "FWD"
    assert "position_conflict" in codes(run_quality_gates(f), Severity.ERROR)


def test_unpopulated_starts_become_null_not_zero() -> None:
    f = _fresh()
    fid = int(f.merged_gw["fixture"].iloc[0])
    f.merged_gw.loc[f.merged_gw["fixture"] == fid, "starts"] = 0
    out = run_quality_gates(f)
    assert "starts_unpopulated" in codes(out, Severity.WARNING)
    assert out.merged_gw.loc[out.merged_gw["fixture"] == fid, "starts"].isna().all()
