"""Decision journal evaluation (M1.4) on hand-built results: the evaluator reproduces the
official gameweek rules (auto-substitution in bench order, vice-captain fallback, Triple
Captain) for the recorded action and for HOLD, keeps an evaluation pending or partial until
every relevant fixture is final and recorded, never invents a total, and states comparisons
without grading the decision."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pandas as pd
import pytest

from fpl_api.journal import (
    CAVEAT,
    DecisionEvidence,
    Expectation,
    GainExpectation,
    JournalRefused,
    PlanEvidence,
    SystemRationale,
    _reconstruct_hold_lineup,
    adherence,
    choose,
    evaluate_decision,
    evidence_from_workbench,
    exported_before,
    verify_decision,
    workbench_recommended_index,
)
from fpl_domain.enums import Position
from fpl_domain.rules import load_ruleset

SEASON, GW = "2026-27", 5
POS = {1: "GK", 2: "GK", 3: "DEF", 4: "DEF", 5: "DEF", 6: "DEF", 7: "DEF", 8: "MID", 9: "MID"}
POS |= {10: "MID", 11: "MID", 12: "MID", 13: "FWD", 14: "FWD", 15: "FWD"}
TEAM = {c: (100 if c % 2 else 200) for c in POS}  # odd codes play fixture 1, even fixture 2
STARTERS = [1, 3, 4, 5, 6, 8, 9, 10, 11, 13, 14]
BENCH = [2, 7, 12, 15]
NOW = datetime(2026, 9, 20, tzinfo=UTC)
DEADLINE = "2026-09-12T17:30:00+00:00"


def plan(label: str = "hold", action: str = "HOLD", **kw: object) -> PlanEvidence:
    base: dict[str, object] = {
        "label": label,
        "action": action,
        "sells": [],
        "buys": [],
        "hit_points": 0,
        "chip": None,
        "starters": STARTERS,
        "bench": BENCH,
        "captain": 8,
        "vice_captain": 9,
        "lineup_source": "stored",
        "expected": Expectation(mean=50.0, p10=35.0, p90=68.0),
    }
    base.update(kw)
    return PlanEvidence.model_validate(base)


def evidence(chosen: PlanEvidence | None, **kw: object) -> DecisionEvidence:
    base: dict[str, object] = {
        "source_kind": "recommendation",
        "source_id": "rec_test",
        "source_plan": "best",
        "source_state": "current",
        "source_reasons": [],
        "recorded_at": "2026-09-11T10:00:00+00:00",
        "season": SEASON,
        "gameweek": GW,
        "deadline_at": DEADLINE,
        "cutoff": None,
        "state_id": "mstate_test",
        "data_snapshot_id": "snap_test",
        "prediction_run_id": "pred_test",
        "forecast_key": None,
        "ruleset_version": SEASON,
        "model_versions": {},
        "data_as_of": None,
        "squad_before": sorted(POS),
        "positions": {str(c): p for c, p in POS.items()},
        "teams": {str(c): t for c, t in TEAM.items()},
        "fixtures": {"100": [1], "200": [2]},
        "recommended": plan("best"),
        "hold": plan(),
        "chosen": chosen,
        "system_rationale": SystemRationale(decision="Hold"),
    }
    base.update(kw)
    return DecisionEvidence.model_validate(base)


def results(
    points: dict[int, int],
    minutes: dict[int, int] | None = None,
    fixture_status: dict[int, tuple[str, bool]] | None = None,
    gw_status: str = "finalized",
    moved: tuple[int, ...] = (),
    rows_final: bool | None = None,
) -> dict[str, pd.DataFrame]:
    """Fixtures 1 (club 100) and 2 (club 200) of the gameweek with their result rows. A row is
    ``finalized`` (captured after the finalisation lag) when its fixture is, unless
    ``rows_final`` says otherwise."""
    status = fixture_status or {1: ("final", True), 2: ("final", True)}
    fx = pd.DataFrame(
        [
            {
                "season": SEASON,
                "fixture_id": fid,
                "gw": None if fid in moved else GW,
                "home_team_code": home,
                "away_team_code": away,
                "status": "unscheduled" if fid in moved else status[fid][0],
                "finalized": False if fid in moved else status[fid][1],
            }
            for fid, home, away in ((1, 100, 300), (2, 200, 400))
        ]
    )
    played = {
        fid
        for fid, (st, _) in status.items()
        if st in ("final", "provisional") and fid not in moved
    }
    team_fixture = {100: 1, 200: 2}
    pm = pd.DataFrame(
        [
            {
                "season": SEASON,
                "gw": GW,
                "fixture_id": team_fixture[TEAM[c]],
                "player_code": c,
                "minutes": (minutes or {}).get(c, 90),
                "points": points.get(c, 2),
                "finalized": status[team_fixture[TEAM[c]]][1] if rows_final is None else rows_final,
            }
            for c in POS
            if team_fixture[TEAM[c]] in played
        ],
        columns=["season", "gw", "fixture_id", "player_code", "minutes", "points", "finalized"],
    )
    gws = pd.DataFrame(
        [{"season": SEASON, "gw": GW, "status": gw_status, "deadline_at": pd.Timestamp(DEADLINE)}]
    )
    players = pd.DataFrame(
        [
            {
                "season": SEASON,
                "player_code": c,
                "source_id": c + 1000,
                "team_code": TEAM[c],
                "position": POS[c],
            }
            for c in POS
        ]
    )
    return {"fixtures": fx, "player_match": pm, "gameweeks": gws, "players": players}


PTS = {c: c % 5 + 1 for c in POS}


def xi_points(starters: list[int], points: dict[int, int], armband: int, mult: int = 2) -> int:
    return sum(points[c] for c in starters) + (mult - 1) * points[armband]


def test_final_when_every_relevant_fixture_is_final_and_recorded() -> None:
    chosen = plan("best", action="HIT", hit_points=4, sells=[7], buys=[16])
    ev = evidence(chosen)
    r = evaluate_decision(ev, "HIT", results(PTS), NOW, "snap_after")
    assert r.status == "final" and r.detail["provisional"] is False
    assert r.realized_points == xi_points(STARTERS, PTS, 8) - 4  # hits come off
    assert r.realized_hold_points == xi_points(STARTERS, PTS, 8)
    assert r.detail["difference_vs_hold"] == -4
    assert r.data_snapshot_id == "snap_after"
    assert r.detail["caveat"] == CAVEAT


def test_captain_who_did_not_play_passes_the_armband_and_the_bench_comes_on() -> None:
    mins = {8: 0}
    r = evaluate_decision(evidence(plan()), "HOLD", results(PTS, mins), NOW, None)
    # MID 8 (captain) did not play: the first bench outfielder who keeps a legal formation
    # (DEF 7, giving 5-3-2) comes on, and vice-captain 9 takes the armband
    xi = [c for c in STARTERS if c != 8] + [7]
    assert r.realized_points == xi_points(xi, PTS, 9)
    assert r.detail["chosen"]["armband"] == 9 and r.detail["chosen"]["auto_subs"] == [[8, 7]]
    assert any("vice-captain took the armband" in s for s in r.detail["statements"])


def test_triple_captain_triples_the_armband() -> None:
    tc = plan("best", action="CHIP", chip="triple_captain_1")
    r = evaluate_decision(evidence(tc), "CHIP", results(PTS), NOW, None)
    assert r.realized_points == xi_points(STARTERS, PTS, 8, mult=3)


def test_a_followed_hold_has_zero_difference_to_holding() -> None:
    r = evaluate_decision(evidence(plan()), "HOLD", results(PTS), NOW, None)
    assert r.detail["difference_vs_hold"] == 0
    assert any("zero by construction" in s for s in r.detail["statements"])


def test_pending_before_any_relevant_match_and_partial_until_all_are_recorded() -> None:
    upcoming = {1: ("scheduled", False), 2: ("scheduled", False)}
    r = evaluate_decision(
        evidence(plan()), "HOLD", results(PTS, fixture_status=upcoming, gw_status="live"), NOW, None
    )
    assert r.status == "pending" and r.realized_points is None
    half = {1: ("final", True), 2: ("scheduled", False)}
    r = evaluate_decision(
        evidence(plan()), "HOLD", results(PTS, fixture_status=half, gw_status="live"), NOW, None
    )
    # no totals: players whose match is still to come would count as not playing
    assert r.status == "partial" and r.realized_points is None
    assert r.detail["fixtures"]["recorded"] == 1 and "players_so_far" in r.detail


def test_provisional_totals_until_fpl_finalises_the_gameweek() -> None:
    prov = {1: ("provisional", False), 2: ("final", True)}
    r = evaluate_decision(
        evidence(plan()),
        "HOLD",
        results(PTS, fixture_status=prov, gw_status="provisional"),
        NOW,
        None,
    )
    assert r.status == "partial" and r.detail["provisional"] is True
    assert r.realized_points == xi_points(STARTERS, PTS, 8)


def test_a_fixture_moved_out_of_the_gameweek_is_reported_and_not_scored_here() -> None:
    r = evaluate_decision(evidence(plan()), "HOLD", results(PTS, moved=(2,)), NOW, None)
    assert r.detail["fixtures"]["moved_out"] == [2]
    assert r.status == "final"  # FPL scores a postponed match in the gameweek it is played in
    # the even-coded team no longer plays: its starters are auto-substituted where possible
    assert r.realized_points is not None
    assert any("moved out of this gameweek" in s for s in r.detail["statements"])


def test_deferred_and_unknown_actions_are_never_scored_as_an_action() -> None:
    before = datetime(2026, 9, 10, tzinfo=UTC)
    r = evaluate_decision(evidence(None), "DEFER", results(PTS), before, None)
    assert r.status == "pending"
    r = evaluate_decision(evidence(None), "DEFER", results(PTS), NOW, None)
    assert r.status == "unavailable" and "before the deadline" in r.detail["reason"]
    r = evaluate_decision(evidence(None), "UNKNOWN", results(PTS), NOW, None)
    assert r.status == "unavailable" and r.realized_points is None
    assert r.realized_hold_points == xi_points(STARTERS, PTS, 8)  # context, not your action


def test_something_else_stays_open_until_its_reference_totals_are_final() -> None:
    """Review finding: "something else" was unavailable before any result and then left the
    hourly sweep, so holding's and the recommendation's totals were never computed."""
    ev = evidence(None)
    upcoming = {1: ("scheduled", False), 2: ("scheduled", False)}
    r = evaluate_decision(
        ev, "UNKNOWN", results(PTS, fixture_status=upcoming, gw_status="live"), NOW, None
    )
    assert r.status == "pending" and "not recorded" in r.detail["reason"]
    half = {1: ("final", True), 2: ("scheduled", False)}
    r = evaluate_decision(
        ev, "UNKNOWN", results(PTS, fixture_status=half, gw_status="live"), NOW, None
    )
    assert r.status == "partial" and r.realized_hold_points is None
    prov = {1: ("provisional", False), 2: ("provisional", False)}
    r = evaluate_decision(
        ev, "UNKNOWN", results(PTS, fixture_status=prov, gw_status="provisional"), NOW, None
    )
    assert r.status == "partial" and r.detail["provisional"] is True
    assert r.realized_points is None and r.realized_hold_points == xi_points(STARTERS, PTS, 8)
    r = evaluate_decision(ev, "UNKNOWN", results(PTS), NOW, None)
    assert r.status == "unavailable" and r.realized_hold_points is not None


def test_final_needs_results_captured_after_they_were_final() -> None:
    """Review finding: FPL can finalise the fixtures and the gameweek while the loaded result
    rows were captured provisionally (before bonus); that is not a final outcome."""
    ev = evidence(plan())
    early = evaluate_decision(ev, "HOLD", results(PTS, rows_final=False), NOW, None)
    assert early.status == "partial" and early.detail["provisional"] is True
    assert early.detail["fixtures"]["results_final"] == 0
    assert any("captured before they were final" in s for s in early.detail["statements"])
    late = evaluate_decision(ev, "HOLD", results(PTS), NOW, None)
    assert late.status == "final" and late.digest != early.digest


def test_an_armband_change_on_hold_is_not_zero_by_construction() -> None:
    """Review finding: a held squad with another captain was described as 'zero by
    construction' whenever the two captains happened to score the same."""
    assert PTS[13] == PTS[8]
    chosen = plan(captain=13, vice_captain=9)
    ev = evidence(chosen, captain_override=True, offered_captain=8, offered_vice_captain=9)
    r = evaluate_decision(ev, "HOLD", results(PTS), NOW, None)
    text = " ".join(r.detail["statements"])
    assert "by construction" not in text and "made no difference" in text
    assert r.detail["difference_vs_hold"] == 0


