"""The forecast record (M1.5; ``docs/architecture/season-evaluation.md`` §2).

The serving forecast is a file that retention deletes once it is old and 24 newer snapshots
exist, so before M1.5 no per-player forecast outlived its gameweek. The record keeps the one
that matters for evaluation — the forecast the API would have served at each gameweek's
decision cutoff — and proves when it existed:

* **Promotion.** The forecast job writes a ``forecast_promotions`` row (the forecast file's
  SHA-256, its prediction run, the snapshot's export time; ``promoted_at`` from the database
  clock) *before* the ready marker that lets the API serve it (``record_promotion``).
* **Server time.** The host's clocks can lag after sleep, so the proof of *when* comes from FPL:
  ``forecast_attestations`` holds FPL's HTTP ``Date`` around the computation and, if that
  failed, from a later request after re-hashing the stored file (``fpl_api.attestation``).
* **Checkpoint.** After the cutoff, ``capture`` freezes the forecast of the newest snapshot (by
  export time) whose file FPL's server time shows existed by the cutoff — or, failing any such
  proof, the one the local clock places there (``DataService._newest(ready_only=True)``'s
  choice) — into ``forecast_checkpoints`` (header), ``predictions`` (the forecast's numbers per
  player × target gameweek) and two content-addressed ``artifact_blobs``: the decision-time
  context and baselines, and the exact points distributions. It is ``prospective`` only when
  FPL's server time precedes the cutoff, the file is byte-identical to the promoted one, its
  cutoff is the gameweek's and the data shows no leakage; without the server time it is at best
  ``prospective_unverified``. Capture time is recorded, never a criterion: the file is never
  rewritten and retention keeps it until captured (``pending_candidates``), so a host asleep at
  the cutoff loses nothing.
* **Nothing is recomputed.** A gameweek without a promoted forecast, or whose file is gone, is
  recorded ``missed`` with the reason. Gameweeks whose cutoff precedes the first promotion ever
  recorded predate the record and get no row.

Insert-only and idempotent: one checkpoint per (season, gameweek, kind); a repeated or
concurrent capture finds the existing row.
"""

from __future__ import annotations

import hashlib
import io
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import joblib
import numpy as np
import pandas as pd
import structlog
from sqlalchemy import DateTime, Engine, cast, func, insert, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError

from fpl_api.attestation import ServerClock, ServerTime, official
from fpl_api.attestation import read as read_server_time
from fpl_api.provenance import build_revision, code_digest
from fpl_api.services import DataService, history_cutoffs
from fpl_api.settings import Settings
from fpl_domain.hashing import short_id
from fpl_domain.rules import load_ruleset
from fpl_forecasting import distributions as dist
from fpl_forecasting.forecast_eval import (
    POINT_THRESHOLDS,
    ForecastEvalConfig,
    prediction_rows,
    train_benchmarks,
)
from fpl_forecasting.pipeline import Forecast
from fpl_forecasting.walkforward import FeatureCache
from fpl_storage import models as m
from fpl_storage.dataset import load_snapshot
from fpl_storage.db import session_scope
from fpl_storage.pit import PointInTimeView, decision_cutoff

log = structlog.get_logger("fpl_api.ledger")
KIND = "live"
COMPONENTS = (
    "appearance",
    "goals",
    "assists",
    "clean_sheet",
    "goals_conceded",
    "saves",
    "penalty_saves",
    "penalty_misses",
    "yellow_cards",
    "red_cards",
    "own_goals",
    "defensive_contribution",
    "bonus",
)
BASELINE_COLUMNS = (
    "pred_position_mean",
    "pred_recent_form",
    "pred_ppg_availability",
    "pred_fpl_style",
    "base_p_start",
    "base_exp_minutes",
)


def _utc(v: Any) -> datetime:
    t = pd.Timestamp(v)
    return (t.tz_localize(UTC) if t.tzinfo is None else t.tz_convert(UTC)).to_pydatetime()


def forecast_path(settings: Settings, key: str) -> Path:
    return settings.artifact_dir / "forecasts" / f"{key}.joblib"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ----------------------------------------------------------------------------- promotion


def record_promotion(
    engine: Engine, settings: Settings, data: DataService, fc: Forecast, snapshot_id: str
) -> None:
    """Commit to a serving forecast before it is marked ready: the forecast file's hash and
    the snapshot's export time, under the database clock. Insert-if-absent (the first promotion
    of a snapshot is kept). Raises when it cannot record — the caller then must not promote."""
    p = fc.provenance
    key = str(p["forecast_key"])
    snap = data.snapshot_path(snapshot_id)
    if snap is None:
        raise FileNotFoundError(f"snapshot {snapshot_id} is not stored; cannot record promotion")
    created = json.loads((snap / "manifest.json").read_text())["created_at"]
    with session_scope(engine) as s:
        s.execute(
            pg_insert(m.ForecastPromotionRow)
            .values(
                snapshot_id=snapshot_id,
                season=str(p["season"]),
                gameweek=int(p["decision_gw"]),
                forecast_key=key,
                prediction_run_id=str(p["prediction_run_id"]),
                snapshot_created_at=_utc(created),
                artifact_sha256=sha256_file(forecast_path(settings, key)),
                code_digest=p.get("code_digest"),
                cutoff_at=_utc(p["cutoff"]) if p.get("cutoff") is not None else None,
            )
            .on_conflict_do_nothing(index_elements=["snapshot_id", "forecast_key"])
        )


def promotion_recorded(engine: Engine, snapshot_id: str, forecast_key: str) -> bool:
    with session_scope(engine) as s:
        return s.get(m.ForecastPromotionRow, (snapshot_id, forecast_key)) is not None


