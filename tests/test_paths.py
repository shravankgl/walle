"""Path construction and the validation that keeps names inside the tree (D85)."""

from __future__ import annotations

from pathlib import Path

import pytest

from walle.paths import check_name, log_dir, principal_dir, stream_id, stream_path

ROOT = Path("/tmp/walle-root")


def test_prefixes_are_added_here_not_accepted() -> None:
    """A caller passes `maya`; this module produces `p-maya`.

    Taking the prefixed form would invite `p-p-maya`, and would make every call site
    responsible for a rule only one of them remembers.
    """
    assert principal_dir(ROOT, "maya").name == "p-maya"
    assert stream_id("mbp") == "d-mbp"
    assert stream_path(ROOT, "maya", "mbp").name == "d-mbp.jsonl"


def test_the_layout_is_the_one_the_architecture_draws() -> None:
    assert stream_path(ROOT, "maya", "mbp") == (
        ROOT / "principals" / "p-maya" / "log" / "d-mbp.jsonl"
    )
    assert log_dir(ROOT, "maya") == ROOT / "principals" / "p-maya" / "log"


def test_an_already_prefixed_name_is_rejected() -> None:
    """`p-maya` has a `-` after a letter, which is legal — so this is caught by the result
    being `p-p-maya`, not by the name rule. Asserted so the double-prefix stays visible."""
    assert principal_dir(ROOT, "p-maya").name == "p-p-maya"


@pytest.mark.parametrize(
    "name",
    [
        "..",
        "../etc",
        "a/b",
        "a\\b",
        "Maya",
        "",
        " ",
        "-leading",
        "_leading",
        ".hidden",
        "has.dot",
        "m" * 64,
        "tab\there",
        "null\x00byte",
        "emoji🙂",
    ],
    ids=[
        "dotdot",
        "traversal",
        "slash",
        "backslash",
        "uppercase",
        "empty",
        "space",
        "leading-dash",
        "leading-underscore",
        "hidden",
        "dot",
        "too-long",
        "tab",
        "nul",
        "non-ascii",
    ],
)
def test_unusable_names_are_rejected(name: str) -> None:
    """Rejected, never sanitized: sanitizing turns a bad name into a valid path somewhere
    unintended, while rejecting turns it into an error message (D85, D109)."""
    with pytest.raises(ValueError, match="not a usable"):
        check_name(name, kind="principal")


@pytest.mark.parametrize("name", ["maya", "m", "maya-2", "maya_2", "0", "m" * 63])
def test_ordinary_names_are_accepted(name: str) -> None:
    assert check_name(name, kind="principal") == name


def test_the_error_says_which_kind_of_name_was_wrong() -> None:
    with pytest.raises(ValueError, match="device"):
        stream_id("../mbp")


@pytest.mark.parametrize(
    "name", ["maya\n", "maya\r\n", "maya\n\n", "\nmaya"], ids=["lf", "crlf", "two-lf", "leading"]
)
def test_a_trailing_newline_does_not_sneak_through(name: str) -> None:
    """Python's `$` matches before a trailing newline, so `^...$` with `.match` accepted
    "maya\\n" and built the directory `p-maya\\n`. Every validator here uses `fullmatch`."""
    with pytest.raises(ValueError, match="not a usable"):
        check_name(name, kind="principal")
