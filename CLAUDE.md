# EYE public repository instructions

Read `docs/public-repo-build-brief.md` before changing code. This repository contains the public
source for a private, access-controlled application. A GitHub build proves only the code and
synthetic demo; private installation, credentials, real feeds and access tests happen elsewhere.

## Scope and boundaries

- Work on the package named in the current task. Finish its tests and documentation before
  starting another package. Return a reviewable branch or PR; never merge or deploy it yourself.
- Use synthetic fixtures in this repository and CI. Do not request, store, log or commit real
  observations, provider credentials, account identifiers, private hostnames, user data, backup
  paths or copies of any private Home Lab document. Do not connect to the owner's server.
- The default demo binds to loopback and needs no external account. Production mode must refuse
  startup if the private authentication adapter is absent; never fall back to fake auth.
- Leave real provider adapters disabled until their terms, storage rights, quotas, provenance,
  egress and failure controls are recorded in the source-policy register. No paid request or
  global bulk download is part of a default command or CI run.
- Nothing the project starts may open a browser or interactive window on its own. Disable
  automatic browser launch in tools and subprocesses, including login and error paths. A
  deliberately invoked headless browser test is allowed only within that test command.
- Keep deterministic facts separate from AI prose. A model must not create observed positions,
  dimensions, event outcomes, crowd counts, eclipse times or evidence labels.
- Do not add a project `LICENSE` or call this project open source until the owner chooses a
  licence. Review third-party licences before importing code, models, media or data.

## Build rules

- Use one versioned API schema for REST and WebSocket messages. Generate client types from it and
  validate incoming data at runtime. Bound queries, responses, queues, scratch space and client
  buffers. Preserve source timestamps, coverage, confidence and correction history.
- Keep configuration in one place; do not hardcode machine names, account details, paths, ports
  or provider credentials. Make recurring setup, verification and recovery tasks one command.
- Label every command in documentation and runbooks with where it runs: developer laptop, CI,
  or private server. Keep server-only commands out of public cloud-session instructions.
- Pin dependencies and container images to reviewed versions or digests, document how to update
  them and rerun the relevant gates after an update.
- Run a command that might prompt directly. Do not pipe or redirect it, since doing so can hide
  a password or approval prompt.
- A negative test needs a nearby positive control. Test the actual boundary with a separate
  mechanism where possible: invalid input must fail and a valid input must succeed. A failed
  query or test instrument is `UNVERIFIED`, never evidence that a value is absent.
- Protect history: a raw batch is not eligible for deletion until its derivation and an
  independent verified backup have passed. A failed step must stop pruning and leave the last
  known good copy intact.

## Review and handoff

- Before committing, inspect the exact tracked-file list and diff for private content. Do not
  commit generated secrets, real captures, caches, environment files or local settings.
- Follow `.claude/settings.json` for commit and PR attribution; do not add attribution text or
  session links manually. Use the repository's configured Git author; report its identity and
  commit trailers for review instead of silently changing Git configuration.
- Report the commands run, their exit status, the positive and negative controls, files changed,
  known limitations, source/licence decisions and the next package's prerequisites. Do not call
  a cloud test a successful private deployment.
