# Task 4 follow-up: the reader refuses a non-integer or out-of-range seq

Issue #20, from the independent review of PR #19. Branch `task/04-seq`.

**Shipped:**
- `src/walle/reader.py`: `_record` now checks the *raw* `seq` from the file against the
  writer's rule in `writer._seq_of`: a JSON integer, not a bool, at least 1. A `seq` too
  large for a ref (more than 19 digits) is now a `CorruptStreamError` naming `file:line`. It
  used to escape from `make_ref` as a bare `ValueError`. I also updated the module docstring's
  "everything else is fatal" bullet to list these cases.
- `tests/test_reader.py`: eight new tests.
  - The four rows from the issue (`"2"`, `true`, `2.0`, `10000000000000000000`), parametrised.
  - The range edge: the largest seq a ref can carry still loads.
  - A guard that D111 keeps priority over the new check.
  - Two tests that pin `Skip.offset` and `Skip.preview`.

No change to `events.py`, `writer.py` or any fixture.

**Why:**
- **D80, and `_record`'s own rule.** A complete line that is not a valid event is fatal, not
  skipped. Pydantic validates `seq: int | None = Field(ge=1)` in lax mode. So `"2"` and `2.0`
  came out as 2 and `true` came out as 1, and a corrupt line loaded as if it were fine.
- **`true` was worse than the issue's table shows.** Coerced to 1 on line 2, it matched seq 1
  on line 1. The adjacent-duplicate rule (rule 3) then *dropped the real line 1* as
  "unacknowledged" and reported a misleading skip. That is a real record silently lost. I
  reproduced it on `main` at `414155b` before writing any code.
- **Hard constraint 5.** The rebuild test reads the log through this module. If the reader is
  laxer than the writer, a rebuild can load lines the writer would refuse to number from.
- **D111.** `parse_event` still runs before the seq check, so a newer-schema event halts as
  `FutureSchemaError` by name, even if its seq looks odd to this build.

**Verify:** the criterion in issue #20 is "each of the four rows raises `CorruptStreamError`
with `file:line`, matching `writer.py`'s `_seq_of` rule; a test asserts `Skip.offset` and
`Skip.preview` for all three skip branches".
- `test_a_seq_that_is_not_a_json_integer_in_range_halts_naming_the_line[string|bool|float|out-of-range]`
  writes seq 1, the bad seq on line 2, then seq 3. It asserts
  `CorruptStreamError` matching `d-mbp\.jsonl:2: .*seq`. I wrote it first. Against the
  unchanged `reader.py`, `tests/test_reader.py` gave **4 failed, 25 passed**:
  - `"2"` and `2.0` loaded.
  - `true` loaded, and line 1 was dropped.
  - The big int raised a bare `ValueError`.
- `test_each_skip_reports_the_byte_offset_and_preview_of_its_line` builds one synthetic file
  that hits all three skip branches, in this order:
  1. a mid-file fragment (line 2);
  2. an unacknowledged record whose seq the next line reuses (line 3);
  3. a torn tail (line 5).

  It works out the expected offsets from the bytes it wrote, not from the reader's arithmetic.
  It asserts exact previews, and both long lines exceed 80 bytes, so the cut is tested. It
  also checks that each offset lands on the first byte of its preview.
- `test_a_preview_cut_inside_a_multibyte_character_is_replaced_not_an_error` covers the
  lenient decode, which ends in U+FFFD.

The offset tests passed before the fix. The issue said that arithmetic was *unprotected*, not
wrong. To show they have teeth, I applied four mutations to `_parse` one at a time and
restored the file afterwards. The main offset test failed under each one:
- dropped the `+ 1` in the line-advance arithmetic;
- dropped the `- 1` in the adjacent-duplicate offset;
- added `+ 1` to the torn-tail offset;
- shortened the preview to 79 bytes.

```
uv run pytest               128 passed
uv run ruff check           All checks passed!
uv run ruff format --check  23 files already formatted
uv run pyright              0 errors, 0 warnings, 0 informations
```

**Two-runner walkthrough:** not applicable. The change is in pure parsing after the read.
Locking, the read itself and the file are untouched, so task 4's walkthrough still holds as
written.

**Stranger pass:** the diff is under 30 lines of source, but it changes an error path, so I
re-read it cold.
- **Status messages.** "seq must be a JSON integer >= 1, found '2'" is true for every value
  that reaches it. "seq N is too large for a ref" is true only if nothing else can make
  `make_ref` fail there. I checked:
  - the stream id was already validated in `read_file`;
  - `seq >= 1` has just been checked;
  - the ref regex `[1-9][0-9]{0,18}` then fails only on more than 19 digits.

  So the message cannot lie. The `except ValueError` carries a comment saying why it is safe
  to name the seq.
- **Order of checks.** The raw `seq` is read before `parse_event`, in case a future upcaster
  rewrites the dict. The check itself runs after `parse_event`, so D111 wins (pinned by
  `test_an_event_from_a_newer_build_still_halts_by_name_whatever_its_seq`).
  - A `null` or missing seq still gets the existing "no seq" message.
  - A non-numeric string still fails Pydantic and gets "not a valid event".
  - Both are `CorruptStreamError` with `file:line`.
- **The `raw_seq < 1` branch can't be reached today**, because Pydantic's `ge=1` rejects 0
  and negatives first. I kept it so the condition reads exactly like `_seq_of`, which is what
  the issue asks for. If `ge=1` were ever loosened, this would still hold the line.
- **Rule 3 interaction.** The seq check now runs before the duplicate check, so a bad seq can
  no longer make a real line look like a duplicate. The torn tail is still skipped without
  being parsed, so strictness doesn't turn a torn tail into a fatal error.
- **Ignored errors, leaks, lock scope.** Nothing new is caught and dropped. Both new `raise`s
  chain or name the value. No resource or lock changes.

**Review:** pending.

**Choices made inside the task:**
- **I checked the raw value in `reader.py`** rather than making the field `Field(strict=True)`
  in `events.py`. Strict mode on the model would change validation for every caller,
  including code that builds events in Python. The issue limits the change to `reader.py`.
  The rule the issue names is the writer's file-level rule, so the reader enforces it at the
  same level, on the parsed JSON.
- **The upper bound comes from the ref format (19 digits), not from the writer.** The writer
  has no upper bound (see below). A seq the reader can't turn into a ref can't be loaded as a
  `Record`, so it has to halt. The question was only whether it halts with `file:line`.
- **`Record.seq` is still `event.seq`, not `raw_seq`.** After the check they're equal, and
  this keeps the record tied to the validated model.

**What to read first:** the new block in `_record`, then the `bool` row of the parametrised
test. The `bool` row is the case that used to lose a real record without any error.

**Noticed, not done:**
- **`writer._seq_of` has no upper bound.** Given a last line with
  `"seq": 10000000000000000000`, it returns that value (checked), and the next append would
  write seq 10^20 + 1. `Event.seq` is `ge=1` with no maximum, so the writer can produce a
  line the reader now refuses. That's unreachable in practice: it needs 10^19 appends or a
  hand edit. Still, the two halves disagree at the top of the range. A follow-up could cap
  the writer, or `Event.seq`, at the ref limit. Out of scope here: `writer.py` and
  `events.py` are outside this task.

**Cost:** pending.
