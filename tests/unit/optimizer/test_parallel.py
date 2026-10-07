"""Independent solves in worker processes give exactly the in-process results, in order."""

from __future__ import annotations

from dataclasses import asdict, replace

import numpy as np
import pytest

from fpl_decision.stability import stability
from fpl_optimizer.parallel import map_ordered
from tests.opt_util import tiny_league


def _square(x: int) -> int:
    return x * x


def _fail_on_three(x: int) -> int:
    if x == 3:
        raise ValueError("task 3 failed")
    return x


def test_map_ordered_keeps_input_order_for_any_worker_count() -> None:
    tasks = list(range(7))
    expected = [x * x for x in tasks]
    assert map_ordered(_square, tasks, 1) == map_ordered(_square, tasks, 3) == expected
    assert map_ordered(_square, [], 4) == []


def test_map_ordered_reraises_a_task_failure() -> None:
    with pytest.raises(ValueError, match="task 3 failed"):
        map_ordered(_fail_on_three, list(range(6)), 2)


def test_stability_does_not_depend_on_workers() -> None:
    prob = tiny_league(7, extras=(2, 2, 2, 2), horizon=3)
    st = prob.config.stability.model_copy(update={"perturbations": 6})
    prob = replace(prob, config=prob.config.model_copy(update={"stability": st}))
    serial = stability(prob, frozenset(), frozenset())
    parallel = stability(prob, frozenset(), frozenset(), workers=3)
    np.testing.assert_equal(asdict(parallel), asdict(serial))  # NaN-safe, field by field
    assert serial.perturbations == 6
