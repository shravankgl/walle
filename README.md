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

## Install the SessionStart hook

Not usable yet (see the status above), but this is how the hot tier reaches Claude Code
(D54). walle never edits your settings: you paste this into `~/.claude/settings.json`
yourself, replacing the two paths and the name. Keep any `hooks` you already have.

```json
{
  "hooks": {
    "SessionStart": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "/path/to/walle/.venv/bin/walle-hook --root /path/to/walle-data --principal maya",
            "timeout": 10
          }
        ]
      }
    ]
  }
}
```

- `/path/to/walle` is your checkout, after `uv sync`. `/path/to/walle-data` is the folder
  that holds `principals/`, and `maya` is whose memory this session gets.
- With no `matcher`, it runs on startup, resume, `/clear` and compaction alike.
- The hook only reads the hot tier that `walle render` wrote. If anything is wrong, or it takes
  more than 2 seconds, it prints nothing and the session starts without memory (D115).
  `timeout` is Claude Code's own limit, a guard behind that one; without it the host waits
  600 seconds.
- To see what it injects and roughly how many tokens that is, run it by hand:
  `echo '{"hook_event_name": "SessionStart"}' | /path/to/walle/.venv/bin/walle-hook --root
  /path/to/walle-data --principal maya --measure`. The JSON goes to stdout, the count to
  stderr.

## The MCP server

Not usable yet either. `walle-mcp` serves three tools over stdio (D48): `recall` (search
current facts, or fetch one by ref), `remember` (only *stages* a proposal; you keep or drop it
in `walle review`) and `why` (where a fact came from). Every piece of memory comes back in an
untrusted envelope with a random per-result id, labelled `_meta.ifc` from its provenance
(D55, D84, D97). For Claude Code:

```json
{
  "mcpServers": {
    "walle": {
      "command": "/path/to/walle/.venv/bin/walle-mcp",
      "args": ["--root", "/path/to/walle-data", "--principal", "maya", "--device", "mbp"]
    }
  }
}
```

- **Schema cost (D102): 1,102 characters, about 368 tokens** for all three tools, measured on
  the `tools/list` answer a client receives at 3 characters per token (an estimate on the safe
  side; Claude's tokenizer is not available offline). The cap is 400 and is a test. Check it
  with `walle-mcp --root /path/to/walle-data --principal maya --device mbp --measure`.
- The envelope cannot stop the host from acting on what it reads. A memory layer runs inside
  the host and cannot taint its session or refuse its tool calls (D55).

### Running the server sandboxed

D99: the server should run under [`srt`](https://github.com/anthropic-experimental/sandbox-runtime)
(seatbelt on macOS, bubblewrap on Linux). walle prints the profile for you:

```sh
walle-mcp --root /path/to/walle-data --principal maya --device mbp --srt-profile > walle.srt.json
srt --settings walle.srt.json walle-mcp --root /path/to/walle-data --principal maya --device mbp
```

- **Reads:** Maya's folder and the Python that runs the server. The home folder and every
  other principal's folder are denied by the profile.
- **Writes:** Maya's `log/` folder only. So `remember` still stages, and `recall` and `why`
  cannot refresh the index: they answer from the index as last built and say so. Refresh it
  with `walle` outside the sandbox.
- **Network:** none.
- The `log/` folder must exist first; a `walle` command run outside the sandbox makes it.
- **Not yet verified under `srt`.** The profile's contents and the server's behaviour on
  read-only files are tested here. The four tests that run the server under the real `srt`
  skip where `srt` is absent, and have not yet been run on a machine with it. Until they
  have, treat the profile as written to the `srt` documentation, not as proven. On Linux
  `srt` also needs `bwrap` and `socat`.

## Documents

- **[ARCHITECTURE.md](ARCHITECTURE.md)** — the system: layers, loops, event log, profiles
- **[PLAN.md](PLAN.md)** — the roadmap: phases, metrics, writing track, risks
- **[CONVERSATION.md](CONVERSATION.md)** — decision log: what was chosen, what was rejected, why
- **[REFERRALS.md](REFERRALS.md)** — repos reviewed for adoption: what was taken, what was left, why
- **[CLAUDE.md](CLAUDE.md)** — working agreement for Claude Code sessions in this repo

## Evals

What the boundary refuses is measured, not asserted (D77), and what the memory does is
measured against public benchmarks adapted to a memory layer (D98). `walle eval` runs both
and writes a dated, per-model report under [`reports/`](reports/):

```
uv run walle eval                       # the fake model, offline; exit 1 if the ship gate fails
uv run walle eval --longmemeval path/to/longmemeval_s.json --locomo path/to/locomo10.json
uv run --with 'aisuite[anthropic]' walle eval --model anthropic:claude-...   # a real model
```

- **Boundary (D77).** The three corpora under `tests/corpora/` (benign, dangerous, injection;
  synthetic, generated from templates) go through the proposer with the model named and
  through the MCP server's `remember` and `recall`. The metric is **false-allows**: a
  dangerous or injection item that reaches a proposal, a verdict, the wire, or escapes the
  envelope. The **ship gate** is zero on both, and zero benign refused. The latest report
  with the fake model: [`reports/2026-11-04-fake.md`](reports/2026-11-04-fake.md), gate
  PASS, 90 items.
- **What a fake-model PASS proves, and does not.** The fake is deterministic and offline, and
  the hazard filter withholds every dangerous and injection item before any model is called.
  So `SHIP GATE: PASS` with the fake proves only that **the harness works**: it runs every
  item through both legs, and its detectors fire on planted faults. It says **nothing about a
  real model**, which has not been run (needs an API key), and the detectors have known gaps
  that are open in [#46](https://github.com/shravankgl/walle/issues/46). Do not cite it as a
  safety result.
- **Capability (D98).** LongMemEval and LoCoMo adapted to `recall`: every turn of a history
  becomes a belief with the session's own time, and each question is asked of `recall`.
  `hit@5` (an evidence session or turn among the five results) needs no model; QA accuracy
  needs an answerer and a judge. The datasets are not in this repository.

| benchmark | hit@5 | QA accuracy | status |
|---|---|---|---|
| LongMemEval (five abilities) | — | — | **not run**: needs `longmemeval_s.json` and model keys |
| LoCoMo (ten conversations) | — | — | **not run**: needs `locomo10.json` and model keys |

These cells are filled from a report, never typed in. Until a run exists they stay empty.
