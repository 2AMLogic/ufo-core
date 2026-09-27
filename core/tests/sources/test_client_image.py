from pathlib import Path

import yaml

ROOT = Path(__file__).parents[3]
ACTION = ROOT / ".github" / "actions" / "client-gh" / "action.yml"
GH_SCRIPT = ROOT / "client" / "scripts" / "build-gh.sh"
BUILD_SCRIPT = ROOT / "client" / "build.rs"
RELEASE_JOBS = {
    "ci.yaml": ("sandbox-client",),
    "client.yml": ("bench", "build"),
    "integration.yaml": ("integration",),
    "sandbox-image.yml": ("publish",),
}
INTEGRATION = ROOT / ".github" / "workflows" / "integration.yaml"
CLIENT_PATH_STEP = (
    'echo "$GITHUB_WORKSPACE/client/target/x86_64-unknown-linux-musl/release" >> "$GITHUB_PATH"'
)


def _check_every_release_job_builds_the_gh_payload_first() -> None:
    for filename, jobs in RELEASE_JOBS.items():
        workflow = yaml.safe_load((ROOT / ".github" / "workflows" / filename).read_text())
        for name in jobs:
            steps = workflow["jobs"][name]["steps"]
            releases = [
                index
                for index, step in enumerate(steps)
                if "cargo build --release" in step.get("run", "")
                or "cargo codspeed build" in step.get("run", "")
            ]
            assert releases
            for index in releases:
                assert any(
                    step.get("uses") == "./.github/actions/client-gh" for step in steps[:index]
                )


def _check_gh_payload_is_pinned_to_the_runtime_that_reads_the_bundle() -> None:
    steps = yaml.safe_load(ACTION.read_text())["runs"]["steps"]
    (setup,) = [step for step in steps if step.get("uses") == "actions/setup-go@v6"]
    assert setup["with"]["go-version"] == "1.27.0"
    script = GH_SCRIPT.read_text()
    assert "github.com/cli/cli/v2/cmd/gh@v2.99.0" in script
    assert "GOTOOLCHAIN=local" in script
    assert "gzip -9" in script
    assert "cargo:rerun-if-changed=" in BUILD_SCRIPT.read_text()
    (build,) = [step for step in steps if step.get("shell") == "bash"]
    assert "cygpath -w" in build["run"]


def _check_gh_payload_is_cached_on_the_pins_that_determine_it() -> None:
    steps = yaml.safe_load(ACTION.read_text())["runs"]["steps"]
    (cache,) = [step for step in steps if str(step.get("uses", "")).startswith("actions/cache@")]
    assert cache["with"]["path"] == "${{ runner.temp }}/ufo-gh-${{ inputs.target }}.gz"
    assert cache["with"]["key"] == (
        "ufo-gh-${{ inputs.target }}-"
        "${{ hashFiles(format('{0}/scripts/build-gh.sh', inputs.client_dir)) }}"
    )
    hit = f"steps.{cache['id']}.outputs.cache-hit"
    (setup,) = [step for step in steps if step.get("uses") == "actions/setup-go@v6"]
    assert setup["if"] == f"{hit} != 'true'"
    (build,) = [step for step in steps if step.get("shell") == "bash"]
    assert f'if [ "${{{{ {hit} }}}}" != true ]; then' in build["run"]
    assert '"${{ inputs.client_dir }}/scripts/build-gh.sh"' in build["run"]
    assert 'gzip -t "$archive"' in build["run"]
    assert steps.index(cache) < steps.index(setup) < steps.index(build)


def _check_integration_puts_the_client_it_builds_on_path() -> None:
    workflow = yaml.safe_load(INTEGRATION.read_text())
    steps = workflow["jobs"]["integration"]["steps"]
    build = next(
        index
        for index, step in enumerate(steps)
        if step.get("run") == "cargo build --release --target x86_64-unknown-linux-musl"
    )
    path = next(index for index, step in enumerate(steps) if step.get("run") == CLIENT_PATH_STEP)
    test = next(
        index
        for index, step in enumerate(steps)
        if step.get("run", "").startswith("make test-integration")
    )
    assert build < path < test


def test_client_image_contract() -> None:
    checks = tuple(value for name, value in globals().items() if name.startswith("_check_"))
    assert len(checks) == 4
    for check in checks:
        check()
