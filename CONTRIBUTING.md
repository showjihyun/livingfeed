# Contributing to Living Feed

Thanks for taking an interest. This document covers what you need to get a change merged.

A note before you start: **the codebase comments and commit messages are written in Korean.** The interface and this document are in English, but reading the source comfortably currently requires Korean. Issues and pull requests are welcome in either language.

## Getting set up

Follow [Running the project](README.md#run) in the README first — you need the Docker data stores up before most tests will do anything.

```bash
pnpm install && uv sync
```

## What CI will check

Run these before you push; they are exactly what [CI](.github/workflows/ci.yml) runs:

```bash
pnpm lint && pnpm typecheck && pnpm test && pnpm build
uv run ruff check . && uv run pytest
```

CI only runs the halves your change touches, but running both locally costs little.

## Tests need an explicit, isolated target

This is the one piece of local setup that is easy to get wrong, and getting it wrong is destructive.

Tests that drop schemas, delete streams, or flush databases **refuse to run unless you name the target explicitly.** With no `LF_TEST_*` variables set, those fixtures skip — so a green `pytest` run does not necessarily mean the integration tests ran.

Point them at the dedicated test targets, never at the running world:

```bash
export LF_TEST_DATABASE_URL=postgresql://livingfeed:livingfeed@localhost:5433/livingfeed_test
export LF_TEST_REDIS_URL=redis://localhost:6380/15
export LF_TEST_NATS_URL=nats://localhost:4223          # compose nats-test, NOT 4222
```

Create the test database once if you have not:

```bash
docker exec livingfeed-postgres-1 psql -U livingfeed -d postgres -c "CREATE DATABASE livingfeed_test;"
```

Two guards back this up, and both **fail rather than skip** when violated — a silent pass just moves the accident to the next person:

- PostgreSQL: the database name must end in `_test`.
- NATS: the server must carry a test marker stream. A server holding `LF_*` streams without one is treated as the live world and rejected.

These guards exist because the live world was destroyed twice on 2026-07-17. Please do not route around them.

## Changing an event schema

Event contracts in `packages/schemas` are the seam between the Python services and the TypeScript client, and generated code is committed. If you touch a schema:

```bash
uv run --package lf-schemas python packages/schemas/scripts/generate.py
pnpm --filter @livingfeed/schemas generate
```

Commit the regenerated output. CI fails on drift, and separately re-validates archived sample events against your change — events already written to the store must keep parsing.

## Pull requests

- Branch off `main`; keep the change focused.
- Explain **why** in the description, not just what. The commit history here reads as reasoning, and reviews go faster when the PR matches that.
- Include a test when you fix a bug. If a change is hard to test, say so in the PR rather than skipping it silently.
- `ci-ok` is the required check.

## Architecture decisions

Living Feed keeps ADRs, but in a private working repository — they are not part of this repo. The README's [How it's built](README.md#-how-its-built) section is the public summary. If a change would contradict something there, raise it in an issue first so the decision can be recorded properly.
