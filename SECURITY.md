# Security policy

## Supported versions

Only the latest release (currently the `0.2.x` line) receives fixes. Please reproduce issues on
the newest tag or on `master` before reporting.

## Reporting a vulnerability

Please **do not open a public issue** for security problems. Report them privately through
GitHub's [private vulnerability reporting](https://github.com/marcelpetrick/GitLabTeamPulse/security/advisories/new)
or by e-mail to Marcel Petrick <mail@marcelpetrick.it>. Include the affected version, the
steps to reproduce and the impact you expect. You will get an acknowledgement within a few
days.

## Deployment assumptions

GitLab Team Pulse is a prototype for **trusted internal networks** (see
[`VISION.md`](VISION.md) §6.3 and §32). It has no login of its own, so keep these in place:

- **Limit network exposure.** Bind to localhost or an internal interface (the compose file
  publishes on `127.0.0.1` only), and put a reverse proxy with TLS and authentication in front
  for wider access.
- **Use a read-only token.** A `read_api` token is sufficient; never grant `api` (write)
  scope. See [`docs/GRAPHQL.md`](docs/GRAPHQL.md#permissions).
- **Deliver the token as a secret.** Use a Docker secret (`/run/secrets/gitlab_token`) or a
  secret file, not an image layer or a committed `.env`. The token is never written to SQLite,
  logs, API responses or error messages, and it is only sent to the configured GitLab host.
- **Protect the data volume.** The container runs as uid 10001, with `/data` at mode `0700`.
  The SQLite file holds cached GitLab data (names, titles, timelogs), so treat backups of it
  accordingly.

Built-in safeguards: a strict Content Security Policy, no CORS, sanitized GitLab links,
throttled manual refresh, a non-root image, and weekly Dependabot updates for pinned
dependencies.
