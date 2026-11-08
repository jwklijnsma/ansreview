# ansreview: common tasks. `make` or `make help` lists them.

PYTHON ?= python3
BUMP   ?= patch
IMAGE  ?= ansreview:dev

.DEFAULT_GOAL := help
.PHONY: help install test serve docker version release release-patch release-minor release-major release-dry-run

help: ## show this list
	@grep -hE '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  make %-16s %s\n", $$1, $$2}'
	@echo
	@echo "  release examples: make release | make release BUMP=minor | make release VERSION=1.0.0"

install: ## install for development (editable, with ansible-lint and pytest)
	$(PYTHON) -m pip install -e ".[dev,lint]"

test: ## run the test suite
	$(PYTHON) -m pytest -q

serve: ## start the web UI for the repos in ../
	$(PYTHON) -m ansreview serve --root .. --open

docker: ## build the Docker image locally (ansreview:dev)
	docker build -t $(IMAGE) .

version: ## print the current version
	@sed -n 's/^__version__ = "\(.*\)"$$/\1/p' src/ansreview/__init__.py

release: ## bump the version (BUMP=patch|minor|major or VERSION=X.Y.Z), tag and push
	@PYTHON=$(PYTHON) scripts/release.sh $(or $(VERSION),$(BUMP))

release-patch: ## release X.Y.Z+1
	@$(MAKE) --no-print-directory release BUMP=patch

release-minor: ## release X.Y+1.0
	@$(MAKE) --no-print-directory release BUMP=minor

release-major: ## release X+1.0.0
	@$(MAKE) --no-print-directory release BUMP=major

release-dry-run: ## show what `make release` would do, change nothing
	@DRY_RUN=1 PYTHON=$(PYTHON) scripts/release.sh $(or $(VERSION),$(BUMP))
