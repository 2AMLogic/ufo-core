from pathlib import Path

import yaml

WORKFLOWS = Path(__file__).parents[3] / ".github" / "workflows"
PRE_COMMIT = Path(__file__).parents[3] / ".pre-commit-config.yaml"
RUNNER_BUILDS = ("ci.yaml", "integration.yaml", "sandbox-image.yml", "client.yml")


def _check_reaches_cargo() -> bool:
    """`make check` runs every pre-commit hook, so a cargo hook there compiles on a job whose own
    script never says `cargo`."""
    loaded = yaml.load(PRE_COMMIT.read_text(), Loader=yaml.BaseLoader)
    assert isinstance(loaded, dict)
    return any(
        "cargo" in str(hook.get("entry", ""))
        for repo in loaded["repos"]
        for hook in repo.get("hooks", [])
    )


_CHECK_RUNS_CARGO = _check_reaches_cargo()


def test_runner_builds_restore_their_package_caches() -> None:
    for workflow in RUNNER_BUILDS:
        loaded = yaml.load((WORKFLOWS / workflow).read_text(), Loader=yaml.BaseLoader)
        assert isinstance(loaded, dict)
        jobs = loaded["jobs"]
        assert isinstance(jobs, dict)

        for job_name, job in jobs.items():
            assert isinstance(job, dict)
            steps = job.get("steps", [])
            assert isinstance(steps, list)
            script = "\n".join(step.get("run", "") for step in steps if isinstance(step, dict))

            if "uv " in script:
                setup = [
                    step
                    for step in steps
                    if isinstance(step, dict)
                    and str(step.get("uses", "")).startswith("astral-sh/setup-uv@")
                ]
                assert len(setup) == 1, job_name
                assert setup[0]["with"]["enable-cache"] == "true", job_name
                first_install = next(
                    index
                    for index, step in enumerate(steps)
                    if isinstance(step, dict) and "uv " in step.get("run", "")
                )
                assert steps.index(setup[0]) < first_install, job_name

            if "cargo " in script or ("make check" in script and _CHECK_RUNS_CARGO):
                cache = [
                    step
                    for step in steps
                    if isinstance(step, dict)
                    and str(step.get("uses", "")).startswith("Swatinem/rust-cache@")
                ]
                assert cache, job_name
                first_build = next(
                    index
                    for index, step in enumerate(steps)
                    if isinstance(step, dict)
                    and ("cargo " in step.get("run", "") or "make check" in step.get("run", ""))
                )
                assert all(steps.index(step) < first_build for step in cache), job_name
