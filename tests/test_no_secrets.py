"""Committed files must not contain credentials."""

from __future__ import annotations

import re
from pathlib import Path

from scripts.constants import ROOT
from scripts.redact import redact

SCAN_ROOTS = ("scripts", "tests", "schema")
SCAN_FILES = ("README.md", ".env.example", "pyproject.toml", "requirements.txt", ".gitignore")
SECRET_ASSIGNMENT = re.compile(
    r"(?i)(token|password|client_secret|api_key)\s*[:=]\s*['\"][^'\"]{8,}['\"]"
)


def _text_files() -> list[Path]:
    files = []
    for name in SCAN_FILES:
        path = ROOT / name
        if path.is_file():
            files.append(path)
    for folder in SCAN_ROOTS:
        for path in (ROOT / folder).rglob("*"):
            if path.is_file() and path.suffix in {".py", ".json", ".md", ".toml", ".txt", ".example"}:
                files.append(path)
    return files


def test_env_example_leaves_secrets_blank():
    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert "SPEEDIANCE_USER_ID=\n" in text
    assert "SPEEDIANCE_TOKEN=\n" in text
    assert "SPEEDIANCE_REGION=Global" in text
    assert ".env" in (ROOT / ".gitignore").read_text(encoding="utf-8")


def test_tree_has_no_secret_literals():
    offenders = []
    for path in _text_files():
        text = path.read_text(encoding="utf-8")
        if "super-secret-token-value" in text and path.name != "test_stale.py" and path.name != "test_no_secrets.py":
            offenders.append(str(path))
        for match in SECRET_ASSIGNMENT.finditer(text):
            offenders.append(f"{path}:{match.group(0)}")
    assert offenders == []


def test_redact_replaces_only_the_env_token(monkeypatch):
    monkeypatch.setenv("SPEEDIANCE_TOKEN", "super-secret-token-value")
    assert redact("header Token super-secret-token-value") == "header Token [redacted]"
    assert "workout" in redact("workout")
