"""Shared locations. The skills are data on disk, so the contract tests read them directly and need
no `ufo` in the environment; only `test_registry.py` needs the runtime."""

import importlib.util
from pathlib import Path

import pytest

SKILLS_ROOT = Path(__file__).resolve().parents[1] / "ufo_ext_pulse" / "skills"
SKILL_NAMES = (
    "pulse-handoff",
    "field-pulse",
    "field-report",
    "brief-continuity",
    "coverage-honesty",
)


@pytest.fixture
def covered(tmp_path, monkeypatch):
    """The ledger module, loaded from the path a skill load puts it at, over a temporary workspace.

    The working directory is the workspace, the way a sandbox command runs, because that is what the
    relative ledger path resolves against."""
    monkeypatch.chdir(tmp_path)
    spec = importlib.util.spec_from_file_location(
        "covered", SKILLS_ROOT / "brief-continuity" / "covered.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def seen(tmp_path, monkeypatch):
    """The sightings pool, loaded and run the same way as `covered`: over a temporary workspace, with
    the working directory as the workspace, because that is what the relative pool path resolves
    against."""
    monkeypatch.chdir(tmp_path)
    spec = importlib.util.spec_from_file_location(
        "seen", SKILLS_ROOT / "brief-continuity" / "seen.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def coverage(tmp_path, monkeypatch):
    """The coverage-state store, loaded and run the way the other two are: over a temporary
    workspace, with the working directory as the workspace the relative store path resolves
    against."""
    monkeypatch.chdir(tmp_path)
    spec = importlib.util.spec_from_file_location(
        "coverage_state", SKILLS_ROOT / "brief-continuity" / "coverage.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
