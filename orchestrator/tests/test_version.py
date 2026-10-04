# Copyright (c) 2026 Mechemsi. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

"""Tests for version resolution."""

from pathlib import Path

import pytest

from rorch import version
from rorch.version import PYPROJECT, resolve_version


def _pyproject(tmp_path: Path, ver: str) -> Path:
    path = tmp_path / "pyproject.toml"
    path.write_text(f'[project]\nname = "rorch"\nversion = "{ver}"\n', encoding="utf-8")
    return path


def test_build_env_wins_over_pyproject(tmp_path: Path) -> None:
    pyproject = _pyproject(tmp_path, "1.0.0")
    assert resolve_version({"RORCH_BUILD_VERSION": "1.2.3"}, pyproject) == "1.2.3"


def test_leading_v_is_stripped(tmp_path: Path) -> None:
    pyproject = _pyproject(tmp_path, "1.0.0")
    assert resolve_version({"RORCH_BUILD_VERSION": "v1.2.3"}, pyproject) == "1.2.3"


def test_compose_image_tag_var_is_ignored(tmp_path: Path) -> None:
    # RORCH_VERSION picks the image tag in compose and can be "latest";
    # it must never be reported as the running version.
    pyproject = _pyproject(tmp_path, "1.4.0")
    assert resolve_version({"RORCH_VERSION": "latest"}, pyproject) == "1.4.0"


def test_blank_build_env_falls_back_to_pyproject(tmp_path: Path) -> None:
    pyproject = _pyproject(tmp_path, "2.0.1")
    assert resolve_version({"RORCH_BUILD_VERSION": "  "}, pyproject) == "2.0.1"


def test_falls_back_to_metadata_then_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    missing = tmp_path / "nope.toml"
    monkeypatch.setattr(version, "_from_metadata", lambda: "0.9.0")
    assert resolve_version({}, missing) == "0.9.0"
    monkeypatch.setattr(version, "_from_metadata", lambda: "")
    assert resolve_version({}, missing) == "unknown"


def test_repo_pyproject_is_found() -> None:
    assert PYPROJECT.is_file()
    assert resolve_version({}) not in ("", "unknown")