def attest(
    engine: Engine,
    settings: Settings,
    clock: ServerClock | None,
    snapshot_id: str,
    forecast_key: str,
    before: ServerTime | None = None,
) -> str:
    """Hash the stored forecast file, *then* ask FPL the time: the file with that hash existed
    by ``not_after``. With ``before`` (a time read before this job computed the file) the row is
    a ``generation`` attestation bounding the computation on both sides; otherwise
    ``existence``. A reading that contradicts FPL's time already on record — ``before`` later
    than this one's upper bound, or an upper bound earlier than a ``Date`` FPL gave before (a
    stale cached response) — is stored nowhere (``inconsistent``). Returns ``attested`` or why
    not (``no_clock``, ``no_file``, ``unavailable``, ``inconsistent``); never raises — the
    caller decides whether a missing proof is a failure."""
    if clock is None:
        return "no_clock"
    try:
        path = forecast_path(settings, forecast_key)
        if not path.exists():
            return "no_file"
        sha = sha256_file(path)
        after = read_server_time(clock, "forecast_attestation")
        if after is None:
            return "unavailable"
        with session_scope(engine) as s:
            seen = latest_server_date(s)
            if (before is not None and before.not_before > after.not_after) or (
                seen is not None and after.not_after < seen
            ):
                log.warning(
                    "server_time_inconsistent",
                    before=str(before.date) if before is not None else None,
                    after=str(after.date),
                    latest_on_record=str(seen),
                )
                return "inconsistent"
            s.add(
                m.ForecastAttestationRow(
                    snapshot_id=snapshot_id,
                    forecast_key=forecast_key,
                    artifact_sha256=sha,
                    kind="generation" if before is not None else "existence",
                    not_before=before.not_before if before is not None else None,
                    not_after=after.not_after,
                    source_url=after.url,
                    evidence={
                        "before": before.evidence() if before is not None else None,
                        "after": after.evidence(),
                        "build_revision": build_revision(),
                        "code_digest": code_digest(),
                    },
                )
            )
    except Exception as exc:  # evidence, never a reason to stop serving
        log.warning("forecast_attestation_failed", snapshot=snapshot_id, error=str(exc)[:300])
        return "unavailable"
    log.info(
        "forecast_attested", snapshot=snapshot_id, key=forecast_key, not_after=str(after.not_after)
    )
    return "attested"


@dataclass(frozen=True)
class ServerTimes:
    """What FPL's server time shows about one promotion's file (its promoted hash, official
    source only)."""

    existed_by: datetime | None  # earliest not_after: the file existed by then
    computed_after: datetime | None  # latest not_before of a generation attestation

    @property
    def consistent(self) -> bool:
        """A computation cannot start after its file existed: contradictory readings (one of
        them stale) prove nothing."""
        return (
            self.existed_by is None
            or self.computed_after is None
            or self.computed_after <= self.existed_by
        )


def latest_server_date(s: Any) -> datetime | None:
    """The latest ``Date`` FPL has given the record: a lower bound on a moment already past, so
    no honest later reading can claim an upper bound before it."""
    t = s.scalar(
        select(
            func.max(
                func.greatest(
                    m.ForecastAttestationRow.not_before,
                    cast(
                        m.ForecastAttestationRow.evidence["after"]["date"].astext,
                        DateTime(timezone=True),
                    ),
                )
            )
        )
    )
    return _utc(t) if t is not None else None


def server_times(s: Any, season: str) -> dict[tuple[str, str], ServerTimes]:
    """Per (snapshot, forecast key) of ``season``. An attestation counts only for the hash the
    promotion recorded and only from FPL's official API."""
    fa, fp = m.ForecastAttestationRow, m.ForecastPromotionRow
    rows = s.execute(
        select(fa.snapshot_id, fa.forecast_key, fa.not_before, fa.not_after, fa.source_url)
        .join(
            fp,
            (fp.snapshot_id == fa.snapshot_id)
            & (fp.forecast_key == fa.forecast_key)
            & (fp.artifact_sha256 == fa.artifact_sha256),
        )
        .where(fp.season == season)
    ).all()
    out: dict[tuple[str, str], ServerTimes] = {}
    for snap, key, nb, na, url in rows:
        if not official(url):
            continue
        cur = out.get((snap, key), ServerTimes(None, None))
        by = _utc(na) if cur.existed_by is None else min(cur.existed_by, _utc(na))
        after = cur.computed_after
        if nb is not None:
            after = _utc(nb) if after is None else max(after, _utc(nb))
        out[(snap, key)] = ServerTimes(by, after)
    return out


def unattested_newest(engine: Engine, now: datetime | None = None) -> tuple[str, str] | None:
    """The newest promotion — the candidate for the next cutoff — when FPL's server time does
    not yet show its file existed (the generation attestation failed or predates M1.5's
    attestation). Database reads only (the scheduler's check)."""
    now = now or datetime.now(UTC)
    gws = season_gameweeks(engine)
    if not gws:
        return None
    with session_scope(engine) as s:
        row = served_at(s, gws[0].season, now)
        if row is None:
            return None
        if server_times(s, gws[0].season).get((row.snapshot_id, row.forecast_key)):
            return None
        return row.snapshot_id, row.forecast_key


