# Architecture

A forward deployment engineer for anyone. Phase 0 in progress — see [PLAN.md](PLAN.md) §9.

Rationale, rejected alternatives and revisions live in [CONVERSATION.md](CONVERSATION.md) as
numbered decisions (D1–D119, R1–R7). Repos reviewed for adoption are cataloged in
[REFERRALS.md](REFERRALS.md). This document is the synthesized current view; where the two
disagree, the decision log is the record.

---

## 1. Thesis

Most people do not need a coding agent. They need what a good **forward deployment engineer**
does: show up, watch how you actually work, pick the one thing worth automating, build it
against *your* systems, sit with you while it breaks, fix it, and earn the right to do the next
thing.

The differentiating behavior is **initiative**. A chatbot waits to be asked. An FDE says *"you
have done this every Monday for a month — want me to take it?"* Everything below follows from
that one requirement, because proposing means observing, and observing means a loop that runs
independently of the conversation.

The same system serves a chef, a parent, a student and an older adult, because the *role* is
identical. Only the shipped workflows differ.

## 2. Principles

| # | Principle | Consequence |
|---|---|---|
| 1 | The kernel is immutable | Self-modification has a floor it cannot dig through |
| 2 | Append, never mutate | Corruption is structurally impossible; rollback is free |
| 3 | Resolution is a pure function of the log | Replayable, debuggable, no model call in the resolver |
| 4 | Stable prefix, volatile suffix | Prefix caching is 10× cheaper, and appends preserve it |
| 5 | Profiles are physical | Isolation enforced by the filesystem, not by policy |
| 6 | Local is the system of record | Portability and audit are the moat, not privacy theater |
| 7 | Restraint is enforced | Hard cap on active automations |
| 8 | Trust is earned per domain | Explicit, visible, mechanical ladder |
| 9 | No third-party code | Locally-authored code is fine; strangers' code is not |
| 10 | No capability without an eval | Untested self-modification is a random walk downhill |

## 3. Layers

```
L4  Org           audit, retention, consent, org visibility      (enterprise fork)
L3  Apps          capabilities + state schema + persona + jobs
L2  Capabilities  skills, MCP servers, hooks, subagents — loaded on demand
L1  Substrate     the event log + derived views                  (agent-writable)
L0  Kernel        event loop, model router, tool dispatch, permission engine,
                  log writer, capability loader        ← IMMUTABLE, human-written
```

**The L0/L1 boundary is the most important line in the system.** If the Builder can edit the
permission engine or the hook allowlist, there is no safety property left — only a system one
clever turn away from unbounded. The kernel lives in a separate signed repo and the loader
refuses to run a modified one — a SHA-256 over its files in sorted order, relative paths
included so a rename is caught (D65).

It is also the port seam: Python now, Rust for L0 later. The kernel is small and stable by
design, so porting *it* is cheap while capabilities stay scripted.

### 3.1 Delivery surface

**The first shippable thing is a memory layer for harnesses people already run**, not a harness.
Nobody installs a new harness; many people would try a memory layer for the one they have.

| Interface | Purpose |
|---|---|
| Host hook | Deliver the hot tier on `session_start` — the vocabulary Claude Code and OpenHands share (`session_start` / `session_end` / `user_prompt_submit` / `pre_tool_use` / `post_tool_use` / `stop`), so one config covers both (D54, D90). Never a tool the model must remember to call. Phase 0 |
| MCP Resource `memory://hot-tier` | The same hot tier, published with `resources/updated` notifications, for hosts that auto-load resources (D83) |
| ACP client injection | For any Agent-Client-Protocol host — Claude Code, Codex, Gemini CLI — the client prepends the hot tier (D83). Phase 1 |
| MCP `recall` | Retrieve cold-tier memory on demand. `readOnlyHint`, results labeled `_meta.ifc: untrusted / private` (D84). The three tools' schemas together stay under ~400 tokens, measured and reported (D102) |
| MCP `remember` | *Propose* something for durable memory — never writes directly |
| MCP `why` | Provenance: walk `caused_by` back to the events behind a belief |
| CLI `review` | Approve, edit or drop pending assertions |
| CLI `context` | Show and edit exactly what is carried into every conversation |
| CLI `forget` | Append a supersession |
| CLI `rebuild` | The rebuild test |

