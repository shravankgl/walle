# personal-harness

> A forward deployment engineer for anyone.

**Status:** Phase 0 implementation, not yet usable. Agents are building it and code lands before
human review (R6) — see [PLAN.md](PLAN.md) §9 and [devlog/](devlog/). Package name: `walle` (D105).

Built solo, evenings and weekends. Each phase ships an article alongside the code — see
[PLAN.md](PLAN.md) §4.

---

## The problem

Every agent harness today is built for a job title. OpenClaw, Hermes, Pi, the DeepSeek
harness — they are coding tools, testing tools, content tools. They are also *bloated*, but
not in the way people usually mean: the bloat is not features, it is **unconditional resident
context**. Every tool schema, skill preamble, and system rule sits in the window on turn one,
paying for itself in latency, cost, and diluted attention whether it is needed or not.

And every session starts from zero. Remembering anything means handing your history to a
vendor's cloud.

## The thesis

Most people do not need a coding agent. They need what a good **forward deployment engineer**
does: someone who shows up, watches how you actually work, picks the one thing worth
automating, builds it against *your* systems, sits with you while it breaks, fixes it, and
then earns the right to do the next thing.

That is a role, not a chat interface. A chatbot waits to be asked. An FDE says *"you have done
this every Monday for a month — want me to take it?"*

The same system serves a chef scouting locations, a parent running a household, a student
learning anything, and an older adult who wants the news read aloud and their medication
tracked — because the role is identical. Only the shipped workflows differ.

## How it works

| An FDE... | ...becomes |
|---|---|
| Shadows how you actually work | **Observation loop** — continuous, local, cheap |
| Scopes ruthlessly and says no | **Scoper** — one first win, hard WIP cap |
| Ships working glue in days | **Builder** — local code against your systems |
| Sits with you while it fails | **Conversation loop** — chat or voice |
| Keeps it running as things change | **Operator** — detects rot, repairs, retires |
| Knows when to say "get a lawyer" | Kernel-level escalation rules |

Underneath: an **append-only event log** is the only source of truth. Memory, personas,
skills, prompts and hooks are never edited — new versions are appended and old ones
superseded. Everything the model sees is *derived* from that log. Nothing is destroyed, every
belief can be traced to the event that produced it, and rollback is just reading up to an
earlier sequence number.

The log lives on your machine as plain JSONL. You can read it with `cat`, grep it, back it up,
and carry it to a different model vendor.

## Principles

1. **The kernel is immutable.** The agent may rewrite its skills, memory, and prompts. It may
   never rewrite the thing that constrains it.
2. **Append, never mutate.** The log is truth; all state is a fold over it.
3. **Resolution is a pure function.** Deciding which fact or skill version is live never
   requires a model call.
4. **Local is the system of record.** Inference is remote — be honest about that — but your
   history, memory and capabilities are yours and portable.
5. **Restraint is enforced, not encouraged.** A hard cap on active automations. To add one,
   retire one.
6. **Trust is earned per domain.** Observe → propose → act-with-confirm → autonomous-bounded.
   Trusted with your calendar is not trusted with your money.

## Documents

- **[ARCHITECTURE.md](ARCHITECTURE.md)** — the system: layers, loops, event log, profiles
- **[PLAN.md](PLAN.md)** — the roadmap: phases, metrics, writing track, risks
- **[CONVERSATION.md](CONVERSATION.md)** — decision log: what was chosen, what was rejected, why
- **[REFERRALS.md](REFERRALS.md)** — repos reviewed for adoption: what was taken, what was left, why
- **[CLAUDE.md](CLAUDE.md)** — working agreement for Claude Code sessions in this repo
