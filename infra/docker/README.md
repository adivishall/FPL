# Container images

`python.Dockerfile` builds one image for the API (`uvicorn fpl_api.main:app`), the RQ worker
(`fpl-worker work`) and the scheduler (`fpl-worker schedule`).

* Dependencies are installed from `uv.lock` (`uv sync --frozen`), so images are reproducible.
* The runtime stage is `python:3.12-slim`, runs as a non-root `app` user, needs no `apt` (the
  OpenMP runtime required by LightGBM is copied from the builder), and has a Python-based
  healthcheck against `/api/v1/health`.
* Behind a TLS-inspecting proxy, pass the proxy CA as a BuildKit secret (used only in the
  builder stage, never written to the runtime image):

```bash
docker build --secret id=extra_ca,src=/path/to/ca-bundle.crt \
  -f infra/docker/python.Dockerfile -t fpl-engine:local .
```
