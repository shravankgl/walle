"""The `walle` command. Every later task adds a subcommand here."""

import logging
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

import click
from pydantic import ValidationError

from walle import __version__
from walle import acp as walle_acp
from walle import context as walle_context
from walle import curator as walle_curator
from walle import hot as walle_hot
from walle import index as walle_index
from walle import models as walle_models
from walle import observer as walle_observer
from walle import proposer as walle_proposer
from walle import staging as walle_staging
from walle import why as walle_why
from walle.evals import report as walle_evals
from walle.events import AnyEvent, Assertion, FutureSchemaError, Proposal, Supersede
from walle.paths import principal_dir
from walle.reader import CorruptStreamError
from walle.resolver import ResolutionError, resolve
from walle.writer import CorruptLogError, StreamWriter


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
            "it again, move your edit somewhere safe and delete the file. To change what it "
            "says, use `walle context`, which writes the change to the log.",
            err=True,
        )
    if report.preserved:
        raise SystemExit(1)


# --- the staging queue (task 9) ---------------------------------------------------------

_ROOT = click.option(
    "--root",
    type=click.Path(file_okay=False, path_type=Path),
    default=".",
    show_default=True,
    help="The folder that holds principals/.",
)
_DEVICE = click.option(
    "--device",
    required=True,
    help="This machine's stream, without the d- prefix. What this command writes goes there.",
)

# Everything reading or appending to the log can raise, said plainly rather than as a
# traceback. ValueError covers pydantic's ValidationError and an unsafe name.
_LOG_ERRORS = (
    walle_index.DanglingRefError,
    FutureSchemaError,
    CorruptStreamError,
    CorruptLogError,
    ResolutionError,
    OSError,
    ValueError,
)


@main.command("remember")
@_ROOT
@click.option("--principal", required=True, help="Whose memory, without the p- prefix.")
@_DEVICE
@click.option(
    "--summary",
    default=None,
    help=f"One line for the index, {walle_staging.SUMMARY_LIMIT} characters at most. "
    "Defaults to the text's first line.",
)
@click.argument("text")
def remember_command(
    root: Path, principal: str, device: str, summary: str | None, text: str
) -> None:
    """Stage TEXT as something to remember (D49, D86).

    It is not remembered yet: nothing staged reaches the hot tier until `walle review` keeps
    it.
    """
    try:
        item = walle_staging.remember(root, principal, device, text, summary=summary)
    except _LOG_ERRORS as exc:
        raise click.ClickException(_describe(exc)) from exc
    click.echo(f"{principal}: staged {item.ref}: {item.proposal.summary}")
    click.echo("Not in the hot tier until you keep it: run `walle review`.")


_CHOICES = {"k": "keep", "e": "edit", "d": "drop", "s": "skip", "q": "quit"}


def _choice(value: str) -> str:
    answer = value.strip().lower()
    for letter, word in _CHOICES.items():
        if answer in (letter, word):
            return letter
    raise click.UsageError("answer k(eep), e(dit), d(rop), s(kip) or q(uit)")


def _summary(value: str) -> str:
    try:
        return walle_staging.clean_summary(value)
    except ValueError as exc:
        raise click.UsageError(str(exc)) from exc


def _body(value: str) -> str:
    try:
        return walle_staging.clean_body(value)
    except ValueError as exc:
        raise click.UsageError(str(exc)) from exc


def _show(position: str, item: walle_staging.Staged) -> None:
    proposal = item.proposal
    click.echo(f"\n{position} {item.ref}, staged {proposal.ts[:10]} by {proposal.actor}")
    click.echo(f"  summary: {proposal.summary}")
    for line in proposal.body.splitlines():
        click.echo(f"  | {line}")
    if proposal.valid_from or proposal.valid_to:
        click.echo(f"  true from {proposal.valid_from or '?'} to {proposal.valid_to or 'now'}")