def test_the_digest_covers_only_the_scored_plans() -> None:
    """Review finding: a correction for a player of an unchosen workbench plan wrote a
    'revision' with identical totals."""
    ev = evidence(
        plan(),
        positions={**{str(c): p for c, p in POS.items()}, "16": "MID"},
        teams={**{str(c): t for c, t in TEAM.items()}, "16": 100},
    )

    def with_16(points16: int) -> dict[str, pd.DataFrame]:
        ds = results(PTS)
        row = ds["player_match"].iloc[[0]].assign(player_code=16, points=points16, fixture_id=1)
        ds["player_match"] = pd.concat([ds["player_match"], row], ignore_index=True)
        return ds

    a = evaluate_decision(ev, "HOLD", with_16(2), NOW, None)
    b = evaluate_decision(ev, "HOLD", with_16(9), NOW, None)
    assert a.digest == b.digest and a.realized_points == b.realized_points


def test_a_rebuilt_hold_says_whether_it_kept_the_stored_armband() -> None:
    """Review finding: a rebuilt HOLD XI silently used its own armband while its note said
    'captain and vice as stored'; the expected-difference band is then not reported."""
    rs = load_ruleset(SEASON)
    codes = sorted(POS)
    xp = {c: float(c) for c in codes}  # higher codes start
    kept = _reconstruct_hold_lineup(codes, POS, xp, 15, 14, "default", rs)
    assert kept is not None and kept[2:] == (15, 14, True)
    replaced = _reconstruct_hold_lineup(codes, POS, xp, 1, 14, "default", rs)  # GK 1 is benched
    assert replaced is not None and replaced[4] is False and replaced[2] != 1
    gain = GainExpectation(mean=1.0, p10=-3, p50=1, p90=5, probability_positive=0.6)
    chosen = plan("best", action="TRANSFER", sells=[7], buys=[16], gain_vs_hold=gain)
    for flag, band in ((True, True), (False, False)):
        hold = plan(lineup_source="reconstructed", lineup_note="rebuilt", armband_as_stored=flag)
        r = evaluate_decision(evidence(chosen, hold=hold), "TRANSFER", results(PTS), NOW, None)
        assert ("difference_band" in r.detail) is band
        assert "rebuilt." in r.detail["statements"]


