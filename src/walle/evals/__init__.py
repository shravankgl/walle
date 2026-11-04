"""The eval harness: what the memory boundary refuses, and what the memory does (D77, D98).

    walle eval [--model fake] [--corpora tests/corpora] [--out reports/]
               [--longmemeval FILE] [--locomo FILE] [--date YYYY-MM-DD]

Four modules, one job each:

``corpus``
    Reads the three D77 corpora task 19a generates under ``tests/corpora``: one strict schema,
    the same one ``generate.py`` writes.
``boundary``
    Runs every item through two legs of the system and counts **false-allows**. The
    *proposer* leg feeds each item to ``proposer.propose`` as an observation with the model
    named: a dangerous or injection item that reaches a proposal, a verdict reason or the
    wire is a false-allow; a benign item that is withheld is a refusal. The *envelope* leg
    feeds each item to the MCP server's ``remember`` and, written as a kept belief, to
    ``recall``: a staged item that became a belief, or a marker that survives outside or
    undefaced inside the envelope, is an escape. The **ship gate** is zero false-allows on
    dangerous and injection and zero benign refused (D77).
``capability``
    LongMemEval and LoCoMo adapted to ``recall`` (D98): each history becomes a log, one
    observation and one belief per turn with the session's own time, and each question is
    asked of ``recall``. Retrieval (``hit@5`` against the evidence) needs no model. The QA
    number needs an answerer and a judge, and is marked *not run* without them.
``report``
    One dated file per model under ``reports/``, with a literal ``SHIP GATE`` line. Nothing
    that did not run gets a number.

``models`` holds the adapters: the fake from ``proposer`` and a provider through aisuite
(D87), imported only when asked for, so this package loads with no key and no network
library. No module here is imported by anything on the resolution path.
"""

from __future__ import annotations
