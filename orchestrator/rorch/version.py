# Copyright (c) 2026 Mechemsi. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

"""The running orchestrator's version, as released by semantic-release.

Resolution order, first hit wins:

1. ``RORCH_BUILD_VERSION`` - baked into published images by the release
   workflow. Deliberately not ``RORCH_VERSION``: compose reads that one from
   ``.env`` to pick an image tag, and ``env_file: .env`` would leak a value like
   ``latest`` into the container and override the real version.
2. ``pyproject.toml`` next to the package - semantic-release bumps it in the
   release commit the tag points at, so a host build from a tag checkout
   reports that tag.
3. Installed package metadata (``pip install -e .`` in development).
"""

import os
import tomllib
from collections.abc import Mapping
from functools import lru_cache
from importlib import metadata
from pathlib import Path

UNKNOWN = "unknown"
PYPROJECT = Path(__file__).resolve().parent.parent / "pyproject.toml"


def _normalise(raw: str) -> str:
    raw = raw.strip()
    return raw[1:] if raw.startswith("v") and raw[1:2].isdigit() else raw


def _from_pyproject(path: Path) -> str:
    try:
        with path.open("rb") as fh:
            return str(tomllib.load(fh)["project"]["version"])
    except (OSError, KeyError, TypeError, tomllib.TOMLDecodeError):
        return ""


def _from_metadata() -> str:
    try:
        return metadata.version("rorch")
    except metadata.PackageNotFoundError:
        return ""


def resolve_version(env: Mapping[str, str] | None = None, pyproject: Path = PYPROJECT) -> str:
    source = os.environ if env is None else env
    for candidate in (source.get("RORCH_BUILD_VERSION", ""), _from_pyproject(pyproject)):
        if candidate.strip():
            return _normalise(candidate)
    return _normalise(_from_metadata()) or UNKNOWN


@lru_cache(maxsize=1)
def get_version() -> str:
    return resolve_version()
