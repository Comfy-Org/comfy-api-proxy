"""Regression test for a public-repo information leak: docs/sync-workflow.md
and spec/README.md used to reference private cloud-repo internals verbatim
(an internal CI workflow filename, internal file paths, an internal service
name, an internal secret name, internal issue-tracker prefixes, an internal
design-doc nickname, an internal tool name, and an internal architecture
descriptor) — all things this repo, being public, must never carry.

This repeats (as an executable check) the same manual grep used to verify
the fix, so a future edit to either doc can't silently reintroduce one of
these markers. It deliberately excludes scripts/filter_openapi.py and
tests/test_filter_openapi.py: those files' keyword lists / fixtures ARE the
redaction mechanism and its test data, not a leak — they legitimately
contain these strings so the filter knows what to strip from the *cloud*
spec before it lands in spec/openapi.yaml.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Paths whose content is allowed to mention these markers (they're the
# redaction rule itself / its test fixtures), not a leak.
_EXEMPT = {
    REPO_ROOT / "scripts" / "filter_openapi.py",
    REPO_ROOT / "tests" / "test_filter_openapi.py",
}

# Directories worth scanning: prose and the scripts that describe/perform
# the sync, where the leak was found and fixed.
_SCAN_DIRS = ["docs", "spec", "scripts"]

_LEAK_PATTERNS = [
    re.compile(r"push-ingest-types-to-frontend\.yml"),
    re.compile(r"api/v2/openapi\.yaml"),
    re.compile(r"api/v2/README\.md"),
    re.compile(r"\bingest\b", re.IGNORECASE),
    re.compile(r"\bPR_GH_TOKEN\b"),
    re.compile(r"\b(?:BE|ENG|INFRA|SEC)-\d+\b"),
    re.compile(r"\bComfy SDK TDD\b"),
    re.compile(r"\bthe TDD\b"),
    re.compile(r"\bopenapi-project\b"),
    re.compile(r"\bserverless gateway\b", re.IGNORECASE),
    # The private upstream monorepo slug — naming it reveals a private repo.
    re.compile(r"Comfy-Org/cloud\b"),
]


_TEXT_SUFFIXES = {".md", ".py", ".sh", ".yaml", ".yml", ""}


def _files_to_scan() -> list[Path]:
    files = []
    for d in _SCAN_DIRS:
        base = REPO_ROOT / d
        if not base.exists():
            continue
        for p in base.rglob("*"):
            if not p.is_file() or p in _EXEMPT:
                continue
            if "__pycache__" in p.parts:
                continue
            if p.suffix not in _TEXT_SUFFIXES:
                continue
            files.append(p)
    # Root-level developer-facing docs (README.md et al.) live outside the
    # scanned dirs but are exactly where an internal reference is most likely to
    # slip in unnoticed — scan them too.
    for p in REPO_ROOT.glob("*.md"):
        if p.is_file() and p not in _EXEMPT:
            files.append(p)
    return files


def test_no_internal_markers_leak_outside_the_filter_and_its_test():
    leaks: list[str] = []
    for path in _files_to_scan():
        try:
            text = path.read_text(errors="ignore")
        except (UnicodeDecodeError, OSError):
            continue
        for pattern in _LEAK_PATTERNS:
            if pattern.search(text):
                leaks.append(f"{path.relative_to(REPO_ROOT)}: matched {pattern.pattern!r}")
    assert not leaks, "internal marker(s) found outside filter_openapi.py/its test:\n" + "\n".join(
        leaks
    )
