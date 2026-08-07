## What this changes

<!-- One concern per PR. What and why. -->

## What you verified

<!--
Not what you changed — what you checked, and how. For example:
"Retries only fire on transient errno values; confirmed ENOENT still fails on
the first attempt rather than burning three."
-->

## Checklist

- [ ] `pytest` passes (398 tests)
- [ ] `ruff check src tests` passes
- [ ] Tests added for the behaviour changed

If this touches the copy, verification, or delete path:

- [ ] There is a test that **fails against the old code** — not merely one that
      passes against the new code
- [ ] This does not make a "Verified" verdict easier to reach, or the PR
      explains why the change is safe
- [ ] [`docs/data-safety.md`](https://github.com/owenpkent/offloader/blob/main/docs/data-safety.md) still describes
      reality, including its "what is still not protected" list

If this changes documented behaviour:

- [ ] The relevant `docs/*.md` is updated
- [ ] The test count in `README.md` is updated if it moved

<!--
Anything surprising you hit along the way is worth putting in the commit
message. The log here is the project's real archaeology.
-->