@main.command("review")
@_ROOT
@click.option("--principal", required=True, help="Whose staging queue, without the p- prefix.")
@_DEVICE
def review_command(root: Path, principal: str, device: str) -> None:
    """Keep, edit or drop each staged item, one batch (D49).

    A keep or an edit appends an assertion; a drop appends a supersede, and the dropped item
    stays in the log. Each answer is written as it is given, so quitting part-way keeps the
    answers already made.
    """
    try:
        walle_staging.check_device(device)
        items = walle_staging.pending(root, principal)
    except _LOG_ERRORS as exc:
        raise click.ClickException(_describe(exc)) from exc
    if not items:
        click.echo(f"{principal}: nothing staged.")
        return

    click.echo(f"{principal}: {_plural(len(items), 'item')} staged.")
    done = {"k": 0, "e": 0, "d": 0, "s": 0, "elsewhere": 0}
    reviewed = 0

    def report() -> None:
        line = (
            f"\n{principal}: kept {done['k']}, edited {done['e']}, dropped {done['d']}, "
            f"skipped {done['s']}"
        )
        if done["elsewhere"]:
            line += f", {done['elsewhere']} already decided elsewhere"
        if reviewed < len(items):
            line += f", {len(items) - reviewed} not reached"
        click.echo(line + ".")
        if done["k"] or done["e"]:
            click.echo("Run `walle render` to put what you kept in the hot tier.")

    for position, item in enumerate(items, start=1):
        _show(f"[{position}/{len(items)}]", item)
        try:
            answer = click.prompt(
                "keep, edit, drop, skip or quit? [k/e/d/s/q]",
                value_proc=_choice,
                prompt_suffix=" ",
            )
        except click.Abort:
            report()  # what was already answered is in the log; say so before stopping
            raise
        if answer == "q":
            break
        if answer == "s":
            reviewed += 1
            done["s"] += 1
            click.echo("  skipped; still staged")
            continue
        try:
            if answer == "k":
                new = walle_staging.keep(root, principal, device, item.ref)
                click.echo(f"  kept as {new}")
            elif answer == "e":
                click.echo("  Enter keeps the current text.")
                summary = click.prompt(
                    "  summary", default=item.proposal.summary, value_proc=_summary
                )
                body = click.prompt(
                    "  body", default=item.proposal.body, show_default=False, value_proc=_body
                )
                new = walle_staging.edit(
                    root, principal, device, item.ref, body=body, summary=summary
                )
                click.echo(f"  edited, kept as {new}")
            else:
                new = walle_staging.drop(root, principal, device, item.ref)
                click.echo(f"  dropped {item.ref}: {new} retires it; it stays in the log")
        except walle_staging.NotStagedError as exc:
            reviewed += 1
            done["elsewhere"] += 1
            click.echo(f"  {exc}; left as it is")
            continue
        except click.Abort:
            report()
            raise
        except _LOG_ERRORS as exc:
            report()
            raise click.ClickException(f"{item.ref} was not decided: {_describe(exc)}") from exc
        reviewed += 1
        done[answer] += 1

    report()


# --- forget, why and context (task 12) --------------------------------------------------


@main.command("forget")
@_ROOT
@click.option("--principal", required=True, help="Whose memory, without the p- prefix.")
@_DEVICE
@click.option(
    "--valid-to",
    default=None,
    help="When it stopped being true, if you know: YYYY-MM-DD or an RFC 3339 instant (D93).",
)
@click.option("--reason", default=None, help="A note kept with the retraction.")
@click.argument("ref")
def forget_command(
    root: Path, principal: str, device: str, valid_to: str | None, reason: str | None, ref: str
) -> None:
    """Forget the current assertion REF: append a supersede that retires it.

    Nothing is deleted. REF stays in the log, and `walle why REF` still explains it.
    """
    try:
        retire = walle_context.forget(
            root, principal, device, ref, valid_to=valid_to, reason=reason
        )
    except (walle_context.NotCurrentError, *_LOG_ERRORS) as exc:
        raise click.ClickException(_describe(exc)) from exc
    click.echo(f"{principal}: forgot {ref}: {retire} retires it, and it stays in the log.")
    click.echo(f"`walle why {ref}` still explains it. Run `walle render` to update the hot tier.")


