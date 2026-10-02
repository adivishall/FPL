from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest
from hypothesis import given
from hypothesis import strategies as st

from fpl_domain.config import config_from_dict, load_versioned_config
from fpl_domain.hashing import canonical_json, content_hash, short_id

json_values = st.recursive(
    st.none()
    | st.booleans()
    | st.integers()
    | st.text(max_size=8)
    | st.floats(allow_nan=False, allow_infinity=False),
    lambda children: (
        st.lists(children, max_size=4) | st.dictionaries(st.text(max_size=6), children, max_size=4)
    ),
    max_leaves=12,
)


@given(st.dictionaries(st.text(max_size=6), json_values, max_size=6))
def test_hash_independent_of_key_order(d: dict) -> None:
    reordered = dict(reversed(list(d.items())))
    assert content_hash(d) == content_hash(reordered)


def test_equivalent_datetimes_hash_equal() -> None:
    utc = datetime(2026, 8, 28, 17, 30, tzinfo=UTC)
    bst = utc.astimezone(timezone(timedelta(hours=1)))
    assert content_hash({"t": utc}) == content_hash({"t": bst})


def test_naive_datetime_rejected() -> None:
    with pytest.raises(ValueError, match="naive"):
        canonical_json({"t": datetime(2026, 1, 1)})


def test_short_id_format() -> None:
    sid = short_id("snap", {"a": 1})
    assert sid.startswith("snap_") and len(sid) == len("snap_") + 16


def test_versioned_config_requires_version(tmp_path) -> None:
    (tmp_path / "optimizer").mkdir()
    (tmp_path / "optimizer" / "x.yaml").write_text("a: 1\n")
    with pytest.raises(ValueError, match="version"):
        load_versioned_config("optimizer", "x", root=tmp_path)


def test_versioned_config_ref_and_hash(tmp_path) -> None:
    (tmp_path / "optimizer").mkdir()
    (tmp_path / "optimizer" / "x.yaml").write_text("version: '1.0.0'\na: 1\n")
    cfg = load_versioned_config("optimizer", "x", root=tmp_path)
    assert cfg.ref.startswith("optimizer/x@1.0.0#")
    assert (
        cfg.content_hash
        == config_from_dict("optimizer", "x", {"version": "1.0.0", "a": 1}).content_hash
    )


def test_sources_config_pins_an_immutable_commit() -> None:
    cfg = load_versioned_config("", "sources")
    commit = cfg.data["historical_repo"]["commit"]
    assert len(commit) == 40 and all(c in "0123456789abcdef" for c in commit)