MCP because Claude Code and a growing set of others already speak it — installation is one
config line. The CLI carries the human-in-the-loop operations and covers non-MCP hosts.

**What this shrinks.** The kernel needs no conversation loop: the host harness *is* the loop.
L0 becomes log writer, resolver, MCP server and CLI. Everything else in this document — the
event log, the resolver, profiles, retrieval, the trust model — survives unchanged. The full
system stays as the roadmap.

**What this obliges.** Principle 9 rejects third-party code and MCP servers on injection-plus-RCE
grounds. Shipping one makes us that third party for other people, so the same bar applies:
declared capabilities, no surprise network access, local-only by default, auditable.

**Recalled memory is an injection vector.** Content flows from the log into a host harness's
trusted context, so a stored memory reading "ignore previous instructions" would be executed as
one unless handled. **Recalled memory is always rendered as data, never as instruction**, with
explicit provenance framing at the boundary. This is a correctness requirement of the product,
not a hardening nicety.

Concretely: every `recall` result is wrapped in an untrusted envelope closed by a random
per-result id, with any marker-shaped text defaced before wrapping (D55), and carries an
information-flow label — `integrity: untrusted, confidentiality: private` — for hosts that
enforce labels instead of reading prose (D84). The integrity label is never assigned: an
assertion is untrusted iff any event in its `caused_by` closure is text from a non-principal,
computed on every fold (D97). Memory is derived from transcripts that held
other people's words; it is untrusted by construction. Anything labeled `user-identity` is
pseudonymized before egress (D18). The host tells the layer which profile a session belongs to
through MCP `roots`; the tool never decides that itself (D83). What a memory layer
cannot do is taint the host's session or refuse its tool calls — that half of the defense
stays with the host, and the README must say so.

**The boundary is evaluated, not trusted.** Three corpora ship with Phase 0 — benign, dangerous
(secrets, third-party PII), injection (planted instructions, forged markers) — with
false-allows as the metric: zero dangerous items become assertions unflagged, zero injected
instructions reach an assertion or escape the envelope. A fourth corpus tests what memory
*does*: LongMemEval's five abilities — extraction, multi-session reasoning, knowledge updates,
temporal reasoning, abstention — and LoCoMo, asked of `recall` over a log built from their
histories (D98). Reports are dated, per model, checked into the repo, and a regression
against the floor blocks the ship (D77). The shipped MCP server carries its own `srt` sandbox
profile — read the profile folder, write only the log, no network — and is tested under it
(D99).

## 4. The three loops

| Loop | Cadence | Model | Job |
|---|---|---|---|
| **Observation** | event-triggered + daily batch | local, small | Shadow. Write events. Detect patterns. Never speaks. Runs as a declared role at **rung 1** — read and append observations, nothing else — bounded by a human-written watch file, yielding to any live session, and switched off entirely on a host where that bound cannot be enforced (D108). |
| **Conversation** | user-initiated | frontier | Talk. Text, later voice. |
| **Engagement review** | weekly | frontier | "Here is what I shipped, what you actually use, what I am retiring, what is next." |

The third loop is cheap and is what makes this feel like an engagement rather than a tool.

**Every background job carries its principal explicitly.** Observation, the proposer,
consolidation and index rebuilds all run off the conversation, and all of them act *for*
someone. They start through one helper that binds the principal, the profile root and the
stream from the caller; a job that starts without one raises rather than falling back, because
the default profile is the most likely wrong answer, not the safest one (D113). Background work
also uses its own model client and never touches a live session's cached prefix (D114, R1).

### 4.1 Observation scope

| Dimension | Decision |
|---|---|
| **Sources** | Structured personal data only — calendar, mail/messages, contacts, documents. No screen capture, no passive sensors. |
| **Cadence** | Event-triggered plus a daily batch pass. Never continuous polling. A pass missed while the machine slept runs **once** on wake, never N times; overlapping runs are skipped (D79). Consolidation runs only when the profile has been idle ≥ 30 min, has ≥ 20 new observations, and ≥ 12 h have passed since the last run (D95). |
| **Retention** | Raw logged with a TTL (start: 30 days), then distilled to assertions and crypto-shredded. |
| **Grants** | Per-workflow and earned — a source is granted in service of one shipped automation and revoked when it retires. |
| **Bootstrap** | A scheduled discovery conversation, not ambient monitoring. |