def _wb_plan(i: int, label: str, action: str, **kw: Any) -> dict[str, Any]:
    p: dict[str, Any] = {
        "label": label,
        "kind": "hold" if i == 0 else "best",
        "valid": True,
        "passes_thresholds": i > 0,
        "objective_gain_vs_hold": 0.0 if i == 0 else 1.0 - i / 10,
        "sells": [] if action == "HOLD" else [7],
        "buys": [] if action == "HOLD" else [16],
        "hit_now": 0,
        "expected_points_1gw": 50.0,
        "timeline": [{"action": action, "p10": 35.0, "p90": 68.0}],
        "lineup": {"starters": STARTERS, "bench": BENCH, "captain": 8, "vice_captain": 9},
    }
    p.update(kw)
    return p


def test_the_workbench_recommendation_follows_the_engine_rule() -> None:
    """Review finding: a plan that holds this gameweek (moves later) was taken as the
    recommended move, inverting adherence."""
    plans = [
        _wb_plan(0, "hold", "HOLD"),
        _wb_plan(1, "best", "HOLD"),
        _wb_plan(2, "alt", "TRANSFER"),
    ]
    assert workbench_recommended_index(plans) == 2
    assert workbench_recommended_index(plans[:2]) == 0


def test_workbench_evidence_names_what_the_plans_were_computed_on() -> None:
    """Review findings: the evidence must use decision-time clubs (not a squad saved before a
    club move), the snapshot and forecast the plans were computed on (not the ones the run
    named), and plans that failed the rules replay cannot be chosen."""
    ds = results(PTS)
    ds["players"] = pd.concat(
        [
            ds["players"],
            pd.DataFrame(
                [
                    {
                        "season": SEASON,
                        "player_code": 16,
                        "source_id": 1016,
                        "team_code": 200,
                        "position": "DEF",
                    }
                ]
            ),
        ],
        ignore_index=True,
    )
    svc = SimpleNamespace(data=SimpleNamespace(ds=ds))
    squad = [
        SimpleNamespace(
            player_code=c, position=Position(POS[c]), team_code=999 if c == 3 else TEAM[c]
        )
        for c in POS
    ]
    plans = [
        _wb_plan(0, "hold", "HOLD"),
        _wb_plan(1, "best", "TRANSFER"),
        _wb_plan(2, "illegal", "TRANSFER", valid=False, passes_thresholds=False),
    ]
    run = {
        "id": "wb_test",
        "state_id": "mstate_test",
        "snapshot_id": "snap_named",
        "forecast_key": "fk_named",
        "result": {
            "season": SEASON,
            "snapshot_id": "snap_used",
            "forecast_key": "fk_used",
            "prediction_run_id": "pred_used",
            "result": {"gameweek": GW, "plans": plans},
        },
    }
    ev, options = evidence_from_workbench(
        svc, run, SimpleNamespace(squad=squad), {"state": "current"}, NOW
    )
    assert ev.teams["3"] == TEAM[3]  # the club the run used, not the stale saved one
    assert ev.teams["16"] == 200 and ev.fixtures["200"] == [2]
    assert (ev.data_snapshot_id, ev.forecast_key) == ("snap_used", "fk_used")
    assert "2:illegal" not in options and "1:best" in options
    with pytest.raises(JournalRefused) as exc:
        choose(options, "plan", "2:illegal", None, None)
    assert exc.value.status == 422


