"""The `walle` command. Every later task adds a subcommand here."""

from pathlib import Path

import click

from walle import __version__
from walle import hot as walle_hot
from walle import index as walle_index
from walle.events import FutureSchemaError
from walle.reader import CorruptStreamError
from walle.resolver import ResolutionError, resolve


@click.group(invoke_without_command=True)
@click.version_option(__version__, prog_name="walle")
@click.pass_context
def main(ctx: click.Context) -> None:
    """Append-only memory for AI agent harnesses."""
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


def _describe(exc: BaseException) -> str:
    """The exception's message with its notes, which carry the file and the build (D111)."""
    return "\n".join([str(exc), *getattr(exc, "__notes__", [])])


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


@main.command("rebuild")
@click.option(
    "--root",
    type=click.Path(file_okay=False, path_type=Path),
    default=".",
    show_default=True,
    help="The folder that holds principals/.",
)
@click.option("--principal", required=True, help="Whose index, without the p- prefix.")
@click.option("--full", is_flag=True, help="Discard the index and rebuild it from the log alone.")
def rebuild_command(root: Path, principal: str, full: bool) -> None:
    """Bring the SQLite index up to date with the log (D10, D96).

    Incremental by default: only seqs after each stream's tracking record are read in. The
    log is the truth, so the index can always be deleted and rebuilt with --full.
    """
    build = walle_index.rebuild if full else walle_index.update
    try:
        report = build(root, principal)
    except (
        walle_index.DanglingRefError,
        FutureSchemaError,
        CorruptStreamError,
        FileNotFoundError,
        ValueError,
    ) as exc:
        raise click.ClickException(_describe(exc)) from exc

    for stream, skip in report.skips:
        click.echo(f"skipped {stream} line {skip.line}: {skip.reason}", err=True)
    streams = _plural(len(report.streams), "stream")
    if report.full:
        click.echo(
            f"{principal}: rebuilt the index from nothing ({report.reason}): "
            f"{_plural(report.events, 'event')} from {streams}."
        )
    else:
        click.echo(
            f"{principal}: indexed {_plural(len(report.added), 'new event')} (incremental); "
            f"{_plural(report.events, 'event')} from {streams}."
        )

    try:
        # The rows this build committed, read before it released index.lock. Reading the
        # index again here could already show another builder's work, and the hash would
        # disagree with the counts printed above.
        state = walle_index.state_hash(resolve(report.records))
    except ResolutionError as exc:
        raise click.ClickException(
            f"the index is up to date with the log, but the log does not fold: {exc}"
        ) from exc
    click.echo(f"resolved state: {state}")


@main.command("render")
@click.option(
    "--root",
    type=click.Path(file_okay=False, path_type=Path),
    default=".",
    show_default=True,
    help="The folder that holds principals/.",
)
@click.option("--principal", required=True, help="Whose hot tier, without the p- prefix.")
def render_command(root: Path, principal: str) -> None:
    """Render the hot tier from the log into principals/p-<name>/hot/ (D64, D81, D107).

    The rendered files are read-only. A file edited since walle wrote it is kept, reported,
    and not rendered over; the command then exits 1. rules.md is never touched.
    """
    try:
        report = walle_hot.render(root, principal)
    except (
        walle_index.DanglingRefError,
        FutureSchemaError,
        CorruptStreamError,
        ResolutionError,
        OSError,  # a read-only hot/ folder, a full disk: said plainly, not as a traceback
        ValueError,
    ) as exc:
        raise click.ClickException(_describe(exc)) from exc

    for stream, skip in report.skips:
        click.echo(f"skipped {stream} line {skip.line}: {skip.reason}", err=True)
    if report.manifest_note:
        click.echo(report.manifest_note, err=True)

    shape = (
        "every one in full"
        if report.mode is walle_hot.Mode.FULL
        else f"{report.in_full} in full, {report.summarised} as one-line summaries"
    )
    click.echo(
        f"{principal}: hot tier for {_plural(report.assertions, 'assertion')}, "
        f"{report.mode} mode, {shape}."
    )
    click.echo(f"  written: {', '.join(report.written) or 'nothing'}")
    click.echo(f"  unchanged: {', '.join(report.unchanged) or 'nothing'}")
    if report.preserved:
        # On stdout too: the line above must not read as if every file now holds this render.
        names = ", ".join(kept.name for kept in report.preserved)
        click.echo(f"  kept as edited, not this render: {names}")
    rules = "present, left alone" if report.rules_present else "none yet"
    click.echo(f"  {walle_hot.RULES}: {rules}")
    click.echo(f"resolved state: {report.state}")

    for kept in report.preserved:
        click.echo(
            f"kept your edit to {kept.path}: {kept.reason}, so it was not rendered over. "
            "The hot tier carries your version until you deal with it. To let walle render "
            "it again, move your edit somewhere safe and delete the file. Edits made through "
            "`walle context` are not built yet.",
            err=True,
        )
    if report.preserved:
        raise SystemExit(1)
