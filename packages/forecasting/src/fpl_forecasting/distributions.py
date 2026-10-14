"""Exact, compact storage of per-row empirical point distributions (M1.5 forecast ledger).

FPL points are integers, so a row's Monte Carlo (or climatology) samples are fully described
by how often each value occurs. Storing those counts instead of the samples loses nothing that
the sample-based scores use — CRPS and the randomised PIT depend on the empirical distribution,
not on sample order — and is about a twentieth of the size.
"""

from __future__ import annotations

import io

import numpy as np
import numpy.typing as npt


def encode(samples: npt.ArrayLike) -> tuple[int, npt.NDArray[np.int32]]:
    """``samples`` [rows, n] of integer points → (lowest value, counts [rows, values])."""
    x = np.asarray(samples)
    xi = np.rint(x).astype(np.int64)
    if not np.array_equal(xi, x):
        raise ValueError("point samples must be integers")
    if xi.size == 0:
        return 0, np.zeros((xi.shape[0], 0), dtype=np.int32)
    lo, hi = int(xi.min()), int(xi.max())
    width = hi - lo + 1
    counts = np.zeros((xi.shape[0], width), dtype=np.int32)
    rows = np.repeat(np.arange(xi.shape[0]), xi.shape[1])
    np.add.at(counts, (rows, (xi - lo).ravel()), 1)
    return lo, counts


def decode(lo: int, counts: npt.NDArray[np.integer]) -> npt.NDArray[np.float64]:
    """The samples back, sorted per row ([rows, n]); every row must hold the same number."""
    n = counts.sum(axis=1)
    if len(n) and not (n == n[0]).all():
        raise ValueError("every row must hold the same number of samples")
    values = np.arange(lo, lo + counts.shape[1], dtype=np.float64)
    return np.stack([np.repeat(values, c) for c in counts]) if len(n) else np.zeros((0, 0))


def to_bytes(lo: int, counts: npt.NDArray[np.integer]) -> bytes:
    buf = io.BytesIO()
    np.savez_compressed(buf, lo=np.array([lo]), counts=counts.astype(np.int32))
    return buf.getvalue()


def from_bytes(data: bytes) -> tuple[int, npt.NDArray[np.int32]]:
    with np.load(io.BytesIO(data)) as z:
        return int(z["lo"][0]), z["counts"].astype(np.int32)
