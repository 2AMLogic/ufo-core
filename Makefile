.DEFAULT_GOAL := help

DEBUGGER := extensions/debugger/frontend
EMAIL ?= $(shell git config user.email)
T ?=
FILE ?=
SHARD ?=
PYTEST_TIMEOUT_SECONDS := 120

.PHONY: help install reinstall build debugger init serve db check fmt test test-one test-preview check-preview \
	test-client test-client-load cover-client bench-client check-client test-integration

help: ## List targets
	@grep -hE '^[a-z][a-z-]*:.*## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN{FS=":.*## "}{printf "  %-18s %s\n", $$1, $$2}'

install: ## Sync Python deps and git hooks
	uv sync
	uv run pre-commit install

reinstall: ## Rebuild the wheel into the venv — single-file extensions are copied, not linked
	uv sync --reinstall-package ufo

build: ## Build the ufo terminal client
	cargo build --manifest-path client/Cargo.toml --locked

debugger: ## Build the session debugger page (needs pnpm)
	pnpm -C $(DEBUGGER) install --frozen-lockfile
	pnpm -C $(DEBUGGER) run build

init: ## Write ufo.toml, apply the schema, onboard the workspace (EMAIL=you@example.com)
	@test -n "$(EMAIL)" || { echo "EMAIL is required: make init EMAIL=you@example.com"; exit 1; }
	uv run ufoctl init --email $(EMAIL)

serve: ## Run surfaces, workers, and jobs on :8710 (needs UFO_ANTHROPIC_API_KEY)
	uv run ufoctl serve

db: ## Start the Postgres (:5541) and Redis (:5543) the test matrix needs
	docker compose up -d

check: ## Run every static gate CI runs — ruff, gates.py, mypy, actionlint, cargo fmt and clippy
	uv run pre-commit run --all-files --hook-stage pre-push

fmt: ## Format the tree
	uv run ruff format

test: ## Run the parallel suite; T=<paths> narrows it; SHARD=1/10 runs one slice
	uv run pytest $(if $(T),,-n auto) $(if $(SHARD),--shard $(SHARD)) \
		--timeout $(PYTEST_TIMEOUT_SECONDS) --timeout-method thread \
		-m "not serial and not integration and not docker" -q $(T)

test-one: reinstall ## Run one file or node id serially (FILE=path) — xdist only pays above ~100 tests
	@test -n "$(FILE)" || { echo "FILE is required: make test-one FILE=core/tests/test_hooks.py"; exit 1; }
	uv run pytest -m "not integration and not docker" -q $(FILE)

test-preview: ## Run the preview renderer suite — needs soffice and preview setup scripts
	@lib=$$(ls servers/preview/.pdfium/libpdfium.* 2>/dev/null | head -n1); \
	test -n "$$lib" || { echo "missing servers/preview/.pdfium — run servers/preview/scripts/fetch-pdfium.sh first" >&2; exit 1; }; \
	profile=servers/preview/.soffice-profile; \
	if ! test -s "$$profile/.ufo-preview-version" \
		|| ! test -s "$$profile/user/extensions/buildid" \
		|| ! grep -q 'ooSetupLastVersion' "$$profile/user/registrymodifications.xcu"; then \
		echo "missing $$profile — run servers/preview/scripts/seed-soffice-profile.sh $$profile first" >&2; \
		exit 1; \
	fi; \
	UFO_PREVIEW_PDFIUM_LIB="$$PWD/$$lib" UFO_PREVIEW_SOFFICE_PROFILE="$$PWD/$$profile" \
	sh -c 'cd servers/preview && cargo test -- --include-ignored --test-threads=4'

check-preview: ## Run the preview crate's static gates — fmt and clippy
	cd servers/preview && cargo fmt --check && cargo clippy --all-targets -- -D warnings

test-client: ## Run the client crate's suite
	cd client && cargo nextest run

test-client-load: ## Run the client crate's suite with every core busy — hunts wall-clock fragility
	cd client && hogs=$$(( $$(getconf _NPROCESSORS_ONLN) * 2 )); pids=""; i=0; \
	while [ $$i -lt $$hogs ]; do (while :; do :; done) & pids="$$pids $$!"; i=$$((i + 1)); done; \
	cargo nextest run; status=$$?; kill $$pids 2>/dev/null; exit $$status

cover-client: ## Measure the client crate's coverage — fails below the line floor
	cd client && cargo llvm-cov nextest --branch --fail-under-lines 89

bench-client: ## Benchmark the client crate
	cd client && cargo bench

check-client: ## Run the client crate's static gates — fmt and clippy
	cd client && cargo fmt --check && cargo clippy --all-targets -- -D warnings

test-integration: ## Run the serial, docker, and live-dependency pass (needs Docker and Postgres); SHARD=1/3 runs one slice
	UFO_INTEGRATION_REQUIRED=1 uv run pytest -rs -m "serial or integration or docker" $(if $(SHARD),--shard $(SHARD))
