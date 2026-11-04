"""The D77 corpora as the harness reads them.

``tests/corpora/generate.py`` writes them from templates; this reads them back, strictly,
against a schema with the same fields and the same ties between them. The two are kept in
agreement by a test, not by one importing the other: the generator is a test-side script,
and nothing under ``src/`` imports from ``tests/``.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

CORPORA: tuple[str, ...] = ("benign", "dangerous", "injection")

VERDICT: dict[str, str] = {"benign": "allow", "dangerous": "flag", "injection": "refuse"}
"""What each corpus expects of every item (D77)."""

HAZARDS: dict[str, tuple[str, ...]] = {
    "benign": ("none",),
    "dangerous": ("credential", "secret", "third-party-pii"),
    "injection": ("planted-instruction", "forged-envelope", "false-approval"),
}

FORGED_HAZARD = "forged-envelope"
"""The injection hazard whose markers must come back defaced, not merely enveloped."""

_ID_RE = re.compile(r"(benign|dangerous|injection)-[a-z-]+-\d{3}")
_FAKE_RE = re.compile(r"FAKE-[A-Z]+-NOT-REAL-\d{4}")

Role = Literal["user", "assistant", "tool"]


class Turn(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    role: Role
    text: str = Field(min_length=1)


class Item(BaseModel):
    """One corpus item. The same shape as the generator's, and the same ties."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    v: Literal[1]
    id: str
    corpus: Literal["benign", "dangerous", "injection"]
    template: str = Field(min_length=1)
    hazard: str
    expected: Literal["allow", "flag", "refuse"]
    synthetic: Literal[True]
    note: str
    turns: list[Turn] = Field(min_length=1)
    markers: list[str]

    @model_validator(mode="after")
    def _consistent(self) -> Item:
        if self.expected != VERDICT[self.corpus]:
            raise ValueError(f"a {self.corpus} item expects {VERDICT[self.corpus]!r}")
        if self.hazard not in HAZARDS[self.corpus]:
            raise ValueError(f"hazard {self.hazard!r} does not belong in {self.corpus}")
        if not _ID_RE.fullmatch(self.id) or not self.id.startswith(
            f"{self.corpus}-{self.template}-"
        ):
            raise ValueError("id must be <corpus>-<template>-<nnn>")
        if bool(self.markers) == (self.corpus == "benign"):
            raise ValueError("markers are required outside benign and forbidden inside it")
        if len(set(self.markers)) != len(self.markers):
            raise ValueError("markers repeat")
        for marker in self.markers:
            if not marker or not any(marker in turn.text for turn in self.turns):
                raise ValueError(f"marker {marker!r} is not in any turn")
        if self.hazard in ("credential", "secret"):
            for marker in self.markers:
                if not _FAKE_RE.fullmatch(marker):
                    raise ValueError(f"credential {marker!r} is not shaped {_FAKE_RE.pattern}")
        return self

    @property
    def transcript(self) -> str:
        """Every turn as the observer would write it, ``role: text`` per line, tool turns
        included: a payload must be caught wherever it sits."""
        return "\n".join(f"{turn.role}: {turn.text}" for turn in self.turns)


def load(path: Path) -> tuple[Item, ...]:
    """Every item in one corpus file, strictly, or ``ValueError`` naming ``file:line``.

    The file's name says which corpus it is, and every item must agree.
    """
    path = Path(path)
    corpus = path.stem
    if corpus not in CORPORA:
        raise ValueError(f"{path}: not a corpus file; expected one of {', '.join(CORPORA)}")
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"{path}: not UTF-8 ({exc.reason})") from exc
    items: list[Item] = []
    for number, line in enumerate(text.split("\n"), start=1):
        if not line:
            continue
        try:
            item = Item.model_validate(json.loads(line))
        except ValueError as exc:
            raise ValueError(f"{path}:{number}: {exc}") from exc
        if item.corpus != corpus:
            raise ValueError(f"{path}:{number}: a {item.corpus} item in {corpus}.jsonl")
        items.append(item)
    if not items:
        raise ValueError(f"{path}: no items")
    return tuple(items)


def load_all(folder: Path) -> dict[str, tuple[Item, ...]]:
    """The three corpora from ``folder``, by name, in D77's order."""
    return {name: load(Path(folder) / f"{name}.jsonl") for name in CORPORA}
