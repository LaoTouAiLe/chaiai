"""源码规模棘轮：指标口径、单向基线更新及默认 pytest 入口。"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from textwrap import dedent

import pytest

PROJECT = Path(__file__).resolve().parents[1]
SCRIPT = PROJECT / "scripts/check_size.py"
spec = importlib.util.spec_from_file_location("chaiai_code_size", SCRIPT)
assert spec is not None and spec.loader is not None
size = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = size
spec.loader.exec_module(size)


def _source(root: Path, text: str, name: str = "mod.py") -> Path:
    root.mkdir(parents=True, exist_ok=True)
    path = root / name
    path.write_text(dedent(text).lstrip("\n"), encoding="utf-8", newline="\n")
    return path


def _branches(count: int) -> str:
    return "def f(value):\n" + "".join(
        f"    if value == {index}:\n        return {index}\n" for index in range(count)
    ) + "    return -1\n"


def _debt(metric: str, value: int) -> str:
    if metric == "file_lines":
        return "value = 1\n" + "\n" * (value - 1)
    if metric == "function_lines":
        return "def f():\n" + "    pass\n" * (value - 1)
    if metric == "complexity":
        return _branches(value - 1)
    return "def f():\n" + "".join(
        "    " * (index + 1) + "if True:\n" for index in range(value)
    ) + "    " * (value + 1) + "return 0\n"


def _args(root: Path, baseline: Path, *mode: str) -> list[str]:
    return ["--root", str(root), "--baseline", str(baseline), *mode]


def _init(root: Path, baseline: Path, text: str = "value = 1\n") -> None:
    _source(root, text)
    assert size.main(_args(root, baseline, "--init")) == 0


@pytest.mark.parametrize(("source", "complexity", "nesting"), [
    ("def f():\n    return 1\n", 1, 0),
    ("def f(x):\n    if x: return 1\n    elif x is None: return 2\n    else: return 3\n", 3, 1),
    ("def f(x):\n    if x: pass\n    else:\n        if x is None: pass\n", 3, 2),
    ("def f(xs):\n    for x in xs:\n        while x: break\n    else: pass\n", 3, 2),
    ("def f():\n    try: pass\n    except ValueError: pass\n    except TypeError: pass\n    else: pass\n", 4, 1),
    ("def f():\n    try: pass\n    finally:\n        if True: pass\n", 2, 2),
    ("async def f(xs, lock):\n    async for x in xs:\n        async with lock:\n            if x: pass\n", 3, 3),
    ("def f(x):\n    match x:\n        case 0: pass\n        case 1 | 2: pass\n        case _: pass\n", 3, 1),
    ("def f(x):\n    match x:\n        case 0: pass\n        case name: pass\n", 2, 1),
    ("def f(x):\n    match x:\n        case name if name: pass\n", 2, 1),
    ("def f(xs, flag):\n    return [x for x in xs if x and x > 0] if flag else []\n", 1, 0),
    ("def f():\n    def inner(x):\n        if x: pass\n    return inner\n", 3, 0),
    ("def f():\n    class Inner:\n        def method(self, x):\n            if x: pass\n    return Inner\n", 3, 0),
])
def test_metric_semantics(tmp_path: Path, source: str, complexity: int, nesting: int) -> None:
    root = tmp_path / "src"
    _source(root, source)
    metrics = size.scan(root).metrics["function:mod.py::f"]
    assert metrics["complexity"] == complexity
    assert metrics["nesting"] == nesting


@pytest.mark.skipif(not hasattr(__import__("ast"), "TryStar"), reason="except* requires Python 3.11")
def test_exception_groups_are_counted(tmp_path: Path) -> None:
    root = tmp_path / "src"
    _source(root, "def f():\n    try: pass\n    except* ValueError: pass\n    else: pass\n")
    assert size.scan(root).metrics["function:mod.py::f"]["complexity"] == 3


def test_decorators_bom_comments_and_missing_final_newline_are_physical_lines(tmp_path: Path) -> None:
    root = tmp_path / "src"
    source = "\ufeff@one\n@two(\n    1,\n)\nasync def f():\n    # kept\n    return 1"
    _source(root, source)
    snapshot = size.scan(root)
    assert snapshot.metrics["file:mod.py"]["file_lines"] == 7
    assert snapshot.metrics["function:mod.py::f"]["function_lines"] == 7
    assert snapshot.locations["function:mod.py::f"] == 5


def test_qualified_names_do_not_depend_on_line_numbers(tmp_path: Path) -> None:
    root = tmp_path / "src"
    source = dedent("""
        class A:
            @property
            def value(self): return 1
            @value.setter
            def value(self, value): pass
        class B:
            def value(self): return 2
        def outer():
            async def inner(): return 3
            return inner
    """).lstrip("\n")
    path = _source(root, source)
    before = size.scan(root)
    assert set(before.metrics) == {
        "file:mod.py", "function:mod.py::A.value", "function:mod.py::A.value#2",
        "function:mod.py::B.value", "function:mod.py::outer",
        "function:mod.py::outer.<locals>.inner",
    }
    path.write_text("\n" * 10 + source, encoding="utf-8")
    after = size.scan(root)
    assert {k: v for k, v in before.metrics.items() if k.startswith("function:")} == {
        k: v for k, v in after.metrics.items() if k.startswith("function:")
    }


@pytest.mark.parametrize("metric", list(size.LIMITS))
def test_new_debt_cannot_be_accepted_by_update(tmp_path: Path, metric: str) -> None:
    root, baseline = tmp_path / "src", tmp_path / "baseline.json"
    _init(root, baseline)
    original = baseline.read_bytes()
    _source(root, _debt(metric, size.LIMITS[metric] + 1))
    assert size.main(_args(root, baseline)) == 1
    assert size.main(_args(root, baseline, "--update-baseline")) == 1
    assert baseline.read_bytes() == original


@pytest.mark.parametrize("metric", list(size.LIMITS))
def test_existing_debt_cannot_grow(tmp_path: Path, metric: str) -> None:
    root, baseline = tmp_path / "src", tmp_path / "baseline.json"
    _init(root, baseline, _debt(metric, size.LIMITS[metric] + 1))
    original = baseline.read_bytes()
    assert size.main(_args(root, baseline)) == 0
    _source(root, _debt(metric, size.LIMITS[metric] + 2))
    assert size.main(_args(root, baseline)) == 1
    assert size.main(_args(root, baseline, "--update-baseline")) == 1
    assert baseline.read_bytes() == original


def test_shortening_a_function_cannot_offset_new_complexity(tmp_path: Path) -> None:
    root, baseline = tmp_path / "src", tmp_path / "baseline.json"
    _init(root, baseline, _debt("function_lines", 305))
    original = baseline.read_bytes()
    _source(root, _branches(20))
    assert size.main(_args(root, baseline, "--update-baseline")) == 1
    assert baseline.read_bytes() == original


@pytest.mark.parametrize("metric", list(size.LIMITS))
def test_improvement_must_tighten_baseline_before_next_growth(tmp_path: Path, metric: str) -> None:
    root, baseline = tmp_path / "src", tmp_path / "baseline.json"
    _init(root, baseline, _debt(metric, size.LIMITS[metric] + 3))
    _source(root, _debt(metric, size.LIMITS[metric] + 2))
    assert size.main(_args(root, baseline)) == 1  # 不能留下旧余量，必须显式收紧。
    assert size.main(_args(root, baseline, "--update-baseline")) == 0
    assert size.main(_args(root, baseline)) == 0
    tighter = baseline.read_bytes()
    _source(root, _debt(metric, size.LIMITS[metric] + 3))
    assert size.main(_args(root, baseline, "--update-baseline")) == 1
    assert baseline.read_bytes() == tighter


def test_removed_debt_and_limits_are_not_kept_as_latent_exemptions(tmp_path: Path) -> None:
    root, baseline = tmp_path / "src", tmp_path / "baseline.json"
    _init(root, baseline, _branches(22))
    _source(root, "def f():\n    return 1\n")
    assert size.main(_args(root, baseline)) == 1
    assert size.main(_args(root, baseline, "--update-baseline")) == 0
    assert size.load_baseline(baseline) == {}
    _source(root, _branches(20))
    assert size.main(_args(root, baseline)) == 1


def test_deletion_and_rename_do_not_silently_transfer_an_exemption(tmp_path: Path) -> None:
    root, baseline = tmp_path / "src", tmp_path / "baseline.json"
    _init(root, baseline, _branches(22))
    original = baseline.read_bytes()
    _source(root, _branches(22).replace("def f(", "def renamed("))
    assert size.main(_args(root, baseline, "--update-baseline")) == 1
    assert baseline.read_bytes() == original
    _source(root, "value = 1\n")
    assert size.main(_args(root, baseline, "--update-baseline")) == 0
    assert size.load_baseline(baseline) == {}


def test_growth_within_limits_is_allowed_and_update_is_idempotent(tmp_path: Path) -> None:
    root, baseline = tmp_path / "src", tmp_path / "baseline.json"
    _init(root, baseline)
    original = baseline.read_bytes()
    original_time = baseline.stat().st_mtime_ns
    _source(root, _branches(19))
    assert size.main(_args(root, baseline, "--check")) == 0
    assert size.main(_args(root, baseline, "--update-baseline")) == 0
    assert baseline.read_bytes() == original
    assert baseline.stat().st_mtime_ns == original_time


@pytest.mark.parametrize("content", [
    "{bad", "[]", "{}",
    '{"version":1,"version":1,"limits":{},"exceptions":{}}',
])
def test_invalid_or_missing_baseline_is_never_repaired(tmp_path: Path, content: str) -> None:
    root, baseline = tmp_path / "src", tmp_path / "baseline.json"
    _source(root, "value = 1\n")
    assert size.main(_args(root, baseline)) == 2
    assert not baseline.exists()
    baseline.write_text(content, encoding="utf-8")
    before = baseline.read_bytes()
    assert size.main(_args(root, baseline, "--update-baseline")) == 2
    assert size.main(_args(root, baseline, "--init")) == 2
    assert baseline.read_bytes() == before


@pytest.mark.parametrize(("target", "metric", "value"), [
    ("file:../outside.py", "file_lines", 2001),
    ("file:/outside.py", "file_lines", 2001),
    ("file:C:/outside.py", "file_lines", 2001),
    ("file:mod.py", "unknown", 9999),
    ("file:mod.py", "complexity", 21),
    ("function:mod.py::f", "file_lines", 2001),
    ("function:mod.py::f", "complexity", True),
    ("function:mod.py::f", "complexity", 21.5),
    ("function:mod.py::f", "complexity", 20),
])
def test_invalid_exemptions_are_rejected(tmp_path: Path, target: str, metric: str, value: object) -> None:
    baseline = tmp_path / "baseline.json"
    document = {"version": 1, "limits": size.LIMITS, "exceptions": {target: {metric: value}}}
    baseline.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(size.SizeCheckError):
        size.load_baseline(baseline)


@pytest.mark.parametrize("change", ["version", "limits", "unknown", "empty_exemption"])
def test_baseline_format_and_limits_are_fixed(tmp_path: Path, change: str) -> None:
    document = {"version": 1, "limits": dict(size.LIMITS), "exceptions": {}}
    if change == "version":
        document["version"] = True
    elif change == "limits":
        document["limits"]["complexity"] = 100
    elif change == "unknown":
        document["ignored"] = True
    else:
        document["exceptions"] = {"file:mod.py": {}}
    baseline = tmp_path / "baseline.json"
    baseline.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(size.SizeCheckError):
        size.load_baseline(baseline)


def test_parse_error_or_empty_scan_cannot_shrink_baseline(tmp_path: Path) -> None:
    root, baseline = tmp_path / "src", tmp_path / "baseline.json"
    _init(root, baseline, _branches(22))
    original = baseline.read_bytes()
    _source(root, "def broken(\n")
    assert size.main(_args(root, baseline, "--update-baseline")) == 2
    (root / "mod.py").unlink()
    assert size.main(_args(root, baseline, "--update-baseline")) == 2
    assert baseline.read_bytes() == original


def test_permission_error_cannot_be_treated_as_an_empty_subdirectory(tmp_path: Path, monkeypatch) -> None:
    root, baseline = tmp_path / "src", tmp_path / "baseline.json"
    _init(root, baseline, _branches(22))
    original = baseline.read_bytes()

    def denied_walk(root, *, followlinks, onerror):
        onerror(PermissionError("test: inaccessible source directory"))
        yield str(root), [], []

    monkeypatch.setattr(size.os, "walk", denied_walk)
    assert size.main(_args(root, baseline, "--update-baseline")) == 2
    assert baseline.read_bytes() == original


def test_failed_atomic_update_preserves_the_old_baseline(tmp_path: Path, monkeypatch) -> None:
    root, baseline = tmp_path / "src", tmp_path / "baseline.json"
    _init(root, baseline, _branches(22))
    original = baseline.read_bytes()
    _source(root, "value = 1\n")

    def fail_replace(*args):
        raise PermissionError("test: replace denied")

    monkeypatch.setattr(size.os, "replace", fail_replace)
    assert size.main(_args(root, baseline, "--update-baseline")) == 2
    assert baseline.read_bytes() == original
    assert not list(tmp_path.glob(".baseline.json.*"))


def test_unreadable_file_cannot_commit_a_partial_scan(tmp_path: Path, monkeypatch) -> None:
    root, baseline = tmp_path / "src", tmp_path / "baseline.json"
    _init(root, baseline, _branches(22))
    original = baseline.read_bytes()
    _source(root, "value = 1\n")
    _source(root, "value = 2\n", name="z_bad.py")
    original_open = size.tokenize.open

    def blocked_open(path):
        if Path(path).name == "z_bad.py":
            raise PermissionError("test: unreadable source")
        return original_open(path)

    monkeypatch.setattr(size.tokenize, "open", blocked_open)
    assert size.main(_args(root, baseline, "--update-baseline")) == 2
    assert baseline.read_bytes() == original


@pytest.mark.parametrize("kind", ["symlink", "redirect"])
def test_source_links_fail_closed_without_needing_windows_symlink_privileges(
    tmp_path: Path, monkeypatch, kind: str,
) -> None:
    root = tmp_path / "src"
    source = _source(root, "value = 1\n")
    original_resolve = Path.resolve
    original_link = Path.is_symlink
    if kind == "symlink":
        monkeypatch.setattr(Path, "is_symlink", lambda path: path == source or original_link(path))
    else:
        monkeypatch.setattr(Path, "resolve", lambda path, **kwargs:
                            tmp_path / "outside.py" if path == source
                            else original_resolve(path, **kwargs))
    with pytest.raises(size.SizeCheckError, match="链接|重定向"):
        size.scan(root)


def test_cli_uses_explicit_paths_and_does_not_depend_on_cwd(tmp_path: Path) -> None:
    root, baseline = tmp_path / "src", tmp_path / "baseline.json"
    _init(root, baseline)
    result = subprocess.run(
        [sys.executable, str(SCRIPT), *_args(root, baseline, "--check")], cwd=tmp_path,
        capture_output=True, text=True, encoding="utf-8", timeout=30, check=False,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_checked_in_baseline_is_current() -> None:
    """默认 pytest 中的真实源码门槛；新增或恶化超标会直接使本用例失败。"""
    errors = size.problems(size.scan(size.SOURCE_ROOT), size.load_baseline(size.BASELINE))
    assert not errors, "\n".join(errors)


def test_production_complexity_agrees_with_installed_ruff() -> None:
    """与现有 Ruff C901 对照，避免自写计数与项目既有口径漂移。"""
    snapshot = size.scan(size.SOURCE_ROOT)
    expected = {(target.split(":", 1)[1].split("::", 1)[0], snapshot.locations[target]):
                values["complexity"] for target, values in snapshot.metrics.items()
                if values.get("complexity", 0) > size.LIMITS["complexity"]}
    result = subprocess.run(
        [sys.executable, "-m", "ruff", "check", "--isolated", "--select", "C901",
         "--config", "lint.mccabe.max-complexity=20", "--ignore-noqa", "--no-cache",
         "--output-format", "json", str(size.SOURCE_ROOT)],
        capture_output=True, text=True, encoding="utf-8", timeout=30, check=False,
    )
    assert result.returncode in {0, 1}, result.stderr
    actual = {}
    for item in json.loads(result.stdout):
        match = re.search(r"\((\d+) > 20\)$", item["message"])
        assert item["code"] == "C901" and match is not None, item
        path = Path(item["filename"]).relative_to(size.SOURCE_ROOT).as_posix()
        actual[path, item["location"]["row"]] = int(match.group(1))
    assert actual == expected
