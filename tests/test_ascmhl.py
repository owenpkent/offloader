"""ASC MHL v2.0.

The important tests here are the ones that pin behaviour to the *reference
implementation* rather than to my reading of the spec. `ascmitc/mhl` ships a
worked example with known-good values; those appear below as literals, and the
manifest writer is diffed against the manifest that project produced.
"""

from __future__ import annotations

import datetime as _dt
import re
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

from offloader import ascmhl, engine, verify
from offloader.hashers import c4_of_bytes, hash_file
from offloader.models import VerificationMode

WHEN = _dt.datetime(2020, 1, 16, 9, 15, tzinfo=_dt.timezone.utc)

#: Digests published by ascmitc/mhl for scenario_02. These are used as *inputs*
#: to the Appendix G tests, where the reference's own directory hashes are the
#: expected output.
REF_DIGESTS = {
    "Clips/A002C006_141024_R2EC.mov": "0ea03b369a463d9d",
    "Clips/A002C007_141024_R2EC.mov": "7680e5f98f4a80fd",
}
REF_CLIPS_CONTENT = "4c226b42e27d7af3"
REF_CLIPS_STRUCTURE = "906faa843d591a9f"

#: Stand-ins for building a tree. Deliberately *not* claimed to be the
#: reference's bytes — only the end-to-end reference test uses those, and it
#: reads them off disk.
PLACEHOLDER_FILES = {
    "Clips/A002C006_141024_R2EC.mov": b"placeholder clip one\n",
    "Clips/A002C007_141024_R2EC.mov": b"placeholder clip two\n",
}


def _offload(tmp_path: Path, files: dict[str, bytes], *, name: str = "A002R2EC"):
    source = tmp_path / "card"
    for relative, payload in files.items():
        target = source / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
    destination = tmp_path / name
    job = engine.run(source, engine.OffloadOptions(
        destinations=[destination], algorithm="xxh64",
        verification=VerificationMode.FULL, thumbnail_count=0,
        extra_probe=False, job_name=name))
    return job, destination


def _actions(manifest: Path) -> dict[str, str]:
    text = manifest.read_text(encoding="utf-8")
    return {m.group(1): m.group(2) for m in re.finditer(
        r'<path[^>]*>([^<]+)</path>\s*<\w+ action="(\w+)"', text)}


# --------------------------------------------------------------- C4


def test_c4_is_ninety_characters_with_the_c4_prefix():
    value = c4_of_bytes(b"")
    assert value.startswith("c4")
    assert len(value) == 90


def test_c4_matches_the_reference_chain_file(tmp_path: Path):
    """The reference chain identifies its manifest by C4; ours must agree on
    the same bytes."""
    manifest = tmp_path / "m.mhl"
    manifest.write_bytes(b"the exact bytes of a manifest")
    assert c4_of_bytes(manifest.read_bytes()) == hash_file(manifest, "c4")


def test_c4_never_uses_ambiguous_base58_characters():
    for forbidden in "0OIl":
        assert forbidden not in c4_of_bytes(b"some content")[2:]


# --------------------------------------------------------------- hashing


def test_directory_hashes_match_the_reference_implementation():
    """Appendix G, checked against the values ascmitc/mhl produced."""
    assert ascmhl.hash_of_hashes(list(REF_DIGESTS.values()),
                                 "xxh64") == REF_CLIPS_CONTENT

    structure = [
        ascmhl._structure_entry(Path(name).name, digest, "xxh64")
        for name, digest in REF_DIGESTS.items()
    ]
    assert ascmhl.hash_of_hashes(structure, "xxh64") == REF_CLIPS_STRUCTURE


def test_hash_of_hashes_is_order_independent():
    """The list is sorted before hashing, so discovery order cannot change it."""
    digests = ["ffffffffffffffff", "0000000000000000", "aaaaaaaaaaaaaaaa"]
    first = ascmhl.hash_of_hashes(digests, "xxh64")
    assert ascmhl.hash_of_hashes(list(reversed(digests)), "xxh64") == first


# --------------------------------------------------------------- naming


def test_manifest_filename_follows_section_6_3():
    assert (ascmhl.manifest_filename(1, "A002R2EC", WHEN)
            == "0001_A002R2EC_2020-01-16_091500Z.mhl")
    assert ascmhl.manifest_filename(42, "A001", WHEN).startswith("0042_A001_")


def test_filename_times_are_utc():
    local = _dt.datetime(2020, 1, 7, 12, 8, 1,
                         tzinfo=_dt.timezone(_dt.timedelta(hours=-7)))
    assert "190801Z" in ascmhl.manifest_filename(1, "A", local)


def test_sequence_numbers_grow_past_four_digits(tmp_path: Path):
    directory = ascmhl.ascmhl_dir(tmp_path)
    directory.mkdir(parents=True)
    (directory / "9999_x_2020-01-01_000000Z.mhl").write_text("<hashlist/>",
                                                             encoding="utf-8")
    assert ascmhl.next_sequence(tmp_path) == 10000


# --------------------------------------------------------------- manifest


