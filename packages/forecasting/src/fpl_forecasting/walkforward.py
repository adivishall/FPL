"""Walk-forward evaluation harness for player forecasts (§27, §57.2, §70).

For every evaluation cutoff (deadline − buffer of GW t in the chosen seasons):

1. build (or load) the point-in-time feature snapshot for GWs t … t+H−1;
2. assemble the training table *as known at that cutoff*: feature rows of earlier gameweeks
   joined with outcomes from ``PointInTimeView(cutoff).player_match()`` (labels obey the same
   availability rule as features);
3. fit each forecaster on that table and predict the snapshot rows;
4. only then join realised outcomes of the target gameweeks (evaluation-only access).

Feature snapshots are materialised under ``<root>/<snapshot_id>/<feature_version>/`` as Parquet
with a JSON manifest (cutoff, max source availability, row count, content hash).
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

import numpy as np
import pandas as pd

from fpl_domain.rules import load_ruleset
from fpl_features.builder import FeatureFrame, build_features
from fpl_features.labels import realized_player_gw
from fpl_features.registry import FEATURE_VERSION
from fpl_forecasting import metrics as fm
from fpl_storage.dataset import CanonicalDataset
from fpl_storage.pit import PointInTimeView, decision_cutoff


class PointForecaster(Protocol):
    name: str
    version: str

    def fit(self, train: pd.DataFrame) -> None: ...

    def predict(self, frame: pd.DataFrame) -> pd.Series: ...


@dataclass(frozen=True)
class Cutoff:
    season: str
    gw: int
    cutoff: pd.Timestamp


def cutoffs(
    ds: CanonicalDataset,
    seasons: Iterable[str],
    gameweeks: Iterable[int] | None = None,
    buffer_minutes: int = 90,
) -> list[Cutoff]:
    g = ds["gameweeks"]
    wanted = set(gameweeks) if gameweeks is not None else None
    out = []
    for season in seasons:
        for r in g[g["season"] == season].sort_values("gw").itertuples():
            if wanted is None or r.gw in wanted:
                out.append(
                    Cutoff(season, int(r.gw), decision_cutoff(r.deadline_at, buffer_minutes))
                )
    return out


class FeatureCache:
    def __init__(self, ds: CanonicalDataset, horizon: int, root: Path | None = None) -> None:
        self.ds, self.horizon = ds, horizon
        self.root = (Path(root) / ds.snapshot_id / FEATURE_VERSION) if root else None
        self._mem: dict[tuple[str, int], FeatureFrame] = {}

    def _path(self, c: Cutoff) -> Path | None:
        return self.root / f"{c.season}_gw{c.gw:02d}_h{self.horizon}.parquet" if self.root else None

    def get(self, c: Cutoff) -> FeatureFrame:
        key = (c.season, c.gw)
        if key in self._mem:
            return self._mem[key]
        path = self._path(c)
        meta = None
        if path is not None and path.exists():
            meta = json.loads(path.with_suffix(".json").read_text())
            # an entry is only reused for exactly this cutoff and data snapshot
            if (
                pd.Timestamp(meta["cutoff"]) != c.cutoff
                or meta.get("data_snapshot_id") != self.ds.snapshot_id
            ):
                meta = None
        if path is not None and meta is not None:
            ff = FeatureFrame(
                frame=pd.read_parquet(path),
                season=c.season,
                gameweek=c.gw,
                horizon=self.horizon,
                cutoff=pd.Timestamp(meta["cutoff"]),
                max_source_available_at=pd.Timestamp(meta["max_source_available_at"])
                if meta["max_source_available_at"]
                else None,
            )
        else:
            view = PointInTimeView(self.ds, c.cutoff)
            ff = build_features(view, c.season, c.gw, self.horizon, load_ruleset(c.season))
            if path is not None:
                # concurrent replays share this cache: the sidecar is published first and the
                # parquet (whose existence readers test) last, each by an atomic rename
                path.parent.mkdir(parents=True, exist_ok=True)
                tag = f".{os.getpid()}.tmp"
                side, side_tmp = path.with_suffix(".json"), path.with_suffix(".json" + tag)
                side_tmp.write_text(
                    json.dumps(
                        {
                            "feature_snapshot_id": ff.snapshot_id,
                            "season": c.season,
                            "gw": c.gw,
                            "horizon": self.horizon,
                            "cutoff": ff.cutoff.isoformat(),
                            "max_source_available_at": ff.max_source_available_at.isoformat()
                            if ff.max_source_available_at is not None
                            else None,
                            "feature_version": FEATURE_VERSION,
                            "rows": len(ff.frame),
                            "data_snapshot_id": self.ds.snapshot_id,
                        },
                        indent=2,
                    )
                )
                os.replace(side_tmp, side)
                frame_tmp = path.with_name(path.name + tag)
                ff.frame.to_parquet(frame_tmp, index=False)
                os.replace(frame_tmp, path)
        self._mem[key] = ff
        return ff


def training_table(
    cache: FeatureCache,
    upto: Cutoff,
    history: Sequence[Cutoff],
    horizons: Iterable[int] = (0,),
) -> pd.DataFrame:
    """Feature rows of earlier cutoffs (for the given horizons) joined with outcomes known at
    ``upto`` — a target fixture's label is only included once its ``available_at`` ≤ cutoff."""
    labels = PointInTimeView(cache.ds, upto.cutoff).player_match()
    lab = labels[
        [
            "season",
            "fixture_id",
            "player_code",
            "points",
            "minutes",
            "starts",
            "goals",
            "assists",
            "clean_sheets",
            "bonus",
            "saves",
            "dc",
            "goals_conceded",
        ]
    ]
    hz = set(horizons)
    parts = []
    for c in history:
        if c.cutoff >= upto.cutoff:
            continue
        f = cache.get(c).frame
        parts.append(f[f["horizon"].isin(hz)])
    if not parts:
        return pd.DataFrame()
    feats = pd.concat(parts, ignore_index=True)
    return feats.merge(lab, on=["season", "fixture_id", "player_code"], how="inner")


