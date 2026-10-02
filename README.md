# FPL Decision Engine

An uncertainty-aware decision engine for Fantasy Premier League: it starts from a manager's real
squad, forecasts minutes and point *distributions*, searches legal transfer/chip paths over
multiple gameweeks with a MILP optimiser, compares every plan against **holding**, stress-tests
it, and explains the recommendation from stored evidence.

> Status: under active construction, milestone by milestone. The authoritative progress record
> is [`docs/BUILD_STATUS.md`](docs/BUILD_STATUS.md); architecture decisions are in
> [`docs/decisions/`](docs/decisions/).

## Quick start (development)

```bash
uv sync --frozen          # Python 3.12 workspace (all packages + dev tools)
make test                 # unit + property + integration (ephemeral PostgreSQL)
make ci                   # lint + types + architecture layering + tests
```