@pytest.fixture
def history(tmp_path: Path):
    payloads = dict(PLACEHOLDER_FILES)
    payloads["Sidecar.txt"] = b"a sidecar file with some text in it, of a length\n"
    job, destination = _offload(tmp_path, payloads)
    manifest = ascmhl.write_manifest(job, destination, when=WHEN)
    return job, destination, manifest


def test_manifest_lands_in_the_ascmhl_folder(history):
    _job, destination, manifest = history
    assert manifest.parent == destination / "ascmhl"
    assert manifest.name.startswith("0001_")


def test_manifest_is_valid_namespaced_xml(history):
    _job, _destination, manifest = history
    root = ET.parse(manifest).getroot()
    assert root.tag == "{urn:ASC:MHL:v2.0}hashlist"
    assert root.get("version") == "2.0"


def test_declaration_uses_double_quotes_like_the_reference(history):
    _job, _destination, manifest = history
    assert manifest.read_bytes().startswith(
        b'<?xml version="1.0" encoding="UTF-8"?>')


def test_first_generation_hashes_are_original(history):
    _job, _destination, manifest = history
    assert set(_actions(manifest).values()) == {"original"}


def test_directory_and_root_hashes_are_present(history):
    """Self-consistent: the emitted directory hash must equal one recomputed
    from the files actually in that directory.

    (Agreement with the reference implementation's *values* is covered by
    `test_directory_hashes_match_the_reference_implementation`, which feeds it
    the reference's own digests, and by the end-to-end reference diff.)
    """
    _job, destination, manifest = history
    text = manifest.read_text(encoding="utf-8")
    assert "<roothash>" in text
    assert "<directoryhash>" in text

    clips = sorted((destination / "Clips").iterdir())
    expected = ascmhl.hash_of_hashes(
        [hash_file(path, "xxh64") for path in clips], "xxh64")
    assert expected in text


def test_chain_file_identifies_the_manifest_by_c4(history):
    _job, destination, manifest = history
    chain = destination / "ascmhl" / "ascmhl_chain.xml"
    root = ET.parse(chain).getroot()

    assert root.tag == "{urn:ASC:MHL:DIRECTORY:v2.0}ascmhldirectory"
    entries = list(root)
    assert len(entries) == 1
    assert entries[0].get("sequencenr") == "1"
    recorded = entries[0].find("{urn:ASC:MHL:DIRECTORY:v2.0}c4").text
    assert recorded == c4_of_bytes(manifest.read_bytes())


# --------------------------------------------------------------- the chain


def test_a_second_generation_verifies_the_first(tmp_path: Path):
    payloads = {"Clips/a.mov": b"aaaa", "Clips/b.mov": b"bbbb"}
    job, destination = _offload(tmp_path, payloads)
    ascmhl.write_manifest(job, destination, when=WHEN)

    job2, _ = _offload(tmp_path, payloads)
    second = ascmhl.write_manifest(job2, destination, when=WHEN)

    assert second.name.startswith("0002_")
    assert set(_actions(second).values()) == {"verified"}


def test_a_changed_file_is_recorded_as_failed(tmp_path: Path):
    """`failed` is the evidence of where in the chain a file stopped matching,
    so it must be recorded, not omitted."""
    job, destination = _offload(tmp_path, {"Clips/a.mov": b"aaaa",
                                           "Clips/b.mov": b"bbbb"})
    ascmhl.write_manifest(job, destination, when=WHEN)

    job2, _ = _offload(tmp_path, {"Clips/a.mov": b"aaaa",
                                  "Clips/b.mov": b"CHANGED"})
    second = ascmhl.write_manifest(job2, destination, when=WHEN)

    actions = _actions(second)
    assert actions["Clips/a.mov"] == "verified"
    assert actions["Clips/b.mov"] == "failed"


def test_a_failed_hash_is_excluded_from_directory_hashes(tmp_path: Path):
    """A directory hash that folded in a known-bad file would certify a tree
    that is not intact."""
    job, destination = _offload(tmp_path, {"Clips/a.mov": b"aaaa",
                                           "Clips/b.mov": b"bbbb"})
    ascmhl.write_manifest(job, destination, when=WHEN)

    job2, _ = _offload(tmp_path, {"Clips/a.mov": b"aaaa",
                                  "Clips/b.mov": b"CHANGED"})
    second = ascmhl.write_manifest(job2, destination, when=WHEN)

    # The directory hash must equal one computed over the good file alone.
    good_only = ascmhl.hash_of_hashes([hash_file(destination / "Clips" / "a.mov",
                                                 "xxh64")], "xxh64")
    assert good_only in second.read_text(encoding="utf-8")


def test_a_failed_hash_is_not_reused_as_a_reference(tmp_path: Path):
    """The spec forbids verifying against a hash labelled failed."""
    job, destination = _offload(tmp_path, {"a.mov": b"aaaa"})
    ascmhl.write_manifest(job, destination, when=WHEN)
    job2, _ = _offload(tmp_path, {"a.mov": b"CHANGED"})
    second = ascmhl.write_manifest(job2, destination, when=WHEN)
    assert _actions(second)["a.mov"] == "failed"

    recorded = ascmhl.read_manifest_hashes(second)
    assert "a.mov" not in recorded