Screen capture is deferred, not rejected — revisit when proposal quality plateaus on structured
data alone.

The grant model makes observation a function of delivered value: the agent earns a wider view
by being useful rather than demanding one at install. It is the trust ladder applied to seeing
rather than acting.

**Bootstrap matters more than it looks.** Earned grants create a chicken-and-egg — nothing can
be observed before the first workflow exists. The resolution is to refuse the premise: a real
FDE's first week is interviews and scheduled shadowing, explicit and time-boxed. So the first
workflow comes from a discovery *conversation*, which is also the one interface every target
persona already has.

## 5. The event log

### 5.1 Shape

One append-only JSONL stream per log.

```json
{
  "seq": 45012,
  "ts": "2026-09-05T14:37:02Z",
  "type": "assertion",
  "actor": "curator",
  "caused_by": ["d-mbp:44881", "d-mbp:44903"],
  "payload": { }
}
```

`caused_by` makes provenance free — *"why do you think that?"* is answered by walking sequence
numbers, not by maintaining a citation field. `subject` and `visibility` are absent by design:
file location implies both (§7).

`seq` is scoped to its stream — one stream per `(profile, device)`, one file each — so two
machines' logs merge by union ordered by `(stream, seq)` with nothing to reconcile, because
nothing was ever edited (D57). Sync is deferred; the scoping is not. A provenance ref is
therefore always `stream:seq`; the event carries no `stream` field of its own because the file
it sits in implies it.

An `utterance` is a completed turn. Streaming deltas are transport and never enter the log;
anything transient that must still reach a reader carries a **negative `seq`** so it can never
be replayed as history (D56).

### 5.2 Taxonomy

| Type | Written by | Purpose |
|---|---|---|
| `observation` | Observation loop | Something happened in the user's world. Carries `observed_at` — the source's own time — beside `ts`, the append time (D93) |
| `utterance` | Conversation loop | A turn, user or agent |
| `proposal` | Human or Curator | A belief waiting in the staging queue for a human yes. Never folded into what is believed (D120) |
| `assertion` | Curator | A fact about the subject, with confidence, a `level` (explicit / deductive / inductive), `valid_from` / `valid_to` in world time, and — only inside a workflow whose manifest declares the cross-folder read — `about: p-<other>` (D93, D94) |
| `supersede` | any | Retract or replace a prior seq |
| `contradiction` | Curator | Two assertions that cannot both hold; `caused_by` names both. Resolved, or not, by a later `supersede` (D58) |
| `capability` | Builder | A new version of a skill / MCP / hook |
| `mission` | Builder, Operator | proposed → approved → started → step → done/failed |
| `trust` | Kernel | Rung change, scoped to a domain |
| `snapshot` | Curator | A materialized digest — *itself an event* — carrying `forgets: [stream:seq, …]`, the refs it stands in for, and the completion id that produced it (D89) |
| `feedback` | Conversation loop | Directional critique — why it was wrong, what to prefer |
| `outcome` | Operator | Result observed against a `capability` or `mission` |

A `mission` payload is a DAG of steps (`node_link_data`); a step is ready when every
predecessor is done — a pure function of node status — and each step declares what it `reads`
and `writes` — and what it sends off-machine, `egress` (D75). That declaration is the manifest
of §7 rule 2, checked before dispatch. `step` events are node-status deltas; the resolved DAG
is a fold, never an in-place mutation (D67). When a step needs a human and nobody is watching,
the ask goes to an **inbox** — approval, question or notification — resolved exactly once,
first responder wins, idempotent by `(mission, step)`, with the suspended run persisted so a
restart rebuilds the wait. Unattended changes where the human is reached, never what the
agent may do (D78).

### 5.3 The three hard parts

DeepSeek's harness has an append-only session log. Ours is **lifetime-scoped**, and the gap
between those is where all the difficulty lives.