def attest_existence(
    engine: Engine, settings: Settings, clock: ServerClock | None, snapshot_id: str, key: str
) -> str:
    """An ``existence`` attestation for a stored promotion whose file still has its promoted
    hash (``file_changed`` otherwise: the capture flags it, nothing here vouches for it)."""
    with session_scope(engine) as s:
        promo = s.get(m.ForecastPromotionRow, (snapshot_id, key))
        promoted_sha = promo.artifact_sha256 if promo is not None else None
    if promoted_sha is None:
        return "no_promotion"
    path = forecast_path(settings, key)
    if not path.exists():
        return "no_file"
    if sha256_file(path) != promoted_sha:
        log.warning("forecast_file_changed", snapshot=snapshot_id, key=key)
        return "file_changed"
    return attest(engine, settings, clock, snapshot_id, key)


# ----------------------------------------------------------------------------- what is due


@dataclass(frozen=True)
class Gameweek:
    season: str
    gameweek: int
    deadline: datetime
    cutoff: datetime
    first_kickoff: datetime | None
    final_expected: datetime | None = None  # last kickoff + the ruleset's finalisation lag


def record_started(engine: Engine) -> datetime | None:
    """When the record began: the first promotion ever recorded."""
    with session_scope(engine) as s:
        t = s.scalar(select(func.min(m.ForecastPromotionRow.promoted_at)))
    return _utc(t) if t is not None else None


def season_gameweeks(engine: Engine, season: str | None = None) -> list[Gameweek]:
    """Every gameweek of ``season`` (default: the newest season with gameweeks) with its
    official deadline and decision cutoff, from the hourly-updated ``gameweeks`` table."""
    with session_scope(engine) as s:
        if season is None:
            season = s.scalar(
                select(m.SeasonRow.season_code)
                .join(m.GameweekRow, m.GameweekRow.season_id == m.SeasonRow.id)
                .order_by(m.SeasonRow.season_code.desc())
                .limit(1)
            )
            if season is None:
                return []
        rows = s.execute(
            select(
                m.GameweekRow.gw,
                m.GameweekRow.deadline_at,
                m.GameweekRow.started_at,
                m.GameweekRow.finalized_at,
            )
            .join(m.SeasonRow, m.SeasonRow.id == m.GameweekRow.season_id)
            .where(m.SeasonRow.season_code == season)
            .order_by(m.GameweekRow.gw)
        ).all()
        if not rows:  # an unknown season (also one without a ruleset): nothing to show
            return []
        last_kickoff = dict(
            s.execute(
                select(m.FixtureRow.gw, func.max(m.FixtureRow.kickoff_at))
                .join(m.SeasonRow, m.SeasonRow.id == m.FixtureRow.season_id)
                .where(m.SeasonRow.season_code == season, m.FixtureRow.gw.is_not(None))
                .group_by(m.FixtureRow.gw)
            ).all()
        )
    timing = load_ruleset(season).timing
    buffer = timing.decision_buffer_minutes
    lag = timedelta(hours=timing.finalization_lag_hours)

    def final_expected(gw: int, finalized: Any) -> datetime | None:
        """FPL's own finalisation time once known; until then the last kickoff plus the
        ruleset's finalisation lag (an estimate)."""
        if finalized is not None:
            return _utc(finalized)
        k = last_kickoff.get(gw)
        return _utc(k) + lag if k is not None else None

    return [
        Gameweek(
            season=season,
            gameweek=int(gw),
            deadline=_utc(deadline),
            cutoff=decision_cutoff(_utc(deadline), buffer).to_pydatetime(),
            first_kickoff=_utc(started) if started is not None else None,
            final_expected=final_expected(int(gw), final),
        )
        for gw, deadline, started, final in rows
    ]


def due_gameweeks(engine: Engine, now: datetime | None = None) -> list[Gameweek]:
    """Gameweeks of the current season whose cutoff has passed since the record began and that
    have no checkpoint yet. Database reads only (the scheduler's due-check)."""
    now = now or datetime.now(UTC)
    started = record_started(engine)
    if started is None:
        return []
    gws = season_gameweeks(engine)
    if not gws:
        return []
    with session_scope(engine) as s:
        have = set(
            s.scalars(
                select(m.ForecastCheckpointRow.gameweek).where(
                    m.ForecastCheckpointRow.season == gws[0].season,
                    m.ForecastCheckpointRow.kind == KIND,
                )
            ).all()
        )
    return [g for g in gws if started <= g.cutoff <= now and g.gameweek not in have]


def served_at(s: Any, season: str, cutoff: datetime) -> m.ForecastPromotionRow | None:
    """The promotion the API served at ``cutoff``: the newest snapshot by export time among
    those promoted at or before it (``DataService._newest(ready_only=True)``)."""
    row: m.ForecastPromotionRow | None = s.scalars(
        select(m.ForecastPromotionRow)
        .where(
            m.ForecastPromotionRow.season == season,
            m.ForecastPromotionRow.promoted_at <= cutoff,
        )
        .order_by(
            m.ForecastPromotionRow.snapshot_created_at.desc(),
            m.ForecastPromotionRow.promoted_at.desc(),
        )
        .limit(1)
    ).first()
    return row


def _order(p: m.ForecastPromotionRow) -> tuple[datetime, datetime]:
    return _utc(p.snapshot_created_at), _utc(p.promoted_at)


@dataclass(frozen=True)
class Candidate:
    promotion: m.ForecastPromotionRow | None
    attested_at: datetime | None  # FPL server time by which its file existed, if ≤ the cutoff
    existed_by: datetime | None = None  # the earliest FPL server time for it, whenever it is
    later: m.ForecastPromotionRow | None = None  # newer, placed before the cutoff by local clock