def _gist(event: AnyEvent) -> str:
    """One line saying what an event holds, for `walle why`."""
    if isinstance(event, Assertion | Proposal):
        text = event.summary
    elif isinstance(event, Supersede):
        text = f"retracts {event.target}" + (f": {event.reason}" if event.reason else "")
    else:
        text = getattr(event, "body", None) or getattr(event, "note", None) or ""
    line = " ".join(str(text).split())
    return line if len(line) <= 120 else line[:119] + "…"


@main.command("why")
@_ROOT
@click.option("--principal", required=True, help="Whose log, without the p- prefix.")
@click.argument("ref")
def why_command(root: Path, principal: str, ref: str) -> None:
    """Explain REF: the full caused_by chain behind it, and what became of each step (D59).

    A forgotten or edited assertion is still explained, with what retired it.
    """
    try:
        explanation = walle_why.explain(root, principal, ref)
    except KeyError as exc:
        raise click.ClickException(str(exc.args[0])) from exc
    except (walle_index.StaleIndexError, *_LOG_ERRORS) as exc:
        raise click.ClickException(_describe(exc)) from exc
    for step in explanation.steps:
        indent = "  " * step.depth
        event = step.event
        head = f"{indent}{step.ref} {event.type} {event.ts[:10]} by {event.actor}"
        click.echo(head + (f": {step.status}" if step.status else ""))
        if gist := _gist(event):
            click.echo(f"{indent}  | {gist}")


def _keep_unsaved(root: Path, principal: str, text: str) -> str:
    """Put a refused save where the person can find it. Returns where, or why it could not."""
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    try:
        fd, path = tempfile.mkstemp(
            prefix=f"context-unsaved-{stamp}-", suffix=".md", dir=principal_dir(root, principal)
        )
        with open(fd, "w", encoding="utf-8") as f:
            f.write(text)
    except OSError as exc:
        return f"nowhere: it could not be saved ({exc}). Here it is:\n{text}"
    return path


@main.command("context")
@_ROOT
@click.option("--principal", required=True, help="Whose memory, without the p- prefix.")
@_DEVICE
def context_command(root: Path, principal: str, device: str) -> None:
    """Edit what walle believes in $EDITOR; the changes are appended to the log (D107).

    Change an entry to edit it, delete it to forget it, or head a new one [#new]. Every
    change is a new event; nothing in the log is edited or deleted.
    """
    try:
        walle_staging.check_device(device)
        view = walle_context.open_view(root, principal)
    except _LOG_ERRORS as exc:
        raise click.ClickException(_describe(exc)) from exc

    edited = click.edit(view.text, extension=".md", require_save=True)
    if edited is None or not edited.strip():
        click.echo(f"{principal}: no changes saved; nothing written.")
        return

    try:
        applied = walle_context.apply(root, principal, device, view, edited)
    except walle_context.PartialWriteError as exc:
        kept = _keep_unsaved(root, principal, edited)
        raise click.ClickException(f"{exc}\nYour edited text is in {kept}") from exc
    except (walle_context.StaleViewError, *_LOG_ERRORS) as exc:
        kept = _keep_unsaved(root, principal, edited)
        raise click.ClickException(
            f"{_describe(exc)}\nNothing was written. Your edited text is in {kept}"
        ) from exc

    if not applied.written:
        click.echo(f"{principal}: nothing changed; nothing written.")
        return
    click.echo(
        f"{principal}: edited {len(applied.edited)}, forgot {len(applied.forgotten)}, "
        f"added {len(applied.added)}."
    )
    for old, new, retire in applied.edited:
        click.echo(
            f"  edited {old} as {new}; {retire} retires the old text, which stays in the log"
        )
    for ref, retire in applied.forgotten:
        click.echo(f"  forgot {ref}; {retire} retires it, and it stays in the log")
    for ref in applied.added:
        click.echo(f"  added {ref}")
    click.echo("Run `walle render` to put this in the hot tier.")


# --- the proposer (tasks 16-18) ---------------------------------------------------------


def _model_name(_ctx: click.Context, _param: click.Parameter, value: str) -> str:
    """``--model``: ``fake`` or ``provider:model``. Only the name is checked here; the client
    is built when a run needs one, so ``--score`` reads the shadow log with no provider."""
    if value != "fake":
        try:
            walle_models.ProviderModel(value)
        except ValueError as exc:
            raise click.BadParameter(f"{exc} (e.g. `ollama:llama3.1:8b`, D87)") from None
    return value


