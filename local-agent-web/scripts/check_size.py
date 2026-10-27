"""后端源码规模棘轮：默认只读；初始化和收紧基线必须显式执行。"""

from __future__ import annotations

import argparse
import ast
import json
import os
import tempfile
import tokenize
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

PROJECT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = PROJECT / "src/local_agent"
BASELINE = Path(__file__).with_name("code_size_baseline.json")
LIMITS = {"file_lines": 2000, "function_lines": 300, "complexity": 20, "nesting": 6}
Metrics = dict[str, dict[str, int]]
Function = ast.FunctionDef | ast.AsyncFunctionDef


class SizeCheckError(ValueError):
    """源码或基线无效，不能安全完成检查。"""


@dataclass(frozen=True)
class Snapshot:
    """一次完整扫描的指标与诊断位置；不含源码正文或绝对路径。"""

    metrics: Metrics
    locations: dict[str, int]
    files: int
    functions: int


def _irrefutable(pattern: ast.pattern) -> bool:
    if isinstance(pattern, ast.MatchAs):
        return pattern.pattern is None or _irrefutable(pattern.pattern)
    return isinstance(pattern, ast.MatchOr) and any(_irrefutable(p) for p in pattern.patterns)


def complexity(node: Function) -> int:
    """按 Ruff C901 统计决策点；嵌套命名函数也计入外层控制流。"""
    result = 1
    decisions = (ast.If, ast.For, ast.AsyncFor, ast.While, ast.ExceptHandler,
                 ast.FunctionDef, ast.AsyncFunctionDef)
    for child in ast.walk(ast.Module(body=node.body, type_ignores=[])):
        result += isinstance(child, decisions)
        if isinstance(child, (ast.Try, getattr(ast, "TryStar", ast.Try))):
            result += bool(getattr(child, "orelse", []))
        elif isinstance(child, ast.Match):
            result += len(child.cases)
            last = child.cases[-1]
            if last.guard is None and _irrefutable(last.pattern):
                result -= 1
    return result


def _suites(node: ast.stmt) -> list[list[ast.stmt]]:
    suites = [getattr(node, field, []) for field in ("body", "orelse", "finalbody")]
    suites.extend(handler.body for handler in getattr(node, "handlers", []))
    suites.extend(case.body for case in getattr(node, "cases", []))
    return [suite for suite in suites if suite]


def nesting(body: Sequence[ast.stmt], depth: int = 0) -> int:
    """统计控制块深度；嵌套函数/类独立计算，同级 elif 不多算一层。"""
    maximum = depth
    for node in body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        for suite in _suites(node):
            is_elif = (isinstance(node, ast.If) and suite is node.orelse
                       and len(suite) == 1 and isinstance(suite[0], ast.If)
                       and suite[0].col_offset == node.col_offset)
            maximum = max(maximum, nesting(suite, depth if is_elif else depth + 1))
    return maximum


class _Functions(ast.NodeVisitor):
    def __init__(self, path: str, metrics: Metrics, locations: dict[str, int]) -> None:
        self.path = path
        self.metrics = metrics
        self.locations = locations
        self.scope: list[str] = []
        self.counts: dict[str, int] = {}

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        """将类名加入限定名，区分不同类的同名方法。"""
        self.scope.append(node.name)
        for child in node.body:
            self.visit(child)
        self.scope.pop()

    def visit_FunctionDef(self, node: Function) -> None:
        """收集当前函数，并独立遍历其内部定义。"""
        name = ".".join([*self.scope, node.name])
        self.counts[name] = self.counts.get(name, 0) + 1
        ordinal = self.counts[name]
        unique = name if ordinal == 1 else f"{name}#{ordinal}"
        target = f"function:{self.path}::{unique}"
        first = min([node.lineno, *(item.lineno for item in node.decorator_list)])
        assert node.end_lineno is not None
        self.metrics[target] = {
            "function_lines": node.end_lineno - first + 1,
            "complexity": complexity(node),
            "nesting": nesting(node.body),
        }
        self.locations[target] = node.lineno
        self.scope.extend([unique.rsplit(".", 1)[-1], "<locals>"])
        for child in node.body:
            self.visit(child)
        del self.scope[-2:]

    visit_AsyncFunctionDef = visit_FunctionDef


def _walk_error(error: OSError) -> None:
    raise error


def _source_files(root: Path) -> list[Path]:
    if not root.is_dir():
        raise SizeCheckError("源码根目录不存在或不是目录")
    found: list[Path] = []
    for directory, dirs, files in os.walk(root, followlinks=False, onerror=_walk_error):
        parent = Path(directory)
        for name in [*dirs, *(name for name in files if name.endswith(".py"))]:
            path = parent / name
            if path.is_symlink() or path.resolve() != path.absolute():
                raise SizeCheckError(f"源码目录内不允许链接或路径重定向：{path.relative_to(root)}")
        found.extend(parent / name for name in files if name.endswith(".py"))
    if not found:
        raise SizeCheckError("源码根目录内没有 Python 文件")
    return sorted(found)


