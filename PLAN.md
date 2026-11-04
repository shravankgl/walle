# Plan

**Status:** Phase 0 implementation; tasks 1–3 shipped, agents building the rest until 2026-11-27 (R6). Last updated 2026-11-04.
**Resourcing:** agents on Hyperagent credit to 2026-11-27, then solo review (R6).
**Objective:** a working system *and* a public writing track — the second is not a by-product
of the first.

System design lives in [ARCHITECTURE.md](ARCHITECTURE.md). Rationale and rejected alternatives
live in [CONVERSATION.md](CONVERSATION.md) as numbered decisions. Repos reviewed for what can
be adopted are cataloged in [REFERRALS.md](REFERRALS.md).

---

## 1. What success means

An FDE is measured by workflows shipped and *still in use* — not satisfaction, not tokens.

| Metric | Definition | Target |
|---|---|---|
| **TTFW** | Time to first shipped workflow | < 1 session |
| **30-day survival** | Shipped automations still running at day 30 | > 60% |
| **Proposal acceptance** | Observer proposals the user accepts | measures Observer quality |
| **Unprompted-value ratio** | Share of value delivered without being asked | a chatbot scores 0 |
| **Active WIP** | Live automations | hard cap, start at 7 |

The unprompted-value ratio is the one that says whether an FDE was built or just a wrapper.

## 2. Phase 0 — the memory layer (12–17 weeks of build; see §8)

Ship a memory layer that plugs into a harness people already run, and prove it proposes
something worth remembering that the user would not have flagged themselves.

**Build:**

| | |
|---|---|
| Log | JSONL append-only, one stream per profile, with a pure-function resolver |
| Index | SQLite + the rebuild test wired into CI from day one, replaying a golden log per shipped schema version (D88) |
| Observation | **The harness transcript itself.** Denser than email or calendar, no OAuth, no connector work, and exactly where the pain is — the harness forgets between sessions. Ordered by the `parentUuid` DAG, never by timestamp; carries the entry's `version`; never keys on the per-turn session id (D101). |
| Surface | Hot tier delivered by the host hook, and published as an MCP Resource (D54, D83); MCP server (`recall`, `remember`, `why`) with standard annotations and labeled results (D84) + CLI (`review`, `context`, `forget`, `rebuild`) |
| Human gate | Observations append freely; assertions need confirmation, reviewed in batches |
| Proposer | Weekly or at session end: read the log, propose what is worth remembering. Silence is a result — a run with nothing to say skips the report (D63) |
| Boundary eval | Three corpora — benign, dangerous, injection — with false-allows as the metric and a ship gate; dated per-model reports in the repo (D77); a fourth, LongMemEval + LoCoMo, for what memory does (D98). The proposer's ranking runs in shadow against the review batch from day one (D82). The MCP server ships an `srt` profile and is tested under it (D99) |

**Deliberately deferred:** email and calendar connectors, Builder, Curator, evals, voice,
retrieval, trust-ladder machinery, and **the local model** — at n=1 the cost case for it does
not apply yet.

**Kept despite the cut:** multi-principal schema fields and a per-`(profile, device)` `seq`
(both free now, unretrofittable later — D57) and the rebuild test (it catches the mistake you
cannot see).

**Task format.** Every Phase 0 task carries a one-line binary verify criterion — it passes or
it does not. The ship note follows a fixed contract: Scope Delivered / Architecture Changes /
Test Evidence / Known Gaps / Rollback / Demo Steps. The README states a *measured*
resident-context number and an honest Working / Untested table (D69).

**Gate: the system proposed something worth remembering that I had not flagged myself, I
accepted it, and it was still earning its place in my context at day 30.**

Honest narrowing: *propose what to remember* is a weaker form of initiative than *propose a
workflow to automate*. That is accepted deliberately — the narrower claim is shippable by week
12 and genuinely testable, and workflow proposal returns in Phase 2 once connectors exist.

