"""The tag-triggered release workflow and its notes.

The property worth protecting is a negative one: `docs/release-plan.md`
requires every Windows download to be signed, signing needs a hardware token
that only exists on the release workstation, and hosted CI therefore builds
unsigned. So the workflow must never attach what it built to a release. That
is one line away from being wrong at any time, and wrong in a way no test of
the build itself would notice.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml", reason="PyYAML is a dev dependency")

REPO = Path(__file__).resolve().parent.parent
WORKFLOW = REPO / ".github" / "workflows" / "release.yml"


def _notes_module():
    path = REPO / "scripts" / "release_notes.py"
    spec = importlib.util.spec_from_file_location("_release_notes", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def workflow() -> dict:
    with open(WORKFLOW, encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def _steps(workflow: dict) -> list[tuple[str, dict]]:
    return [(job, step)
            for job, spec in workflow["jobs"].items()
            for step in spec["steps"]]


# -------------------------------------------------------------- the workflow


def test_it_triggers_on_version_tags(workflow):
    # PyYAML reads the bare `on` key as the boolean True.
    triggers = workflow.get("on", workflow.get(True))
    assert triggers["push"]["tags"] == ["v*"]


def test_nothing_it_builds_is_attached_to_a_release(workflow):
    """The one that matters. CI cannot sign, and an unsigned installer offered
    as a download is exactly what the release plan forbids."""
    for job, step in _steps(workflow):
        script = step.get("run", "")
        assert "release upload" not in script, \
            f"{job}/{step.get('name')} uploads a release asset"


def test_the_draft_is_created_as_a_draft(workflow):
    """A release created non-draft is public the moment it exists, which for
    an unsigned candidate is the failure this whole design avoids."""
    creating = [step for _job, step in _steps(workflow)
                if "gh release create" in step.get("run", "")]
    assert creating, "no step creates the draft"
    for step in creating:
        assert "--draft" in step["run"]


def test_write_permission_is_limited_to_the_drafting_job(workflow):
    """The build job runs third-party packaging tools; it has no business
    holding a token that can publish."""
    assert workflow["permissions"] == {"contents": "read"}
    jobs = workflow["jobs"]
    assert jobs["candidate"].get("permissions") is None
    assert jobs["draft"]["permissions"] == {"contents": "write"}


def test_the_candidate_is_built_unsigned(workflow):
    """Not a preference: the token is not present, and `build.py` refuses a
    signed build from a dirty or unauthenticated environment anyway."""
    builds = [step for _job, step in _steps(workflow)
              if "build/windows/build.py" in step.get("run", "")]
    assert builds
    for step in builds:
        assert "--no-sign" in step["run"]


def test_the_tag_is_gated_before_anything_is_built(workflow):
    """A tag that disagrees with the version literal should cost a few seconds,
    not a full packaging run and a draft that has to be deleted."""
    steps = workflow["jobs"]["candidate"]["steps"]
    scripts = [step.get("run", "") for step in steps]
    gate = next(i for i, s in enumerate(scripts) if "check_tag.py" in s)
    build = next(i for i, s in enumerate(scripts) if "build/windows/build.py" in s)
    assert gate < build


def test_the_draft_job_waits_for_the_candidate(workflow):
    assert workflow["jobs"]["draft"]["needs"] == "candidate"


def test_a_rehearsal_run_does_not_touch_releases(workflow):
    """`workflow_dispatch` exists to exercise the checks. Guarded by a ref
    condition so a manual run cannot create a draft for a tag that does not
    exist."""
    condition = workflow["jobs"]["draft"]["if"]
    assert "refs/tags/v" in condition


# ------------------------------------------------------------------ the notes


def test_the_notes_say_the_draft_is_not_publishable():
    body = _notes_module().notes("v0.4.0", "abc1234", version="0.4.0")
    assert "Not publishable" in body
    assert "signed installer" in body


def test_the_notes_name_the_artifacts_for_that_version():
    body = _notes_module().notes("v0.4.0", "abc1234", version="0.4.0")
    assert "Offloader-Setup-0.4.0.exe" in body
    assert "SHA256SUMS.txt" in body
    assert "abc1234" in body


def test_the_notes_refuse_a_tag_that_contradicts_the_version():
    """Otherwise the heading names one release and the download instructions
    name another's filenames, which reads as a broken release rather than a
    mistagged one."""
    with pytest.raises(SystemExit, match="contradict"):
        _notes_module().notes("v0.9.9", "abc1234", version="0.4.0")


@pytest.mark.parametrize("tag", ["v0.4.0", "0.4.0", "refs/tags/v0.4.0"])
def test_the_notes_accept_a_tag_in_any_of_its_forms(tag: str):
    assert _notes_module().notes(tag, "abc1234", version="0.4.0")