def checkpoint_candidate(s: Any, season: str, gameweek: int, cutoff: datetime) -> Candidate:
    """The forecast a gameweek's checkpoint freezes (contract §2.2). The host's choice is what
    ``DataService._newest(ready_only=True)`` served by its own clock: the newest snapshot (by
    export time) promoted at or before ``cutoff`` — unless FPL's server time shows that forecast
    was computed after the cutoff (a clock that lagged after sleep). When FPL's server time
    shows a forecast *of this gameweek* existed by the cutoff — the earlier of the gameweek's
    and the forecast's own, if FPL moved the deadline — with no contradicting reading, the
    newest such one is frozen instead and a newer unproven one is reported (``later``), never
    used; otherwise the host's choice is frozen and can be ``prospective_unverified`` at best."""
    fp = m.ForecastPromotionRow
    promos = list(s.scalars(select(fp).where(fp.season == season)))
    times = server_times(s, season)

    def t(p: m.ForecastPromotionRow) -> ServerTimes:
        return times.get((p.snapshot_id, p.forecast_key), ServerTimes(None, None))

    def own_cutoff(p: m.ForecastPromotionRow) -> datetime:
        return min(cutoff, _utc(p.cutoff_at)) if p.cutoff_at is not None else cutoff

    local = [
        p
        for p in promos
        if _utc(p.promoted_at) <= cutoff
        and not ((a := t(p).computed_after) is not None and a > cutoff)
    ]
    attested = [
        p
        for p in promos
        if p.gameweek == gameweek
        and t(p).consistent
        and (b := t(p).existed_by) is not None
        and b <= own_cutoff(p)
    ]
    host = max(local, key=_order, default=None)
    if attested:
        best = max(attested, key=_order)
        later = host if host is not None and _order(host) > _order(best) else None
        return Candidate(best, t(best).existed_by, t(best).existed_by, later)
    if host is None:
        return Candidate(None, None)
    return Candidate(host, None, t(host).existed_by if t(host).consistent else None)


def pending_candidates(engine: Engine | None, now: datetime | None = None) -> set[tuple[str, str]]:
    """(snapshot id, forecast key) pairs retention keeps (snapshot, caches, marker and file):
    every due gameweek's candidates until its checkpoint is written — the attested one and the
    local clock's — and, for the next cutoff, the newest promotion and the newest forecast of
    that gameweek FPL's server time proves, whatever the ready marker says."""
    if engine is None:
        return set()
    now = now or datetime.now(UTC)
    out: set[tuple[str, str]] = set()
    gws = season_gameweeks(engine)
    with session_scope(engine) as s:
        for g in due_gameweeks(engine, now):
            c = checkpoint_candidate(s, g.season, g.gameweek, g.cutoff)
            for row in (c.promotion, c.later, served_at(s, g.season, g.cutoff)):
                if row is not None:
                    out.add((row.snapshot_id, row.forecast_key))
        if gws:
            newest = served_at(s, gws[0].season, now)
            if newest is not None:
                out.add((newest.snapshot_id, newest.forecast_key))
            # the next gameweek's checkpoint if newer promotions stay unproven: its newest
            # forecast FPL's server time already proves
            nxt = next((g for g in gws if g.cutoff > now), None)
            if nxt is not None:
                proven = checkpoint_candidate(s, nxt.season, nxt.gameweek, now)
                if proven.promotion is not None and proven.attested_at is not None:
                    out.add((proven.promotion.snapshot_id, proven.promotion.forecast_key))
    return out


# ----------------------------------------------------------------------------- capture


def _frame_digest(df: pd.DataFrame) -> str:
    return hashlib.sha256(df.to_csv(index=False, float_format="%.10g").encode()).hexdigest()


def _blob(s: Any, data: bytes, content_type: str) -> str:
    sha = hashlib.sha256(data).hexdigest()
    s.execute(
        pg_insert(m.ArtifactBlob)
        .values(sha256=sha, content_type=content_type, data=data)
        .on_conflict_do_nothing(index_elements=["sha256"])
    )
    return sha


def _parquet(df: pd.DataFrame) -> bytes:
    buf = io.BytesIO()
    df.to_parquet(buf, index=False)
    return buf.getvalue()


def forecast_context(fc: Forecast) -> pd.DataFrame:
    """Per player × target gameweek, from the forecast itself: the fixture ids and club it
    assumed, position, and P(start) before the availability layer (``fc.minutes`` is the
    minutes model's output; the simulated players carry it after the multiplier)."""
    pl = fc.players
    df = pd.DataFrame(
        {
            "player_code": np.asarray(pl.player_code, dtype=np.int64),
            "target_gw": np.asarray(pl.gameweek, dtype=np.int64),
            "fixture_id": np.asarray(pl.fixture_id, dtype=np.int64),
            "team_code": np.asarray(pl.team_code, dtype=np.int64),
            "no_start_model": 1.0 - np.asarray(fc.minutes.p_start, dtype=float),
        }
    )
    g = df.groupby(["player_code", "target_gw"])
    out = g.agg(
        team_code=("team_code", "first"),
        n_teams=("team_code", "nunique"),
        no_start_model=("no_start_model", "prod"),
    )
    out["fixture_ids"] = g["fixture_id"].agg(lambda x: ",".join(str(i) for i in sorted(x)))
    out["p_start_before_availability"] = 1.0 - out.pop("no_start_model")
    return out.reset_index()


def _status_at_cutoff(ds: Any, season: str, cutoff: datetime) -> pd.DataFrame:
    snap = PointInTimeView(ds, pd.Timestamp(cutoff)).latest_snapshot(season)
    cols = ["player_code", "status", "chance_of_playing"]
    if snap.empty:
        return pd.DataFrame(columns=cols)
    return snap.assign(
        chance_of_playing=pd.to_numeric(snap["chance_of_playing_next_round"], errors="coerce")
    )[cols]