**Why this ordering.** The Phase 0 work is nearly identical either way — the log, the resolver,
the hot-tier builder. What changes is that the output is installable by strangers in week 12
rather than by nobody until year two.

**Shipped as an open-source repo, not an installable app.** The users of a memory layer are by
definition people who already run a harness, which is essentially all developers; a consumer
app has no audience until voice makes non-technical users reachable (Phase 4). Open source is
also cheaper by weeks at this resourcing, converts article readers into users on the spot, and
*is* the ownership argument rather than merely claiming it.

Distribution is not "publish to GitHub". It is: one-line install (`uvx`/`pipx`, no
clone-and-build), a README that shows the thing in fifteen seconds with a recording, a
copy-pasteable MCP config for Claude Code, and the article and repo shipping together.

**Guard the hours.** Open source on evenings and weekends can be swallowed by issues and PRs.
State support expectations in the README and decline PRs early — adoption is the goal, a
maintenance obligation is not.

## 3. Later phases

| Phase | Build | Gate |
|---|---|---|
| **1** | Email + calendar connectors with cursors as a fold over the log, no side store (D92); local model via `ollama:` (D87); ACP-client delivery for a second host (D83); Curator in propose-only mode; entity index only if D34's instrumentation shows entity-shaped misses (D59) | Proposal acceptance rate is respectable |
| **2** | Builder + eval harness; workflow proposal, not just memory proposal; missions as DAGs with per-step manifests (D67); Builder output in a container executor (D68); the inbox for unattended asks (D78); rung 3½ reviewer with its eval corpora (D76, D77); adopt Opik | Rebuild test green; no eval regressions |
| **3** | Operator; rot detection and retirement; household profiles and digests; multi-principal conversation rules and the rung-3/4 protocols (D61, D62, D73, D74) | 30-day survival > 60% |
| **4** | Voice / always-on | Latency < 800ms to first audio |
| **5** | Enterprise fork — audit, retention, consent, org visibility | Legal review first, not last |
| **X** | App marketplace | Deferred indefinitely. Revisit only on evidence. |

**Scope boundary:** Phases 0-4 are households and individuals. Workplace cases belong to Phase
5's fork.

## 4. The writing track

Articles are deliverables. With solo evenings-and-weekends resourcing, writing and building
compete for the same hours unless they are the same activity — so each phase ends in a piece,
and the decision log is the raw material rather than something written up afterwards.

| Ships | Piece |
|---|---|
| **Now**, before any code | The thesis — why every harness is built for programrs, and what an FDE-shaped one looks like instead |
| Phase 0 | Append-only memory for agents: the architecture, its three hard parts, and a thing you can install |
| Phase 0 gate | 30 days with an agent that remembers — including what it chose to remember that I would not have |
| Phase 2 | What happened when it started writing its own capabilities |

Article one exists in substance already. `CONVERSATION.md` is a real decision log with rejected
alternatives and documented reversals — rarer and more credible than a demo.

**Framing:** lead with the checkable claim. "I built an agent harness with append-only memory
and ran it on myself for 30 days" beats any claim about where this eventually goes.

## 5. Risks

| Risk | Mitigation |
|---|---|
| Frontier vendors ship native memory and apps, erasing the differentiator | Be the substrate, not the chat loop. Stay harness-agnostic; every model is a routable backend. |
| Self-modification degrades quality invisibly | Evals gate promotion; regression floor, not delta; two-stage gate |
| Agent ships 40 half-working automations and the user drowns | Hard WIP cap; Operator retirement is not optional polish; deterministic prune always on, model pass opt-in, `pinned` never auto-retired (D114) |
| Dead automations erode trust silently | Rot detection in Phase 3 |
| Onboarding non-technical users through OAuth | Caregiver/admin role as a first-class primitive; computer use as fallback |
| Consumer unit economics | Local model for the always-on tier; cache-aligned prefix |
| Workplace monitoring liability | Deferred to the enterprise fork; Phases 0-4 ship households and individuals only |
| Cross-principal confidentiality leak | Profile isolation is physical; contradictions surfaced privately only |
| Employer IP claims | Do not build on employer data or write publicly about internal process |
| Approval fatigue erodes the gate — openworker measured ~15 hand-approvals per run before adding relief | Request/session/always grant scopes (D73); split-and-justify matching (D74); rung 3½ reviewer under fail-closed invariants (D76) |
| Writing track slips behind the build | Each phase gate includes its article; the decision log is the draft |
| Open-source maintenance consumes all available hours | README states support expectations plainly; PRs declined early |
| Recalled memory used as an injection vector into a host harness | Memory is always rendered as data, never instruction — the untrusted envelope with a random per-result id (D55). A memory layer cannot taint or block the host's tool calls; that limit is stated in the README |

