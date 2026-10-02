"""Source adapters: retry/backoff, SSRF protection, API contracts (§6 'Extract', §75)."""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from fpl_domain.rules import load_ruleset
from fpl_ingestion.http import DisallowedHostError, HttpConfig, HttpFetcher, SourceUnavailableError
from fpl_ingestion.live import canonicalize_bootstrap
from fpl_ingestion.sources import fpl_api_schemas as s
from fpl_ingestion.sources.fpl_api import FplApiClient
from fpl_storage.dataset import CanonicalDataset
from tests.fixtures_util import API_DIR, SNAPSHOT_AT

BASE = "https://fantasy.premierleague.com/api"


def fetcher(max_retries: int = 3) -> HttpFetcher:
    return HttpFetcher(
        HttpConfig(
            allowed_hosts=("fantasy.premierleague.com",),
            max_retries=max_retries,
            backoff_initial_seconds=0.001,
            backoff_max_seconds=0.002,
        )
    )


@respx.mock
def test_retries_transient_failures_then_succeeds() -> None:
    route = respx.get(f"{BASE}/fixtures/").mock(
        side_effect=[httpx.Response(503), httpx.ConnectError("boom"), httpx.Response(200, json=[])]
    )
    res = fetcher().get(f"{BASE}/fixtures/")
    assert res.status == 200 and res.attempts == 3 and route.call_count == 3


@respx.mock
def test_gives_up_after_max_retries() -> None:
    respx.get(f"{BASE}/fixtures/").mock(return_value=httpx.Response(429))
    with pytest.raises(SourceUnavailableError):
        fetcher(max_retries=2).get(f"{BASE}/fixtures/")


@respx.mock
def test_non_retryable_status_fails_fast() -> None:
    route = respx.get(f"{BASE}/entry/1/").mock(return_value=httpx.Response(404))
    with pytest.raises(SourceUnavailableError):
        fetcher().get(f"{BASE}/entry/1/")
    assert route.call_count == 1


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.example.com/api/fixtures/",
        "http://fantasy.premierleague.com/api/fixtures/",
        "https://169.254.169.254/latest/meta-data/",
    ],
)
def test_disallowed_urls_never_requested(url: str) -> None:
    with respx.mock(assert_all_called=False) as router:
        router.get(url).mock(return_value=httpx.Response(200))
        with pytest.raises(DisallowedHostError):
            fetcher().get(url)
        assert not router.calls


@pytest.mark.parametrize("bad", [0, -5, 10**9, True, "123"])
def test_user_supplied_ids_are_bounded(bad: object) -> None:
    client = FplApiClient(fetcher())
    with pytest.raises(ValueError, match="entry_id"):
        client.entry(bad)  # type: ignore[arg-type]


@respx.mock
def test_bootstrap_contract_and_canonicalisation() -> None:
    boot_bytes = (API_DIR / "bootstrap-static.json").read_bytes()
    fx_bytes = (API_DIR / "fixtures.json").read_bytes()
    respx.get(f"{BASE}/bootstrap-static/").mock(
        return_value=httpx.Response(200, content=boot_bytes)
    )
    respx.get(f"{BASE}/fixtures/").mock(return_value=httpx.Response(200, content=fx_bytes))
    client = FplApiClient(fetcher())
    raw, boot = client.bootstrap_static()
    _, fixtures = client.fixtures()
    assert raw.sha256 and raw.content == boot_bytes  # raw payload stored unchanged
    assert len(boot.elements) == 616 and len(fixtures) == 380
    canon, _ = canonicalize_bootstrap(boot, fixtures, SNAPSHOT_AT, load_ruleset("2026-27"))
    ds = CanonicalDataset(canon.frames())
    assert len(ds["players"]) == 616
    gw = ds["gameweeks"].set_index("gw")
    assert gw.loc[2, "status"] == "upcoming" and gw.loc[1, "status"] == "finalized"
    assert ds["player_snapshots"]["source"].eq("fpl_api:bootstrap").all()


def test_schema_rejects_malformed_payload() -> None:
    data = json.loads((API_DIR / "bootstrap-static.json").read_text())
    data["elements"][0]["now_cost"] = 9999
    with pytest.raises(ValueError, match="now_cost"):
        s.BootstrapStatic.model_validate(data)