@main.command("propose")
@_ROOT
@click.option("--principal", required=True, help="Whose log to read, without the p- prefix.")
@_DEVICE
@click.option(
    "--model",
    required=True,
    callback=_model_name,
    help="`fake` (deterministic, offline), or provider:model through aisuite: "
    "`ollama:<model>` for the local tier, `openrouter:<author>/<model>:free`, "
    "`openai:<model>`. The provider's key comes from its own environment variable. "
    "A failed call is not retried.",
)
@click.option(
    "--score",
    is_flag=True,
    help="Propose nothing; print how the model's shadow verdicts compare with your reviews.",
)
def propose_command(root: Path, principal: str, device: str, model: str, score: bool) -> None:
    """Read the log and stage what is worth remembering; judge the queue in shadow (D44, D82).

    Proposals go to the same queue as `walle remember`, and only `walle review` decides
    them. The shadow verdicts are recorded beside the log and act on nothing. A run with
    nothing new prints nothing (D63).
    """
    try:
        if score:
            result = walle_proposer.score(root, principal, model=model)
            click.echo(
                f"{principal}: shadow score for {result.model}: {result.decided} decided "
                f"({result.agree} agree, {result.false_allow} would have kept a dropped item, "
                f"{result.missed} would have dropped a kept item), {result.pending} still staged."
            )
            return
        report = walle_proposer.propose(root, principal, device, walle_models.resolve(model))
    except (
        walle_models.ModelUnavailable,
        walle_proposer.ModelReplyError,
        walle_proposer.ModelCallError,
    ) as exc:
        raise click.ClickException(f"nothing written: {exc}") from None
    except _LOG_ERRORS as exc:
        raise click.ClickException(_describe(exc)) from exc

    for stream, skip in report.skips:
        click.echo(f"skipped {stream} line {skip.line}: {skip.reason}", err=True)
    if report.silent:
        return
    if report.proposed:
        click.echo(
            f"{principal}: proposed {_plural(len(report.proposed), 'item')} to the staging "
            f"queue: {', '.join(report.proposed)}."
        )
    else:
        # Not silent, so something below needs saying: a refused reply, a race, a shadow
        # problem. "Nothing new" on its own is the report D63 says to skip.
        click.echo(f"{principal}: nothing staged this run.")
    if report.withheld:
        kinds = ", ".join(f"{w.ref} ({', '.join(w.hazards)})" for w in report.withheld)
        click.echo(
            f"  withheld {_plural(len(report.withheld), 'observation')} from the model: {kinds}"
        )
    if report.dropped:
        click.echo(
            f"  refused {_plural(len(report.dropped), 'reply item')}: {', '.join(report.dropped)}"
        )
    if report.already_proposed:
        click.echo(f"  {report.already_proposed} already proposed by another run meanwhile")
    if report.verdicts:
        click.echo(
            f"  shadow: recorded {_plural(report.verdicts, 'verdict')} for {report.model}; "
            "they act on nothing (D82)."
        )
    if report.shadow_error:
        click.echo(f"  shadow: the shadow log is incomplete: {report.shadow_error}", err=True)
    if report.proposed:
        click.echo("Not in the hot tier until you keep them: run `walle review`.")


# --- the Curator, propose-only (issue #55) ------------------------------------------------


def _curator_model_name(ctx: click.Context, param: click.Parameter, value: str) -> str:
    """``--model`` for the Curator: the proposer's rule, then D24's: never the local tier."""
    name = _model_name(ctx, param, value)
    try:
        walle_curator.refuse_local(name)
    except walle_curator.LocalModelRefused as exc:
        raise click.BadParameter(str(exc)) from None
    return name


