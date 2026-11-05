"""``walle bench``: the persist / replay / recover numbers in the README, measured (D69).

The README's measured table must hold numbers someone actually measured, with the command that
measured them. This is that command for the three that are about the log: how long one append
takes, how long a full fold takes, and how long recovery takes when every derived file is gone.
It also renders the hot tier for the synthetic profile and reports its size the way
``walle-hook --measure`` does, so the resident number for a profile of that size comes from the
same run.

What it measures
----------------
It writes ``events`` synthetic events to principal ``bench``, device ``bench``, in a folder of
its own. Nine in ten are assertions with a short made-up household fact; every tenth retracts
the assertion just before it, so the fold has supersedes to honour (the live count is reported).

- **persist**: one ``StreamWriter.append``, timed per call. That is the real write path: take
  the stream lock, read the tail for the next ``seq``, write, ``fsync``. The median and p95 of
  every append.
- **replay**: read the whole stream and fold it with the resolver, from the log alone with no
  index. Timed ``runs`` times.
- **recover**: ``index.rebuild``, which deletes the SQLite index and rebuilds it from the log
  (hard constraint 5), followed by a fold of what it committed. Timed ``runs`` times. Each run
  checks that the state hash after the rebuild equals the hash of the fold from the log; the
  answer is printed, never assumed.
- **hot tier**: ``walle render`` for the profile, then the same read and characters-per-token
  estimate as ``walle-hook --measure``. One render: it is a size, not a timing.

The numbers are this machine's, on this disk, at this moment, and say so. ``fsync`` dominates
persist, and its cost differs by an order of magnitude between a laptop SSD, a CI runner and a
network disk.

Safety
------
It never writes into a folder that holds anything. Given ``--root``, the folder must be absent
or empty, and is kept afterwards so ``walle-hook --measure`` and ``walle-mcp --measure`` can be
run against it. Given nothing, it uses a fresh temporary folder and removes it, and only it.
"""

from __future__ import annotations

import math
import platform
import shutil
import statistics
import sys
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import click

from walle import hook, hot, index
from walle.events import ACTOR_USER, Assertion, Event, Supersede
from walle.reader import read_stream
from walle.resolver import resolve
from walle.writer import StreamWriter

PRINCIPAL = "bench"
DEVICE = "bench"
RETRACT_EVERY = 10
"""Every tenth event supersedes the assertion written just before it."""

_START = datetime(2026, 1, 1, tzinfo=UTC)
_FACTS = (
    "the recycling goes out on Tuesday night",
    "the dentist is on the second Thursday",
    "the bakery on the corner shuts at four",
    "piano practice is twenty minutes after school",
    "the boiler service is due in March",
    "the library books go back on Saturdays",
    "the plant by the window wants water weekly",
)


@dataclass(frozen=True)
class Timing:
    median_ms: float
    p95_ms: float
    samples: int


@dataclass(frozen=True)
class BenchReport:
    root: Path
    events: int
    live: int
    """Assertions still current after the fold."""
    persist: Timing
    replay: Timing
    recover: Timing
    state_equal: bool
    """Every recover run folded to the same state hash as the fold from the log."""
    hot_chars: int
    hot_tokens: int
    hot_mode: str
    machine: str


def _timing(samples_ns: list[int]) -> Timing:
    ms = sorted(s / 1e6 for s in samples_ns)
    p95 = ms[math.ceil(0.95 * len(ms)) - 1]
    return Timing(statistics.median(ms), p95, len(ms))


def _timed[T](work: Callable[[], T]) -> tuple[int, T]:
    started = time.perf_counter_ns()
    out = work()
    return time.perf_counter_ns() - started, out


def _event(n: int) -> Event:
    """The ``n``th synthetic event, 1-based. Its ``seq`` will be ``n``: one writer, one stream."""
    ts = (_START + timedelta(minutes=n)).isoformat(timespec="microseconds")
    if n % RETRACT_EVERY == 0:
        return Supersede(ts=ts, actor=ACTOR_USER, target=f"d-{DEVICE}:{n - 1}", reason="bench")
    fact = f"{_FACTS[n % len(_FACTS)]} (synthetic fact {n})"
    return Assertion(ts=ts, actor=ACTOR_USER, body=fact, summary=fact)


def machine() -> str:
    return (
        f"{platform.system()} {platform.machine()}, Python {platform.python_version()}, "
        f"{sys.implementation.name}"
    )


