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
import os
import shutil
import subprocess
import sys
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


def _evaluate(condition: str, **context: str) -> bool:
    """The slice of GitHub's expression syntax this workflow uses.

    Written out rather than pattern-matched on the condition text, because the
    defect here was a condition that read correctly and was true in a case
    nobody had enumerated. Three events, one of them allowed.
    """
    def value(token: str) -> str:
        token = token.strip()
        if token.startswith("'") and token.endswith("'"):
            return token[1:-1]
        return context[token]

    for clause in (part.strip() for part in condition.split("&&")):
        if clause.startswith("startsWith(") and clause.endswith(")"):
            left, right = clause[len("startsWith("):-1].split(",")
            if not value(left).startswith(value(right)):
                return False
        elif "==" in clause:
            left, right = clause.split("==")
            if value(left) != value(right):
                return False
        else:
            raise AssertionError(f"unsupported condition clause: {clause!r}")
    return True


@pytest.mark.parametrize("event,ref,allowed", [
    ("push", "refs/tags/v0.1.0b1", True),
    ("workflow_dispatch", "refs/heads/main", False),
    ("workflow_dispatch", "refs/tags/v0.1.0b1", False),
])
def test_only_a_pushed_tag_may_mutate_a_release(workflow, event, ref, allowed):
    """REGRESSION. The ref test alone let the third case through: a dispatch
    can be started against an existing tag, and `github.ref` is a tag ref then
    too. The rehearsal took the write token and edited the release, including
    passing `--draft` to one that had been published."""
    condition = workflow["jobs"]["draft"]["if"]
    assert _evaluate(condition, **{"github.event_name": event,
                                   "github.ref": ref}) is allowed


def test_a_rehearsal_checks_out_the_commit_it_was_started_from(workflow):
    """REGRESSION. The dispatch input was also used as the checkout ref, so
    entering a proposed tag that has no ref yet failed in checkout before
    `check_tag.py` could validate it -- which is the whole documented purpose
    of the rehearsal."""
    checkout = next(step for step in workflow["jobs"]["candidate"]["steps"]
                    if str(step.get("uses", "")).startswith("actions/checkout"))
    assert "inputs.tag" not in checkout["with"]["ref"]
    assert "github.ref" in checkout["with"]["ref"]


def test_the_proposed_tag_still_reaches_the_version_gate(workflow):
    """The other half: not using it as a ref must not mean ignoring it."""
    gate = next(step for step in workflow["jobs"]["candidate"]["steps"]
                if "check_tag.py" in step.get("run", ""))
    assert "inputs.tag" in gate["run"]


def test_the_draft_classification_comes_from_the_gated_version(workflow):
    """REGRESSION. `--prerelease` was passed unconditionally, so a stable tag
    published a prerelease. The updater is not misled by that (it reads the
    releases collection, skips drafts, and takes the channel from the tag's
    version), but GitHub is: a prerelease never becomes the "Latest" release,
    so `/releases/latest` and the releases page went on pointing manual
    downloads at the release before it."""
    assert workflow["jobs"]["candidate"]["outputs"]["prerelease"]
    step = next(step for _job, step in _steps(workflow)
                if "gh release create" in step.get("run", ""))
    assert "--prerelease=\"$PRERELEASE\"" in step["run"]
    # Both paths, so a rerun after a version change corrects an existing
    # draft rather than inheriting whatever the first run chose.
    assert step["run"].count('--prerelease="$PRERELEASE"') == 2
    assert step["env"]["PRERELEASE"] == "${{ needs.candidate.outputs.prerelease }}"


# ----------------------------------------------- the drafting step, executed
#
# The checks above read the YAML. These run the drafting step's own script
# under bash with `gh` and `python` replaced by stubs that record how they were
# called, because the defect below was in the order of shell commands, not in
# any text a pattern could match.

_GH_STUB = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$GH_LOG"
if [ "$1 $2" = "release view" ]; then
  case "$STUB_RELEASE" in
    none) echo "release not found" >&2; exit 1 ;;
    draft) echo true ;;
    published) echo false ;;
  esac
fi
exit 0
"""

_PYTHON_STUB = """#!/usr/bin/env bash
while [ "$#" -gt 0 ]; do
  if [ "$1" = "--out" ]; then echo notes > "$2"; fi
  shift
done
"""


def _drafting_script(workflow: dict) -> str:
    return next(step["run"] for step in workflow["jobs"]["draft"]["steps"]
                if "gh release create" in step.get("run", ""))


def _run_drafting_step(script: str, tmp_path: Path, release: str):
    stubs = tmp_path / "bin"
    stubs.mkdir()
    for name, body in (("gh", _GH_STUB), ("python", _PYTHON_STUB)):
        stub = stubs / name
        stub.write_text(body, encoding="utf-8")
        stub.chmod(0o755)
    log = tmp_path / "gh.log"
    log.touch()
    env = {**os.environ,
           "PATH": f"{stubs}{os.pathsep}{os.environ.get('PATH', '')}",
           "GH_LOG": str(log), "STUB_RELEASE": release,
           "TAG": "v0.1.0b1", "VERSION": "0.1.0b1", "PRERELEASE": "true",
           "GITHUB_SHA": "abc1234"}
    result = subprocess.run(["bash", "-c", script], cwd=tmp_path, env=env,
                            capture_output=True, text=True, timeout=30)
    return result, log.read_text(encoding="utf-8").splitlines()


_needs_bash = pytest.mark.skipif(
    sys.platform == "win32" or shutil.which("bash") is None,
    reason="runs the step under bash with POSIX executable stubs")


@_needs_bash
def test_a_rerun_leaves_a_published_release_untouched(workflow, tmp_path):
    """REGRESSION. The event guard stops a dispatch, but a rerun of the
    original tag push is still a push of that tag. Once its draft had been
    published, the existence check succeeded and the step ran `gh release
    edit --draft`, withdrawing the live release and replacing its notes with
    candidate instructions."""
    result, calls = _run_drafting_step(_drafting_script(workflow), tmp_path,
                                       "published")
    assert result.returncode != 0
    assert "already published" in result.stdout
    assert not [c for c in calls if c.startswith(("release edit",
                                                   "release create"))], calls


@_needs_bash
def test_a_rerun_refreshes_an_existing_draft(workflow, tmp_path):
    result, calls = _run_drafting_step(_drafting_script(workflow), tmp_path,
                                       "draft")
    assert result.returncode == 0, result.stderr
    edits = [c for c in calls if c.startswith("release edit")]
    assert len(edits) == 1 and "--draft" in edits[0]
    assert not [c for c in calls if c.startswith("release create")]


@_needs_bash
def test_a_first_run_creates_the_draft(workflow, tmp_path):
    result, calls = _run_drafting_step(_drafting_script(workflow), tmp_path,
                                       "none")
    assert result.returncode == 0, result.stderr
    creates = [c for c in calls if c.startswith("release create")]
    assert len(creates) == 1 and "--draft" in creates[0]
    assert not [c for c in calls if c.startswith("release edit")]


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
