# Security model

## What is protected
Bankroll, bets, opportunity cards, decisions, logs, API keys and notification credentials.

## Where things live
| Item | Location | Exposure |
|---|---|---|
| Code | repository | as visible as the repository |
| State (`fbs-data` branch), Actions logs, command issues | repository | **private only**: the engine refuses to run in a public repository (`guard` job checks through the API) |
| API keys, ntfy topic, Cloudflare token | GitHub Actions secrets | server-side only; never written to state, the app or logs |
| iPhone app + `state.json` | Cloudflare Pages | published **only** when `FBS_ACCESS_PROTECTED=true`, i.e. after a Cloudflare Access policy (email one-time code) covers the project, including preview URLs |
| Push content | ntfy / Telegram | **minimal by default**: event name only, no amounts or selections (`FBS_PUSH_DETAIL=minimal`) |

## Authentication and commands
* The app holds **no credentials** and makes no privileged API calls. It even erases settings stored by older versions.
* Viewing: Cloudflare Access authenticates the owner at the edge before any file, `state.json` included, is served.
* Commands (START/STOP, CONTINUE, confirm, decline, manual settle): the app opens a pre-filled GitHub issue, which the owner
  submits while signed in to GitHub (GitHub handles authentication, including 2FA). The workflow then:
  1. runs only if the repository is private,
  2. accepts only issues whose author is the repository owner (`author_association == OWNER`),
  3. parses the body as JSON data from an environment variable (never interpolated into a shell), with a command
     allowlist and strict field validation (id pattern, numeric ranges, reference character set, exact `CONTINUE`),
  4. replies on the issue and closes it. Refusals are reported, not executed.
* `workflow_dispatch` remains available from GitHub itself (requires write access).
* State writes are serialized (`concurrency: fbs-state`), so a command and an hourly run cannot interleave.

## Betting safety
No bet is ever placed or submitted automatically. No bookmaker credentials are stored. Opening Bet365 is never
recorded as a bet; only the owner's confirmation with the stake and odds Bet365 accepted creates one.

## Known residual risks
* ntfy.sh topics are not access-controlled on the free plan: anyone who learns the topic name can read pushes
  (hence minimal content) or post fake ones. Telegram (private bot chat) is the stronger free alternative.
* A fork of a public repository cannot be made private on GitHub; the code must live in a separate private repository.
* `fbs-data` history and Actions logs created while the repository was public remain public until deleted (they contain
  paper-trading test data only: no real bets and no secrets).
