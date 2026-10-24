"""T84：真实受控基线、结构漂移和 CLI 失败不能静默覆盖。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from local_agent.release_contracts import (
    canonical_document,
    changed_contracts,
    compatibility_manifest,
    fingerprint,
)

PROJECT = Path(__file__).resolve().parents[1]
SCRIPT = PROJECT / "scripts/check_release_contracts.py"


def _cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args], cwd=PROJECT, capture_output=True, check=False,
        encoding="utf-8", env={**os.environ, "PYTHONIOENCODING": "utf-8"}, timeout=30,
    )


def test_controlled_baseline_matches_actual_contracts() -> None:
    expected = json.loads((PROJECT / "docs/release/compatibility-baseline.json").read_text("utf-8"))
    assert changed_contracts(expected, compatibility_manifest()) == []
    assert _cli("--check").returncode == 0


def test_export_is_deterministic_and_independent_of_user_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    before = canonical_document(compatibility_manifest())
    (tmp_path / "config.yaml").write_text("not: valid: yaml", encoding="utf-8")
    (tmp_path / ".env").write_text("API_KEY=PRIVATE-SENTINEL", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("API_KEY", "PRIVATE-SENTINEL")
    assert canonical_document(compatibility_manifest()) == before
    assert "PRIVATE-SENTINEL" not in before
    assert fingerprint({"a": 1, "b": 2}) == fingerprint({"b": 2, "a": 1})


@pytest.mark.parametrize("key", [
    "config_schema_sha256", "config_defaults_sha256", "openapi_sha256",
    "event_types", "database_versions", "dto_schema_version",
])
def test_changed_contract_fails_cli_and_preserves_baseline(tmp_path: Path, key: str) -> None:
    manifest = compatibility_manifest()
    manifest[key] = "INTENTIONAL-DRIFT"
    baseline = tmp_path / "baseline.json"
    baseline.write_text(canonical_document(manifest), encoding="utf-8")
    original = baseline.read_bytes()
    result = _cli("--baseline", str(baseline))
    assert result.returncode == 1
    assert key in result.stdout
    assert baseline.read_bytes() == original


@pytest.mark.parametrize("content", [None, "{bad", "[]"])
def test_missing_or_invalid_baseline_is_not_created_or_repaired(
    tmp_path: Path, content: str | None,
) -> None:
    baseline = tmp_path / "baseline.json"
    if content is not None:
        baseline.write_text(content, encoding="utf-8")
    assert _cli("--baseline", str(baseline)).returncode == 1
    assert baseline.exists() is (content is not None)
    if content is not None:
        assert baseline.read_text("utf-8") == content


def test_missing_and_unknown_manifest_fields_are_detected() -> None:
    assert changed_contracts({"a": None}, {"b": None}) == ["a", "b"]