def _claim(root: Path | None) -> tuple[Path, bool]:
    """The folder to use, and whether this run made it temporarily and must remove it."""
    if root is None:
        return Path(tempfile.mkdtemp(prefix="walle-bench-")), True
    root = Path(root)
    if root.exists():
        if not root.is_dir():
            raise ValueError(f"{root} is not a folder")
        if any(root.iterdir()):
            raise ValueError(f"{root} is not empty; walle bench only writes into a fresh folder")
    root.mkdir(parents=True, exist_ok=True)
    return root, False


def measure(root: Path | None, *, events: int, runs: int) -> BenchReport:
    """Write ``events`` synthetic events into a fresh folder and time persist, replay, recover."""
    if events < 1:
        raise ValueError(f"events must be at least 1, not {events}")
    if runs < 1:
        raise ValueError(f"runs must be at least 1, not {runs}")
    folder, temporary = _claim(root)
    try:
        report = _measure(folder, events, runs)
    except BaseException:
        if temporary:
            # Best effort only here: the error that stopped the run is the one to report.
            shutil.rmtree(folder, ignore_errors=True)
        raise
    if temporary:
        # Not ignore_errors: the command says the folder was removed, so it must have been.
        shutil.rmtree(folder)
    return report


def _measure(root: Path, events: int, runs: int) -> BenchReport:
    writer = StreamWriter(root, PRINCIPAL, DEVICE)
    appends: list[int] = []
    for n in range(1, events + 1):
        elapsed, _ = _timed(lambda n=n: writer.append(_event(n)))
        appends.append(elapsed)

    replays: list[int] = []
    expected = ""
    live = 0
    for _ in range(runs):
        elapsed, resolution = _timed(lambda: resolve(read_stream(root, PRINCIPAL, DEVICE)))
        replays.append(elapsed)
        expected = index.state_hash(resolution)
        live = len(resolution.current)

    recovers: list[int] = []
    equal = True
    for _ in range(runs):
        elapsed, state = _timed(
            lambda: index.state_hash(resolve(index.rebuild(root, PRINCIPAL).records))
        )
        recovers.append(elapsed)
        equal = equal and state == expected

    rendered = hot.render(root, PRINCIPAL)
    text = hook.context(root, PRINCIPAL)
    return BenchReport(
        root=root,
        events=events,
        live=live,
        persist=_timing(appends),
        replay=_timing(replays),
        recover=_timing(recovers),
        state_equal=equal,
        hot_chars=len(text),
        hot_tokens=math.ceil(len(text) / hook.CHARS_PER_TOKEN),
        hot_mode=str(rendered.mode),
        machine=machine(),
    )


def _ms(timing: Timing) -> str:
    return f"median {timing.median_ms:.2f} ms, p95 {timing.p95_ms:.2f} ms"


@click.command("bench")
@click.option(
    "--events",
    type=int,
    default=1000,
    show_default=True,
    help="Synthetic events to write. Every tenth retracts the one before it.",
)
@click.option("--runs", type=int, default=5, show_default=True, help="Replay and recover runs.")
@click.option(
    "--root",
    type=click.Path(path_type=Path),
    default=None,
    help="An absent or empty folder to write into and keep. Default: a temporary one, removed.",
)
def bench_command(events: int, runs: int, root: Path | None) -> None:
    """Measure persist, replay and recover on synthetic data, on this machine (D69)."""
    try:
        report = measure(root, events=events, runs=runs)
    except (ValueError, OSError) as exc:
        raise click.ClickException(str(exc)) from exc
    where = str(report.root) if root is not None else "a temporary folder, now removed"
    click.echo(f"walle bench: {report.events:,} synthetic events in {where}")
    click.echo(f"persist   one fsync'd append: {_ms(report.persist)} ({report.persist.samples:,})")
    click.echo(
        f"replay    read and fold {report.events:,} events from the log: "
        f"{_ms(report.replay)} ({report.replay.samples} runs)"
    )
    click.echo(
        f"recover   delete the index, rebuild it from the log, fold: "
        f"{_ms(report.recover)} ({report.recover.samples} runs)"
    )
    click.echo(f"          state hash equal after rebuild: {'yes' if report.state_equal else 'NO'}")
    click.echo(
        f"hot tier  {report.live:,} live facts: {report.hot_chars:,} characters, about "
        f"{report.hot_tokens:,} tokens (characters / {hook.CHARS_PER_TOKEN}), "
        f"{report.hot_mode} mode"
    )
    click.echo(f"machine   {report.machine}")
    if not report.state_equal:
        raise SystemExit(1)
