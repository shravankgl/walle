# devlog

One file per agent-built task, `task-NN.md` (R6). These are what Shravan reads to review and
learn the code later, so explain the reasoning as well as the result. `DEVLOG.md` holds the
history up to task 3.

```markdown
# Task NN: <title>

**Shipped:** the files, and what each one is for.
**Why:** the decisions it implements, with D-numbers, in plain words.
**Verify:** the criterion from PLAN §9, the test that proves it, and the check output.
**Two-runner walkthrough:** if it touches the log, a lock or a derived file, what writers A and B
each see at each step.
**Stranger pass:** what the re-read found, and what was done about it.
**Review:** which model reviewed it, how (`ocr delegate` or direct), findings and outcomes.
**Choices made inside the task:** anything a reader might have done differently, and why not.
**What to read first:** the part most worth a human's time.
**Noticed, not done:** anything left for later.
**Cost:** dollars for this task, author and reviewer, from Hyperagent.
```