def test_identical_inputs_give_the_same_digest_and_revised_results_a_new_one() -> None:
    ev = evidence(plan())
    a = evaluate_decision(ev, "HOLD", results(PTS), NOW, "s1")
    b = evaluate_decision(ev, "HOLD", results(PTS), NOW, "s2")
    assert a.digest == b.digest  # the snapshot id alone is not a change of outcome
    revised = dict(PTS) | {9: PTS[9] + 3}
    c = evaluate_decision(ev, "HOLD", results(revised), NOW, "s3")
    assert c.digest != a.digest and c.realized_points == a.realized_points + 3


def test_statements_compare_without_grading() -> None:
    chosen = plan(
        "best",
        action="TRANSFER",
        sells=[7],
        buys=[16],
        gain_vs_hold=GainExpectation(mean=1.5, p10=-4, p50=1, p90=7, probability_positive=0.6),
    )
    r = evaluate_decision(evidence(chosen), "TRANSFER", results(PTS), NOW, None)
    text = " ".join(r.detail["statements"]).lower()
    for word in ("good", "bad", "wrong", "mistake", "correct", "right decision"):
        assert word not in text
    assert "expected difference when you decided: +1.5" in text


def test_verification_compares_with_the_official_picks_without_rewriting_the_record() -> None:
    ev = evidence(plan())
    picks = [(c, i + 1, c == 8, c == 9) for i, c in enumerate([*STARTERS, *BENCH])]
    assert verify_decision(ev, "HOLD", picks, None)[0] == "matches"
    other_cap = [(c, p, c == 10, v) for c, p, _, v in picks]
    verdict, detail = verify_decision(ev, "HOLD", other_cap, None)
    assert verdict == "differs" and detail["differences"] == ["captain"]
    verdict, detail = verify_decision(evidence(None), "UNKNOWN", picks, "bench_boost")
    assert verdict == "observed" and detail["actual"]["chip"] == "bench_boost"


