"""Render a recommendation package as the §68.2 explanation sections (Markdown).

The renderer only formats numbers and evidence already present in the package — it adds no
reasons of its own, so every sentence is traceable to an evidence id, a scenario or an
identifier (an LLM paraphrase, if ever used, must keep these as the source of truth; §68).
"""

from __future__ import annotations

from fpl_decision.engine import RecommendationPackage


def _n(names: dict[int, str] | None, c: int) -> str:
    return (names or {}).get(c, str(c))


def render_markdown(pkg: RecommendationPackage, names: dict[int, str] | None = None) -> str:
    e = pkg.explanation
    d = pkg.decision
    out = [f"# GW{pkg.gameweek} recommendation — {d['action']}", ""]
    out += ["## Decision", "", e.decision, ""]
    conf = e.confidence
    out += [
        "## Expected effect",
        "",
        f"- This gameweek: {e.expected_effect['gain_1gw']:+.2f} points vs holding",
        f"- Over the horizon: {e.expected_effect['gain_horizon']:+.2f} points "
        f"(80 % interval {conf['gain_p10']:+.1f} to {conf['gain_p90']:+.1f}); "
        f"P(beats holding) {conf['probability_beats_hold']:.0%}",
        f"- Squad this gameweek: {d['expected_points']:.1f} expected points "
        f"(p10 {d['p10']:.0f}, p90 {d['p90']:.0f})",
        f"- Stability under forecast perturbation: {d.get('stability') or 'not assessed'}",
        "",
    ]
    out += ["## Why", ""]
    if e.primary_drivers:
        for drv in e.primary_drivers:
            out.append(f"- {drv['text']}  `[{', '.join(drv['evidence_ids'])}]`")
    else:
        out.append("- No move cleared the decision thresholds:")
        out += [f"  - {r}" for r in e.refusal_reasons]
    if e.constraints_binding:
        out += ["", "Binding constraints:"] + [f"- {c}" for c in e.constraints_binding]
    out += ["", "## What could go wrong", ""]
    if e.downside_scenarios:
        for s in e.downside_scenarios:
            gain = s.get("gain_vs_hold")
            g = f"{gain:+.2f} pts vs holding" if gain is not None else "re-optimised"
            flag = "" if s.get("feasible", True) else " — move becomes infeasible"
            out.append(f"- **{s['scenario']}**: {s['description']} → {g}{flag}")
    else:
        out.append("- Scenarios not run for this request.")
    hc = e.hold_counterfactual
    out += [
        "",
        "## If you do nothing",
        "",
        f"- Holding: {hc['expected_points_gw']:.1f} expected points this gameweek, "
        f"{hc['expected_points_horizon']:.1f} over the horizon.",
        "",
        "## Alternatives",
        "",
    ]
    if e.alternatives:
        for a in e.alternatives:
            move = (
                (
                    ", ".join(_n(names, c) for c in a["sells"])
                    + " → "
                    + ", ".join(_n(names, c) for c in a["buys"])
                )
                if a["sells"]
                else a["action"]
            )
            out.append(
                f"- {a['label']}: {move} — {a['gain_horizon']:+.2f} pts, "
                f"P>0 {a['probability_positive']:.0%}"
            )
    else:
        out.append("- No materially different feasible plan.")
    out += ["", "## Assumptions", ""] + [f"- {a}" for a in e.assumptions]
    out += [
        "",
        "## Reproducibility",
        "",
        f"- decision `{pkg.decision_id}`, optimiser run `{pkg.optimizer_run_id}`",
        f"- data snapshot `{pkg.snapshot_id}`, ruleset `{pkg.ruleset_version}`",
        "- models: " + ", ".join(f"{k}={v}" for k, v in sorted(pkg.model_versions.items())),
        f"- data as of {e.data_timestamp}",
        "",
    ]
    return "\n".join(out)
