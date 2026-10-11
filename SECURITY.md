# Security policy

FPL Copilot handles invite-only accounts, session cookies, API keys and Fantasy Premier League
manager data. Please report vulnerabilities privately.

## Reporting a vulnerability

* Use GitHub's **private vulnerability reporting** for this repository (Security → Report a
  vulnerability) where it is offered; otherwise contact the maintainer privately through their
  GitHub profile ([@adivishall](https://github.com/adivishall)) and ask for a private channel.
  Do not open a public issue for a security problem.
* Describe the affected route, page or component, the steps to reproduce, and the impact.
* Reproduce only against a deployment you run yourself (`docker compose up -d`, see the README).
  There is no public deployment to test against.

## Never include in a report, issue or pull request

* API keys or their hashes, `.env` contents, database passwords, webhook secrets.
* Session tokens or cookies (`fpl_session`), invitation codes, passwords.
* Manager keys, FPL entry IDs or squads belonging to real people, exported manager data.
* Logs, database dumps or screenshots that contain any of the above.

Redact them (e.g. `mgr_xxxxxxxx`) — a reproduction never needs real values.

## Scope

In scope: authentication and sessions, manager-scoped authorisation (every manager key a request
names must belong to the signed-in user), the web server's API-key proxy, webhook SSRF
protection, rate limiting, export and erasure, and the Docker deployment configuration.

The security model and its known limits are documented in
[`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) (*Accounts and beta access*) and
[`docs/KNOWN_LIMITATIONS.md`](docs/KNOWN_LIMITATIONS.md) §6.

## Supported versions

Only the current development branch receives fixes; there are no released versions yet.