def test_adherence_is_derived_from_the_recorded_action() -> None:
    rec = plan("best", action="TRANSFER", sells=[7], buys=[16])
    assert adherence(rec, rec, "recommended") == "followed"
    assert adherence(rec, rec.model_copy(update={"captain": 10}), "recommended") == "modified"
    assert adherence(rec, plan(), "hold") == "rejected"
    other = plan("alt", action="TRANSFER", sells=[5], buys=[17])
    assert adherence(rec, other, "plan") == "modified"
    assert adherence(rec, None, "defer") == "deferred"
    assert adherence(rec, None, "other") == "rejected"


def test_choose_only_accepts_reconstructible_choices() -> None:
    options = {"recommended": plan("best"), "hold": plan()}
    chosen, _action, override, offered = choose(options, "hold", None, 10, 11)
    assert chosen is not None and (chosen.captain, chosen.vice_captain) == (10, 11)
    assert override and offered is not None and offered.captain == 8
    with pytest.raises(JournalRefused) as exc:
        choose(options, "hold", None, 7, 9)  # a bench player cannot captain
    assert exc.value.status == 422
    with pytest.raises(JournalRefused):
        choose(options, "plan", "3:alt", None, None)
    assert choose(options, "other", None, None, None)[:2] == (None, "UNKNOWN")