@dataclass
class _Built:
    context: pd.DataFrame
    mc: np.ndarray  # [rows, S] Monte Carlo points samples
    clim: np.ndarray | None  # [rows, n] climatology samples (None: baselines unavailable)
    checks: dict[str, Any]


def data_checks(ds: Any, season: str, gw: int, cutoff: datetime) -> dict[str, dict[str, Any]]:
    """Leakage detection on the forecast's own data: no FPL news item in the snapshot carries a
    ``news_added`` stamp after the cutoff, and no fixture of the gameweek had a result
    (provisional or final) and no result row of it was loaded. A failure proves post-cutoff
    data reached the forecast; a pass proves nothing about time — news is not stamped on a
    schedule and a match in progress has no canonical trace — so the time evidence is FPL's
    server time (``fpl_server_time_before_cutoff``), never these."""
    snaps = ds["player_snapshots"]
    snaps = snaps[snaps["season"] == season]
    added = pd.to_datetime(snaps.get("news_added"), utc=True, errors="coerce").dropna()
    latest_news = added.max() if len(added) else None
    fx = ds["fixtures"]
    fx = fx[(fx["season"] == season) & (fx["gw"] == gw)]
    played = fx[fx["status"].isin(["provisional", "final"])]
    pm = ds["player_match"]
    rows = pm[(pm["season"] == season) & (pm["gw"] == gw)]
    return {
        "no_news_after_cutoff": {
            "ok": latest_news is None or latest_news <= pd.Timestamp(cutoff),
            "detail": f"latest FPL news stamp {latest_news}, cutoff {cutoff}",
        },
        "no_results_of_the_gameweek": {
            "ok": played.empty and rows.empty,
            "detail": f"{len(played)} fixtures with results, {len(rows)} result rows for GW{gw}",
        },
    }


def _build_rows(settings: Settings, fc: Forecast, season: str, gw: int, ds: Any) -> _Built:
    """Context, baselines and distributions for every forecast row. Baselines are fitted on the
    forecast's own snapshot through the point-in-time view at its cutoff (the walk-forward
    benchmark code); without that snapshot they are unavailable, never fitted elsewhere."""
    p = fc.provenance
    summary = fc.summary.rename(columns={"gw": "target_gw"})
    keys = summary[["player_code", "target_gw"]]
    ctx = keys.merge(forecast_context(fc), on=["player_code", "target_gw"], how="left")
    sim = fc.simulation
    pi = np.searchsorted(sim.player_codes, keys["player_code"].to_numpy())
    gi = np.searchsorted(sim.gameweeks, keys["target_gw"].to_numpy())
    mc = sim.points[:, pi, gi].T  # [rows, S]
    checks: dict[str, Any] = {}
    clim = None
    if ds is None:
        checks["baselines"] = {"ok": False, "reason": "the forecast's snapshot is no longer stored"}
        return _Built(ctx, mc, clim, checks)
    hist = history_cutoffs(ds, season)
    cut = next(c for c in hist if c.season == season and c.gw == gw)
    horizon = int(summary["horizon"].max()) + 1
    cfg = ForecastEvalConfig(horizon=max(horizon, settings.forecast_horizon))
    cache = FeatureCache(ds, cfg.horizon, settings.feature_store_dir)
    ff = cache.get(cut)
    same_frame = ff.snapshot_id == p.get("feature_snapshot_id")
    checks["feature_snapshot_matches"] = {
        "ok": same_frame,
        "detail": f"{ff.snapshot_id} vs {p.get('feature_snapshot_id')}",
    }
    if not same_frame or pd.Timestamp(cut.cutoff) != pd.Timestamp(p["cutoff"]):
        checks["baselines"] = {
            "ok": False,
            "reason": "the forecast's feature frame could not be reproduced on its snapshot",
        }
        return _Built(ctx, mc, clim, checks)
    bench = train_benchmarks(cache, cut, hist, cfg, direct_model=False)
    pr = prediction_rows(cache, cut, fc, bench, cfg)
    rows = pr.rows[["player_code", "target_gw", "position", "start_rate_long", *BASELINE_COLUMNS]]
    rows = rows.assign(clim_mean=pr.clim_samples.mean(axis=1))
    for k in POINT_THRESHOLDS:
        rows[f"clim_prob_{k}_plus"] = (pr.clim_samples >= k).mean(axis=1)
    order = keys.merge(
        pr.rows[["player_code", "target_gw"]].reset_index(), on=["player_code", "target_gw"]
    )["index"].to_numpy()
    if len(order) != len(keys):
        raise RuntimeError("benchmark rows do not cover the forecast rows")
    clim = pr.clim_samples[order]
    frame = ff.frame.drop_duplicates("player_code").set_index("player_code")
    ctx = ctx.merge(rows, on=["player_code", "target_gw"], how="left")
    ctx["price"] = ctx["player_code"].map(frame["price"])
    status = _status_at_cutoff(ds, season, cut.cutoff)
    ctx = ctx.merge(status, on="player_code", how="left")
    checks["baselines"] = {"ok": True}
    return _Built(ctx, mc, clim, checks)


def _checkpoint_id(*parts: Any) -> str:
    return short_id("ckp", {"parts": [str(x) for x in parts]})


