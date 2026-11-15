"""Test-case IDs: every integration test carries @pytest.mark.case("ID"); IDs are unique."""

from __future__ import annotations

import pytest


def pytest_collection_modifyitems(config, items):
    owners: dict[str, str] = {}
    problems: list[str] = []
    for item in items:
        function = f"{item.module.__name__}::{getattr(item, 'originalname', item.name)}"
        ids = [mark.args[0] for mark in item.iter_markers("case")]
        if "/integration/" in str(item.path) and not ids:
            problems.append(f"{item.nodeid}: integration test without @pytest.mark.case")
        for case_id in ids:
            if owners.setdefault(case_id, function) != function:
                problems.append(f"case {case_id} used by both {owners[case_id]} and {function}")
    if problems:
        raise pytest.UsageError("test-case ID check failed:\n" + "\n".join(sorted(set(problems))))
