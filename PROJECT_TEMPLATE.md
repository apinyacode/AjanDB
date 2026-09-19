# PROJECT_TEMPLATE

Copy this into a new repo's first commit. Fill in the two skeletons below,
delete the instructions in *(italics)*, and you're set up.

---

## 1. README.md skeleton

*The pitch, for a stranger. What it is and how to run it — nothing else.
Never put changelog history, test counts, or "shipped in build order" here;
that belongs in the context doc.*

```markdown
# <Project Name>

<One or two sentences: what this is and who/what it's for.>

## What it does

<3-6 bullets. Features, not implementation details.>

## Setup

<Exact commands. Copy-pasteable, in order.>

## Run locally

<Exact command(s) to start it.>

## Tests

<Exact command(s) to run the test suite.>
```

---

## 2. Context doc skeleton

*The living reference — this is what you hand a fresh Claude Code session
so it doesn't have to re-derive decisions from scratch. Update it in the
same session the change happens, not later. If "Not yet done" isn't
genuinely current, it's actively misleading — better empty than stale.*

```markdown
# <Project Name> — Project Context & Architecture

<Date> · @<owner>

## Overview

<What it is, who it's for, the two or three sentence version. Which parts
of the repo exist and what each one is responsible for.>

## Architecture

<Per major component/directory: what it does, key files, how data flows
through it. Enough that a fresh session doesn't have to read every file to
understand the shape of the thing.>

## Key features

<For each feature: what it does and, where it's non-obvious, *why* it's
built that way — especially anything that looks like it could be done more
simply but isn't (there's usually a reason).>

## Conventions and patterns

<Test isolation approach, commit message convention, any repeated
verification workflow, design philosophy — anything a new session should
follow without being told each time.>

## Current state

<Test count and what's covered, at a glance. What's shipped, in build
order if that's useful context.>

**Known limitations:**
<Real ones. Things that are true trade-offs, not TODOs in disguise.>

**Not yet done / natural next steps:**
<Either genuinely current, or explicitly "none pending — last request was
X, shipped and tested.">

## Repository and links

<GitHub link + branch/latest commit, local checkout path, setup command
recap, how to run tests, how to run locally.>
```

---

## 3. New-project checklist (5 minutes, once)

- [ ] Repo created + `.gitignore` in place
- [ ] README.md written from the skeleton above (what/why/run — not history)
- [ ] Context doc started from the skeleton above, *on day one*, not
      retrofitted later once things get complicated
- [ ] Test runner wired before the second feature gets built, not the tenth
- [ ] Commit message convention decided before the first real commit, e.g.:
  ```
  <what changed, imperative mood>

  <why, if not obvious from the diff itself>

  Co-Authored-By: Claude <noreply@anthropic.com>
  Claude-Session: <link>
  ```

## 4. Division of labor (Claude Code vs. chat)

- **Claude Code**: anything touching the repo — implementation, tests,
  commits, live verification against a running instance.
- **Chat (here)**: planning a feature before committing Claude Code to
  building it, reviewing a diff or decision, generating docs/decks for
  people who aren't you, quick lookups against the live repo.
- Bring output back the other way deliberately: paste a Claude Code
  session's summary here to update the context doc; paste a plan drafted
  here into a fresh Claude Code session as the spec.
