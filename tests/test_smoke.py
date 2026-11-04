from click.testing import CliRunner

import walle
from walle.cli import main


def test_version_is_a_string() -> None:
    assert isinstance(walle.__version__, str)
    assert walle.__version__.count(".") == 2


def test_cli_reports_version() -> None:
    result = CliRunner().invoke(main, ["--version"])
    assert result.exit_code == 0
    assert walle.__version__ in result.output
