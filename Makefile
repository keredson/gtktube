SHELL := /usr/bin/env bash
.SHELLFLAGS := -eu -o pipefail -c

PYTHON ?= python3
REMOTE ?= origin
BRANCH ?= main

.PHONY: check deploy-new-point-release deploy-new-minor-release deploy-new-major-release _deploy-release
.SILENT: _deploy-release

check:
	$(PYTHON) -m py_compile $$(git ls-files '*.py')
	$(PYTHON) -m unittest discover -s tests
	git diff --check

deploy-new-point-release: BUMP=point
deploy-new-point-release: _deploy-release

deploy-new-minor-release: BUMP=minor
deploy-new-minor-release: _deploy-release

deploy-new-major-release: BUMP=major
deploy-new-major-release: _deploy-release

_deploy-release:
	bump_script="$$(mktemp)"; \
	update_script="$$(mktemp)"; \
	trap 'rm -f "$$bump_script" "$$update_script"' EXIT; \
	printf '%s\n' \
		'import sys' \
		'kind, version = sys.argv[1:3]' \
		'parts = version.split(".")' \
		'assert len(parts) == 3 and all(part.isdigit() for part in parts), f"expected X.Y.Z, got {version!r}"' \
		'major, minor, patch = map(int, parts)' \
		'next_version = {' \
		'    "point": (major, minor, patch + 1),' \
		'    "minor": (major, minor + 1, 0),' \
		'    "major": (major + 1, 0, 0),' \
		'}[kind]' \
		'print(".".join(map(str, next_version)))' \
		> "$$bump_script"; \
	printf '%s\n' \
		'import pathlib' \
		'import re' \
		'import sys' \
		'path = pathlib.Path("pyproject.toml")' \
		'version = sys.argv[1]' \
		'text = path.read_text(encoding="utf-8")' \
		'text, count = re.subn(r"^version = \"[^\"]+\"", f"version = \"{version}\"", text, count=1, flags=re.MULTILINE)' \
		'assert count == 1, "could not update pyproject.toml version"' \
		'path.write_text(text, encoding="utf-8")' \
		> "$$update_script"; \
	current="$$( \
		$(PYTHON) -c 'import tomllib; print(tomllib.load(open("pyproject.toml", "rb"))["project"]["version"])' \
	)"; \
	next="$$( $(PYTHON) "$$bump_script" "$(BUMP)" "$$current" )"; \
	tag="v$$next"; \
	echo "Current version: $$current"; \
	echo "Next version:    $$next"; \
	echo "Release tag:     $$tag"; \
	echo; \
	if [[ "$$(git branch --show-current)" != "$(BRANCH)" ]]; then \
		echo "Release must run from $(BRANCH)." >&2; \
		exit 1; \
	fi; \
	if [[ -n "$$(git status --porcelain)" ]]; then \
		echo "Release requires a clean worktree, including untracked files." >&2; \
		git status --short; \
		exit 1; \
	fi; \
	git fetch "$(REMOTE)"; \
	read behind ahead < <(git rev-list --left-right --count "$(REMOTE)/$(BRANCH)...HEAD"); \
	if [[ "$$behind" != "0" ]]; then \
		echo "Local $(BRANCH) is behind $(REMOTE)/$(BRANCH); pull/merge first." >&2; \
		exit 1; \
	fi; \
	if git rev-parse -q --verify "refs/tags/$$tag" >/dev/null; then \
		echo "Tag $$tag already exists locally." >&2; \
		exit 1; \
	fi; \
	if git ls-remote --exit-code --tags "$(REMOTE)" "refs/tags/$$tag" >/dev/null 2>&1; then \
		echo "Tag $$tag already exists on $(REMOTE)." >&2; \
		exit 1; \
	fi; \
	echo "Running release checks before changing Git state..."; \
	$(PYTHON) -m py_compile $$(git ls-files '*.py'); \
	$(PYTHON) -m unittest discover -s tests; \
	git diff --check; \
	backup="$$(mktemp)"; \
	cp pyproject.toml "$$backup"; \
	trap 'mv "$$backup" pyproject.toml 2>/dev/null || true; rm -f "$$bump_script" "$$update_script"' EXIT; \
	$(PYTHON) "$$update_script" "$$next"; \
	git diff --check; \
	echo; \
	git diff -- pyproject.toml; \
	echo; \
	echo "This will commit pyproject.toml, create annotated tag $$tag, and push $(BRANCH) plus $$tag to $(REMOTE)."; \
	read -r -p "Type yes to continue: " answer; \
	if [[ "$$answer" != "yes" ]]; then \
		mv "$$backup" pyproject.toml; \
		trap - EXIT; \
		echo "Release aborted; pyproject.toml restored."; \
		exit 1; \
	fi; \
	rm -f "$$backup"; \
	trap - EXIT; \
	git add pyproject.toml; \
	git commit -m "Bump version for $$tag"; \
	git tag -a "$$tag" -m "Release $$tag"; \
	git push "$(REMOTE)" "$(BRANCH)"; \
	git push "$(REMOTE)" "$$tag"; \
	echo "Released $$tag."