**Retraction.** State is a fold over the log; `supersede` resolves conflicts. Complexity does
not vanish here, it relocates — from "safe mutation" to "correct resolution". Good trade, but
budget for it. The fold must be computable without a model call.

**Time.** Two axes. `ts` is when the log learned something; `valid_from` / `valid_to` are when
it was true in the world. Two assertions contradict only if their validity windows overlap;
a `supersede` carries the `valid_to` it closes, so a retraction has a world-time and a
log-time (D93). The proposer fills valid time only when the text carries a marker that
resolves without guessing; inductive facts take their earliest supporting observation; vague
expressions stay null, because a wrong date looks precise. Set fields are editable in review
(D103).

**Compaction.** The log grows forever, so snapshots are required. The trap: an LLM-generated
snapshot is not reproducible, so replay cannot regenerate it. Fix — **snapshots are events**.
"At seq 45000 the Curator produced this persona" is a fact *in* the log, not a rebuild artifact.
A snapshot names the refs it forgets explicitly rather than implying "everything before N" —
with per-device streams there is no total order for that to mean. Operating rules from the one
built implementation reviewed (D89): condense the older half and keep the recent half verbatim,
at a regular cadence, because condensing destroys the prompt cache and a fixed cut keeps the
rebuild cheap; a size limit is a *soft* trigger that may skip and retry, a context overflow is
*hard* and forces a full reset.

**Evolution.** A lifetime log holds events from every schema version ever shipped, and the
resolver must load all of them forever. Removed fields get a permanent deprecation handler —
never `extra: forbid` without one — and every shipped version leaves a golden log under
`tests/baselines/vN/` with sentinel values the rebuild test checks for silent loss. CI loads
the last tagged release's fixtures through the current resolver; a release that cannot read
its predecessor does not ship. Each event type carries a `v`; on load, `upcast_v1_v2`,
`upcast_v2_v3`, … run in sequence — one permanent, testable function per step (D88, D96). The
index keeps a tracking record per stream, `(stream, last_seq)`, so its normal rebuild is
incremental; the from-nothing rebuild remains the test (D96).

**Deletion.** Append-only and "delete my data" are in direct tension. Use **crypto-shredding**:
sensitive payloads encrypted under a per-subject key; destroy the key and the entry is
unreadable while sequence and structure survive. Retrofitting this into a year-old log is
miserable, so it goes in from the start.

### 5.4 Storage

- **Canonical:** JSONL, one stream per file. Greppable, diffable, portable, `cat`-able — the
  ownership moat made literal. Appends are fsync'd; derived files are rewritten atomically
  (temp path, then rename) and the log itself is never rewritten. The reader tolerates a
  malformed trailing line — an append interrupted mid-write — by skipping it deterministically
  and reporting the skip; a bad line must never brick a stream (D80).
- **Index:** SQLite, a rebuildable cache. Never truth.
- **Provenance index:** an `edges (src, dst, kind)` table in the same SQLite file, each end a
  `stream:seq` ref, rebuilt from `caused_by`. `why` is a recursive query, not a graph store
  (D59).
- **The rebuild test:** a command that destroys every derived artifact and reconstructs from
  the log alone. Run it in CI. If it fails, authoritative state has leaked into a cache — and
  you find out in week two instead of month nine. It compares canonical-JSON content hashes of
  the resolved state — sorted keys, unknown fields forbidden except through a deprecation
  handler — so the assertion is one line (D66, D88). It replays every golden log from every
  prior schema, not only the current one.
- **Recovery is a fold.** On resume, a mission's unmatched steps — an action with no
  observation, rejection or error — are found by a reverse scan and re-dispatched or marked
  failed, explicitly; a run never continues past a step whose outcome is unknown. Stuck
  patterns — repeated or alternating action/observation pairs, a context-error loop — are
  detected the same way, with no model call. Time-to-recover is a reported number (D91).

### 5.5 Capabilities are versions

The Builder never edits a skill. It appends v2; the resolver picks the highest active version
whose eval passes. Rollback is a `supersede` pointing back at v1. The Operator retires dead
versions with tombstones, never deletions.