def _missed(s: Any, g: Gameweek, reason: str, promo: dict[str, Any] | None) -> str:
    """A ``missed`` checkpoint: what was (not) there, never a recomputed forecast."""
    cid = _checkpoint_id(KIND, g.season, g.gameweek, "missed")
    known = (
        {
            "forecast_key": promo["forecast_key"],
            "data_snapshot_id": promo["snapshot_id"],
            "prediction_run_id": promo["prediction_run_id"],
            "promoted_at": promo["promoted_at"],
            "snapshot_created_at": promo["snapshot_created_at"],
            "artifact_sha256": promo["artifact_sha256"],
            "attested_at": promo.get("attested_at"),
        }
        if promo
        else {}
    )
    s.add(
        m.ForecastCheckpointRow(
            id=cid,
            season=g.season,
            gameweek=g.gameweek,
            kind=KIND,
            state="missed",
            reasons=[reason],
            checks={},
            cutoff_at=g.cutoff,
            deadline_at=g.deadline,
            first_kickoff_at=g.first_kickoff,
            capture_code_digest=code_digest(),
            build_revision=build_revision(),
            provenance_json={},
            populations_json={},
            n_rows=0,
            **known,
        )
    )
    return cid


def _existing(engine: Engine, season: str, gw: int) -> str | None:
    with session_scope(engine) as s:
        cid: str | None = s.scalar(
            select(m.ForecastCheckpointRow.id).where(
                m.ForecastCheckpointRow.season == season,
                m.ForecastCheckpointRow.gameweek == gw,
                m.ForecastCheckpointRow.kind == KIND,
            )
        )
    return cid


TIME_CHECK = "fpl_server_time_before_cutoff"


def prospective_checks(
    promo: dict[str, Any],
    sha: str,
    provenance: dict[str, Any],
    season: str,
    gw: int,
    cutoff: datetime,
    existed_by: datetime | None,
) -> dict[str, dict[str, Any]]:
    """The checks a checkpoint must pass to be ``prospective`` (contract §2.3): FPL's server
    time shows the file existed at or before the cutoff (``existed_by``: the earliest such time
    on record for the promoted hash), the file is byte-identical to the promoted one, the
    forecast identity is the promotion's, and the forecast's cutoff is the gameweek's. A moved
    deadline is flagged and the server time is then judged against the earlier of the two
    cutoffs. The host's clock never decides (``local_clock``)."""
    p = provenance
    prov_cutoff = _utc(p["cutoff"])
    earliest = min(prov_cutoff, cutoff)
    if existed_by is None:
        detail = f"no FPL server time shows when the file existed; cutoff {earliest}"
    else:
        gap = (earliest - existed_by).total_seconds() / 60
        detail = (
            f"FPL server time shows the file existed by {existed_by}; cutoff {earliest} "
            f"({abs(gap):.0f} min {'before' if gap >= 0 else 'after'} it)"
        )
    checks: dict[str, dict[str, Any]] = {
        TIME_CHECK: {"ok": existed_by is not None and existed_by <= earliest, "detail": detail},
        "file_unchanged_since_promotion": {
            "ok": sha == promo["artifact_sha256"],
            "detail": f"{sha} vs {promo['artifact_sha256']}",
        },
        "identity_matches_promotion": {
            "ok": p.get("prediction_run_id") == promo["prediction_run_id"]
            and p.get("data_snapshot_id") == promo["snapshot_id"]
            and p.get("forecast_key") == promo["forecast_key"]
            and str(p.get("season")) == season
            and int(p.get("decision_gw", -1)) == gw,
            "detail": f"{p.get('prediction_run_id')} {p.get('data_snapshot_id')}",
        },
        "cutoff_matches_deadline": {
            "ok": True,
            "detail": f"forecast cutoff {prov_cutoff}, gameweek cutoff {cutoff}",
        },
    }
    if prov_cutoff != cutoff:  # FPL moved the deadline: flagged, judged on the earlier cutoff
        checks["deadline_moved"] = {"ok": True, "detail": "FPL moved the deadline"}
    return checks


def local_clock(promo: dict[str, Any], cutoff: datetime) -> dict[str, Any]:
    """Promotion and export times by this host's clocks against the cutoff: how the fallback
    candidate was chosen, and a skew signal next to FPL's time. Informational only."""
    promoted, exported = _utc(promo["promoted_at"]), _utc(promo["snapshot_created_at"])
    return {
        "ok": promoted <= cutoff and exported <= cutoff,
        "detail": f"promoted {promoted}, exported {exported}, cutoff {cutoff} (host clocks)",
    }


def state_of(checks: dict[str, Any]) -> tuple[str, list[str]]:
    """``prospective`` when every deciding check holds; ``prospective_unverified`` when only FPL's
    server time is missing or late; ``live_unverified`` when anything else failed."""
    failed = [k for k, v in checks.items() if k != "info" and not v["ok"]]
    if [k for k in failed if k != TIME_CHECK]:
        return "live_unverified", failed
    return ("prospective_unverified" if failed else "prospective"), failed