## 6. Positioning

**Public framing is memory and ownership**, never the FDE metaphor — that stays as the internal
design spec. Lead with the universally-felt problem: *every AI conversation starts from zero.*
One idea, not twelve: **your AI should never forget, and the memory should be a file you own.**

The FDE framing also has a real scope limit worth knowing: it holds for workflow-rich users
with systems to integrate, and collapses for companionship personas where there is nothing to
build.

**Not a startup.** Commercial viability, market sizing and funding are explicitly out of scope
and must not reshape the plan. The objective is a system actually used plus a public writing
track (§4). Success is measured by §1's metrics and by pieces published — never by market
potential. If a commercial thread ever matters it will arrive through the writing, and it can
be evaluated then.

## 7. Open questions

**Substrate or full harness as the first shippable thing?** Nobody installs a new harness; many
people would try a memory layer for the one they already run. Same architecture, same Phase 0
work — the difference is whether the output is usable by strangers in week 12 or by nobody
until year two. Unresolved.

None outstanding on architecture.

**Parked with a named resolution:** employer visibility into organization profiles is a
workplace-monitoring question with jurisdiction-dependent legal exposure. It is not solved in
the consumer product — an **enterprise fork** takes it. The fork is additive (audit, retention
policy, admin visibility, consent flows) and requires no schema change, which is what makes
parking it safe.

## 8. Estimate

**Cadence:** 1–2 hours a day, ~10 h/week nominal (D71). Bottom-up from §2's scope. Ranges are
honest, not padded. At 7 h/week multiply the weeks by ~1.4; at 14 h/week by ~0.7.

### Phase 0 build

| Piece | Hours | Note |
|---|---|---|
| Log + resolver (append, `supersede`, `stream:seq` refs, fold) | 10–15 | Well-specified |
| SQLite index, FTS5, edges table, rebuild test in CI | 10–15 | |
| Transcript observer (`~/.claude/projects/*.jsonl` → events) | 8–12 | **Risk:** undocumented format that changes under you |
| Hot-tier builder + `SessionStart` hook delivery (D54, D64) | 10–14 | |
| MCP server: `recall`, `remember`, `why` + envelope (D55) | 10–15 | |
| CLI: `review`, `context`, `forget`, `rebuild` | 10–15 | `review` is the batch UX; worth doing well |
| Proposer + staging queue (D44, D60) | 12–20 | **The unknown.** Prompt iteration until proposals are worth accepting |
| Packaging: `uvx`, README, recording, MCP config snippet | 8–12 | |
| Tests, verify criteria, bug-fixing | 15–25 | |
| Boundary eval corpora + reports (D77, D98) | 10–16 | Injection corpus is the one that matters; LongMemEval needs adapting to a layer, not a chat system |
| The Phase 0 article (§4) | 10–15 | |
| **Total** | **~115–176** | |

### Milestones

| Milestone | What exists | Hours | Calendar @ 10 h/wk |
|---|---|---|---|
| **M1 — usable by me** | Log, index, transcript observer, hot tier in every session, `remember` + `review`. Remembers across sessions; what it carries is visible and editable. No proposer, no packaging. | 45–60 | **5–6 weeks** |
| **M2 — Phase 0 shippable** | All of §2; installable by a stranger; article drafted | 115–176 | **12–18 weeks** |
| **Gate passed** | M2 + the 30-day survival window | — | **~4.5–6 months** from start |

