# personal-harness

> A forward deployment engineer for anyone.

**Status:** Phase 0 implementation. It installs and runs (below), but it is early, built largely
by agents, and code lands before human review (R6) — see [PLAN.md](PLAN.md) §9 and
[devlog/](devlog/). The [status table](#what-works-and-what-is-untested) says what has been tested
and how. Package name: `walle` (D105).

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

## Install

You need [`uv`](https://docs.astral.sh/uv/getting-started/installation/) (on a Mac,
`curl -LsSf https://astral.sh/uv/install.sh | sh`). Then one command installs `walle`,
`walle-hook` and `walle-mcp`:

```sh
uv tool install --python 3.12 https://github.com/shravankgl/walle/archive/refs/heads/main.zip
```

and one line gives Claude Code its memory:

```sh
claude mcp add walle -- walle-mcp --root ~/walle-data --principal me --device mac
```

- `--python 3.12` matters. Without it, uv builds the tool on the first Python it finds, which on
  a fresh Mac is the system's 3.9, and the install fails; with it, uv downloads 3.12 itself.
- The source is a GitHub archive of `main`, not a `git+` URL, because a fresh Mac has no `git`
  until the developer tools are installed. walle is **not on PyPI**, so `uvx walle` does not
  work yet, and a package called `walle` on PyPI is not this project.
- `~/walle-data` is created on first use. `me` is whose memory it is and `mac` names this
  machine's stream; pick your own.
- If uv says its bin folder is not on your `PATH`, run `uv tool update-shell` and open a new
  terminal before the `claude mcp add` line.
- `walle --help` lists the rest. The usual first loop: Claude calls `remember`, which only
  stages; you run `walle review --root ~/walle-data --principal me --device mac` to keep or
  drop; `walle render --root ~/walle-data --principal me` writes the hot tier; and the
  [SessionStart hook](#install-the-sessionstart-hook) puts it in every new session.
- To remove it: `uv tool uninstall walle` and `claude mcp remove walle`. Your memory stays in
  `~/walle-data` as plain JSONL until you delete it.

## Measured

D69 and D102: every number here was measured, by the command beside it, and anything that was
not measured says so. The numbers were taken on 2026-11-05 in the build agent's sandbox, a
2-core Linux x86_64 container on an xfs disk, Python 3.12.16. **Not on a Mac**: `fsync`, which
dominates persist, costs very differently on a laptop SSD, so re-run `walle bench` on your own
machine before quoting these.

| What | Measured | Command |
|---|---|---|
| Tool schema, all three MCP tools (D102) | 1,102 characters, about **368 tokens** (characters / 3); cap 400, enforced by a test | `walle-mcp --root /tmp/walle-bench-100 --principal bench --device bench --measure` |
| Hot tier, 1 fact | 2,065 characters, about **517 tokens** (characters / 4) | `walle remember`, `walle review`, `walle render`, then `walle-hook … --measure` (as in [the hook section](#install-the-sessionstart-hook)) |
| Hot tier, 80 live facts | 8,911 characters, about **2,228 tokens**, index mode | `walle bench --events 100 --root /tmp/walle-bench-100`; the same figure from `walle-hook --root /tmp/walle-bench-100 --principal bench --measure` |
| Hot tier, 800 live facts | 63,633 characters, about **15,909 tokens**, index mode: **over Claude Code's 10,000-character cap**, so the host shows the model a file path and a preview instead | `walle bench` |
| Persist: one `fsync`ed append | median **0.18 ms**, p95 0.21 ms, over 1,000 appends | `walle bench` |
| Replay: read and fold 1,000 events from the log, no index | median **14.59 ms**, p95 16.13 ms, 5 runs | `walle bench` |
| Recover: delete the index, rebuild from the log, fold | median **64.53 ms**, p95 113.68 ms, 5 runs; state hash equal after rebuild | `walle bench` |
| Replay / recover at 100 events | median 1.47 ms / 7.50 ms | `walle bench --events 100 --root /tmp/walle-bench-100` |
| Hook, whole process: start Python, read the hot tier, answer (1-fact profile) | **29 ms** on average over 20 runs (0.581 s in all); the hook's own `--measure`, which starts its clock after Python is up, says 3 ms | `time (for i in $(seq 20); do echo '{"hook_event_name": "SessionStart"}' \| walle-hook --root ~/walle-data --principal me >/dev/null; done)` |
| Resident tokens on a real person's memory | **not measured**: there is no real profile in this repository, by design | — |
| Any of the above on a Mac | **not measured** | — |
| Install, nothing cached, Python 3.12 downloaded by uv | 2.8 s to 3.5 s over three runs in the sandbox; **on a fresh Mac, not measured** | the install command above, timed with `time` |

Token counts are estimates (characters / 4 for the hot tier, / 3, the safe side, for the
schema): Claude's tokenizer is not available offline. `walle bench` writes only synthetic data
into a folder that must be absent or empty; nine events in ten are made-up household facts and
every tenth retracts the one before it.

## What works and what is untested

| Part | Status |
|---|---|
| Log, writer, reader, resolver, SQLite index, rebuild | **Working**, tested: concurrent writers, torn tails, golden logs per schema version (D88) |
| `walle remember` / `review` / `forget` / `why` / `context` / `render` | **Working**, tested with the CLI runner and run by hand on a fresh install |
| `walle-mcp` (`recall`, `remember`, `why`, the hot-tier resource) | **Working** over stdio, tested in-process and, from a fresh install, by a real stdio client. Not yet exercised inside a real Claude Code session by the agents who built it |
| SessionStart hook | Tested with fake hook payloads. Not yet verified by the agents inside a real Claude Code session |
| Transcript observer (`walle observe`) | Tested on synthetic transcripts only |
| Proposer and curator | Tested with the fake model only |
| **Model route** (`provider:model` through aisuite, ollama as the local tier) | **Tested against fakes only.** No real provider and no real ollama has been called by the tests |
| **Google connectors** (Gmail, Google Calendar) | **Tested against fakes only.** Neither has talked to a real Google account |
| **ACP client** (a second host, [#66](https://github.com/shravankgl/walle/pull/66)) | **Tested against fakes only**, and not on `main` at the time of writing |
| `srt` sandbox profile | Written to the `srt` documentation; **not verified under `srt`** (see below) |
| Boundary eval with a real model; LongMemEval and LoCoMo | **Not run** (see [Evals](#evals)). `walle eval` also needs a checkout, for `tests/corpora` |
| Install on a fresh Mac | **Not verified on a Mac.** In a Linux sandbox: the command above installed from GitHub with an empty home folder and neither `git` nor any Python on `PATH`; and `tests/test_packaging.py` installs this checkout's wheel the same way with only a system Python 3.9 on `PATH`, then starts the server with the `claude mcp add` line's command and stages a memory through a real stdio client. Claude Code itself was not run |

## Support

Built by one person, on evenings and weekends (PLAN §2, "guard the hours").

- **Issues** are welcome and read when there is time. There is no response time.
- **Pull requests are declined.** The work is planned in [PLAN.md](PLAN.md) and built from
  issues; an unplanned PR is closed, however good. Open an issue instead.
- **Security problems:** please do not post details in a public issue. Open an issue that
  says only that you have a security report, and a private channel will be arranged.
- No compatibility promise before a tagged release. The log format is the exception: logs
  written by any shipped schema version keep loading (D88).

## Install the SessionStart hook

This is how the hot tier reaches Claude Code at the start of every session (D54). walle never
edits your settings: you paste this into `~/.claude/settings.json` yourself, with your own home
folder in place of `/Users/you` and your own name for `me`. Keep any `hooks` you already have.

```json
{
  "hooks": {
    "SessionStart": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "/Users/you/.local/bin/walle-hook --root /Users/you/walle-data --principal me",
            "timeout": 10
          }
        ]
      }
    ]
  }
}
```

- `/Users/you/.local/bin` is where the install above puts `walle-hook`; `uv tool dir --bin`
  prints the folder if yours differs. The full path is deliberate: a hook that cannot be found
  fails silently. `/Users/you/walle-data` is the folder that holds `principals/`, and `me` is
  whose memory this session gets.
- With no `matcher`, it runs on startup, resume, `/clear` and compaction alike.
- The hook only reads the hot tier that `walle render` wrote. If anything is wrong, or it takes
  more than 2 seconds, it prints nothing and the session starts without memory (D115).
  `timeout` is Claude Code's own limit, a guard behind that one; without it the host waits
  600 seconds.
- To see what it injects and roughly how many tokens that is, run it by hand:
  `echo '{"hook_event_name": "SessionStart"}' | walle-hook --root ~/walle-data --principal me
  --measure`. The JSON goes to stdout, the count to
  stderr.

## The MCP server

`walle-mcp` serves three tools over stdio (D48): `recall` (search
current facts, or fetch one by ref), `remember` (only *stages* a proposal; you keep or drop it
in `walle review`) and `why` (where a fact came from). Every piece of memory comes back in an
untrusted envelope with a random per-result id, labelled `_meta.ifc` from its provenance
(D55, D84, D97). The `claude mcp add` line in [Install](#install) is all Claude Code needs.
The same thing as JSON, for a project's `.mcp.json` or another host:

```json
{
  "mcpServers": {
    "walle": {
      "command": "walle-mcp",
      "args": ["--root", "/Users/you/walle-data", "--principal", "me", "--device", "mac"]
    }
  }
}
```

- **Schema cost (D102): 1,102 characters, about 368 tokens** for all three tools, measured on
  the `tools/list` answer a client receives at 3 characters per token (an estimate on the safe
  side; Claude's tokenizer is not available offline). The cap is 400 and is a test. Check it
  with `walle-mcp --root ~/walle-data --principal me --device mac --measure`.
- The envelope cannot stop the host from acting on what it reads. A memory layer runs inside
  the host and cannot taint its session or refuse its tool calls (D55).

### Running the server sandboxed

D99: the server should run under [`srt`](https://github.com/anthropic-experimental/sandbox-runtime)
(seatbelt on macOS, bubblewrap on Linux). walle prints the profile for you:

```sh
walle-mcp --root ~/walle-data --principal me --device mac --srt-profile > walle.srt.json
srt --settings walle.srt.json walle-mcp --root ~/walle-data --principal me --device mac
```

- **Reads:** this principal's folder and the Python that runs the server. The home folder and every
  other principal's folder are denied by the profile.
- **Writes:** this principal's `log/` folder only. So `remember` still stages, and `recall` and `why`
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
  PASS, 118 items.
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