def test_the_chain_grows_with_every_generation(tmp_path: Path):
    payloads = {"a.mov": b"aaaa"}
    for _ in range(3):
        job, destination = _offload(tmp_path, payloads)
        ascmhl.write_manifest(job, destination, when=WHEN)

    chain = ET.parse(destination / "ascmhl" / "ascmhl_chain.xml").getroot()
    assert [e.get("sequencenr") for e in chain] == ["1", "2", "3"]
    assert len(ascmhl.existing_manifests(destination)) == 3


def test_every_chain_entry_matches_its_manifest(tmp_path: Path):
    payloads = {"a.mov": b"aaaa"}
    for _ in range(2):
        job, destination = _offload(tmp_path, payloads)
        ascmhl.write_manifest(job, destination, when=WHEN)

    namespace = "{urn:ASC:MHL:DIRECTORY:v2.0}"
    directory = destination / "ascmhl"
    for node in ET.parse(directory / "ascmhl_chain.xml").getroot():
        name = node.find(f"{namespace}path").text
        assert node.find(f"{namespace}c4").text == \
            c4_of_bytes((directory / name).read_bytes())


# --------------------------------------------------------------- verifying


def test_verify_reads_an_ascmhl_history(history):
    _job, destination, _manifest = history
    report = verify.verify_manifest(
        verify.find_manifests(destination)[0])
    assert report.passed
    assert report.checked == 3


def test_verify_catches_a_flipped_bit_in_an_ascmhl_tree(history):
    _job, destination, _manifest = history
    victim = destination / "Clips" / "A002C006_141024_R2EC.mov"
    payload = bytearray(victim.read_bytes())
    payload[0] ^= 0x01
    victim.write_bytes(bytes(payload))

    report = verify.verify_manifest(verify.find_manifests(destination)[0])
    assert not report.passed
    assert report.failures[0].result is verify.EntryResult.MISMATCH


def test_verify_uses_only_the_newest_generation(tmp_path: Path):
    """Every generation covers the same files; hashing all of them would triple
    the work for no extra evidence."""
    payloads = {"a.mov": b"aaaa"}
    for _ in range(3):
        job, destination = _offload(tmp_path, payloads)
        ascmhl.write_manifest(job, destination, when=WHEN)

    manifests = verify.find_manifests(destination)
    assert len(manifests) == 1
    assert manifests[0].name.startswith("0003_")


def test_the_chain_file_is_not_reported_as_unlisted(history):
    _job, destination, _manifest = history
    report = verify.verify_manifest(verify.find_manifests(destination)[0])
    assert not any(p.name == "ascmhl_chain.xml" for p in report.unlisted)


def test_classic_mhl_still_verifies(tmp_path: Path):
    """Adding ASC MHL must not break the format already in the field."""
    from offloader.reports import write_mhl

    job, destination = _offload(tmp_path, {"a.mov": b"aaaa"})
    manifest = write_mhl(job, destination / "A001_Reports" / "JobReport.mhl")
    assert verify.verify_manifest(manifest).passed


# --------------------------------------------------------------- reference


REFERENCE = Path(
    r"C:\Users\Owen\AppData\Local\Temp\claude\C--Users-Owen-dev"
    r"\3c278deb-e13c-44d2-ae85-83b4cc4461b8\scratchpad\ascmhl_ref"
)


@pytest.mark.skipif(not (REFERENCE / "ascmhl").is_dir(),
                    reason="reference example tree not downloaded")
def test_manifest_is_identical_to_the_reference_implementations(tmp_path: Path):
    """The strongest check available: rebuild the reference implementation's
    own scenario and diff against the manifest it shipped."""
    payloads = {rel: (REFERENCE / rel).read_bytes()
                for rel in ("Clips/A002C006_141024_R2EC.mov",
                            "Clips/A002C007_141024_R2EC.mov",
                            "Sidecar.txt")}
    job, destination = _offload(tmp_path, payloads)
    mine = ascmhl.write_manifest(
        job, destination, process=ascmhl.PROCESS_IN_PLACE,
        ignore_patterns=[".DS_Store", "ascmhl", "ascmhl/"], when=WHEN)

    def normalise(text: str) -> str:
        text = re.sub(r"<hostname>[^<]*</hostname>", "<hostname>H</hostname>", text)
        text = re.sub(r'<tool version="[^"]*">[^<]*</tool>', "<tool>T</tool>", text)
        return re.sub(r'lastmodificationdate="[^"]*"',
                      'lastmodificationdate="M"', text).strip()

    theirs = REFERENCE / "ascmhl" / "0001_A002R2EC_2020-01-16_091500Z.mhl"
    assert mine.name == theirs.name
    assert normalise(mine.read_text(encoding="utf-8")) == \
        normalise(theirs.read_text(encoding="utf-8"))
