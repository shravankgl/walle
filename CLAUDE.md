# CLAUDE.md

Working agreement for Claude Code sessions in this repo.

## What this is

`personal-harness` — a **forward deployment engineer for anyone**. Not a chatbot, not a coding
agent. It shadows how someone actually works, scopes one workflow, builds it against their
systems, and keeps it running. Target users are deliberately non-technical: a chef, a parent, a
student, an older adult. Later, teams.

**Current state: Phase 0 implementation, kaizen cadence (D104).** The package is `walle`
(D105). The design documents below are the spec; `PLAN.md` §9 is the task list and says what
today's task is; `DEVLOG.md` is the daily record. One task a day, one verify criterion, shipped
green, explained before it lands.

## Read first

- `ARCHITECTURE.md` — the system design
- `PLAN.md` — the roadmap: phases, metrics, writing track, risks
- `CONVERSATION.md` — decision log with rationale, rejections, and mid-session revisions
- `REFERRALS.md` — external repos reviewed: what was adopted (with D-numbers), what was left, why
- `DEVLOG.md` — one entry per shipped task: what, verify result, next, noticed-but-not-done
- `README.md` — the pitch

`CONVERSATION.md` is the important one. Decisions carry numbers (D1–D119) and revisions carry
R1–R7; **cite them when you build on or challenge a decision** rather than re-deriving it. If
you disagree with a settled decision, say so explicitly and reference its number — do not
quietly design around it.

## Hard constraints

These are settled. Violating one is a bug, not a style choice.

1. **The kernel (L0) is immutable.** Event loop, model router, tool dispatch, permission engine,
   log writer, capability loader. Nothing the agent authors may modify these. If a design needs
   the agent to edit the permission engine, the design is wrong.
2. **Append, never mutate.** Memory, personas, skills, prompts and hooks get new versions
   appended and old ones superseded. No in-place edits, no deletes — use `supersede` tombstones.
   This applies to these documents too: `CONVERSATION.md` is amended by appending, never edited.
3. **Resolution is a pure function of the log.** Picking the live fact or skill version must
   never require a model call. No LLM in the resolver.
4. **Snapshots are events.** An LLM-generated digest is not reproducible by replay, so it must
   be written *into* the log as a fact, never treated as a rebuildable artifact.
5. **JSONL is canonical; SQLite is a rebuildable index.** Truth is always the plain-text log.
   The rebuild test — destroy all derived state, reconstruct from the log — must always pass.
6. **Profiles are physical.** Every subject that holds facts — each person, and the
   organization itself — gets its own folder. `shared/` holds capabilities only: skills, MCP
   configs, hooks. The *how*, never the *what*. Isolation is enforced by the filesystem, not by
   a resolver, and no design may reintroduce a shared data space.
7. **No third-party code.** Locally-authored code is fine. Code, hooks, or MCP servers from
   strangers are not, and the marketplace (when it exists) ships playbooks, not binaries.
8. **No capability without an eval.** A skill that cannot be tested cannot be promoted.
9. **Hard WIP cap on active automations.** Adding one past the cap requires retiring one.

## Conventions

- **Append to `CONVERSATION.md` when a decision is made.** New decisions continue the D-series;
  revisions to earlier decisions continue the R-series and must name what changed and why. Add
  a new dated session heading rather than editing an old one — the document practices what the
  architecture preaches.
- **When a repo is reviewed, add it to `REFERRALS.md`** — what it is, adopted items with their
  D-numbers, rejected items with reasons. Adoption itself still goes through `CONVERSATION.md`;
  the catalog is never the record (D70).
- Keep `ARCHITECTURE.md` and `PLAN.md` current as the synthesized view; keep `CONVERSATION.md` as the append-only
  history. When they disagree, `CONVERSATION.md` is the record and `PLAN.md` is stale.
- Prices, benchmark numbers and model capabilities go stale fast. Anything quantitative in
  these docs was researched on 2026-09-05 — re-verify before relying on it.

## Code conventions (D104, D106)

- **Today's task is the one in `PLAN.md` §9 marked next.** Do that task and nothing else. A
  thing noticed goes in `DEVLOG.md` under *noticed, not done*, never into the diff.
- **Explain before landing.** For every change: what the file is, why it exists, which
  decision it implements, what a reviewer should check. Shravan reads every diff.
- **Green means:** `uv run pytest` passes, `uv run ruff check` and `uv run ruff format --check`
  are clean, `uv run pyright` is clean. A task is not shipped until all four are.
- **Verify criterion is a test.** Each task's criterion in §9 becomes a test that would have
  failed before the task. No task ships on "it works when I run it."
- **Run `scripts/review.sh` before every commit** (D119, R4, R5), after the four checks.
  It is an independent reader — Gemini via `agy`, rules from `open-code-review` — not the
  author. `/delegate-review` remains available when the author-as-reviewer pass is wanted. Findings
  go in the `DEVLOG.md` entry with what was done about each. `scripts/review.sh` is the
  non-interactive path (a configured model, or CI); it exits 2 when the review did *not*
  run — treat that as a failed check, not a pass. In delegation mode the author is the
  reviewer: say so in the entry rather than implying an independent read happened.
- **Author-blind second pass** on any diff over ~30 lines or touching IO, concurrency, or an
  error path: re-read as a stranger, hunt ignored errors, lock scope, leaks, and status
  messages that lie. Say what was found.
- **Two-runner walkthrough** for anything touching the log, a lock, or a derived file: what do
  writers A and B each see at each step? Write the answer in the DEVLOG entry.
- **Layout:** `src/walle/` package, `tests/` mirroring it, `tests/fixtures/` for golden logs
  (D88, D96). Pydantic models with `extra="forbid"` and a permanent upcast chain. No model
  call in anything the resolver imports (constraint 3).
- **Commits:** one per shipped task, message `<area>: <what> (D-numbers)`. Personal email,
  never the employer's (D40). Commit only after Shravan has seen the explanation.
- **Libraries, not frameworks** (D87): Pydantic, `click`, FastMCP, aisuite. Nothing that owns
  the loop.

## Agent build period (R6, until 2026-11-27)

Agents build `PLAN.md` §9 tasks from GitHub issues and auto-merge when `check`, `gate` and
`agent-review` pass. For that work, `.github/agent-brief.md` replaces "explain before landing",
"commit only after Shravan has seen the explanation", "Shravan reads every diff" and the
pre-merge `scripts/review.sh`. Shravan reviews afterwards from `devlog/task-NN.md` and the
`to-learn` label. The hard constraints above still apply.

## Working style

- This is a design collaboration between engineers. Be opinionated, give a recommendation
  rather than a survey of options, and push back when something is wrong.
- Name failure modes concretely. "This degrades" is useless; "self-modification without an eval
  gate is a random walk that degrades within weeks" is not.
- Do not re-litigate settled decisions or restate the premise back before answering.
- When a claim depends on a number (pricing, benchmark, context limits), look it up rather than
  recalling it.

## Open questions

None outstanding on architecture. Everything is settled: Observer scope (D11–D15), local model
and routing (D16–D18), the evolution loop (D19–D25), profiles and the multi-principal model
(D26–D33), retrieval (D34–D35), the employer boundary (D36).

**Parked, deliberately:** employer visibility into organization profiles. Workplace monitoring
carries jurisdiction-dependent legal exposure, so it is not solved here — an enterprise fork
takes it. The fork is additive and needs no schema change.

**Therefore: Phases 0-3 target households and individuals only.** The shop scenario is employee
monitoring regardless of company size and belongs to the fork. Do not build it into the
consumer path.