@main.command("curate")
@_ROOT
@click.option("--principal", required=True, help="Whose beliefs to read, without the p- prefix.")
@_DEVICE
@click.option(
    "--model",
    required=True,
    callback=_curator_model_name,
    help="`fake` (deterministic, offline), or a frontier model as provider:model through "
    "aisuite. The local tier (`ollama:`) is refused: the Curator never runs on it (D24).",
)
def curate_command(root: Path, principal: str, device: str, model: str) -> None:
    """Read the current beliefs; stage what the model would fuse, record what contradicts
    (D24, D29, D58).

    Propose-only: consolidations go to the same queue as `walle remember`, and only `walle
    review` decides them. A contradiction is recorded for you to settle, never resolved
    here. No assertion is written. A run with nothing to say prints nothing (D63).
    """
    try:
        report = walle_curator.curate(root, principal, device, walle_curator.resolve_model(model))
    except walle_curator.LocalModelRefused as exc:
        raise click.UsageError(str(exc)) from None
    except (
        walle_models.ModelUnavailable,
        walle_proposer.ModelReplyError,
        walle_proposer.ModelCallError,
        walle_curator.CuratorError,
    ) as exc:
        raise click.ClickException(f"nothing written: {exc}") from None
    except _LOG_ERRORS as exc:
        raise click.ClickException(_describe(exc)) from exc

    for stream, skip in report.skips:
        click.echo(f"skipped {stream} line {skip.line}: {skip.reason}", err=True)
    if report.silent:
        return
    said: list[str] = []
    if report.proposed:
        said.append(
            f"proposed {_plural(len(report.proposed), 'consolidation')} to the staging queue: "
            f"{', '.join(report.proposed)}"
        )
    if report.contradictions:
        said.append(
            f"recorded {_plural(len(report.contradictions), 'contradiction')}: "
            f"{', '.join(report.contradictions)}"
        )
    click.echo(f"{principal}: {'; '.join(said) if said else 'nothing written this run'}.")
    if report.withheld:
        kinds = ", ".join(f"{w.ref} ({', '.join(w.hazards)})" for w in report.withheld)
        click.echo(
            f"  withheld {_plural(len(report.withheld), 'assertion')} from the model: {kinds}"
        )
    if report.dropped:
        click.echo(
            f"  refused {_plural(len(report.dropped), 'reply item')}: {', '.join(report.dropped)}"
        )
    if report.already:
        click.echo(f"  {report.already} already written by another run meanwhile")
    if report.proposed:
        click.echo("Not in the hot tier until you keep them: run `walle review`.")
    if report.contradictions:
        click.echo("A contradiction is recorded, not resolved: `walle forget` the wrong side.")


# --- the transcript observer (issue #40) ------------------------------------------------


def _without_content(exc: BaseException) -> str:
    """Like ``_describe``, minus anything pydantic quotes from its input.

    The stream holds transcript text, and pydantic's message for a line it refuses quotes the
    offending value. So a validation failure is said as where it is (the file and line, from
    the reader's own words before " — ") and which field failed in what way, by pydantic's
    error code. Never by its message, which can name an input value too ("Input tag 'x'").
    """
    validation = exc if isinstance(exc, ValidationError) else exc.__cause__
    if not isinstance(validation, ValidationError):
        return _describe(exc)
    problems = "; ".join(
        f"{'.'.join(str(part) for part in error['loc']) or 'line'}: {error['type']}"
        for error in validation.errors(include_url=False, include_input=False)
    )
    if validation is exc:
        where = validation.title
    else:  # the reader writes "<file>:<line>: … — <pydantic's message>"; keep only the left
        head, dash, _ = str(exc).partition(" — ")
        where = head if dash else type(exc).__name__
    return "\n".join([f"{where} ({problems})", *getattr(exc, "__notes__", [])])


@contextmanager
def _quiet_transcript_warnings() -> Iterator[None]:
    """Keep the transcript reader's warnings off the terminal for one command.

    They name transcript files and uuids, one line per skipped line, repair and conflicting
    duplicate, and a real folder has hundreds. With no logging configured, Python prints them
    to stderr through its last-resort handler. A ``NullHandler`` stops that and nothing else:
    the records still propagate to any handler the caller did configure. What they report is
    in the counts.
    """
    log = logging.getLogger("walle.transcript")
    handler = logging.NullHandler()
    log.addHandler(handler)
    try:
        yield
    finally:
        log.removeHandler(handler)


