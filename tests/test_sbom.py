"""The bill of materials and the third-party licence inventory.

Two things here are easy to get wrong in a way that looks fine. The first is
the boundary: an inventory of the build environment rather than of what ships
lists pytest and ruff as though they were distributed, and one that resolves
too narrowly omits a package that is. The second is precision about licences:
"BSD License" read off a classifier is a weaker claim than an SPDX expression,
and presenting them identically invents certainty the metadata does not have.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent


def _load(name: str, path: Path):
    """Load a build script as a module.

    Registered in `sys.modules` before execution, which `@dataclass` requires:
    it resolves a class's module through `sys.modules[cls.__module__]`, and an
    unregistered module makes that None.
    """
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _sbom():
    return _load("_sbom", REPO / "build" / "windows" / "sbom.py")


@pytest.fixture(scope="module")
def sbom():
    return _sbom()


@pytest.fixture(scope="module")
def components(sbom):
    return sbom.collect()


# ------------------------------------------------------------- the boundary


def test_the_runtime_dependencies_are_inventoried(components):
    names = {component.name.lower() for component in components}
    assert "xxhash" in names
    assert "reportlab" in names
    assert "pyside6" in names


def test_transitive_dependencies_are_inventoried(components):
    """reportlab pulls pillow, PySide6 pulls shiboken6. A closure that stopped
    at the direct requirements would ship both without listing them."""
    names = {component.name.lower() for component in components}
    assert "pillow" in names
    assert "shiboken6" in names


def test_development_dependencies_are_not_inventoried(components):
    """The `dev` extra is not distributed, and listing pytest as a shipped
    component is a false statement about the bundle. This is the difference
    from the build inventory, which records the whole environment on purpose."""
    names = {component.name.lower() for component in components}
    assert "pytest" not in names
    assert "pymupdf" not in names
    assert "ruff" not in names


def test_offloader_itself_is_not_one_of_its_own_components(components):
    """It is the subject of the document, recorded in `metadata.component`."""
    assert "offloader" not in {component.name.lower() for component in components}


def test_every_component_has_a_version_and_a_licence_claim(components):
    for component in components:
        assert component.version
        assert component.licence
        assert component.source


def test_a_missing_requirement_is_an_error_not_an_omission(sbom, monkeypatch):
    """An SBOM that quietly drops what it could not resolve is worse than one
    that refuses: the gap is invisible in the output."""
    import importlib.metadata as metadata

    original = sbom.importlib.metadata.requires

    def requires(name):
        if sbom.normalize(name) == "xxhash":
            raise metadata.PackageNotFoundError(name)
        return original(name)

    monkeypatch.setattr(sbom.importlib.metadata, "requires", requires)
    with pytest.raises(RuntimeError, match="not installed"):
        sbom.closure()


@pytest.mark.parametrize("name,expected", [
    ("PySide6", "pyside6"),
    ("PySide6_Addons", "pyside6-addons"),
    ("charset-normalizer", "charset-normalizer"),
    ("Pillow", "pillow"),
])
def test_names_normalise_for_deduplication(sbom, name, expected):
    assert sbom.normalize(name) == expected


# ------------------------------------------------------------- the licences


def test_the_licence_source_is_recorded(components):
    """So a reviewer can tell an SPDX expression from a classifier, which is
    the difference between a precise claim and an approximate one."""
    sources = {component.source for component in components}
    assert sources <= {"License-Expression", "Classifier", "License",
                       "License (free text)", "none"}
    assert sources, "no components at all"


def test_copyleft_dependencies_are_flagged_for_review(components):
    """Qt ships under LGPL/GPL while Offloader is MIT. The tool must not be
    the thing that decides that is fine; it must be the thing that makes it
    impossible to miss."""
    flagged = {component.name.lower() for component in components
               if component.needs_review}
    assert "pyside6" in flagged
    assert "shiboken6" in flagged


def test_permissive_dependencies_are_not_flagged(components):
    """A flag on everything is a flag on nothing."""
    by_name = {component.name.lower(): component for component in components}
    assert not by_name["xxhash"].needs_review
    assert not by_name["pillow"].needs_review


def test_prose_in_the_licence_field_is_marked_as_loose(sbom, monkeypatch):
    """reportlab's `License` field is a paragraph. Truncating it to look like
    a licence name would assert a precision the metadata does not carry."""
    class _Meta(dict):
        def get_all(self, _key):
            return []

    paragraph = ("BSD license (see license.txt for details), Copyright (c) "
                 "2000-2024, ReportLab Inc.\nAll rights reserved.")
    monkeypatch.setattr(sbom.importlib.metadata, "metadata",
                        lambda _name: _Meta({"License": paragraph}))

    licence, source = sbom.licence_of("anything")
    assert source == "License (free text)"
    assert licence.endswith("...")


def test_an_absent_licence_says_unknown(sbom, monkeypatch):
    class _Meta(dict):
        def get_all(self, _key):
            return []

    monkeypatch.setattr(sbom.importlib.metadata, "metadata",
                        lambda _name: _Meta())
    assert sbom.licence_of("anything") == ("UNKNOWN", "none")


# ----------------------------------------------------------------- CycloneDX


def test_the_document_is_a_cyclonedx_1_6_bom(sbom, components):
    document = sbom.cyclonedx(components, {"version": "0.4.0"},
                              version="0.4.0")
    assert document["bomFormat"] == "CycloneDX"
    assert document["specVersion"] == "1.6"
    assert document["serialNumber"].startswith("urn:uuid:")
    assert document["version"] == 1
    assert document["metadata"]["component"]["name"] == "Offloader"
    assert document["metadata"]["component"]["version"] == "0.4.0"


def test_every_component_carries_a_purl_and_a_unique_ref(sbom, components):
    document = sbom.cyclonedx(components, {"version": "0.4.0"},
                              version="0.4.0")
    refs = [entry["bom-ref"] for entry in document["components"]]
    assert len(refs) == len(set(refs))
    for entry in document["components"]:
        assert entry["purl"].startswith("pkg:pypi/")
        assert entry["type"] == "library"
        assert entry["licenses"]


def test_only_spdx_expressions_are_declared_as_expressions(sbom):
    """CycloneDX distinguishes an SPDX expression from a bare name. Declaring
    "BSD License" as an expression asserts an identifier that does not exist."""
    spdx = sbom.Component("pillow", "12.0.0", "MIT-CMU", "License-Expression")
    loose = sbom.Component("reportlab", "5.0.0", "BSD License", "Classifier")

    document = sbom.cyclonedx([spdx, loose], {"version": "0.4.0"},
                              version="0.4.0")
    entries = {c["name"]: c["licenses"][0] for c in document["components"]}
    assert entries["pillow"] == {"expression": "MIT-CMU"}
    assert entries["reportlab"] == {"license": {"name": "BSD License"}}


def test_the_serial_number_is_stable_for_the_same_inputs(sbom, components):
    """So two SBOMs can be diffed to see what actually moved. A random serial
    would differ on every rebuild in a field nobody meant to compare."""
    identity = {"version": "0.4.0", "commit": "abc123"}
    first = sbom.cyclonedx(components, identity, version="0.4.0")
    second = sbom.cyclonedx(components, identity, version="0.4.0")
    assert first["serialNumber"] == second["serialNumber"]


def test_the_serial_number_changes_when_a_dependency_does(sbom, components):
    identity = {"version": "0.4.0"}
    baseline = sbom.cyclonedx(components, identity, version="0.4.0")
    moved = sbom.cyclonedx(
        [*components[:-1],
         sbom.Component(components[-1].name, "99.0.0",
                        components[-1].licence, components[-1].source)],
        identity, version="0.4.0")
    assert baseline["serialNumber"] != moved["serialNumber"]


# ------------------------------------------------------------- the artifacts


def test_all_three_files_are_written(sbom, tmp_path):
    written = sbom.write_all(tmp_path, {"version": "0.4.0"})
    assert [path.name for path in written] == [
        "Offloader-0.4.0-sbom.cyclonedx.json",
        "Offloader-0.4.0-third-party-notices.txt",
        "Offloader-0.4.0-requirements.txt",
    ]
    for path in written:
        assert path.read_text(encoding="utf-8").strip()
    json.loads(written[0].read_text(encoding="utf-8"))


def test_the_build_expects_exactly_the_files_that_are_written(sbom, tmp_path):
    """`validate_outputs` refuses any checksummed output it did not expect, so
    the writer and the expectation have to name the same files or a signed
    build fails at its own verification step."""
    build = _load("_build", REPO / "build" / "windows" / "build.py")

    written = {path.name for path in sbom.write_all(tmp_path, {"version": "0.4.0"})}
    assert written == build.sbom_names("0.4.0")


def test_the_notices_name_the_review_packages_twice(sbom, components):
    """Once in the table and once in the summary, because a reader scanning a
    long table should not have to spot a marker to learn there is a decision
    outstanding."""
    body = sbom.notices(components, version="0.4.0")
    assert "[review]" in body
    assert "decision" in body
    assert body.count("PySide6 ") >= 2


def test_the_notices_disclaim_being_advice(sbom, components):
    body = sbom.notices(components, version="0.4.0")
    assert "not legal advice" in body


def test_the_lockfile_pins_every_component(sbom, components):
    body = sbom.lockfile(components)
    for component in components:
        assert f"{component.name}=={component.version}" in body


def test_the_lockfile_is_not_the_build_environment(sbom, components):
    """The distinction that makes it useful: this reproduces what ships, and
    the release inventory records what built it."""
    body = sbom.lockfile(components)
    assert "pytest" not in body
    assert "not the build environment" in body


# --------------------------------------------------- what it does not cover


def test_every_output_states_its_own_boundary(sbom, components, tmp_path):
    """REGRESSION. These three files describe the Python dependency graph, and
    a frozen application ships components that have no packaging metadata to
    walk. Read as a complete inventory while missing the interpreter it ships,
    that is worse than one that says where it stops."""
    written = sbom.write_all(tmp_path, {"version": "0.4.0"}, components)
    document = json.loads(written[0].read_text(encoding="utf-8"))
    properties = {p["name"]: p["value"]
                  for p in document["metadata"]["properties"]}

    assert "not a complete third-party inventory" in properties["offloader:scope"]
    notices = written[1].read_text(encoding="utf-8")
    assert "Not covered here:" in notices
    assert "CPython runtime" in notices
    assert "PyInstaller bootloader" in notices
    assert "no packaging metadata" not in written[2].read_text(encoding="utf-8").lower() \
        or "PyInstaller bootloader" in written[2].read_text(encoding="utf-8")


def test_the_sbom_names_each_uncovered_component(sbom, components):
    document = sbom.cyclonedx(components, {"version": "0.4.0"}, version="0.4.0")
    uncovered = [p["value"] for p in document["metadata"]["properties"]
                 if p["name"] == "offloader:uncovered"]
    assert len(uncovered) == len(sbom.UNCOVERED)
    assert any("python3" in value for value in uncovered)


def test_the_gap_is_measured_against_the_bundle(sbom, tmp_path):
    """Against the built tree rather than asserted from the list, so it shrinks
    as it is closed and cannot be closed by editing a constant."""
    bundle = tmp_path / "Offloader"
    (bundle / "_internal").mkdir(parents=True)
    assert sbom.uncovered_in_bundle(bundle) == []

    (bundle / "_internal" / "python313.dll").write_bytes(b"MZ")
    (bundle / "Offloader.exe").write_bytes(b"MZ")
    found = sbom.uncovered_in_bundle(bundle)

    names = {name for name, _pattern, _why in found}
    assert names == {"CPython runtime", "PyInstaller bootloader"}
    assert len(found) == 2, "only the executables actually present"


def test_the_release_plan_still_carries_the_inventory_gate(sbom):
    """The claim these files support is narrower than the gate they were read
    as discharging, so the gate stays until the frozen runtime is covered."""
    plan = (REPO / "docs" / "release-plan.md").read_text(encoding="utf-8")
    assert "complete third-party inventory" in plan
    assert "remain pending" in plan


# ----------------------------------------------------- publishing them


def test_the_release_instructions_upload_every_checksummed_asset(sbom):
    """REGRESSION. The three files entered `SHA256SUMS.txt` while the generated
    upload command still named only the installer, the checksums and the
    inventory, so following the instructions published checksums for assets
    that were not there."""
    artifacts = _load("_artifacts", REPO / "build" / "windows" / "artifacts.py")
    notes = _load("_notes", REPO / "scripts" / "release_notes.py")
    from offloader._version import __version__

    body = notes.notes(f"v{__version__}", "abc1234")
    for name in artifacts.sbom_names(__version__):
        assert name in body, f"{name} is checksummed but never uploaded"


def test_the_asset_list_and_the_checksums_name_the_same_files(sbom, tmp_path):
    """One list, because the build, its verification and the instructions all
    read it."""
    artifacts = _load("_artifacts", REPO / "build" / "windows" / "artifacts.py")
    build = _load("_build", REPO / "build" / "windows" / "build.py")

    assets = set(artifacts.release_assets("0.4.0"))
    assert build.sbom_names("0.4.0") <= assets
    assert "SHA256SUMS.txt" in assets
    assert "Offloader-Setup-0.4.0.exe" in assets
    assert "Offloader-0.4.0-inventory.json" in assets