**Install is not enable.** The Builder may author, version and stage a capability; turning it
on is a human act. A grant is bound to the manifest it was given for, so a version that declares
more — a new path, a new source, egress it did not have — **lapses the grant** and stays off
until it is granted again. Without that, a capability widens itself one version at a time under
a grant nobody re-read (D117).

Code the Builder writes runs in an **OS-level sandbox** by default — seatbelt on macOS,
bubblewrap + landlock on Linux, closed by default with the workspace and declared paths poked
open, network through an allow-only proxy that resolves each name once and refuses loopback,
link-local, metadata and the host's own addresses. Violations reach the model with the reason
and the sanctioned alternative. A container — only the workspace mounted, capabilities
dropped, resource caps — is the stronger opt-in. "Locally-authored" is a threat model, not an
execution boundary (D68, D99).

## 6. Memory

A 1M window at 96.3% multi-needle recall makes this a **residency** problem, not a retrieval
one.

- **Hot tier** — resident in the cached prefix: persona, active workflows, current-quarter
  facts, open threads. ~150–200k tokens.
- **Cold tier** — retrieved: the multi-year log.

Build the hot tier first; add retrieval when it stops fitting. Provenance is required in both.

**Promotion requires evidence** — stated explicitly, or observed N times. Assertions carry
confidence and decay. The mechanics — a schema-blind extractor feeding a staging queue that
*is* the review batch, a normalizer against a small canonical field registry with `extras`
for the rest, belief update starting at 0.3 with asymptotic reinforcement, salience as
0.6·recency + 0.4·frequency — and their starting numbers are in D60.

**Writes are human-gated, asymmetrically.** Requiring approval for every fact is unusable;
approving nothing is how memory silently rots. The line sits between the two:

- **Observations append freely** — raw, low-stakes, TTL'd, never injected on their own.
- **Assertions require confirmation** — these are the durable beliefs injected into every
  future context, so they get a human yes.

Review is **batched**, not per-item: at session end or weekly, *"here are six things I would
like to remember — keep, edit, or drop."*

**The hot tier is inspectable and editable.** "Here is exactly what I carry into every
conversation, in a file you can open" is the ownership claim made concrete, and no incumbent
offers it — hosted memory is a box you can barely see into. Its files are self-describing —
each opens with what it is and what to do with it, or the model reads its own identity as notes
about a stranger — and split identity / user / **rules** / learned so a lower role can receive
a subset (D64). `rules` is the one file only the human writes: injected above everything the
agent learned, it wins on conflict, and no tool can touch it (D81, enforced by D72).

**Editable, but through the log.** The rendered files are derived, so they are written `0444`
and `walle context` is the editor: it opens the live view, diffs what comes back, and appends
the events that diff implies. The renderer records the hash of what it wrote; if the file has
changed at the next render it does not overwrite — it keeps the edit, stages it for review
(D49), and says so. A derived file that silently loses a human's edit, or silently discards
the log's own state, is the failure this prevents (D107).

Every assertion carries a one-line `summary` from the moment it is proposed. Below ~8k chars
the hot tier renders every assertion in full; above it, the newest ten stay in full, the rest
render as `[#id] summary` lines, and `recall` fetches bodies on demand — with a note to fetch
before acting on anything a summary hints at. Mode is fixed per session. This is *"add
retrieval when it stops fitting"* made automatic, and why `recall` survives D54 (D81).

**Fusion happens at snapshot.** The Curator periodically consolidates assertions into a digest,
and that digest is itself a logged event. Nothing is lost, folds stay cheap, and fusion happens
with maximum context rather than at the moment of least context.

**Contradictions between stated and observed are surfaced, carefully.** When "I don't eat
sugar" meets three dessert orders, the system raises it — but only at a decision point where it
would change an action, framed as a question about the system's belief ("should I still treat
this as a rule?") rather than the person's behavior, batched into the engagement review rather
than interrupting, and **never in front of another principal**. An agent that points out your
inconsistencies in public is an agent people switch off.

## 7. Profiles

The deployment unit is a **tenant** — a household. Inside it, **every subject that holds facts
gets a folder**, modelled on Chrome and Netflix profiles: one account, many profiles.

