"""Edge cases in the layers between the bytes and the paperwork.

`test_fuzz_edges.py` goes after the copy engine and the binary parsers. This
module goes after everything downstream of them: what ffprobe hands back, what
a number formats to, what a digest compares equal to, what a name renders to.

The bugs these were written against shared a shape. Each was a value that was
perfectly legal at the point it was produced and fatal, or silently wrong, at
the point it was consumed, because the two ends disagreed about the contract:

  * ffprobe writes "N/A" in a numeric field. `_build` called `int()` on it.
  * `_parse_rate` accepts "inf/1" because `float()` does. Every fps formatter
    then called `round()` on it, which does not.
  * Another tool writes its hex digests uppercase. `verify` compared with `==`.
  * `render` substituted token values into a string it kept substituting into.

All four are fixed, and both ends of each pair are pinned here: whichever side
a later change moves, the other still has to hold.

Run longer sweeps with:  pytest --fuzz tests/test_edge_cases.py
"""

from __future__ import annotations

import datetime as _dt
import json
import math
import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from offloader import config, history, naming, probe, util, verify
from offloader.hashers import hash_file
from offloader.models import (
    Destination,
    FileEntry,
    FileStatus,
    Job,
    MediaInfo,
    VerificationMode,
)
from offloader.reports import write_mhl

# ------------------------------------------------------- ffprobe says "N/A"


#: ffprobe emits the literal string "N/A" in numeric fields all the time: for
#: codecs that do not declare a sample rate, for data and attachment streams,
#: and for anything it only partially decoded. It is ordinary output, not
#: corruption.
NA_STREAMS = [
    pytest.param({"codec_type": "audio", "channels": "2", "sample_rate": "N/A"},
                 id="sample_rate is N/A"),
    pytest.param({"codec_type": "audio", "channels": "N/A"},
                 id="channels is N/A"),
    pytest.param({"codec_type": "audio", "channels": 2, "bit_rate": "N/A"},
                 id="bit_rate is N/A"),
]


def test_probe_build_handles_healthy_output():
    """Control: the shape ffprobe produces for an ordinary clip."""
    info = probe._build({
        "streams": [{"codec_type": "video", "codec_name": "h264", "width": 1920,
                     "height": 1080, "r_frame_rate": "24/1", "duration": "10"}],
        "format": {"format_name": "mov", "duration": "10"},
    })
    assert info.fps == 24.0


@pytest.mark.parametrize("stream", NA_STREAMS)
def test_probe_build_survives_na_fields(stream):
    """This is the realistic version of 'one bad file kills the job'. It needs
    no crafted BRAW, only a clip whose audio stream ffprobe cannot fully
    describe."""
    probe._build({"streams": [stream], "format": {}})


def test_probe_build_survives_a_non_dict_stream():
    probe._build({"streams": ["garbage"], "format": {}})


def test_probe_survives_an_ffprobe_that_reports_na(tmp_path, monkeypatch):
    """End to end through the public entry point, with ffprobe stubbed to
    return output it genuinely produces."""
    clip = tmp_path / "clip.mov"
    clip.write_bytes(b"\x00" * 64)
    payload = json.dumps({
        "streams": [{"codec_type": "audio", "channels": "2", "sample_rate": "N/A"}],
        "format": {"format_name": "mov"},
    })
    monkeypatch.setattr(
        probe.subprocess, "run",
        lambda *a, **k: SimpleNamespace(returncode=0, stdout=payload, stderr=""))

    assert isinstance(probe.probe(clip), MediaInfo)


# -------------------------------------------------- non-finite frame rates


@pytest.mark.parametrize("rate", ["inf/1", "-inf/1", "nan/1", "Infinity/1"])
def test_parse_rate_never_returns_a_non_finite_number(rate):
    """A frame rate is a physical quantity. The parser is the right place to
    refuse a value that is not one, because nothing downstream checks."""
    parsed = probe._parse_rate(rate)
    assert parsed is None or math.isfinite(parsed)


@pytest.mark.parametrize("fps", [float("inf"), float("nan")])
def test_fps_formatters_accept_whatever_parse_rate_produces(fps):
    """The two ends of the same contract. `_parse_rate` can produce these, so
    the formatters have to survive them, or one of the two has to change."""
    util.format_fps(fps)
    util.format_timecode(240, fps)


# --------------------------------------------------------- size formatting


SIZE_SHAPE = re.compile(r"-?\d+(?:\.\d+)? (bytes|KB|MB|GB|TB)")


@given(size=st.integers(min_value=-10 ** 15, max_value=-1))
@settings(deadline=None)
def test_format_size_promotes_negative_values_too(size: int):
    """`test_fuzz.py` pins this property for non-negative sizes only. A byte
    count that can go negative anywhere (history.py uses -1 as its stat-failure
    sentinel) should still render in a sane unit."""
    text = util.format_size(size)
    assert SIZE_SHAPE.fullmatch(text), text
    if abs(size) >= 1000:
        assert not text.endswith(" bytes"), (
            f"{size} rendered as {text!r} instead of promoting to a larger unit")


