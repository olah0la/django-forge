# 1. Use uv for dependency management

- **Status:** Accepted
- **Date:** 2026-08-24
- **Relates to:** M1-02

## Context

Every project forged from django-forge inherits this decision, and reversing it later means
regenerating lock files and rewriting the Docker build, the Makefile, and the contribution guide.
It is therefore worth deciding deliberately rather than by habit.

The requirements are:

1. **Reproducible installs.** A lock file recording the exact resolved version of every package,
   including transitive dependencies, so an install today and an install in six months are identical.
2. **Separate runtime and development dependency groups**, so production images do not ship linters
   and test frameworks — wasted image size and unnecessary attack surface.
3. **Fast, cache-friendly installs in Docker.** Dependency installation is the slowest layer of the
   image build (M2-01), and it is re-run constantly during development.
4. **Portable metadata.** The project should not become unusable if the tool is abandoned.

Three managers were considered. uv, Poetry, and pipenv were all already installed on the machine
used for this work, so availability was not a differentiator.

## Options considered

### uv

Standards-native: PEP 621 `[project]` metadata and PEP 735 `[dependency-groups]`, which are the
interoperable Python packaging standards rather than tool-specific tables. Resolution and install
are roughly an order of magnitude faster than the alternatives, which matters directly in image
builds. `uv sync --frozen --no-dev` produces exactly the production install requirement 2 asks for.
Lock file is `uv.lock`.

Against it: it is the youngest of the three, so fewer engineers have used it, and the ecosystem
around it is still settling.

### Poetry

The most widely known Python dependency manager, so a new hire is most likely to have used it
already — a real advantage for onboarding. Mature and stable, with a large body of documentation and
community answers.

Against it: noticeably slower resolution and installation, which compounds in Docker builds.
Historically its metadata was tool-specific (`[tool.poetry]`) rather than standard, though 2.x has
moved toward PEP 621.

### pip-tools

The most conservative option. Compiles `requirements.txt` files with hashes from `.in` files; the
output is plain pip-installable, so a production image needs no tooling beyond pip itself. That
portability is genuinely attractive for a template.

Against it: a separate `.in` file per dependency group, a more manual workflow, and no integrated
environment management — meaning more for a newcomer to learn and more places for the documented
process to drift from what people actually do.

## Decision

**Use uv.**

The deciding factors were requirement 3 and requirement 4. Install speed is not a cosmetic
preference here: dependency installation is the layer that dominates image rebuild time, and a fast
resolver changes the development loop from minutes to seconds. Meanwhile uv writes standard PEP 621
and PEP 735 metadata, so `pyproject.toml` stays readable and reusable by other tools — which
substantially limits the cost of being wrong about this decision.

Poetry's familiarity advantage is real but temporary; it is paid down once per engineer, whereas the
speed cost is paid on every build.

## Consequences

**Positive**

- Dependency installs are fast enough not to distort the Docker build (M2-01).
- `pyproject.toml` uses interoperable standards, so it remains meaningful to other tooling.
- `uv sync --frozen --no-dev` gives a clean production install with no extra machinery.
- `uv.lock` pins exact versions *and* hashes, which also protects against tampered artifacts.

**Negative**

- uv must be installed before contributing — one more prerequisite, and one that some engineers will
  be meeting for the first time. `CONTRIBUTING.md` (M1-04) must cover installing it.
- Fewer engineers have prior experience with uv than with Poetry, so expect more questions early on.
- `uv.lock` is uv-specific. Migrating away means regenerating the lock from `pyproject.toml`; the
  dependency declarations survive, the lock does not.
- uv is under active development and moves quickly, so its own version should be pinned in the
  Docker image (M2-01) to keep builds reproducible.

**Neutral**

- `.python-version` pins the interpreter for local development; the container's Python is pinned
  separately by the base image in M2-01. These two must be kept in agreement.
