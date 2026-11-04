#!/usr/bin/env bash
#
# An independent review of the working tree, run before every commit (D119, R4, R5).
#
# Two tools, each doing the half it is good at:
#
#   open-code-review  the deterministic half — which files are in scope, and which review
#                     rules apply to each. No model involved, so it cannot cut corners on a
#                     large changeset or drift off the file list.
#
#   agy (Antigravity) the judgement half — a Gemini model reads each diff against those
#                     rules and reports findings. A different vendor and a different model
#                     from the agent that wrote the code, which is the whole point: coding
#                     rule 4 asks for a reader who did not write it, and until now this
#                     project did not have one.
#
# Advisory, never blocking. It prints findings; a person decides. A blocking gate on a
# nondeterministic judgement is what gets `--no-verify`'d within a week, and a bypassed check
# is worse than no check because it still looks like coverage.
#
# It exits non-zero when the review *did not run* — a missing tool, a failed call. The one
# failure a review script must never have is looking clean because nothing happened.
#
# Usage:
#   scripts/review.sh                  # working tree: staged, unstaged, untracked
#   scripts/review.sh --commit HEAD    # a commit that has already landed
#
set -euo pipefail

cd "$(dirname "$0")/.."

# The Antigravity installer appends to the shell profile, which a non-login shell may not
# have read. Look where it installs rather than depending on inheritance.
[ -x "$HOME/.local/bin/agy" ] && PATH="$HOME/.local/bin:$PATH"

REVIEWER_MODEL="${WALLE_REVIEW_MODEL:-gemini-3.1-pro-high}"
OUT_DIR=".review"
mkdir -p "$OUT_DIR"

# Prompts are passed as an argument, not on stdin — `agy -p` with nothing attached prints
# help rather than reading stdin. ARG_MAX is ~1 MB on macOS; a file whose diff approaches
# that is reported as skipped rather than silently truncated.
MAX_DIFF_BYTES=200000

if ! command -v ocr >/dev/null 2>&1; then
  echo "review did NOT run: 'ocr' is not installed." >&2
  echo "  npm install -g @alibaba-group/open-code-review" >&2
  exit 2
fi

# --- the deterministic half ---------------------------------------------------------------

PREVIEW=$(ocr delegate preview "$@" 2>/dev/null) || {
  echo "review did NOT run: 'ocr delegate preview' failed." >&2
  exit 2
}

# Reviewable files are indented markdown list items:  - `path` [modified]
# Excluded ones are struck through and start at column 0, so requiring the indent separates
# them. No arrays: macOS ships bash 3.2, where `mapfile` does not exist.
FILE_LIST=$(printf '%s\n' "$PREVIEW" | sed -nE 's/^[[:space:]]+- `([^`]+)`.*/\1/p')

if [ -z "$FILE_LIST" ]; then
  echo "nothing reviewable in this change."
  exit 0
fi

COUNT=$(printf '%s\n' "$FILE_LIST" | wc -l | tr -d ' ')

# How to get each file's diff depends on the mode `ocr` reported, so read it from there
# rather than guessing from the arguments.
MODE=$(printf '%s\n' "$PREVIEW" | sed -nE 's/^- mode: (.*)$/\1/p')
COMMIT=$(printf '%s\n' "$PREVIEW" | sed -nE 's/^- commit: (.*)$/\1/p')

diff_for() {
  case "$MODE" in
    commit) git --no-pager show --no-ext-diff --no-textconv --no-color "$COMMIT" -- "$1" ;;
    *)      git --no-pager diff --no-ext-diff --no-textconv --no-color HEAD -- "$1" \
              || true
            # An untracked file has no diff; show it whole so new code is still reviewed.
            if [ -z "$(git --no-pager diff --no-color HEAD -- "$1")" ] && [ -f "$1" ]; then
              echo "--- new file: $1 ---"; cat "$1"
            fi ;;
  esac
}