def scan(root: Path) -> Snapshot:
    """扫描全部 Python 源文件；语法、读取或路径边界错误一律使检查失败。"""
    root = root.resolve()
    metrics: Metrics = {}
    locations: dict[str, int] = {}
    files = _source_files(root)
    for path in files:
        relative = path.relative_to(root).as_posix()
        try:
            with tokenize.open(path) as source:
                text = source.read()
            tree = ast.parse(text, filename=relative)
        except (SyntaxError, UnicodeError) as exc:
            raise SizeCheckError(f"源码无效：{relative}:{getattr(exc, 'lineno', 1)}") from exc
        target = f"file:{relative}"
        metrics[target] = {"file_lines": len(text.splitlines())}
        locations[target] = 1
        _Functions(relative, metrics, locations).visit(tree)
    functions = sum(key.startswith("function:") for key in metrics)
    return Snapshot(metrics, locations, len(files), functions)


def exceptions(snapshot: Snapshot) -> Metrics:
    """只记录高于默认门槛的存量指标；正常代码不产生豁免。"""
    return {target: excess for target, values in snapshot.metrics.items()
            if (excess := {key: value for key, value in values.items() if value > LIMITS[key]})}


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise SizeCheckError(f"基线存在重复键：{key}")
        result[key] = value
    return result


def _valid_target(target: str) -> bool:
    prefix, _, rest = target.partition(":")
    path, separator, name = rest.partition("::")
    relative = PurePosixPath(path)
    return (prefix in {"file", "function"} and bool(path) and "\\" not in path
            and not relative.is_absolute() and ".." not in relative.parts
            and relative.suffix == ".py" and ":" not in path
            and (not separator if prefix == "file" else bool(separator and name))
            and not any(ord(char) < 32 for char in target))


def load_baseline(path: Path) -> Metrics:
    """严格加载基线，拒绝损坏文件、未知指标、放宽门槛和无效豁免。"""
    try:
        data = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique_object)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise SizeCheckError("基线不是有效的 UTF-8 JSON") from exc
    if (not isinstance(data, dict) or set(data) != {"version", "limits", "exceptions"}
            or type(data["version"]) is not int or data["version"] != 1
            or data["limits"] != LIMITS
            or any(type(value) is not int for value in data["limits"].values())
            or not isinstance(data["exceptions"], dict)):
        raise SizeCheckError("基线结构、版本或门槛与检查器不一致")
    result: Metrics = {}
    for target, values in data["exceptions"].items():
        allowed = {"file_lines"} if target.startswith("file:") else set(LIMITS) - {"file_lines"}
        if (not _valid_target(target) or not isinstance(values, dict) or not values
                or not set(values) <= allowed
                or any(type(value) is not int or value <= LIMITS[key]
                       for key, value in values.items())):
            raise SizeCheckError(f"基线豁免无效：{target}")
        result[target] = dict(values)
    return result


def problems(snapshot: Snapshot, baseline: Metrics, *, stale: bool = True) -> list[str]:
    """返回新增/恶化指标；默认也要求及时移除或收紧过期的豁免。"""
    errors = []
    for target, values in snapshot.metrics.items():
        location = target.split(":", 1)[1].split("::", 1)[0]
        for metric, value in values.items():
            ceiling = baseline.get(target, {}).get(metric, LIMITS[metric])
            if value > ceiling:
                errors.append(f"{location}:{snapshot.locations[target]} {target} "
                              f"{metric}={value} > {ceiling}")
    if stale:
        for target, values in baseline.items():
            for metric, old in values.items():
                current = snapshot.metrics.get(target, {}).get(metric)
                if current is None or current < old:
                    errors.append(f"{target} {metric} 已改善或移除，请用 --update-baseline 收紧基线")
    return errors


def baseline_json(snapshot: Snapshot) -> str:
    """生成确定性的相对路径基线，不含时间戳、行号或机器信息。"""
    data = {"version": 1, "limits": LIMITS, "exceptions": exceptions(snapshot)}
    return json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2) + "\n"


def _save_baseline(path: Path, document: str, *, initialize: bool) -> None:
    if initialize:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("x", encoding="utf-8", newline="\n") as output:
            output.write(document)
        return
    if path.read_bytes() == document.encode("utf-8"):
        return
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="\n", dir=path.parent,
            prefix=f".{path.name}.", delete=False,
        ) as output:
            temporary = Path(output.name)
            output.write(document)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main(argv: Sequence[str] | None = None) -> int:
    """默认检查；--init 只创建新基线，--update-baseline 只能收紧现有基线。"""
    parser = argparse.ArgumentParser(description="检查后端源码规模与复杂度棘轮")
    parser.add_argument("--root", type=Path, default=SOURCE_ROOT)
    parser.add_argument("--baseline", type=Path, default=BASELINE)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--check", action="store_true", help="只读检查（默认）")
    modes.add_argument("--init", action="store_true", help="首次创建基线，拒绝覆盖已有文件")
    modes.add_argument("--update-baseline", action="store_true", help="只收紧或移除已有豁免")
    args = parser.parse_args(argv)
    try:
        if args.init and args.baseline.exists():
            raise SizeCheckError("基线已经存在，--init 不允许覆盖")
        baseline = {} if args.init else load_baseline(args.baseline)
        snapshot = scan(args.root)
        errors = [] if args.init else problems(snapshot, baseline, stale=not args.update_baseline)
        if errors:
            print("规模棘轮检查失败：\n" + "\n".join(errors))
            return 1
        if args.init or args.update_baseline:
            _save_baseline(args.baseline, baseline_json(snapshot), initialize=args.init)
            print("规模基线已创建或收紧")
        print(f"规模棘轮检查通过：{snapshot.files} 文件，{snapshot.functions} 函数")
        return 0
    except (OSError, SizeCheckError) as exc:
        print(f"规模棘轮无法检查：{exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