def test_a_plan_that_holds_with_its_own_lineup_is_not_holding_by_construction() -> None:
    """Second review: a workbench plan that holds this gameweek but was solved on its own can
    field another armband; it is compared with HOLD, not declared equal to it."""
    own = plan("best", action="HOLD", captain=10, vice_captain=9)
    r = evaluate_decision(evidence(own), "HOLD", results(PTS), NOW, None)
    text = " ".join(r.detail["statements"])
    assert "by construction" not in text and "Holding would have scored" in text
    assert r.detail["difference_vs_hold"] == PTS[10] - PTS[8]


def test_older_data_never_replaces_newer_even_in_a_pinned_deployment(tmp_path: Path) -> None:
    """Second review: the lifecycle's pinned rule ("every other snapshot is stale") froze the
    journal in a data rollback; evaluations are ordered by export time only."""
    root = tmp_path / "snapshots"
    for name, ts in (
        ("snap_old", "2026-09-01T00:00:00+00:00"),
        ("snap_new", "2026-09-02T00:00:00+00:00"),
    ):
        (root / name).mkdir(parents=True)
        (root / name / "manifest.json").write_text(json.dumps({"created_at": ts}))
    for pinned in (None, root / "snap_old", root / "snap_new"):
        st = SimpleNamespace(snapshots_root=root, snapshot_dir=pinned)
        assert exported_before(st, None, "snap_old", "snap_new") is True
        assert exported_before(st, None, "snap_new", "snap_old") is False
        assert exported_before(st, None, "snap_gone", "snap_new") is False  # unknown: no proof
