"""The options bundle the desktop app hands to the queue.

A `Preset` is everything about a job except which card is offloaded: where the
copies go, how they are verified, and what paperwork comes out. The desktop
app builds one from its form for each job; it is not saved on its own (the
form's settings persist in `settings.json`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .engine import DEFAULT_EXCLUDES, OffloadOptions
from .models import Profile, VerificationMode
from .naming import DEFAULT_TEMPLATE
from .retry import RetryPolicy


@dataclass
class Preset:
    name: str
    destinations: list[Path] = field(default_factory=list)
    algorithm: str = "xxh3-64"
    verification: VerificationMode = VerificationMode.SOURCE_ONLY
    profile: Profile = Profile.MEDIA
    thumbnail_count: int = 4
    reports: list[str] = field(default_factory=lambda: ["pdf"])
    preserve_structure: bool = True
    skip_existing: bool = False
    excludes: list[str] = field(default_factory=list)
    naming_template: str = DEFAULT_TEMPLATE
    retry_attempts: int = 3
    retry_wait: float = 2.0
    logo: Path | None = None
    footer: str | None = None

    def __post_init__(self) -> None:
        # An empty template would render an empty job name, and therefore a
        # report folder called "_Reports". Normalise here so the value is the
        # same however the bundle was built.
        if not (self.naming_template or "").strip():
            self.naming_template = DEFAULT_TEMPLATE
        if not (self.name or "").strip():
            self.name = "Untitled"

    # ---------------------------------------------------------------- engine
    def to_options(self, job_name: str | None = None) -> OffloadOptions:
        return OffloadOptions(
            destinations=list(self.destinations),
            algorithm=self.algorithm,
            verification=self.verification,
            thumbnail_count=self.thumbnail_count,
            excludes=tuple(DEFAULT_EXCLUDES) + tuple(self.excludes),
            preserve_structure=self.preserve_structure,
            skip_existing=self.skip_existing,
            job_name=job_name,
            # Metadata is cheap next to the copy itself and useful even when
            # thumbnails are switched off, so it is always collected — unless
            # this is a data-transfer job, where OffloadOptions turns media
            # probing off for the profile.
            extra_probe=True,
            profile=self.profile,
            retry=RetryPolicy(attempts=max(1, self.retry_attempts),
                              delay=max(0.0, self.retry_wait)),
        )