@pytest.mark.parametrize("size, unit", [
    (999_999, "KB"), (999_999_999, "MB"), (999_999_999_999, "GB"),
])
def test_format_size_mantissa_never_reaches_1000(size: int, unit: str):
    """'1000.0 MB' is not wrong by much, but it is a unit the function's own
    branch structure says it will never print."""
    text = util.format_size(size)
    mantissa, printed = text.rsplit(" ", 1)
    assert float(mantissa) < 1000, (
        f"{size} rendered as {text!r}; expected promotion out of {unit}")
    assert printed != unit, f"{size} should have promoted past {unit}"


@pytest.mark.parametrize("value", [float("nan"), float("inf")])
def test_format_size_does_not_invent_a_unit_for_non_finite_input(value):
    text = util.format_size(value)
    assert SIZE_SHAPE.fullmatch(text), text


# --------------------------------------------------- digest case sensitivity


def _single_file_job(root: Path, digest: str, clip: Path) -> Job:
    when = _dt.datetime(2026, 8, 4, 12, 0).timestamp()
    entry = FileEntry(
        source=clip, source_root=root, size=clip.stat().st_size, created=when,
        modified=when, checksum=digest, media=MediaInfo(),
        destinations=[Destination(root=root, path=clip,
                                  status=FileStatus.VERIFIED, checksum=digest,
                                  created=when, modified=when)],
    )
    return Job(name="A001", source_root=root, destination_roots=[root],
               verification=VerificationMode.SOURCE_ONLY,
               hash_label="XXHash3-64",
               started=_dt.datetime(2026, 8, 4, 5, 0),
               finished=_dt.datetime(2026, 8, 4, 6, 0), files=[entry])


def _manifest_for(tmp_path: Path) -> tuple[Path, str]:
    clip = tmp_path / "clip.mov"
    clip.write_bytes(b"X" * 4096)
    digest = hash_file(clip, "xxh3-64")
    manifest = write_mhl(_single_file_job(tmp_path, digest, clip),
                         tmp_path / "A001.mhl")
    return manifest, digest


def test_verify_passes_a_manifest_we_wrote_ourselves(tmp_path):
    """Control: our own lowercase-hex manifest verifies clean."""
    manifest, _ = _manifest_for(tmp_path)
    assert verify.verify_manifest(manifest).counts() == {"ok": 1}


def test_verify_accepts_an_uppercase_hex_manifest(tmp_path):
    """MHL is an interchange format. Telling an editor their footage is
    corrupt because another tool shifted the case of a hex digit is the most
    expensive kind of false alarm this tool can raise."""
    manifest, digest = _manifest_for(tmp_path)
    shouty = tmp_path / "A001_upper.mhl"
    shouty.write_text(
        manifest.read_text(encoding="utf-8").replace(digest, digest.upper()),
        encoding="utf-8")

    report = verify.verify_manifest(shouty)
    assert report.counts() == {"ok": 1}, (
        f"byte-identical file reported as {report.summary()}")


# ---------------------------------------------------------- name rendering


def test_render_does_not_substitute_into_a_value_it_already_placed():
    """A folder literally named `{index}` is a legal directory name. Rendering
    `{card}` from it puts the text `{index}` into the result, and the later
    `{index}` pass then overwrites the card name with the sequence number."""
    values = naming.context(Path("{index}"), index=7)
    assert values["card"] == "{index}"
    assert naming.render("{card}_{index}", values) == "{index}_007"


# -------------------------------------------------------------- fingerprint


def test_empty_sources_do_not_share_one_fingerprint():
    """The fingerprint decides whether a card has been offloaded before. Two
    different empty volumes must not look like the same prior job."""
    assert history.fingerprint([], Path("/cardA")) != \
        history.fingerprint([], Path("/cardB"))


# ------------------------------------------------------------ config paths


@pytest.mark.parametrize("name", ["D:/evil.json", "/etc/passwd"])
def test_config_file_stays_inside_the_config_directory(name: str):
    """Only ever called with literals today, which is the single reason this
    is latent rather than live."""
    resolved = config.config_file(name).resolve()
    assert config.config_dir().resolve() in resolved.parents


# ----------------------------------------------- things that already hold


@pytest.mark.parametrize("flag", ["--verify", "--profile"])
def test_cli_rejects_an_unknown_enum_value_without_a_traceback(flag: str):
    """models.py's enums define no `_missing_`, so constructing one from an
    unknown string raises a bare ValueError. argparse's `choices=` catches
    these first, so the user gets a usage error rather than a stack trace.
    That guard is the only thing standing between the two."""
    result = subprocess.run(
        [sys.executable, "-m", "offloader", "offload", "nonexistent-source",
         "nonexistent-dest", flag, "definitely-not-a-real-value"],
        capture_output=True, text=True)
    assert result.returncode == 2
    assert "invalid choice" in result.stderr
    assert "Traceback" not in result.stderr


@given(numerator=st.integers(min_value=-10 ** 6, max_value=10 ** 6),
       denominator=st.integers(min_value=-10 ** 6, max_value=10 ** 6))
@settings(deadline=None)
def test_parse_rate_never_raises_on_a_rational(numerator: int, denominator: int):
    """Whatever integers ffprobe puts either side of the slash, including a
    zero denominator, the parser returns a value or None."""
    probe._parse_rate(f"{numerator}/{denominator}")


@given(text=st.text(max_size=24))
@settings(deadline=None)
def test_parse_rate_never_raises_on_junk(text: str):
    probe._parse_rate(text)
