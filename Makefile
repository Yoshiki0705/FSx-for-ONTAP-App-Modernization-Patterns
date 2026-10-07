# Quality gates for FSx-for-ONTAP-App-Modernization-Patterns.
#
# Three invariants this file exists to hold:
#
# 1. Every target is declared in .PHONY. A target named after a directory that
#    exists on disk (docs, templates, scripts) is otherwise treated by make as an
#    up-to-date file, so make prints "up to date" and never runs the recipe.
# 2. Path lists live in variables here and CI calls these targets, so local and
#    CI cannot end up inspecting different trees.
# 3. A gate whose tool is missing fails. A skipped check that reports success is
#    indistinguishable from a check that passed.
#
# Never read a gate's result off a pipe: `make all | tail` returns tail's status.
# Use `make all > /tmp/all.log 2>&1; echo $?`.
#
# Tools resolve from .venv when it exists, otherwise from PATH.
VENV       := .venv
VENV_BIN   := $(VENV)/bin
tool        = $(if $(wildcard $(VENV_BIN)/$(1)),$(VENV_BIN)/$(1),$(shell command -v $(1) 2>/dev/null))
PYTHON     := $(if $(wildcard $(VENV_BIN)/python),$(VENV_BIN)/python,python3)
RUFF       := $(call tool,ruff)
CFN_LINT   := $(call tool,cfn-lint)
RUFF_PINNED := $(shell sed -n 's/^ruff==//p' requirements-dev.txt)

PY_PATHS   := tools scripts
MD_GLOBS   := "**/*.md"
AI_STYLE_PATHS := AGENTS.md README.md README.en.md docs/
# Recursive on purpose: a one-level glob such as templates/*.yaml silently drops a
# template placed one directory deeper, and cfn-lint then exits 0 having checked nothing.
CFN_TEMPLATES = $(shell test -d templates && find templates -type f \( -name '*.yaml' -o -name '*.yml' \) | sort)
CFN_GUARD_RULES = $(shell test -d guard && find guard -type f -name '*.guard' | sort)
SH_FILES   = $(shell git ls-files --cached --others --exclude-standard -- '*.sh' | sort)

define need
	@command -v $(1) >/dev/null 2>&1 || { \
		echo "error: $(1) is not installed, so this gate would check nothing."; \
		echo "       Install it: $(2)"; \
		exit 1; \
	}
endef

.DEFAULT_GOAL := help

.PHONY: help
help: ## このファイルのターゲット一覧
	@grep -hE '^[a-zA-Z0-9_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

.PHONY: install
install: ## .venv を作り requirements-dev.txt の固定版を導入
	@test -d $(VENV) || python3 -m venv $(VENV)
	$(VENV_BIN)/python -m pip install --upgrade pip
	$(VENV_BIN)/python -m pip install -r requirements-dev.txt

.PHONY: python
python: ## ruff の lint と format 検査（未導入・版違いは失敗）
	@test -n "$(RUFF)" || { echo "error: ruff is not installed. Run: make install"; exit 1; }
	@reported=$$($(RUFF) --version) || { echo "error: $(RUFF) does not run"; exit 1; }; \
	case "$$reported" in \
		"ruff $(RUFF_PINNED)") ;; \
		*) echo "error: $(RUFF) reports '$$reported', pinned is $(RUFF_PINNED). Run: make install"; exit 1;; \
	esac
	$(RUFF) check $(PY_PATHS)
	$(RUFF) format --check $(PY_PATHS)

.PHONY: markdown
markdown: ## markdownlint（未導入は失敗）
	$(call need,markdownlint-cli2,npm install -g markdownlint-cli2@0.22.1)
	markdownlint-cli2 $(MD_GLOBS)

.PHONY: headings
headings: ## 日本語の節見出しが体言止めか（本検査の前に自己テスト）
	@$(PYTHON) tools/check_heading_style.py --selftest >/dev/null
	$(PYTHON) tools/check_heading_style.py

.PHONY: role-labels
role-labels: ## ラベルが職種名を名乗っていないか（自己テストの後に走査）
	@$(PYTHON) tools/check_role_labels.py --selftest >/dev/null
	$(PYTHON) tools/check_role_labels.py