@main.command("observe")
@_ROOT
@click.option("--principal", required=True, help="Whose log, without the p- prefix.")
@_DEVICE
@click.option(
    "--projects-dir",
    required=True,
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    help="The Claude Code projects folder to read. Required, with no default: walle reads "
    "only a folder you name.",
)
def observe_command(root: Path, principal: str, device: str, projects_dir: Path) -> None:
    """Append one observation per completed turn in the transcripts under --projects-dir.

    Safe to re-run: a turn the stream already holds is not appended again, and two runs at
    once with the same --device append each turn once (D92). A turn still in progress is
    pending and is appended by a later run (D56). Only counts are printed, never transcript
    text.
    """
    try:
        writer = StreamWriter(root, principal, device)  # an unsafe name fails before any read
        with _quiet_transcript_warnings():
            report = walle_observer.observe_projects(projects_dir)
        result = walle_observer.append_observations(report, writer)
    except _LOG_ERRORS as exc:
        raise click.ClickException(_without_content(exc)) from exc

    transcripts = report.transcripts
    click.echo(f"{principal}: observed transcripts into {writer.stream}")
    click.echo(f"  turns seen: {len(report.turns) + len(report.pending)}")
    click.echo(f"  appended: {len(result.appended)}")
    click.echo(f"  already logged: {len(result.already_logged)}")
    click.echo(f"  pending: {len(report.pending)}")
    click.echo(f"  skipped lines: {len(transcripts.skips)}")
    click.echo(f"  duplicate entries: {len(transcripts.duplicates)}")


# --- the ACP client (task 56) ------------------------------------------------------------------


@main.command("acp", context_settings={"ignore_unknown_options": True})
@_ROOT
@click.option("--principal", required=True, help="Whose memory, without the p- prefix.")
@click.option(
    "--cwd",
    type=click.Path(file_okay=False, exists=True, path_type=Path),
    default=".",
    show_default=True,
    help="The session's working directory, handed to the host.",
)
@click.option(
    "--timeout",
    type=click.FloatRange(min=0, min_open=True),
    default=walle_acp.TIMEOUT,
    show_default=True,
    help="Seconds the host may take to initialize and open the session.",
)
@click.argument("command", nargs=-1, required=True, type=click.UNPROCESSED)
def acp_command(
    root: Path, principal: str, cwd: Path, timeout: float, command: tuple[str, ...]
) -> None:
    """Run an ACP host (Codex, Gemini CLI) as a subprocess with the hot tier in front (D83).

    COMMAND is the host and its arguments, after `--`. Each line on stdin is one turn; the
    host's text comes back on stdout. The first turn carries the hot tier first, inside its
    untrusted envelope (D55). With no hot tier the session runs without memory and says so.
    """
    try:
        memory = walle_acp.memory(root, principal)
    except ValueError as exc:  # an unsafe principal name
        raise click.ClickException(str(exc)) from exc
    if memory.text is None:
        click.echo(
            f"{principal}: no hot tier this session ({memory.why}); the host runs without "
            f"memory. `walle render --principal {principal}` makes one.",
            err=True,
        )
    try:
        host = walle_acp.Host.start(command, timeout=timeout)
    except OSError as exc:
        why = exc.strerror or type(exc).__name__
        raise click.ClickException(f"could not start {command[0]}: {why}") from exc
    except walle_acp.AcpError as exc:
        raise click.ClickException(str(exc)) from exc
    with host:
        try:
            session = host.new_session(cwd, memory=memory, timeout=timeout)
        except walle_acp.AcpError as exc:
            raise click.ClickException(str(exc)) from exc
        for line in sys.stdin:
            text = line.rstrip("\n")
            if not text.strip():
                continue
            try:
                turn = session.prompt(text)
            except walle_acp.HostError as exc:
                click.echo(str(exc), err=True)  # the host is alive; the next line is sent
                continue
            except walle_acp.AcpError as exc:
                raise click.ClickException(str(exc)) from exc
            click.echo(turn.text)


# --- the evals (tasks 19-21) ---------------------------------------------------------------

main.add_command(walle_evals.eval_command)
