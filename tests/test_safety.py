"""Tests for safety validation: algorithms, paths, plan limits."""

import os
from pathlib import Path

import pytest

from gis_assistant.safety import (
    ALLOWED_ALGORITHMS,
    check_overwrite,
    is_algorithm_allowed,
    resolve_algorithm,
    validate_input_path_exists,
    validate_output_path,
    validate_plan_size,
)


# -- Algorithm resolution ----------------------------------------------------

def test_resolve_algorithm_exact():
    assert resolve_algorithm("native:buffer") == "native:buffer"


def test_resolve_algorithm_synonym():
    assert resolve_algorithm("native:reproject") == "native:reprojectlayer"


def test_resolve_algorithm_unknown_raises():
    with pytest.raises(ValueError, match="not in the allowlist"):
        resolve_algorithm("native:foobar")


def test_is_algorithm_allowed_true():
    assert is_algorithm_allowed("native:buffer") is True


def test_is_algorithm_allowed_false():
    assert is_algorithm_allowed("native:dangerous") is False


# -- Path validation ---------------------------------------------------------

def test_validate_output_path_within_workspace(tmp_path):
    workspace = str(tmp_path / "workspace")
    os.makedirs(workspace)
    output = os.path.join(workspace, "output.gpkg")
    result = validate_output_path(output, workspace)
    assert result == str(Path(output).resolve())


def test_validate_output_path_escape_raises(tmp_path):
    workspace = str(tmp_path / "workspace")
    os.makedirs(workspace)
    output = os.path.join(workspace, "..", "escaped.gpkg")
    with pytest.raises(ValueError, match="outside the workspace"):
        validate_output_path(output, workspace)


def test_validate_output_path_symlink_escape(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    # Create a symlink inside workspace pointing outside
    link = workspace / "sneaky_link"
    link.symlink_to(outside)
    output = str(link / "output.gpkg")
    with pytest.raises(ValueError, match="outside the workspace"):
        validate_output_path(output, str(workspace))


def test_validate_output_path_tilde_expansion(tmp_path):
    # Just ensure it doesn't crash on a valid path
    workspace = str(tmp_path / "workspace")
    os.makedirs(workspace)
    output = os.path.join(workspace, "subdir", "output.gpkg")
    result = validate_output_path(output, workspace)
    assert "workspace" in result


def test_validate_input_path_exists(tmp_path):
    f = tmp_path / "data.gpkg"
    f.write_text("fake")
    result = validate_input_path_exists(str(f))
    assert result == str(f.resolve())


def test_validate_input_path_missing(tmp_path):
    with pytest.raises(FileNotFoundError):
        validate_input_path_exists(str(tmp_path / "nope.gpkg"))


# -- Overwrite detection -----------------------------------------------------

def test_check_overwrite_nonexistent(tmp_path):
    assert check_overwrite(str(tmp_path / "nope.gpkg")) is False


def test_check_overwrite_exists(tmp_path):
    f = tmp_path / "exists.gpkg"
    f.write_text("data")
    assert check_overwrite(str(f)) is True


# -- Plan limits -------------------------------------------------------------

def test_validate_plan_size_ok():
    validate_plan_size(20)  # Should not raise


def test_validate_plan_size_exceeds():
    with pytest.raises(ValueError, match="exceeding the maximum"):
        validate_plan_size(21)
