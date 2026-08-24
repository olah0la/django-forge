# Contributing to django-forge

This guide covers how work is set up, branched, committed, and reviewed here. It is deliberately
short — read it once and refer back as needed.

If anything below is unclear or turns out to be wrong, say so. A guide nobody trusts is worse than
no guide.

---

## Before you start

| Tool | Why | Install |
| --- | --- | --- |
| **git** | Version control | Preinstalled on most systems |
| **uv** | Dependency management | https://docs.astral.sh/uv/getting-started/installation/ |
| **make** | Task runner | Preinstalled on macOS and Linux |

You do **not** need a local Python installation — `uv` reads `.python-version` and provisions
Python 3.12 itself.

Docker and Docker Compose **are** prerequisites now: the container stack landed with M2. `make up`
starts the development stack and `make up-prod` the production-like one; `make down` stops both.
Everything except `make install` and `make check` runs inside containers.

## Local setup

```bash
git clone https://github.com/olah0la/django-forge.git
cd django-forge
make install        # create .venv and install runtime + dev dependencies
make check          # lint, type-check, run tests
```

That is the whole setup. `make help` lists every available task.

> **What you cannot do yet.** The Docker stack runs, but there is no application in it: the Django
> project arrives in **M3**, so both services start and then hold themselves open with a message
> naming the issue that replaces them. Targets tagged `[M3]` in `make help` are defined but will
> tell you which file is missing and which issue delivers it. That is expected, not a broken setup.

## Hot-reload development loop

The development stack mounts your working copy into the container, so **an edit on your machine is
live inside the container immediately** — no rebuild, no restart.

```bash
make up          # start the development stack
# edit any file in your editor
# the change is already in the container
```

**What is mounted.** The whole project directory, onto `/app`. That includes files you create after
the container started.

**When you still need a rebuild.** Dependency changes. `uv.lock` is installed into the image at build
time, not read from the mount, so after editing dependencies:

```bash
make lock        # re-resolve into uv.lock
make build       # reinstall them in the image
make up
```

**The production-like stack is deliberately not mounted.** `make up-prod` runs the image exactly as
built, which is what makes it a faithful stand-in for a deployment. If you change a file and it does
not take effect there, that is correct behaviour, not a bug.

### Two things not to "fix"

**The virtualenv lives at `/opt/venv`, not `/app/.venv`.** It has to sit outside the mounted path:
the mount covers all of `/app`, and a venv underneath it would be hidden the moment the stack
starts, so every import would fail with a confusing "module not found". If you move it back, the
development stack breaks.

**The development image is built with your uid and gid.** The `Makefile` exports them from `id -u`
and `id -g`, and Compose passes them as build arguments. A bind mount preserves *numeric* ownership,
so without this the container writes files onto your machine owned by a user that does not exist,
and files you own can be unwritable inside the container. This bites on Linux; macOS hides it behind
its filesystem translation layer, so test on Linux before assuming it is fine.

## Picking something to work on

1. **Start with a [`good first issue`](https://github.com/olah0la/django-forge/issues?q=is%3Aissue+is%3Aopen+label%3A%22good+first+issue%22).**
   These are self-contained and chosen so you can finish one without holding the whole system in
   your head.
2. **Check the *Depends on* field.** If an issue it depends on is still open, pick something else.
   The roadmap is ordered deliberately, and starting downstream work early usually means redoing it.
3. **Read the acceptance criteria before you write anything.** They are written to be objectively
   checkable, and they are what "done" means.

> **An issue you cannot understand is a defect in the issue.** Every issue has a *Context* section
> explaining why the work exists. If it does not make the purpose clear to you, comment and say so —
> it will be rewritten. Issues that need tribal knowledge are why onboarding is slow.

## Branching

**Default: one branch per milestone.** Work on the milestone branch and make one commit per issue.

```
m1-foundations
m2/containerization
```

**Give an issue its own branch if any of these is true:**

- Its estimate is `L` or `XL`
- It changes migrations, security-relevant configuration, or dependency pins
- It modifies a file every downstream project inherits (`Dockerfile`, settings, `pyproject.toml`)
- It might need to be reverted independently of the rest of the milestone

```
m4/03-migration-review-checklist
```

If none of those apply, commit to the milestone branch. You do not need to make a judgement call
beyond that list.

## Commit messages

Start with the issue number, then an imperative summary of what the commit does.

```
#12 Add multi-stage Dockerfile with non-root runtime user
#13 Run the application as a dedicated non-root user
```

- **Imperative mood** — "Add", not "Added" or "Adds". It completes the sentence *"This commit
  will…"*.
- **One issue per commit** where practical. It keeps history readable and makes a revert surgical.
- **Explain *why* in the body** when the reason is not obvious from the diff. The diff already
  shows what changed; it cannot show what you ruled out.

```
#14 Pin base image to a specific digest

Tracking a floating tag meant two developers could build different images from
the same commit, which made a caching bug impossible to reproduce.
```

## Pull requests

Open a PR against the milestone branch (or `main` for a milestone branch itself). In the
description:

- **Reference the issue** it closes.
- **Say how you verified it.** Not "tested locally" — the actual command and what you observed.
- **Call out anything you were unsure about.** That is where review is most valuable.

### What reviewers check

1. **Every acceptance criterion is met**, and was *demonstrated* rather than assumed.
2. **The Definition of Done below is satisfied.**
3. **The change matches the issue's scope** — unrelated improvements belong in their own issue.
4. **A newcomer could understand it.** Comments explain *why*, not *what*.
5. **Nothing secret is committed.** No credentials, tokens, or real `.env` values.

Reviewers ask questions to understand, not to challenge. If a comment reads as blunt, assume it is
brevity rather than criticism — and if a review comment is unclear, ask.

## Definition of Done

Every task inherits this checklist. Confirm each item before asking for review.

- [ ] All acceptance criteria are met and were **demonstrated, not assumed**
- [ ] The change is documented where a future reader will look for it
- [ ] `.env.example` and `README.md` are updated if behaviour or setup changed
- [ ] The full stack starts cleanly from a fresh clone after the change
- [ ] The change has been reviewed and approved by at least one other engineer

## Asking questions

A question asked early costs minutes. A wrong assumption discovered in review costs a day. If you
are unsure what an issue means, which approach is wanted, or why something is the way it is, ask
before building — that is a normal part of the work, not a sign you are behind.

---

## This guide grows

Sections are added as the milestones that need them land. Do not write these early; there is no
implementation to describe yet.

| Section to come | Added by |
| --- | --- |
| Hot-reload development workflow | **M2-07** |
| Migration review checklist | **M4-03** |
| API schema and review expectations | **M5-03** |
| How to add an ADR | **M7-03** |
