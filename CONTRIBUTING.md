# Contributing

Thanks for looking. This document is short on ceremony and long on the one thing
that makes this project different from most.

## The standard everything is held to

**Someone reformats a camera card because this tool said "Verified".**

That single sentence decides most arguments here. A bug in a text editor costs
someone an afternoon; a bug in the copy or verification path costs a production
the only take of a scene that has already been struck. Two such bugs have
already been found and fixed in this codebase — the engine could destroy the card
it was copying, and a failed copy could destroy the good archive copy it was
replacing. Both are regression tests now. Read
[`docs/data-safety.md`](docs/data-safety.md) before touching `engine.py`.

Practical consequences:

- **Anything that touches the copy, verify, or delete path needs a test that
  fails without the change.** Not a test that passes with it — one that
  demonstrably catches the thing you fixed. If you can't make it fail on the old
  code, you haven't characterised the bug yet.
- **Never make a "Verified" verdict easier to reach.** Widening what counts as
  success is the most dangerous kind of change here, and needs a strong argument.
- **A warning is not a failure, and silence is not a pass.** If the tool cannot
  prove something, it should say so rather than omit it.

## Getting set up

```sh
git clone https://github.com/owenpkent/offloader
cd offloader
pip install -e ".[dev]"
```

`ffmpeg` and `ffprobe` on `PATH` are optional — the suite runs without them.

```sh
pytest                      # ~434 tests, about 50s
pytest --fuzz               # property tests at 3000 examples each, about 2 min
ruff check src tests
pytest --cov=offloader --cov-report=term-missing
```

GUI tests run on Qt's offscreen platform. That is set automatically inside the
test files, but if you run Qt code by hand:

```sh
QT_QPA_PLATFORM=offscreen python -m pytest tests/test_gui.py
```

The README's screenshots are generated, not captured, so a change to the
interface can bring them along with it:

```sh
python tools/screenshots.py         # rewrites docs/images/
```

It runs the real app against a throwaway config directory and invented volumes,
so it neither reads your presets nor puts your drive labels in the README.

## Testing without a camera card

Almost nobody has a 27 GB BRAW clip and a failing card reader to hand, so the
suite fakes all of it:

| To exercise | Use |
| --- | --- |
| A BRAW file | `tests/test_braw.py::write_braw` builds a real container atom by atom |
| A flaky reader | `tests/test_retry.py` patches `builtins.open` to fail transiently |
| A failing destination | `tests/test_data_safety.py` returns handles that raise on write |
| Corruption | flip a byte and re-verify; size stays identical, checksum does not |
| An interrupted recording | write a BRAW with `include_moov=False` |
| A deep Windows path | build until `len(str(path)) > 260` — do not hard-code, the temp root's length varies |

If you have real hardware, there are tests that use it and skip cleanly when it
is absent (`test_braw.py::test_synthetic_fixture_matches_a_real_camera_file`).
Please keep that pattern: a synthetic fixture that only matches itself is
worthless.

## Pin to reference implementations, not to your reading of a spec

Implementing a format from prose is how you ship something only your own reader
accepts. Where a reference implementation exists, tests assert against **its
output**:

- ASC MHL manifests are diffed against the ones [`ascmitc/mhl`][mhl] ships.
- The reference report's own checksums appear as literals in `test_util.py`.

Both caught real errors that a careful reading had not. If you add a format,
find something authoritative to diff against before you trust it.

[mhl]: https://github.com/ascmitc/mhl

## Benchmarks

If you make a performance claim, measure it in a way that could disprove it.
[`docs/performance.md`](docs/performance.md) records how the first benchmark in
this repo produced 2465 MB/s off an exFAT volume — a physically impossible number
caused by writes sitting in the page cache and being deleted before they landed.
The method that survives is in that document: equalise durability with `fsync`,
alternate case order, take the best of several runs, and state the confounds.

Absolute throughput figures from a dataset smaller than RAM are not worth
quoting. A/B comparisons measured against each other in one run are.

## Style

- `ruff check src tests` must pass. Rules are in `pyproject.toml`.
- Match the surrounding code. Comments explain **why**, not what — most comments
  here record a constraint or a decision that is not obvious from the code.
- Docstrings on anything non-obvious, especially where a value came from a
  measurement or a spec section.
- Type hints on new public functions.
- Keep the engine free of Qt imports. `volumes.py`, `braw.py` and the rest are
  deliberately importable without a GUI toolkit so they can be tested headlessly.

## Pull requests

- One concern per PR.
- Say what you verified, not just what you changed. "Added retry" is less useful
  than "retries only on transient errno values; verified `ENOENT` still fails on
  the first attempt".
- If you found something surprising, put it in the commit message. The commit
  log here is the project's real archaeology.
- Update the relevant `docs/*.md` if you changed behaviour it describes, and the
  test count in `README.md` if it moved.

## Reporting a bug

Please include the output of:

```sh
offloader info
```

It reports OS, ffmpeg/ffprobe presence, which font the PDF will use, and whether
Windows long-path support is on — which between them explain most environment
differences.

If the bug involves data loss or a wrong verification verdict, see
[`SECURITY.md`](SECURITY.md) and report it privately first.