@dataclass
class EvalResult:
    predictions: pd.DataFrame  # one row per player × target GW
    metrics: pd.DataFrame
    windows: dict[str, object] = field(default_factory=dict)


def evaluate_point_forecasters(
    ds: CanonicalDataset,
    forecasters: Sequence[PointForecaster],
    eval_cutoffs: Sequence[Cutoff],
    history_cutoffs: Sequence[Cutoff],
    horizon: int = 1,
    cache_root: Path | None = None,
) -> EvalResult:
    cache = FeatureCache(ds, horizon, cache_root)
    rows = []
    for c in eval_cutoffs:
        ff = cache.get(c)
        frame = ff.frame
        train = training_table(cache, c, history_cutoffs)
        preds = {}
        for f in forecasters:
            f.fit(train)
            preds[f.name] = f.predict(frame).to_numpy()
        out = frame[
            [
                "season",
                "player_code",
                "position",
                "target_gw",
                "horizon",
                "start_rate_long",
                "fixture_id",
            ]
        ].copy()
        for name, p in preds.items():
            out[f"pred_{name}"] = p
        out["decision_gw"] = c.gw
        rows.append(out)
    allp = pd.concat(rows, ignore_index=True)
    pred_cols = [f"pred_{f.name}" for f in forecasters]
    per_gw = (
        allp.groupby(
            ["season", "decision_gw", "target_gw", "horizon", "player_code", "position"],
            as_index=False,
        )
        .agg({**dict.fromkeys(pred_cols, "sum"), "start_rate_long": "first", "fixture_id": "count"})
        .rename(columns={"fixture_id": "n_fixtures"})
    )
    # Evaluation-only: realised outcomes joined AFTER predictions exist.
    actual = pd.concat(
        [
            realized_player_gw(ds, s, sorted(g["target_gw"].unique()))
            for s, g in per_gw.groupby("season")
        ],
        ignore_index=True,
    )
    per_gw = per_gw.merge(
        actual[["season", "gw", "player_code", "points", "minutes"]],
        left_on=["season", "target_gw", "player_code"],
        right_on=["season", "gw", "player_code"],
        how="left",
    ).drop(columns="gw")
    per_gw[["points", "minutes"]] = per_gw[["points", "minutes"]].fillna(0)
    per_gw["regular"] = per_gw["start_rate_long"].fillna(0) >= 0.5
    met = []
    for (season, horizon_), g in per_gw.groupby(["season", "horizon"]):
        for pop, gg in (("all", g), ("regulars", g[g["regular"]])):
            for f in forecasters:
                p = gg[f"pred_{f.name}"]
                met.append(
                    {
                        "season": season,
                        "horizon": int(horizon_),
                        "population": pop,
                        "forecaster": f.name,
                        "n": len(gg),
                        "mae": fm.mae(p, gg["points"]),
                        "rmse": fm.rmse(p, gg["points"]),
                        "bias": fm.bias(p, gg["points"]),
                        "spearman": float(
                            pd.Series(p.to_numpy()).corr(
                                pd.Series(gg["points"].to_numpy()), method="spearman"
                            )
                        ),
                    }
                )
    windows = {
        "eval": [f"{c.season} GW{c.gw}" for c in eval_cutoffs],
        "n_eval_cutoffs": len(eval_cutoffs),
        "horizon": horizon,
        "data_snapshot_id": ds.snapshot_id,
        "feature_version": FEATURE_VERSION,
    }
    return EvalResult(predictions=per_gw, metrics=pd.DataFrame(met), windows=windows)


def summarize(metrics: pd.DataFrame) -> pd.DataFrame:
    """Row-weighted averages across seasons per forecaster × population × horizon."""

    def wavg(g: pd.DataFrame) -> pd.Series:
        w = g["n"].to_numpy(dtype=float)
        return pd.Series(
            {
                "n": int(w.sum()),
                **{
                    k: float(np.average(g[k], weights=w))
                    for k in ("mae", "rmse", "bias", "spearman")
                },
            }
        )

    return (
        metrics.groupby(["population", "horizon", "forecaster"])
        .apply(wavg, include_groups=False)
        .reset_index()
    )