The 30-day window starts at M1, not M2 — it is calendar-bound and does not shrink with hours.

### Full project (Phases 0–4)

| Phase | Hours | Weeks @ 10 h/wk | Least certain |
|---|---|---|---|
| 0 | 115–176 | 12–18 | Proposer quality |
| 1 | 100–160 | 10–16 | Stable pseudonym mapping (D18) |
| 2 | 200–300 | 20–30 | Self-modification under an eval gate — hardest, least estimable |
| 3 | 120–200 | 12–20 | |
| 4 | 120–200 | 12–20 | Latency is iterative; app packaging is weeks of non-product work |
| **Total** | **~662–1014** | **66–102** | |

With four articles and the 30-day gates partly overlapping the next phase: **about 2 to 2½
years** of sustained cadence; nearer 3 with the weeks that go to zero. Phases 5 and X are out
of scope for this estimate.

**Batch the hours.** Two 3½-hour blocks beat seven 1-hour sessions by roughly 30–40% at the
same weekly total — every session pays a 15–20 minute reload, and the proposer and eval work
need uninterrupted stretches. If 1 h/day is the only shape available, every milestone above
slides by about a third.

Revised at each phase gate against hours actually logged.

## 9. Phase 0 task list

One task a day (D104), or as many as agents finish during the credit period (R6). Each has a
binary verify criterion that becomes a test (D69). `→` marks the next task, and from task 4 the
GitHub issue carries the live status. Done tasks get their date. Tasks that spill are split, never left half-done. Estimates
are the §8 table's, cut into days; the honest number for each is 1–2 hours of focused work.

### Week 1 — the log

| # | Task | Verify | Decisions | Status |
|---|---|---|---|---|
| 1 | Repo scaffold: `uv`, `pyproject` (`walle`, MIT, py ≥ 3.12, `src/` layout), `ruff`, `pyright`, `pytest`, `pre-commit`, GitHub Actions, `DEVLOG.md` | `uv run pytest` green on one trivial test; `ruff`, `pyright` clean; CI green on the private remote | D104–D106 | 2026-09-19 |
| 2 | Event schema: base event (`v`, `ts`, `actor`, `caused_by` as `d-<dev>:<seq>` refs, type discriminator), `observation` (`observed_at`), `assertion` (`level`, `confidence`, `evidence_count`, `summary`, `valid_from`/`valid_to`, `about`), `supersede` (`target`, `valid_to`), `snapshot` (`forgets`), `contradiction`; `extra="forbid"` + upcast-chain scaffold; canonical JSON + hash | round-trip test per type; hash stable across key order; unknown field rejected; `v` present on every event | §5.1–5.2, D57, D58, D66, D85, D89, D93, D94, D96 | 2026-10-10 |
| 3 | Stream writer: append to `principals/p-<name>/log/d-<device>.jsonl`, fsync, per-stream `seq`, path-segment validation, cross-process lock | two concurrent writer processes → contiguous seqs, no torn line; a traversal id is rejected | D10, D57, D80, D85 | 2026-10-10 |
| 4 | Stream reader: iterate in seq order, tolerate a torn tail and report it, parse refs | fixture with a truncated last line loads N−1 events and reports exactly one skip | D80 | → |
| 5 | Resolver fold: current assertions honoring `supersede`; validity windows; overlap-only contradiction rule; no IO, no model | pure-function tests: supersede hides; non-overlapping windows never conflict; overlapping later-valid closes earlier | D8, D93 | |
| 6 | SQLite index: `events`, `edges`, FTS5; tracking record per stream; `walle rebuild` | destroy the index → rebuild → resolved-state hash equal (D66); incremental rebuild picks up only new seqs; **an event whose `v` exceeds this build halts the fold by name; a dangling `caused_by` ref fails the rebuild** | D10, D59, D66, D96, D111 | |
| 7 | Hot-tier renderer: identity / user / rules / learned, self-describing headers, index mode past threshold, `[#ref] summary` lines; rendered `0444`, hash recorded | over-threshold fixture flips to index mode; under-threshold renders in full; `rules` untouched by the renderer; **a render over an out-of-band edit preserves the edit and reports it instead of overwriting** | D50, D64, D81, D107 | |

