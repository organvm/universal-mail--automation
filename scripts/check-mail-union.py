#!/usr/bin/env python3
"""Guard that UMA remains the single owner of mail-triage classifiers."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Iterable

WRAPPER_MARKER = "UMA_MAIL_TRIAGE_WRAPPER"

_SKIP_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "docs",
    "node_modules",
    "tests",
    "__pycache__",
}
_MAX_FILE_BYTES = 512_000
_TEXT_SUFFIXES = {
    ".applescript",
    ".bash",
    ".json",
    ".md",
    ".plist",
    ".py",
    ".scpt",
    ".sh",
    ".tmpl",
    ".txt",
    ".yaml",
    ".yml",
}
_CLASSIFIER_PATTERNS = (
    re.compile(r"\bdef\s+classify_(?:spam|noise|action|message)\b"),
    re.compile(r"\bdef\s+sub_triage_action\b"),
    re.compile(r"\bSPAM_SENDER_PATTERNS\b"),
    re.compile(r"\bNOISE_SENDER_PATTERNS\b"),
    re.compile(r"\bACTION_(?:SENDER_PATTERNS|SUBJECT_KEYWORDS|BODY_KEYWORDS)\b"),
    re.compile(r"Triage unread INBOX messages into SPAM/NOISE/HUMAN/ACTION"),
)


def _iter_files(root: Path) -> Iterable[Path]:
    for current, dirs, files in os.walk(root):
        current_path = Path(current)
        dirs[:] = [
            name
            for name in dirs
            if name not in _SKIP_DIRS
            and not name.startswith("private_")
            and not (current_path.name == "dot_local" and name == "share")
        ]
        if any(part in _SKIP_DIRS for part in current_path.parts):
            continue
        if any(part.startswith("private_") for part in current_path.parts):
            continue
        if "dot_local" in current_path.parts and "share" in current_path.parts:
            continue
        for name in files:
            path = current_path / name
            if path.suffix.lower() not in _TEXT_SUFFIXES:
                continue
            try:
                if path.stat().st_size > _MAX_FILE_BYTES:
                    continue
            except OSError:
                continue
            yield path


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _is_classifier(text: str) -> bool:
    return any(pattern.search(text) for pattern in _CLASSIFIER_PATTERNS)


def check_roots(roots: Iterable[Path], *, uma_root: Path) -> dict:
    violations = []
    scanned_files = 0
    for root in roots:
        if not root.exists():
            violations.append(
                {
                    "path": str(root),
                    "reason": "root_not_found",
                    "wrapper_marker": False,
                }
            )
            continue
        for path in _iter_files(root):
            if _is_relative_to(path, uma_root):
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            except OSError as e:
                violations.append({"path": str(path), "reason": f"read_failed:{e}", "wrapper_marker": False})
                continue
            scanned_files += 1
            if not _is_classifier(text):
                continue
            if WRAPPER_MARKER in text:
                continue
            violations.append(
                {
                    "path": str(path),
                    "reason": "mail_triage_classifier_outside_uma",
                    "wrapper_marker": False,
                }
            )
    return {
        "schema": "uma.mail.union_guard.v1",
        "status": "ok" if not violations else "failed",
        "uma_root": str(uma_root),
        "scanned_files": scanned_files,
        "wrapper_marker": WRAPPER_MARKER,
        "violations": violations,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--uma-root",
        default=str(Path(__file__).resolve().parents[1]),
        help="Canonical UMA root to ignore while scanning",
    )
    parser.add_argument(
        "--root",
        action="append",
        required=True,
        help="Root to scan; may be repeated",
    )
    parser.add_argument("--pretty", action="store_true", help="Pretty-print JSON output")
    args = parser.parse_args()

    result = check_roots(
        [Path(root).expanduser() for root in args.root],
        uma_root=Path(args.uma_root).expanduser(),
    )
    print(json.dumps(result, indent=2 if args.pretty else None, sort_keys=True))
    return 0 if result["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
