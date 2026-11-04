"""The `walle` command. Every later task adds a subcommand here."""

import click

from walle import __version__


@click.group(invoke_without_command=True)
@click.version_option(__version__, prog_name="walle")
@click.pass_context
def main(ctx: click.Context) -> None:
    """Append-only memory for AI agent harnesses."""
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())