### Week 2 — in the session (M1)

| # | Task | Verify | Decisions | Status |
|---|---|---|---|---|
| 8 | `SessionStart` hook (Python now; the first Rust port candidate after M1 — D118): read the pre-rendered hot tier, emit `hookSpecificOutput.additionalContext`, under a 2 s budget; install snippet for `~/.claude/settings.json` | fake hook payload on stdin → JSON with the hot tier; measured token count printed; **a render made slow emits nothing and the session still starts** | D54, D83, D90, D102, D115 | |
| 9 | `walle remember "…"` → staged proposal with `summary`; `walle review` keep / edit / drop → `assertion` events | staged item absent from the hot tier until approved; drop leaves a tombstone, not a deletion | D49, D60, D81 | |
| 10 | Transcript observer, part 1: parse `~/.claude/projects/*/*.jsonl` into the `parentUuid` DAG; walk it | fixture transcript with a resume and a sidechain → entries in DAG order, not timestamp order | D51, D101 | |
| 11 | Transcript observer, part 2: emit `observation` events for completed turns only, with `observed_at` and `version`; never key on session id | N completed turns → N observations; token deltas produce nothing | D56, D93, D101 | |
| 12 | `walle forget <ref>` (supersede with `valid_to`); `walle why <ref>` (recursive CTE over `edges`); `walle context` — edit the live view, diff, append the events it implies | `why` returns the full `caused_by` chain; `forget` hides the fact from the render and `why` still explains it; an edit through `context` appends events and survives a rebuild | D57, D59, D93, D103, D107 | |

**M1 gate:** `walle` is installed in Shravan's own Claude Code; the hot tier appears in every
session; a fact remembered on day N is there on day N+1; the 30-day survival clock starts.

### Weeks 3–6 — shippable (M2)

| # | Task | Verify | Decisions |
|---|---|---|---|
| 13–14 | MCP server: `recall` / `remember` / `why`, annotations, `_meta.ifc` labels computed from the provenance closure, untrusted envelope with per-result id | envelope round-trip test; forged marker defaced; schema total < 400 tokens, measured | D48, D55, D84, D97, D102 |
| 15 | `srt` profile for the server; run the test suite under it | server passes tests sandboxed: profile folder read, log write only, no network | D52, D99 |
| 16–18 | Proposer: session-end / weekly, read the log, propose assertions into staging; shadow score vs review decisions from day one | fixture log → ≥ 1 proposal; shadow verdicts recorded, never acting | D44, D60, D63, D82 |
| 19–21 | Eval corpora: benign, dangerous, injection; LongMemEval + LoCoMo adapted to `recall`; dated per-model reports | ship gate: zero false-allows on dangerous and injection; LongMemEval numbers in the README | D77, D98 |
| 22–23 | Consolidation: `snapshot` with `forgets`; gate on idle / new / hours | snapshot event folds identically to the events it replaces | D89, D95 |
| 24 | Golden logs: `tests/fixtures/v1/` with `__expected__` sentinels; compat check in CI | rebuild over every fixture equals its hash | D88, D96 |
| 25–26 | Packaging: `uvx walle`, README with the measured table (resident tokens, schema tokens, persist / replay / recover), status table, support expectations | fresh machine: one command to install, one config line to run | D53, D69, D102 |
| 27–30 | The Phase 0 article, from `DEVLOG.md` and this log | published alongside the repo going public | D37, D46 |
