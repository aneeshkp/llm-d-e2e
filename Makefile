MANIFEST_REPO ?= https://github.com/aneeshkp/llm-d-conformance-manifests.git
MANIFEST_REF  ?= main
MANIFEST_DIR  ?= deploy/manifests
NAMESPACE     ?= llm-conformance-test
TESTCASE_DIR  ?= configs/testcases
REPORT_DIR    ?= reports
PLATFORM      ?= any
MODEL_SOURCE  ?= hf
KUBECONFIG    ?= $(HOME)/.kube/config

# Test flags
TESTCASE      ?=
PROFILE       ?=
MODEL         ?=
MOCK          ?=
PULL_SECRET   ?=
BEARER_TOKEN  ?=
STORAGE_CLASS ?=
STORAGE_SIZE  ?=
EXTRA_FLAGS   ?=

# Build pytest args
PYTEST_ARGS := tests/test_conformance.py \
	--platform $(PLATFORM) \
	--namespace $(NAMESPACE) \
	--kubeconfig $(KUBECONFIG) \
	--testcase-dir $(TESTCASE_DIR) \
	--report-dir $(REPORT_DIR) \
	--model-source $(MODEL_SOURCE)

ifdef TESTCASE
  PYTEST_ARGS += --testcase $(TESTCASE)
endif
ifdef PROFILE
  PYTEST_ARGS += --profile $(PROFILE)
endif
ifdef MODEL
  PYTEST_ARGS += --model $(MODEL)
endif
ifdef MOCK
  PYTEST_ARGS += --mock $(MOCK)
endif
ifdef PULL_SECRET
  PYTEST_ARGS += --pull-secret $(PULL_SECRET)
endif
ifdef BEARER_TOKEN
  PYTEST_ARGS += --bearer-token $(BEARER_TOKEN)
endif
ifdef STORAGE_CLASS
  PYTEST_ARGS += --storage-class $(STORAGE_CLASS)
endif
ifdef STORAGE_SIZE
  PYTEST_ARGS += --storage-size $(STORAGE_SIZE)
endif

.PHONY: help
help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-24s\033[0m %s\n", $$1, $$2}'

.PHONY: setup
setup: ## Clone manifest repo
	@rm -rf $(MANIFEST_DIR)/*.yaml
	@git clone --depth 1 --branch $(MANIFEST_REF) $(MANIFEST_REPO) /tmp/llm-d-manifests 2>/dev/null || true
	@cp /tmp/llm-d-manifests/*.yaml $(MANIFEST_DIR)/ 2>/dev/null || true
	@rm -rf /tmp/llm-d-manifests
	@echo "Manifests cloned from $(MANIFEST_REF)"

.PHONY: sync
sync: ## Install dependencies with uv
	uv sync

.PHONY: test
test: ## Run conformance tests (TESTCASE=single-gpu)
	uv run pytest $(PYTEST_ARGS) $(EXTRA_FLAGS)

.PHONY: test-profile-smoke
test-profile-smoke: ## Run smoke profile
	uv run pytest $(PYTEST_ARGS) --profile configs/profiles/smoke.yaml

.PHONY: test-profile-all
test-profile-all: ## Run all conformance tests
	uv run pytest $(PYTEST_ARGS) --profile configs/profiles/all.yaml

.PHONY: test-profile-cache-aware
test-profile-cache-aware: ## Run cache-aware tests
	uv run pytest $(PYTEST_ARGS) --profile configs/profiles/cache-aware.yaml

.PHONY: test-profile-pd
test-profile-pd: ## Run P/D disaggregation tests
	uv run pytest $(PYTEST_ARGS) --profile configs/profiles/pd.yaml

.PHONY: test-profile-moe
test-profile-moe: ## Run MoE/DeepSeek tests
	uv run pytest $(PYTEST_ARGS) --profile configs/profiles/deepseek.yaml

.PHONY: unittest
unittest: ## Run smoke/unit tests (no cluster needed)
	uv run pytest tests/test_smoke.py -v

.PHONY: lint
lint: ## Run ruff linter
	uv run ruff check src/ tests/

.PHONY: format
format: ## Format code with ruff
	uv run ruff format src/ tests/

.PHONY: testcases
testcases: ## List available test cases
	@echo "Test cases:"
	@for f in $(TESTCASE_DIR)/*.yaml; do \
		name=$$(grep '^name:' $$f | head -1 | sed 's/name: *//'); \
		desc=$$(grep '^description:' $$f | head -1 | sed 's/description: *"//;s/"$$//'); \
		printf "  %-28s %s\n" "$$name" "$$desc"; \
	done

.PHONY: profiles
profiles: ## List available profiles
	@echo "Profiles:"
	@for f in configs/profiles/*.yaml; do \
		name=$$(grep '^name:' $$f | head -1 | sed 's/name: *//'); \
		desc=$$(grep '^description:' $$f | head -1 | sed 's/description: *"//;s/"$$//'); \
		printf "  %-20s %s\n" "$$name" "$$desc"; \
	done

.PHONY: clean
clean: ## Remove reports and temp files
	rm -rf reports/*.json reports/*.html __pycache__ .pytest_cache