```
tenant/
  kernel/                    # immutable
  shared/                    # capabilities ONLY — no data, ever
    skills/  mcp/  hooks/
  principals/
    p-maya/       { log/ d-mbp.jsonl  d-phone.jsonl   memory/  grants/  keys/ }
    p-dev/        { ... }
    p-household/  { ... }    # an entity, not a person
```

**`shared/` holds the *how*, never the *what*.** Skills, MCP configs, hooks. No logs, no
memory, no data of any kind.

**Organizations are principals.** A household or a shop is a subject that holds facts exactly
as a person is, so it gets a profile rather than a second mechanism. Shared calendars and
opening hours live there.

**Isolation is structural, not policy.** Cross-principal fusion is impossible because the data
is unreachable, rather than being a rule the Curator must obey correctly forever. The same
separation removes the need for a `visibility` field, makes crypto-shredding a folder-and-key
deletion, and gives prefix caching a stable per-person boundary.

The layout is the unit of enforcement: at Phase 3, when a second principal exists, each
`principals/p-<name>/` gets its own OS user and group, so one principal's data is unreachable
by another's *process*, not only by its path — a resolver bug cannot cross what the kernel
refuses. Parent directories are passable but not listable; `shared/` belongs to a common group.
A credential a connector needs but the agent must not read runs behind a wrapper owned by a
second user, which must never print its key (D110).

**Rules**

1. Only capabilities enter `shared/`, and only by explicit publication — never by inference.
2. Cross-folder reads happen only inside a declared workflow whose manifest lists the paths it
   touches. **The manifest is the permission grant.**
3. Stakeholder digests read organization profiles, never a person's folder.
4. Deleting a person is deleting a folder.
5. Path segments are self-describing and prefixed — `p-` principal, `d-` device stream — so a
   collapsed layout is never ambiguous and a device name can never masquerade as a principal.
   Any id arriving from outside is validated as one sanitized segment before it is joined to a
   path; a value that would escape is rejected, not normalized (D85).

**Roles** — three, deliberately not an ACL system:

| Role | Behavior |
|---|---|
| **Operator** | Daily user. Converses, proposes, runs workflows in their domains. |
| **Stakeholder** | Periodic. Receives digests from org profiles, sets policy, does not operate. |
| **Admin** | Grants observation scope and trust rungs for others. Usually also the stakeholder. |

The caregiver is a stakeholder — same machinery as any periodic reviewer, built once.

In a conversation with more than one principal in it, every message carries who is speaking
and what their role permits — attached per message, because the sender changes between turns.
Loaded context ratchets down to the lowest role that has ever spoken there and never recovers;
capability is checked per tool call against the current speaker. Strangers are refused before
a session exists (D61).

**Accepted cost:** unpublished patterns spanning people are invisible. Correct trade for v1.

## 8. Retrieval

**No vector index initially.** The hot tier is the retrieval system, and the dominant queries
are not semantic — date ranges, counts over filters, structured joins on manifests. SQLite FTS5
(BM25) plus indexes on time, actor, type and salience covers them using the index already
required by §5.4.