.PHONY: ai-style
ai-style: ## AI 調の兆候を検査（fail tier で落とす）
	@$(PYTHON) tools/ai_style_rules.py --selftest >/dev/null
	@$(PYTHON) tools/ai_style_rules.py $(AI_STYLE_PATHS) --summary
	$(PYTHON) tools/ai_style_rules.py $(AI_STYLE_PATHS) --fail

.PHONY: shell
shell: ## shellcheck（未導入は失敗。対象 0 本ならその旨を出す）
	$(call need,shellcheck,brew install shellcheck)
	@if [ -z "$(SH_FILES)" ]; then echo "shell: 0 script(s) tracked"; else shellcheck $(SH_FILES); fi

.PHONY: cfn
cfn: ## cfn-lint と cfn-guard（templates/ 配下を再帰で走査）
	@test -n "$(CFN_LINT)" || { echo "error: cfn-lint is not installed. Run: make install"; exit 1; }
	$(call need,cfn-guard,brew install cloudformation-guard)
	@if [ -z "$(CFN_TEMPLATES)" ]; then \
		echo "cfn: 0 template(s) under templates/"; \
	else \
		$(CFN_LINT) $(CFN_TEMPLATES) || exit 1; \
		test -n "$(CFN_GUARD_RULES)" || { echo "error: templates exist but guard/*.guard has no rules"; exit 1; }; \
		for t in $(CFN_TEMPLATES); do cfn-guard validate --data $$t --rules guard/ --show-summary fail || exit 1; done; \
	fi

.PHONY: lint
lint: markdown headings role-labels ai-style python shell cfn ## 文書と Python・シェル・CFn の静的検査

.PHONY: audit
audit: secrets ## 公開物の監査（命名・中立性・PII）と gitleaks
	@$(PYTHON) tools/audit_public_output.py --selftest >/dev/null
	$(PYTHON) tools/audit_public_output.py

.PHONY: secrets
secrets: ## gitleaks で作業ツリーを走査（履歴全体は gitleaks workflow）
	$(call need,gitleaks,brew install gitleaks)
	gitleaks dir . --config .gitleaks.toml --redact --exit-code 1 --no-banner

.PHONY: links
links: ## 内部リンクの解決
	$(PYTHON) tools/check_links.py

# Test directories live here so a tests/ directory not listed runs nowhere; test-coverage below
# fails when a test file on disk is not reached by this target.
TEST_DIRS := scripts/tests
PY_UNITTEST := tools.test_ai_style scripts.tests.test_estimate scripts.tests.test_readonly_scripts \
               scripts.tests.test_reuse_permutations

.PHONY: test
test: ## 検出器の unittest、スクリプトの単体テスト、ガード・フック・シェルの自己テスト
	# Static-analysis and detector self-tests
	$(PYTHON) -m unittest $(PY_UNITTEST)
	$(PYTHON) scripts/guard_irreversible_ops.py --selftest
	$(PYTHON) tools/audit_public_output.py --selftest
	# cfn-guard negative tests: each guard/tests fixture fails exactly its own rule
	$(PYTHON) guard/tests/run_guard_negatives.py
	# AIMF hook self-tests
	$(PYTHON) scripts/aimf/check-hook-wiring.py --selftest
	$(PYTHON) scripts/aimf/block_direct_atx.py --selftest
	$(PYTHON) scripts/aimf/hook_canary.py --selftest
	# Sample-app and probe self-tests (no .NET build here)
	$(PYTHON) scripts/probe_peer.py --selftest
	$(PYTHON) scripts/make-seed.py --selftest
	# Shell dry-run tests (deploy / run-atx / block_direct_atx / check-no-locking / integration-clone)
	bash scripts/tests/dryrun_shell_tests.sh
	# Every test file on disk must be reached by this target
	$(PYTHON) scripts/tests/check_test_coverage.py

.PHONY: ci
ci: lint audit links test ## CI が呼ぶ集約ターゲット

.PHONY: all
all: ci ## コミット前のゲート（ci と同じ）
