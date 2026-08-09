"""Saved offload configurations.

A preset is everything about a job except which card you drop on it: where the
copies go, how they are verified, and what paperwork comes out. Presets carry a
colour so a crew can tell "dailies" from "archive" at a glance on a busy cart.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field, replace
from pathlib import Path

from .config import config_file, read_json, write_json
from .engine import DEFAULT_EXCLUDES, OffloadOptions
from .hashers import ALGORITHMS
from .models import Profile, VerificationMode
from .naming import DEFAULT_TEMPLATE
from .retry import RetryPolicy

PRESETS_FILE = "presets.json"

#: Colour-coding swatches, readable on both light and dark chrome.
PRESET_COLORS = [
    "#5577b0",  # blue
    "#3f9e6b",  # green
    "#c9822b",  # amber
    "#b3565b",  # red
    "#8464b5",  # violet
    "#3f9aa8",  # teal
    "#8a8f98",  # grey
]

SORT_MODES = ("name", "color", "in-use")


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
    #: Read every source file twice and compare. For irreplaceable material.
    paranoid: bool = False
    excludes: list[str] = field(default_factory=list)
    naming_template: str = DEFAULT_TEMPLATE
    retry_attempts: int = 3
    retry_wait: float = 2.0
    color: str = PRESET_COLORS[0]
    logo: Path | None = None
    footer: str | None = None
    use_count: int = 0
    last_used: str | None = None

    def __post_init__(self) -> None:
        # An empty template would render an empty job name, and therefore a
        # report folder called "_Reports". Normalise here so the value is the
        # same however the preset was built — constructed, loaded, or edited.
        if not (self.naming_template or "").strip():
            self.naming_template = DEFAULT_TEMPLATE
        if not (self.name or "").strip():
            self.name = "Untitled"

    # ---------------------------------------------------------------- state
    @property
    def in_use(self) -> bool:
        return self.use_count > 0

    @property
    def is_runnable(self) -> bool:
        return bool(self.destinations)

    def summary(self) -> str:
        """One-line description for the preset list."""
        where = (f"{len(self.destinations)} destination"
                 f"{'s' if len(self.destinations) != 1 else ''}")
        verify = {
            VerificationMode.NONE: "no verification",
            VerificationMode.SOURCE_ONLY: "source-only verify",
            VerificationMode.FULL: "full verify",
        }[self.verification]
        parts = [where, self.algorithm, verify, ", ".join(self.reports) or "no reports"]
        if self.profile is Profile.DATA:
            parts.insert(0, "data")
        return " · ".join(parts)

    def mark_used(self) -> None:
        self.use_count += 1
        self.last_used = _dt.datetime.now().isoformat(timespec="seconds")

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
            # this is a data-transfer preset, where OffloadOptions turns media
            # probing off for the profile.
            extra_probe=True,
            profile=self.profile,
            retry=RetryPolicy(attempts=max(1, self.retry_attempts),
                              delay=max(0.0, self.retry_wait)),
            paranoid=self.paranoid,
        )

    # ---------------------------------------------------------------- codec
    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "destinations": [str(p) for p in self.destinations],
            "algorithm": self.algorithm,
            "verification": self.verification.value,
            "profile": self.profile.value,
            "thumbnail_count": self.thumbnail_count,
            "reports": list(self.reports),
            "preserve_structure": self.preserve_structure,
            "skip_existing": self.skip_existing,
            "paranoid": self.paranoid,
            "excludes": list(self.excludes),
            "naming_template": self.naming_template,
            "retry_attempts": self.retry_attempts,
            "retry_wait": self.retry_wait,
            "color": self.color,
            "logo": str(self.logo) if self.logo else None,
            "footer": self.footer,
            "use_count": self.use_count,
            "last_used": self.last_used,
        }

    @classmethod
    def from_dict(cls, data: dict) -> Preset:
        """Load a preset from JSON, tolerating anything.

        `dict.get(key, default)` returns None when the key is *present* with a
        null value, which is exactly what a hand-edited or version-skewed
        config produces. Every field therefore falls back when the value is
        missing **or** null — a config must never brick the app.
        """
        def value(key, fallback):
            got = data.get(key)
            return fallback if got is None else got

        def as_int(key, fallback):
            try:
                return int(value(key, fallback))
            except (TypeError, ValueError):
                return fallback

        def as_float(key, fallback):
            try:
                return float(value(key, fallback))
            except (TypeError, ValueError):
                return fallback

        def as_list(key):
            got = value(key, [])
            return list(got) if isinstance(got, (list, tuple)) else []

        try:
            verification = VerificationMode(value("verification", "source-only"))
        except (ValueError, TypeError):
            verification = VerificationMode.SOURCE_ONLY

        try:
            profile = Profile(value("profile", "media"))
        except (ValueError, TypeError):
            profile = Profile.MEDIA

        algorithm = value("algorithm", "xxh3-64")
        if algorithm not in ALGORITHMS:
            algorithm = "xxh3-64"

        return cls(
            name=str(value("name", "Untitled")) or "Untitled",
            destinations=[Path(p) for p in as_list("destinations")],
            algorithm=algorithm,
            verification=verification,
            profile=profile,
            thumbnail_count=as_int("thumbnail_count", 4),
            # An explicitly empty list is a real choice — offload without
            # paperwork — so only a missing or null key falls back to the
            # default.
            reports=([r for r in as_list("reports") if isinstance(r, str)]
                     if data.get("reports") is not None else ["pdf"]),
            preserve_structure=bool(value("preserve_structure", True)),
            skip_existing=bool(value("skip_existing", False)),
            paranoid=bool(value("paranoid", False)),
            excludes=[e for e in as_list("excludes") if isinstance(e, str)],
            naming_template=str(value("naming_template", DEFAULT_TEMPLATE)),
            retry_attempts=as_int("retry_attempts", 3),
            retry_wait=as_float("retry_wait", 2.0),
            color=str(value("color", PRESET_COLORS[0])),
            logo=Path(data["logo"]) if data.get("logo") else None,
            footer=data.get("footer"),
            use_count=as_int("use_count", 0),
            last_used=data.get("last_used"),
        )


def default_presets() -> list[Preset]:
    """Seeded on first run so the preset list is never an empty void."""
    return [
        Preset(
            name="Dailies — single copy",
            algorithm="xxh3-64",
            verification=VerificationMode.SOURCE_ONLY,
            reports=["pdf"],
            color=PRESET_COLORS[0],
            naming_template="{card}",
        ),
        Preset(
            name="Archive — two copies, full verify",
            algorithm="xxh3-64",
            verification=VerificationMode.FULL,
            reports=["pdf", "mhl", "csv"],
            color=PRESET_COLORS[1],
            naming_template="{card}_{date}",
        ),
    ]


class PresetStore:
    """The preset collection, persisted as JSON in the config directory."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or config_file(PRESETS_FILE)
        self.presets: list[Preset] = []
        self.load()

    def load(self) -> None:
        payload = read_json(self.path, None)
        if payload is None:
            self.presets = default_presets()
            self.save()
            return
        self.presets = [Preset.from_dict(item) for item in payload]

    def save(self) -> None:
        write_json(self.path, [preset.to_dict() for preset in self.presets])

    # ---------------------------------------------------------------- CRUD
    def add(self, preset: Preset) -> Preset:
        preset.name = self._unique_name(preset.name)
        self.presets.append(preset)
        self.save()
        return preset

    def update(self, index: int, preset: Preset) -> None:
        current = self.presets[index]
        if preset.name != current.name:
            preset.name = self._unique_name(preset.name, skip=index)
        self.presets[index] = preset
        self.save()

    def remove(self, index: int) -> None:
        del self.presets[index]
        self.save()

    def duplicate(self, index: int) -> Preset:
        source = self.presets[index]
        copy = replace(
            source,
            name=self._unique_name(f"{source.name} copy"),
            destinations=list(source.destinations),
            reports=list(source.reports),
            excludes=list(source.excludes),
            use_count=0,
            last_used=None,
        )
        self.presets.append(copy)
        self.save()
        return copy

    def move(self, index: int, offset: int) -> int:
        """Reorder a preset; returns its new index."""
        target = max(0, min(len(self.presets) - 1, index + offset))
        if target != index:
            self.presets.insert(target, self.presets.pop(index))
        return target

    # ---------------------------------------------------------------- views
    def sorted(self, mode: str = "name") -> list[Preset]:
        if mode == "color":
            order = {color: i for i, color in enumerate(PRESET_COLORS)}
            return sorted(self.presets,
                          key=lambda p: (order.get(p.color, 99), p.name.casefold()))
        if mode == "in-use":
            return sorted(self.presets,
                          key=lambda p: (-p.use_count, p.name.casefold()))
        return sorted(self.presets, key=lambda p: p.name.casefold())

    def names(self) -> list[str]:
        return [preset.name for preset in self.presets]

    def _unique_name(self, name: str, skip: int | None = None) -> str:
        taken = {
            preset.name.casefold()
            for index, preset in enumerate(self.presets) if index != skip
        }
        candidate = (name or "Untitled").strip() or "Untitled"
        if candidate.casefold() not in taken:
            return candidate
        for suffix in range(2, 1000):
            trial = f"{candidate} {suffix}"
            if trial.casefold() not in taken:
                return trial
        return candidate
