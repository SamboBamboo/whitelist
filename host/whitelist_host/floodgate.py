"""Read the REAL Floodgate config (§4 drift guard).

The Worker cannot see this file, so it pins its own copy of the two values
that shape normalization. The daemon reads the real ones here and refuses to
match when they disagree — a silent mismatch produces zero matches and looks
exactly like "nobody has applied".
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from . import NORMALIZATION_VERSION


@dataclass(frozen=True)
class FloodgateSettings:
    prefix: str
    replace_spaces: bool


class DriftError(Exception):
    pass


def read_floodgate_config(path: str | Path) -> FloodgateSettings:
    p = Path(path)
    if not p.is_file():
        raise DriftError(f"Floodgate config not found at {p}; cannot pin normalization")
    data = yaml.safe_load(p.read_text()) or {}
    if "username-prefix" not in data or "replace-spaces" not in data:
        raise DriftError(
            f"Floodgate config {p} lacks username-prefix / replace-spaces; "
            "refusing to guess"
        )
    prefix = str(data["username-prefix"])
    replace = bool(data["replace-spaces"])
    return FloodgateSettings(prefix=prefix, replace_spaces=replace)


def check_drift(
    worker_norm: dict, floodgate: FloodgateSettings
) -> list[str]:
    """Compare the Worker's pinned normalization config against the real
    Floodgate settings and this code's contract version. Returns a list of
    human-readable mismatches; empty means safe to match."""
    problems: list[str] = []
    if worker_norm.get("username_prefix") != floodgate.prefix:
        problems.append(
            f"username-prefix: worker pins {worker_norm.get('username_prefix')!r}, "
            f"Floodgate really uses {floodgate.prefix!r}"
        )
    if bool(worker_norm.get("replace_spaces")) != floodgate.replace_spaces:
        problems.append(
            f"replace-spaces: worker pins {worker_norm.get('replace_spaces')!r}, "
            f"Floodgate really uses {floodgate.replace_spaces!r}"
        )
    if worker_norm.get("version") != NORMALIZATION_VERSION:
        problems.append(
            f"normalization version: worker pins {worker_norm.get('version')!r}, "
            f"this code implements {NORMALIZATION_VERSION}"
        )
    return problems
