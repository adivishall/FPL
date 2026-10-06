"""Cache files are published atomically: an interrupted writer leaves nothing under the key."""

from __future__ import annotations

from pathlib import Path

import pytest

from fpl_api.services import _publish


def test_interrupted_write_leaves_no_file_under_the_key(tmp_path: Path) -> None:
    target = tmp_path / "forecasts" / "fc_abc.joblib"

    def killed_midway(tmp: Path) -> None:
        tmp.write_bytes(b"partial")
        raise KeyboardInterrupt  # stands in for the process dying mid-write

    with pytest.raises(KeyboardInterrupt):
        _publish(target, killed_midway)
    assert not target.exists()  # readers never see a truncated cache entry
    assert list(target.parent.iterdir()) == []  # and no temporary file is left behind

    _publish(target, lambda tmp: tmp.write_bytes(b"complete"))
    assert target.read_bytes() == b"complete"
