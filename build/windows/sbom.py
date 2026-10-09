"""Bill of materials and third-party licences for a release.

Three files come out of this, all describing the same set of packages:

* a CycloneDX 1.6 SBOM, for anything that consumes one automatically;
* a human-readable notices file, because distributing these packages means
  distributing their licence texts;
* a pinned requirements list, so the set can be reproduced.

The set is the **runtime dependency closure of the installed package**, not a
dump of the build environment. `build.py`'s inventory already records every
distribution present, which on a developer machine includes pytest and ruff:
useful for reproducing a build, wrong as a statement about what ships.

It is also not the whole of what ships. This walks Python distribution
metadata, and a frozen application carries components that have no metadata to
walk: the CPython runtime DLL the bundle validation requires, and the
PyInstaller bootloader compiled into each executable. `UNCOVERED` names them,
`uncovered_in_bundle` finds the ones actually present, and every output says
so, because an inventory that is read as complete while missing the
interpreter it ships is worse than one that admits its boundary. Closing that
boundary is a release gate in `docs/release-plan.md`, not something these
three files discharge.

This inventories and flags. It does not decide whether a licence is
acceptable, and it must not be read as saying so. PySide6 alone is offered
under `LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only` while Offloader is MIT,
which is a decision for a person: the flags exist so that decision is taken
deliberately rather than by not noticing.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import re
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent

#: The extras whose dependencies are actually bundled. `dev` is not one.
BUNDLED_EXTRAS = ("gui",)

#: What this inventory does not reach. Each is a real third-party component of
#: the shipped application with no Python distribution metadata to read: the
#: closure below cannot see them, and neither can any tool that consumes its
#: output. Named so the gap is stated rather than left to be discovered.
UNCOVERED = (
    ("CPython runtime", "python3*.dll",
     "the interpreter the frozen bundle runs on"),
    ("PyInstaller bootloader", "Offloader.exe",
     "compiled into each frozen executable, not installed as a package"),
    ("PyInstaller bootloader", "offloader-cli.exe",
     "compiled into each frozen executable, not installed as a package"),
    ("PyInstaller bootloader", "offloader-maintenance.exe",
     "compiled into each frozen executable, not installed as a package"),
)

#: The one sentence every output carries about its own boundary.
SCOPE_NOTE = (
    "Scope: the Python distribution dependencies of the frozen application. "
    "Components without Python packaging metadata are not covered, including "
    "the bundled CPython runtime and the PyInstaller bootloader compiled into "
    "each executable. This is not a complete third-party inventory of the "
    "shipped application."
)

#: Licence families that place conditions on redistribution beyond notice.
#: Not a verdict, a prompt: each of these needs a human to say what applies.
REVIEW_FAMILIES = ("GPL", "AGPL", "LGPL", "MPL", "EPL", "CDDL", "CC-BY-SA",
                   "SSPL", "BUSL", "Proprietary")

#: A CycloneDX namespace, so the same inputs produce the same serial number.
#: A random one would make every rebuild differ in a field nobody compares on
#: purpose, which defeats diffing two SBOMs to see what moved.
_NAMESPACE = uuid.UUID("2f8b6f34-3c3e-5f1a-9f3a-0b3f6a1c9d42")


def normalize(name: str) -> str:
    """PEP 503 normalisation, so `PySide6` and `pyside6` are one package."""
    return re.sub(r"[-_.]+", "-", name).lower()


@dataclass(frozen=True, order=True)
class Component:
    """One package that ships, and where its licence claim came from."""

    name: str
    version: str
    licence: str
    source: str

    @property
    def purl(self) -> str:
        return f"pkg:pypi/{normalize(self.name)}@{self.version}"

    @property
    def needs_review(self) -> bool:
        upper = self.licence.upper()
        return any(family in upper for family in REVIEW_FAMILIES)


def _marker_ok(requirement, extras: tuple[str, ...]) -> bool:
    """Whether a requirement applies to this build.

    Evaluated once per bundled extra and once with no extra at all, because a
    dependency guarded by `extra == "dev"` must not ship while one with no
    marker must.
    """
    from packaging.requirements import Requirement

    parsed = requirement if isinstance(requirement, Requirement) else Requirement(requirement)
    if parsed.marker is None:
        return True
    # Platform markers evaluate against this interpreter, which is the one the
    # bundle is built for and with.
    if parsed.marker.evaluate({"extra": ""}):
        return True
    return any(parsed.marker.evaluate({"extra": extra}) for extra in extras)


def closure(root: str = "offloader",
            extras: tuple[str, ...] = BUNDLED_EXTRAS) -> list[str]:
    """Every distribution `root` pulls in at runtime, `root` excluded.

    Breadth first over the installed metadata. A package that is required but
    not installed is reported rather than skipped: an SBOM that silently omits
    something is worse than one that admits it could not resolve it.
    """
    from packaging.requirements import Requirement

    seen: set[str] = {normalize(root)}
    order: list[str] = []
    missing: list[str] = []
    queue = [root]

    while queue:
        current = queue.pop(0)
        try:
            requires = importlib.metadata.requires(current) or []
        except importlib.metadata.PackageNotFoundError:
            missing.append(current)
            continue
        for raw in requires:
            parsed = Requirement(raw)
            if not _marker_ok(parsed, extras):
                continue
            key = normalize(parsed.name)
            if key in seen:
                continue
            seen.add(key)
            order.append(parsed.name)
            queue.append(parsed.name)

    if missing:
        raise RuntimeError(
            "these packages are required but not installed, so the bill of "
            f"materials would be incomplete: {', '.join(sorted(missing))}")
    return sorted(order, key=normalize)


def licence_of(name: str) -> tuple[str, str]:
    """A package's licence and which metadata field it came from.

    Three fields carry it in practice and none of them always. `License-
    Expression` is the PEP 639 answer, the classifiers are the old one, and the
    free-text `License` field is whatever the author typed, including whole
    paragraphs. Recording the source matters: "BSD License" from a classifier
    is a weaker statement than an SPDX expression, and a reviewer should be
    able to tell which one they are reading.
    """
    metadata = importlib.metadata.metadata(name)

    expression = metadata.get("License-Expression")
    if expression:
        return expression.strip(), "License-Expression"

    classifiers = [value for value in metadata.get_all("Classifier") or []
                   if value.startswith("License ::")]
    if classifiers:
        return "; ".join(value.split(" :: ")[-1] for value in classifiers), "Classifier"

    free_text = (metadata.get("License") or "").strip()
    if free_text:
        first = free_text.splitlines()[0].strip()
        # A short first line is a licence name; a long one is prose that
        # happens to start with one, and truncating it silently would invent a
        # precision the metadata does not have.
        if len(first) <= 64 and len(free_text.splitlines()) == 1:
            return first, "License"
        return f"{first[:61]}...", "License (free text)"

    return "UNKNOWN", "none"


def collect(root: str = "offloader",
            extras: tuple[str, ...] = BUNDLED_EXTRAS) -> list[Component]:
    """The shipped packages, with their licences."""
    components = []
    for name in closure(root, extras):
        licence, source = licence_of(name)
        components.append(Component(
            name=name,
            version=importlib.metadata.version(name),
            licence=licence,
            source=source,
        ))
    return sorted(components)


def uncovered_in_bundle(bundle: Path) -> list[tuple[str, str, str]]:
    """The entries from `UNCOVERED` that are actually in this bundle.

    Against the built tree rather than asserted from the list, so the gap
    shrinks as it is closed and cannot be closed by editing a constant.
    """
    bundle = Path(bundle)
    found = []
    for name, pattern, why in UNCOVERED:
        if any(bundle.rglob(pattern)):
            found.append((name, pattern, why))
    return found


def _serial(components: list[Component], identity: dict) -> str:
    material = json.dumps(
        {"identity": identity,
         "components": [[c.name, c.version] for c in components]},
        sort_keys=True,
    )
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()
    return f"urn:uuid:{uuid.uuid5(_NAMESPACE, digest)}"


def _licence_entry(component: Component) -> dict:
    """CycloneDX wants an SPDX expression where there is one and a bare name
    otherwise. Claiming `expression` for "BSD License" would assert an SPDX
    identifier that does not exist."""
    if component.source == "License-Expression":
        return {"expression": component.licence}
    return {"license": {"name": component.licence}}


def cyclonedx(components: list[Component], identity: dict,
              *, version: str) -> dict:
    """A CycloneDX 1.6 document describing what ships."""
    return {
        "bomFormat": "CycloneDX",
        "specVersion": "1.6",
        "serialNumber": _serial(components, identity),
        "version": 1,
        "metadata": {
            "tools": {"components": [{
                "type": "application",
                "name": "offloader-sbom",
                "version": version,
            }]},
            "component": {
                "bom-ref": f"pkg:pypi/offloader@{version}",
                "type": "application",
                "name": "Offloader",
                "version": version,
                "purl": f"pkg:pypi/offloader@{version}",
                "licenses": [{"expression": "MIT"}],
            },
            "properties": [
                {"name": "offloader:scope", "value": SCOPE_NOTE},
                *({"name": "offloader:uncovered",
                   "value": f"{name} ({pattern}): {why}"}
                  for name, pattern, why in UNCOVERED),
                *({"name": f"offloader:{key}", "value": str(value)}
                  for key, value in sorted(identity.items())),
            ],
        },
        "components": [{
            "bom-ref": component.purl,
            "type": "library",
            "name": component.name,
            "version": component.version,
            "purl": component.purl,
            "licenses": [_licence_entry(component)],
            "properties": [
                {"name": "offloader:licence-source", "value": component.source},
            ],
        } for component in components],
    }


def notices(components: list[Component], *, version: str) -> str:
    """The human-readable inventory that ships beside the installer."""
    lines = [
        f"Third-party notices for Offloader {version}",
        "",
        "Offloader is MIT licensed. It is distributed with the packages below,",
        "each under its own licence. This file is an inventory produced from",
        "installed package metadata; it is not legal advice, and where a",
        "licence is recorded loosely the source field says so.",
        "",
        SCOPE_NOTE,
        "",
        "Not covered here:",
        *(f"  {name} ({pattern}) -- {why}" for name, pattern, why in UNCOVERED),
        "",
    ]
    width = max(len(component.name) for component in components) if components else 4
    for component in components:
        flag = "  [review]" if component.needs_review else ""
        lines.append(f"{component.name:<{width}}  {component.version:<12}"
                     f"  {component.licence}{flag}")
        lines.append(f"{'':<{width}}  recorded in: {component.source}")
    review = [component for component in components if component.needs_review]
    if review:
        lines += [
            "",
            "Marked [review]: these carry conditions on redistribution beyond",
            "attribution, and what applies to a frozen bundle is a decision",
            "for a person rather than for this tool:",
            "",
        ]
        lines += [f"  {component.name} {component.version}: {component.licence}"
                  for component in review]
    return "\n".join(lines) + "\n"


def lockfile(components: list[Component]) -> str:
    """Pinned versions for the shipped set, so it can be reproduced."""
    header = ("# The runtime closure Offloader ships, pinned. Generated by\n"
              "# build/windows/sbom.py; not the build environment, which is\n"
              "# recorded in the release inventory.\n"
              "#\n"
              "# Python distribution dependencies only. The bundled CPython\n"
              "# runtime and the PyInstaller bootloader inside each executable\n"
              "# have no packaging metadata and are not listed here.\n")
    return header + "".join(f"{component.name}=={component.version}\n"
                            for component in components)


def write_all(directory: Path, identity: dict,
              components: list[Component] | None = None) -> list[Path]:
    """Write all three files, returning them in a stable order."""
    version = identity["version"]
    if components is None:
        components = collect()
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)

    sbom = directory / f"Offloader-{version}-sbom.cyclonedx.json"
    sbom.write_text(
        json.dumps(cyclonedx(components, identity, version=version), indent=2)
        + "\n", encoding="utf-8")
    licences = directory / f"Offloader-{version}-third-party-notices.txt"
    licences.write_text(notices(components, version=version), encoding="utf-8")
    pins = directory / f"Offloader-{version}-requirements.txt"
    pins.write_text(lockfile(components), encoding="utf-8")
    return [sbom, licences, pins]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=REPO / "dist" / "windows")
    parser.add_argument("--version", default=None,
                        help="override the version recorded in the documents")
    args = parser.parse_args(argv)

    sys.path.insert(0, str(REPO / "src"))
    from offloader._version import __version__

    identity = {"version": args.version or __version__}
    components = collect()
    for path in write_all(args.out, identity, components):
        print(path)

    flagged = [component for component in components if component.needs_review]
    if flagged:
        print(f"\n{len(flagged)} package(s) need a redistribution decision:",
              file=sys.stderr)
        for component in flagged:
            print(f"  {component.name} {component.version}: {component.licence}",
                  file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