Semantics genuinely wins in two places, neither worth pre-building: fuzzy recall ("that
restaurant with the good lighting") and fusion deduplication at snapshot time. Instrument for
these — log the queries the resolver cannot satisfy — and add vectors when that class
measurably hurts.

**If added: `sqlite-vec`, not FAISS.** Architectural, not performance. FAISS is a second store
with its own lifecycle that must be kept consistent with the log; `sqlite-vec` lives inside the
SQLite file that already exists — one artifact, one rebuild path. Retrieval is hybrid, BM25
fused with vector.

Two constraints that the *efficient* implementation would violate:

- **Per-profile, inside the principal's folder.** A tenant-wide index physically reintroduces
  the leak §7 designed out, because similarity search does not respect folder boundaries.
- **Crypto-shredding deletes embeddings.** They are derived from plaintext and partially
  invertible; shredding raw data while leaving vectors keeps a lossy copy of what was destroyed.

The embedding model version is pinned; changing it is a rebuild event.

## 9. Trust

Per principal, per domain. Visible, and user-settable.

| Rung | Capability |
|---|---|
| 1. Observe | Read-only. Surfaces patterns. Takes no action. |
| 2. Propose | "Want me to do this?" |
| 3. Act-with-confirm | Does it, asks first, every time |
| 3½. Act-with-review | A reviewer model clears routine actions and escalates the rest to the human |
| 4. Autonomous-bounded | Acts inside a declared envelope — this calendar, under $50, these contacts |

**Floors sit under every rung.** Anything the system loads on its own is unwritable by the
agent in any mode — `grants/`, `keys/`, the rules file, the watch file, the hook allowlist,
`trust` events, the rendered hot tier and everything under `shared/` — because writing one of
them puts chosen words into a trusted context or changes what runs (D72, D109). Credentials are
matched by file name, never by substring, so `tokenizer.ts` reads like any other file (D109). Tools whose
authority outlives the session — creating or changing an automation, publishing to `shared/`,
moving a rung — are human-only, never cleared by a reviewer; narrowing is floored along with
broadening, because telling them apart means judging intent (D72).

**Rung 3 is a protocol, not a prompt.** The request names the exact command. An approval is
scoped to *this request* and expires at the run boundary; *this session* is the middle tier;
only the literal word `always` writes a durable rule; config allowlists are the ceiling and are
human-edited. A grant that cannot be persisted degrades to session scope, never to nothing, and
says so (D62, D73).

**Rung 3½ has seven invariants** (D76): the reviewer can turn *ask* into *go*, never *blocked*
into *go*; one action per request; fail closed — any parse failure is *unsure* and *unsure* is
the human; it never reads untrusted content, so the attacker can address the agent, never the
judge; costs are asymmetric, so *unsure* is free; it judges scope, not plausibility; and text
that argues with it — asserted approval, authority, urgency — is itself evidence to deny. Tool
names are labels, not evidence. Repeated denials trip a breaker that hands control back and
never trips silently. Every action carries its approval provenance. The reviewer — like every
automated judgment here, including the local model's salience tiers and the proposer's ranking
— ships in **shadow** first: it records what it would have decided while the human decides,
earns its rung against those logged decisions, and keeps writing its shadow verdict after
promotion so a regression shows in the log (D82).

**Rung 4's envelope is a list of standing rules** `tool: pattern*`, with a bare `*` rejected. A
compound command is split on every separator and *each part* must match — over-splitting only
ever produces more parts to justify. Three shapes are never eligible for a prefix rule however
well they match: opaque constructs (substitution, redirects, variables), argument-executors
(`xargs`, `env`, `sudo`, `docker`, `npx`, `uvx` — the outer program cannot vouch for the inner
one), and inline-code interpreters or `-exec` flags. Network clients are never "read-only",
even for GET (D62, D74). Every rule carries a `justification` and `match` / `not_match`
examples that are validated when the rule file loads — a rule that does not match its own
examples is rejected; a `forbidden` rule names the sanctioned alternative; a basename rule
applies only to listed absolute paths (D100).

**Risk is a declared property of every tool** — `read`, `egress`, `write_local`, `exec`,
`external` — and the class, not the name, drives the gate. Egress includes anything whose
query is model-chosen free text. A user override may only tighten (D75). Our own tools declare
the standard MCP annotations for the same classes — `readOnlyHint`, `destructiveHint`,
`openWorldHint` — so a host that reads hints gates them without our help (D84).

**The seam fails closed.** A failure inside the permission engine halts the run; it never
surfaces as a tool error, because a tool error is a string the model reads and routes around.
Nothing is persisted or emitted ahead of the verdict — `remember` only stages, the hot tier
rebuilds only after review, and a mission's stream is buffered until its step is cleared. These
are properties of construction, not convention: the seam order is fixed and the affordances
that would let a caller replace it are rejected loudly. Against an in-process adversary this
is a cooperative contract; the container executor (D68) is the boundary (D86).

An irreversible action may be approved once and never "always": the standing-rule option is not
offered for it, because an affordance that does not exist cannot be misused (D116).

Promotion is earned by demonstrated accuracy at the current rung. **Computer use sits at rung 4
only**, and every action is recorded: a misfired click sends the email or spends the money, and
at ~73% OSWorld success roughly one in four end-to-end attempts fails.

## 10. Model routing

Kernel-level, not an optimization. It is also what buys vendor independence. The routing key
is a `provider:model` string and the provider layer is aisuite — one string swaps the vendor,
and the local tier is an `ollama:` prefix (D87).

| Work | Model | Where |
|---|---|---|
| Retention/salience classification, pseudonymization, pattern detection, routing | 7-8B quantized (~5GB @ Q4) | User's own machine |
| Routine conversation turns | Mid-tier hosted | — |
| Hard reasoning, Builder, Curator, computer-use missions | Frontier | — |

**Local is structural, not thrift.** Continuous observation through a $10-in/$50-out model runs
roughly $5/user/day uncached — about $150/month, for a product aimed at parents and students.

**Classify, never discard.** The local model assigns a retention tier and salience score; it
never drops an observation. A 7-8B model deciding what is worth appending would be making
irreversible calls in an append-only log. This way a filter mistake costs storage, not
information.

**Egress boundary.** Identities are pseudonymized to stable local tokens before any frontier
call; the mapping never leaves and is itself an append-only stream. Stability is load-bearing —
a token must mean the same person across years or the frontier model correlates nothing. It
also gives crypto-shredding one lever: destroy a subject's mapping entry and every historical
reference goes opaque at once.

**Cache discipline.** Cached input runs ~$1/M against ~$10/M uncached, but cache *writes* cost
~$12.50/M. Appends must be strictly ordered and batched to session boundaries — an out-of-order
insert is a cache invalidation at 12.5×. Append-only is precisely the shape prefix caching
rewards; the architecture and the economics agree, which is rare enough to trust.

## 11. The evolution loop

Adapted from the self-evolving recommender literature, with its methodology deliberately
discarded: those systems evolve under *feedback abundance* — millions of impressions, free
statistical power, headline wins of +0.06%. This one evolves under **feedback scarcity**: one
person, a handful of automations, no control group, no A/B. Architectures transfer; anything
needing traffic to validate does not.

**Outcomes are logged, not just changes.** Every `capability` accumulates `outcome` events.
The Curator reads them before proposing — without this it re-proposes the same failed idea
forever.

**Maintenance is two-speed.** The deterministic pass — moving things between active, stale and
retired on their own timestamps — always runs, with no model in it. The model pass that
consolidates and rewrites is separate, opt-in and off by default. Nothing is deleted, only
tombstoned. A **pinned** item is never touched by an automatic transition, and nothing a live
automation's manifest names is retired on usage grounds — rarely-used is not unused (D114).

**Two-stage promotion gate.**

| Stage | When | Test |
|---|---|---|
| 1 — proxy | On every proposal | Evals pass; no regression against the floor |
| 2 — real | 30 days later | The automation is still in use |

**The regression guard is a floor, not a delta.** Guard against *initial* capability, not the
previous version — otherwise a sequence of individually-acceptable regressions walks the system
downhill. Express limits declaratively with threshold abort.

**Feedback is directional.** A feedback event carries why something was wrong and what to
prefer instead, not a scalar. Signals are scarce; extract everything from each one.

**The Curator is frontier-class and must be pushed to explore.** Model-size sensitivity is
sharp in the literature, and agents bias toward incremental tweaks unless the prompt forces
diversity.

## 12. What this deliberately does not build

| Not built | Why |
|---|---|
| App marketplace | Marketplaces are earned. Build the format, hand-build the apps. Revisit when the same playbook is independently rebuilt by several users. |
| Third-party code, hooks, MCP servers | Prompt injection plus RCE plus credential requests, aimed at users who cannot audit any of it. |
| Bandit-routed version selection | Elegant, and compatible with the purity rule if state is a deterministic fold with a seeded RNG. But at n=1 the arms are noise. |
| A plugin meta-framework | Extreme composability is its own bloat — every seam is config, indirection, and a thing to debug. Take the event log, not the framework. |
| Observability stack | The log *is* the trace. Adopt Opik when the eval suite gets real; never a second store. |
| Employer/workplace deployment | Employee monitoring, with jurisdiction-dependent legal exposure. Enterprise fork takes it. |

When the marketplace is eventually revisited it ships **playbooks, not code** — a described
workflow that the local FDE builds against the user's actual systems. `shared/` already holds
capabilities and zero data, so the export format exists with no stripping step.