def capture(
    engine: Engine,
    settings: Settings,
    data: DataService,
    season: str,
    gw: int,
    now: datetime | None = None,
    clock: ServerClock | None = None,
) -> str:
    """Freeze ``gw``'s checkpoint; returns its id (an existing one if already captured), or
    ``not_due`` / ``before_record``. Everything is written in one transaction at the end.
    ``clock`` adds FPL's time at capture to the record (informational)."""
    now = now or datetime.now(UTC)
    g = next((x for x in season_gameweeks(engine, season) if x.gameweek == gw), None)
    if g is None:
        raise LookupError(f"{season} GW{gw} is not a known gameweek")
    if now < g.cutoff:
        return "not_due"
    found = _existing(engine, season, gw)
    if found is not None:
        return found
    started = record_started(engine)
    if started is None or g.cutoff < started:
        return "before_record"
    cols = (
        "snapshot_id",
        "gameweek",
        "forecast_key",
        "prediction_run_id",
        "snapshot_created_at",
        "artifact_sha256",
        "code_digest",
        "promoted_at",
    )
    with session_scope(engine) as s:
        cand = checkpoint_candidate(s, season, gw, g.cutoff)
        promo = cand.promotion
        promo_vals = (
            {**{c: getattr(promo, c) for c in cols}, "attested_at": cand.existed_by}
            if promo is not None
            else None
        )
        later = {c: str(getattr(cand.later, c)) for c in cols} if cand.later is not None else None
    if promo_vals is None or promo_vals["gameweek"] != gw:
        reason = (
            "no forecast was promoted before the cutoff"
            if promo_vals is None
            else f"the forecast served at the cutoff was for GW{promo_vals['gameweek']}"
        )
        return _write_missed(engine, g, reason)
    path = forecast_path(settings, promo_vals["forecast_key"])
    if not path.exists():
        return _write_missed(engine, g, "the forecast file was deleted before capture", promo_vals)
    raw = path.read_bytes()
    sha = hashlib.sha256(raw).hexdigest()
    fc: Forecast = joblib.load(io.BytesIO(raw))
    p = fc.provenance

    # ---- the prospective test (§2.3); capture time is recorded, never a criterion
    snap_id = str(p.get("data_snapshot_id"))
    snap_path = data.snapshot_path(snap_id)
    ds = load_snapshot(snap_path) if snap_path is not None else None
    if ds is not None and ds.snapshot_id != snap_id:
        raise RuntimeError(f"snapshot {snap_path} loaded as {ds.snapshot_id}, expected {snap_id}")
    earliest = min(_utc(p["cutoff"]), g.cutoff)
    checks: dict[str, Any] = prospective_checks(
        promo_vals, sha, p, season, gw, g.cutoff, cand.existed_by
    )
    if ds is not None:
        checks.update(data_checks(ds, season, gw, earliest))
    else:  # retention keeps it until capture; without it the data cannot be checked
        checks["snapshot_available"] = {"ok": False, "detail": f"{snap_id} no longer stored"}
    state, failed = state_of(checks)

    # ---- informational: never part of the prospective test
    built = _build_rows(settings, fc, season, gw, ds)
    info: dict[str, Any] = dict(built.checks)
    info["local_clock"] = local_clock(promo_vals, earliest)
    info["later_promotion_without_server_time"] = {
        "ok": later is None,
        "detail": (
            "none"
            if later is None
            else f"{later['snapshot_id']} {later['forecast_key']}: the host's clock places its "
            f"promotion ({later['promoted_at']}) before the cutoff, but no FPL server time shows "
            "its file existed by then; the checkpoint keeps the attested forecast"
        ),
    }
    at_capture = read_server_time(clock, "capture")
    info["captured_server_time"] = {
        "ok": at_capture is not None,
        "detail": at_capture.evidence() if at_capture is not None else "FPL not reached",
    }
    summary = fc.summary.rename(columns={"gw": "target_gw"})
    with session_scope(engine) as s:
        run = s.get(m.PredictionRunRow, str(p["prediction_run_id"]))
        generated = run.generated_at if run is not None else None
    info["prediction_run_registered"] = {
        "ok": run is not None,
        "detail": str(generated) if generated is not None else "no prediction_runs row",
    }
    checks["info"] = info
    rows = _prediction_rows(summary, built.context, run is not None, str(p["prediction_run_id"]))
    rows_sha = _frame_digest(rows.assign(components_json=rows["components_json"].map(json.dumps)))
    ctx = built.context.drop(columns=["n_teams"], errors="ignore")
    h0 = ctx[summary["horizon"].to_numpy() == 0]
    populations = {
        "rows": len(summary),
        "players": int(summary["player_code"].nunique()),
        "target_gameweeks": sorted(int(x) for x in summary["target_gw"].unique()),
        "h0_rows": len(h0),
        "h0_regulars": int((h0.get("start_rate_long", pd.Series(dtype=float)) >= 0.5).sum()),
        "h0_status_u": int((h0.get("status", pd.Series(dtype=object)) == "u").sum()),
    }
    lo_mc, counts_mc = dist.encode(built.mc)
    blob_parts: dict[str, Any] = {
        "player_code": summary["player_code"].to_numpy(np.int64),
        "target_gw": summary["target_gw"].to_numpy(np.int64),
        "mc_lo": np.array([lo_mc]),
        "mc_counts": counts_mc,
    }
    if built.clim is not None:
        lo_c, counts_c = dist.encode(built.clim)
        blob_parts.update(clim_lo=np.array([lo_c]), clim_counts=counts_c)
    buf = io.BytesIO()
    np.savez_compressed(buf, **blob_parts)
    cid = _checkpoint_id(KIND, season, gw, p["prediction_run_id"], sha)
    reasons = [f"{k}: {checks[k].get('detail', '')}" for k in failed]
    if not info["baselines"]["ok"]:
        reasons.append(f"baselines_unavailable: {info['baselines']['reason']}")
    try:
        with session_scope(engine) as s:
            ctx_sha = _blob(s, _parquet(ctx), "application/vnd.apache.parquet")
            dist_sha = _blob(s, buf.getvalue(), "application/x-npz")
            s.add(
                m.ForecastCheckpointRow(
                    id=cid,
                    season=season,
                    gameweek=gw,
                    kind=KIND,
                    state=state,
                    reasons=reasons,
                    checks=checks,
                    prediction_run_id=str(p["prediction_run_id"]),
                    forecast_key=promo_vals["forecast_key"],
                    data_snapshot_id=promo_vals["snapshot_id"],
                    feature_snapshot_id=str(p.get("feature_snapshot_id")),
                    cutoff_at=g.cutoff,
                    deadline_at=g.deadline,
                    first_kickoff_at=g.first_kickoff,
                    snapshot_created_at=promo_vals["snapshot_created_at"],
                    promoted_at=promo_vals["promoted_at"],
                    generated_at=generated,
                    attested_at=cand.existed_by,
                    artifact_sha256=sha,
                    code_digest=p.get("code_digest"),
                    capture_code_digest=code_digest(),
                    build_revision=str(p.get("build_revision") or "unrecorded"),
                    provenance_json=json.loads(json.dumps(p, default=str)),
                    populations_json=populations,
                    n_rows=len(rows),
                    rows_sha256=rows_sha,
                    context_blob=ctx_sha,
                    distributions_blob=dist_sha,
                )
            )
            s.flush()
            s.execute(
                insert(m.PredictionRow),
                [{**r, "checkpoint_id": cid} for r in rows.to_dict("records")],
            )
    except IntegrityError:  # a concurrent capture wrote it first
        existing = _existing(engine, season, gw)
        if existing is None:
            raise
        return existing
    log.info(
        "forecast_checkpoint",
        season=season,
        gw=gw,
        state=state,
        checkpoint=cid,
        rows=len(rows),
        failed=failed,
    )
    return cid