if ! command -v agy >/dev/null 2>&1; then
  echo "── delegated review (no independent reviewer — the author is the reviewer) ───"
  echo
  printf '%s\n' "$PREVIEW"
  echo
  printf '%s\n' "$FILE_LIST" | tr '\n' '\0' | xargs -0 ocr delegate rule 2>/dev/null \
    | sed 's/^/  /'
  echo
  echo "  Install Antigravity for a reader that did not write the code."
  exit 0
fi

# --- the judgement half --------------------------------------------------------------------

echo "── independent review: $REVIEWER_MODEL via agy, rules from open-code-review ──"
echo "   $COUNT file(s), mode=$MODE"
echo

PROJECT_RULES='This project has hard rules that override general style advice:
- Append, never mutate. A correction is a new record, not an edit.
- A status that contradicts what is on disk is the worst possible bug.
- An ignored error needs a comment naming what can fail and why losing it is acceptable.
- Validate everything before the first side effect; a bad call should leave zero on-disk state.
- Resolution must stay a pure function of the log: no IO and no model call in that path.
Report only defects you are confident are real. Say "no findings" rather than inventing one.
For each: SEVERITY (high/medium), file:line, what breaks, and the concrete fix.'

FAILED=0
REVIEWED=0

for FILE in $FILE_LIST; do
  RULES=$(ocr delegate rule "$FILE" 2>/dev/null || true)
  DIFF=$(diff_for "$FILE" || true)

  if [ -z "$DIFF" ]; then
    echo "  [skipped] $FILE — empty diff"
    continue
  fi
  SIZE=${#DIFF}
  if [ "$SIZE" -gt "$MAX_DIFF_BYTES" ]; then
    echo "  [skipped] $FILE — diff is ${SIZE} bytes, over the ${MAX_DIFF_BYTES} limit"
    FAILED=$((FAILED + 1))
    continue
  fi

  # The no-tools instruction is load-bearing. Without it the reviewer reaches for a command
  # to explore the repo, headless mode cannot prompt for that permission, the call is
  # auto-denied, and it returns *nothing* — which the first version of this script happily
  # reported as "reviewed". Granting the permission instead would mean letting a reviewer
  # run arbitrary commands here, which is a worse trade than handing it the diff.
  PROMPT="Do NOT use any tools. Do not read files, run commands, or search. Everything you
need is in this message; answer from it alone.

You are reviewing ONE file of a larger change — other files in the same commit are being
reviewed separately, so do not report anything as missing from the commit. Review only the
changed (+) lines of this file.

${PROJECT_RULES}

Review checklist for this file:
${RULES}

The diff:
${DIFF}"

  # An empty answer is a failed review, not a clean one. agy exits 0 when a denied tool
  # permission leaves it with nothing to say, so the exit code alone cannot be trusted.
  if OUTPUT=$(agy --model "$REVIEWER_MODEL" -p="$PROMPT" 2>"$OUT_DIR/agy.err") \
     && [ -n "$(printf '%s' "$OUTPUT" | tr -d '[:space:]')" ]; then
    REVIEWED=$((REVIEWED + 1))
    echo "── $FILE"
    printf '%s\n' "$OUTPUT" | sed 's/^/  /'
    echo
  else
    echo "  [FAILED] $FILE — the reviewer returned nothing:" >&2
    tail -3 "$OUT_DIR/agy.err" >&2
    FAILED=$((FAILED + 1))
  fi
done

echo "reviewed $REVIEWED/$COUNT file(s)"

# Nothing reviewed is not a clean review. Say so with an exit code, so the ritual cannot be
# recorded as passed when it never ran.
if [ "$REVIEWED" -eq 0 ]; then
  echo "review did NOT run: no file was reviewed." >&2
  exit 2
fi
[ "$FAILED" -gt 0 ] && echo "warning: $FAILED file(s) were not reviewed." >&2
exit 0