def _write_missed(
    engine: Engine, g: Gameweek, reason: str, promo: dict[str, Any] | None = None
) -> str:
    try:
        with session_scope(engine) as s:
            cid = _missed(s, g, reason, promo)
    except IntegrityError:
        existing = _existing(engine, g.season, g.gameweek)
        if existing is None:
            raise
        return existing
    log.warning("forecast_checkpoint_missed", season=g.season, gw=g.gameweek, reason=reason)
    return cid


def _prediction_rows(
    summary: pd.DataFrame, ctx: pd.DataFrame, run_registered: bool, run_id: str
) -> pd.DataFrame:
    """The forecast's own numbers in the ``predictions`` schema, one row per player × target
    gameweek, in the forecast's order."""
    out = pd.DataFrame(
        {
            "run_id": run_id if run_registered else None,
            "player_code": summary["player_code"].astype(int),
            "gw": summary["target_gw"].astype(int),
            "horizon": summary["horizon"].astype(int),
            "target": "points",
            "n_fixtures": ctx["fixture_ids"].str.count(",").fillna(0).astype(int) + 1,
            **{
                c: summary[c].astype(float)
                for c in (
                    "mean",
                    "p10",
                    "p25",
                    "p50",
                    "p75",
                    "p90",
                    "std",
                    "prob_2_plus",
                    "prob_6_plus",
                    "prob_10_plus",
                    "prob_15_plus",
                    "expected_minutes",
                    "minutes_p10",
                    "minutes_p50",
                    "minutes_p90",
                )
            },
            "start_probability": summary["prob_start"].astype(float),
        }
    )
    comp = summary[[f"xp_{c}" for c in COMPONENTS]].astype(float)
    comp.columns = list(COMPONENTS)
    comp["prob_play"] = summary["prob_play"].astype(float)
    comp["start_probability_model"] = summary["start_probability_model"].astype(float)
    out["components_json"] = comp.to_dict("records")
    return out


# ----------------------------------------------------------------------------- reading


@dataclass
class Checkpoint:
    header: m.ForecastCheckpointRow
    rows: pd.DataFrame  # predictions rows + context/baselines, one per player × target GW
    mc_lo: int | None
    mc_counts: np.ndarray | None
    clim_lo: int | None
    clim_counts: np.ndarray | None


def load_checkpoint(engine: Engine, checkpoint_id: str) -> Checkpoint:
    """A stored checkpoint, exactly as captured (rows, context, baselines, distributions)."""
    with session_scope(engine) as s:
        h = s.get(m.ForecastCheckpointRow, checkpoint_id)
        if h is None:
            raise LookupError(checkpoint_id)
        s.expunge(h)
        preds = pd.read_sql(
            select(m.PredictionRow)
            .where(m.PredictionRow.checkpoint_id == checkpoint_id)
            .order_by(m.PredictionRow.id),
            s.connection(),
        )
        ctx_raw = s.get(m.ArtifactBlob, h.context_blob) if h.context_blob else None
        dist_raw = s.get(m.ArtifactBlob, h.distributions_blob) if h.distributions_blob else None
        ctx_bytes = bytes(ctx_raw.data) if ctx_raw is not None else None
        dist_bytes = bytes(dist_raw.data) if dist_raw is not None else None
    rows = preds.rename(columns={"gw": "target_gw"})
    if ctx_bytes is not None:
        ctx = pd.read_parquet(io.BytesIO(ctx_bytes))
        rows = rows.merge(ctx, on=["player_code", "target_gw"], how="left")
    mc_lo = mc_counts = clim_lo = clim_counts = None
    if dist_bytes is not None:
        with np.load(io.BytesIO(dist_bytes)) as z:
            order = pd.DataFrame(
                {"player_code": z["player_code"], "target_gw": z["target_gw"]}
            ).reset_index()
            idx = rows[["player_code", "target_gw"]].merge(
                order, on=["player_code", "target_gw"], how="left"
            )["index"]
            if idx.isna().any():
                raise RuntimeError(f"{checkpoint_id}: distributions do not cover every row")
            sel = idx.to_numpy(int)
            mc_lo, mc_counts = int(z["mc_lo"][0]), z["mc_counts"][sel]
            if "clim_counts" in z:
                clim_lo, clim_counts = int(z["clim_lo"][0]), z["clim_counts"][sel]
    return Checkpoint(h, rows, mc_lo, mc_counts, clim_lo, clim_counts)
