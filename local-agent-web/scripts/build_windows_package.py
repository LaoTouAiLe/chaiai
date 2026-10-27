"""构建 Windows 目录式试用包（T40）。

在 ``local-agent-web`` 目录下执行：

```powershell
# 先重建前端产物再打包（推荐：包里的页面必须与当前源码一致）
.venv/Scripts/python.exe scripts/build_windows_package.py --frontend

# 复用现有 frontend/dist，只重打包；或只复扫已有产物（改扫描规则后不必重新打包）
.venv/Scripts/python.exe scripts/build_windows_package.py
.venv/Scripts/python.exe scripts/build_windows_package.py --scan-only

# 构建到一个新目录：不删除任何旧产物，因此不会触发本机环境的批量删除审批
$env:CHAIAI_OUT_DIR = "build/packaging/out-20261008"
.venv/Scripts/python.exe scripts/build_windows_package.py
```

产物：``build/packaging/dist/chaiai/``（被 `.gitignore` 的 ``build/`` 覆盖，不进版本库）。
包内顶层是 ``chaiai.exe``、``_internal/``（含 ``frontend/dist``）以及本脚本写入的
``MANIFEST.txt`` / ``BUILD_INFO.txt``。

本脚本承担五件事，缺一不可：

1. **前置检查**：PyInstaller 可用、前端产物存在。缺任何一项都给出**可照做**的命令后以 1 退出，
   不产出半成品包——"朋友的包打开是空白页"比"构建失败"难诊断得多。
2. **调用 spec**（``scripts/chaiai.spec``）：只读资源进包、可写数据不进包的口径写在那个文件里；
   打包完成后把 ``../packaging/使用说明.txt`` 复制到包顶层（见 :func:`_copy_notes`）。
3. **DLL 依赖补齐与自包含校验**（见 :func:`_repair_and_verify_dlls`）：打包器依赖分析在 conda
   环境里会漏收第三方 DLL，**2026-10-08 的试用包就漏了 OpenSSL**（`libssl-3-x64.dll` /
   `libcrypto-3-x64.dll`），只有恰好装过它们的机器才能启动。这里按事实分类补齐，并把"缺了什么"
   变成**构建失败**，而不是朋友机器上的"窗口一闪而过"。
4. **敏感数据扫描**：命中 :data:`FORBIDDEN_PATTERNS` 即构建失败（计划 §T40 的验收要求分发目录
   里没有密钥、真实数据与开发机路径）；命中 :data:`WARN_PATTERNS` 只如实报告，理由见那里的注释。
5. **清单与校验值**：``MANIFEST.txt``（每个文件的相对路径、大小、SHA-256）与 ``BUILD_INFO.txt``
   （Python / PyInstaller / Node 版本、构建时间、源树提交与未提交改动条数），让"这个包对应哪份
   代码"可以复核。
"""

from __future__ import annotations

import argparse
import codecs
import hashlib
import importlib.util
import os
import re
import shutil
import site
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Final

import pefile  # type: ignore[import-untyped]  # 随 PyInstaller 安装，没有类型存根

PROJECT_ROOT: Final[Path] = Path(__file__).resolve().parents[1]
FRONTEND_DIR: Final[Path] = PROJECT_ROOT / "frontend"
FRONTEND_DIST: Final[Path] = FRONTEND_DIR / "dist"
SPEC_PATH: Final[Path] = PROJECT_ROOT / "scripts" / "chaiai.spec"
BUILD_REQUIREMENTS: Final[Path] = PROJECT_ROOT / "scripts" / "requirements-build.txt"
PACKAGING_ROOT: Final[Path] = PROJECT_ROOT / "build" / "packaging"
#: 产物根目录。默认 ``build/packaging/dist``；把环境变量 ``CHAIAI_OUT_DIR`` 指向**一个新目录**，
#: 就可以在**不删除任何东西**的前提下重新构建——本机环境对"一次删除 500 个以上文件"有审批限制，
#: 而产物目录有 1900+ 个文件，原地重建会被拦下（2026-10-08 实测）。中间目录放在产物根下，换根
#: 目录时它也是新的。
DIST_PATH: Final[Path] = (
    Path(os.environ["CHAIAI_OUT_DIR"]).expanduser().resolve()
    if os.environ.get("CHAIAI_OUT_DIR")
    else PACKAGING_ROOT / "dist"
)
WORK_PATH: Final[Path] = DIST_PATH / "_work"
PACKAGE_DIR: Final[Path] = DIST_PATH / "chaiai"
#: 面向朋友的说明：源文件在仓库里，构建后复制到包**顶层**（不走 spec 的 datas，那会藏进
#: ``_internal``，朋友在解压目录里根本看不见）。
NOTES_SOURCE: Final[Path] = PROJECT_ROOT.parent / "packaging" / "使用说明.txt"
NOTES_NAME: Final[str] = "使用说明.txt"
#: 随包发布的**技能**（2026-10-16）：技能的**内容是文件**，只能随包走；"哪个技能可用"是用户的
#: 选择，存在设置库里（`agent.skills.disabled` 例外表），不在这里复制。
#: 包里那份示例配置写的是相对路径 `user_dirs: ["skills"]`，指向的就是下面复制出去的这个目录。
#: 技能源的**唯一**位置（2026-10-20 去重）：仓库根的 ``skills/``，与应用平级。
#: 原先 ``packaging/skills`` 与它逐字节重复，改一处忘另一处就会漂移，因此删掉副本、
#: 构建时从根复制。路径向上走一层是刻意的：技能属于"这个产品"，不属于打包脚本。
SKILLS_SOURCE: Final[Path] = PROJECT_ROOT.parent / "skills"
SKILLS_NAME: Final[str] = "skills"
#: 随包的外部可执行文件（2026-10-19：ripgrep）。**必须放 _internal/bin**：
#: 运行期由 `app_paths.bundled_binary()` 按 `_MEIPASS/bin/<name>.exe` 查找，
#: 放顶层会被当成"用户可见工具"却找不到。许可证文件随二进制一起复制。
BIN_SOURCE: Final[Path] = PROJECT_ROOT.parent / "packaging" / "bin"
BIN_NAME: Final[str] = "bin"

#: 本脚本自己写进包里的文件；它们不参与扫描，也不进清单（否则清单会包含自己，反复变化）。
GENERATED_FILES: Final[frozenset[str]] = frozenset({"MANIFEST.txt", "BUILD_INFO.txt"})

def _local_layout_patterns() -> tuple[bytes, ...]:
    """构建机上"**不该进包**的本地布局路径"，按构建机**现算**（2026-10-16）。

    为什么不再写死：原来这里列的是**开发机上的具体路径**（形如 `X:\\某个目录`，共四条）。
    那些字面量换一台机器构建时**既不会命中、也帮不上忙**，还把开发机的目录结构带进了仓库
    （朋友拿到源码第一眼就看见开发机的盘符长什么样）。现在只算两条**任何机器上都成立**的：
    "同级检出目录"（``PROJECT_ROOT.parent``，同机其它项目住在那儿）与"项目根自身"。
    用户主目录另走 :data:`WARN_PATTERNS`——理由见那里的注释。
    """
    patterns: list[bytes] = []
    for candidate in (PROJECT_ROOT.parent, PROJECT_ROOT):
        text = str(candidate)
        # 跳过盘根/根目录：形如 `<盘>:\` 的前缀会匹配一切，等于把自检关掉。
        if len(text) <= 3:
            continue
        patterns.append(re.escape(text.encode()))
        patterns.append(re.escape(candidate.as_posix().encode()))
    return tuple(patterns)


#: 命中即**构建失败**：密钥形态、真实数据，以及同一台机器上其它项目的路径。
#:
#: 密钥模式刻意写成"``sk-`` + 足够长的密钥字符"，而不是裸 ``sk-``：后者是三个字节的随机串，
#: 实测在 ``sqlite3.dll``、``_asyncio.pyd``、``chaiai.exe`` 里都会命中（``task-``/``mask-``
#: 这类词的一部分），全是误报。报错里带一小段上下文，便于判断"真密钥还是随机字节"。
FORBIDDEN_PATTERNS: Final[tuple[re.Pattern[bytes], ...]] = tuple(
    re.compile(pattern)
    for pattern in (
        rb"sk-[A-Za-z0-9]{24,}",
        rb"DEEPSEEK_API_KEY=",
        rb"hs_err_pid",
        rb"hf-cache",
        *_local_layout_patterns(),
    )
)

#: 命中只**警告**：本项目源码的绝对路径（理论上会经字节码的 ``co_filename`` 进包，.pyc 保存
#: 源文件路径用于异常回溯）。**首次实测（2026-10-08，PyInstaller 6.22.3）这个包里 0 命中**
#: ——打包器把模块路径规范化了。保留这条规则是为了防回归：换打包器版本、或将来有代码直接读
#: 源码路径时，它能第一时间报出来，也保证在任何机器上构建都能自检。
#:
#: 刻意**不**匹配泛化的"系统用户目录前缀"（形如 ``<盘>:\\Users\\``）：实测它在 33 个 .pyd/.dll 里
#: 命中，那是 CPython/conda 上游构建机留下的路径，与本项目无关，只会把真正的泄漏淹掉。
WARN_PATTERNS: Final[tuple[re.Pattern[bytes], ...]] = tuple(
    re.compile(pattern)
    for pattern in (
        re.escape(str(PROJECT_ROOT).encode()),
        re.escape(PROJECT_ROOT.as_posix().encode()),
        # 用户主目录（2026-10-16 起现算）：它进包等于把"这台机器上有个叫某名字的用户"一起发出去。
        # 只警告不硬失败——CPython/conda 的上游二进制里可能残留**构建机**的主目录前缀，
        # 当硬失败会误杀（与上面"刻意不匹配泛化的系统用户目录前缀"同一类误报）。
        re.escape(str(Path.home()).encode()),
        re.escape(Path.home().as_posix().encode()),
    )
)

#: 这些文件名本身就算"真实数据进包"，不参与内容扫描。
FORBIDDEN_NAMES: Final[frozenset[str]] = frozenset({"config.yaml", "mcp.json", ".env", "debug.log"})
FORBIDDEN_SUFFIXES: Final[frozenset[str]] = frozenset({".sqlite", ".sqlite3", ".db"})

#: 逐块扫描的块大小与重叠：重叠必须不小于最长模式，避免模式跨块边界漏检。
CHUNK_BYTES: Final[int] = 4 * 1024 * 1024
OVERLAP_BYTES: Final[int] = 512

#: API set 契约名（``api-ms-win-*`` / ``ext-ms-*``）：由加载器按系统 API set schema 解析，
#: **磁盘上没有对应文件**。按"在 System32 里找得到吗"判断会把它们误报成缺失依赖。
API_SET_PREFIXES: Final[tuple[str, ...]] = ("api-ms-", "ext-ms-")


def _check_pyinstaller() -> str | None:
    """返回错误提示；可用时返回 ``None``。"""
    if importlib.util.find_spec("PyInstaller") is None:
        return (
            "没有找到 PyInstaller（它是构建工具，不是运行依赖）。先执行：\n"
            f"  uv pip install --python .venv/Scripts/python.exe -r {_display(BUILD_REQUIREMENTS)}"
        )
    return None


def _build_frontend() -> None:
    """在 ``frontend/`` 执行 ``npm run build``；失败即中止（不拿旧产物冒充新页面）。"""
    npm = shutil.which("npm")
    if npm is None:
        raise SystemExit("没有找到 npm：先安装 Node.js，或去掉 --frontend 并在前端手动 npm run build")
    print(f"[1/5] 构建前端：npm run build（{_display(FRONTEND_DIR)}）", flush=True)
    completed = subprocess.run([npm, "run", "build"], cwd=str(FRONTEND_DIR), check=False)
    if completed.returncode != 0:
        raise SystemExit(f"npm run build 失败（退出码 {completed.returncode}），未生成包")


def _require_frontend_dist() -> None:
    index = FRONTEND_DIST / "index.html"
    if not index.is_file():
        raise SystemExit(
            f"没有前端产物：{_display(index)} 不存在。先在前端构建，或直接加 --frontend 参数：\n"
            "  .venv/Scripts/python.exe scripts/build_windows_package.py --frontend"
        )


def _run_pyinstaller() -> None:
    print(f"[2/5] 打包：PyInstaller onedir → {_display(PACKAGE_DIR)}", flush=True)
    if PACKAGE_DIR.exists():
        shutil.rmtree(PACKAGE_DIR, ignore_errors=True)
    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--workpath",
        str(WORK_PATH),
        "--distpath",
        str(DIST_PATH),
        str(SPEC_PATH),
    ]
    completed = subprocess.run(command, cwd=str(PROJECT_ROOT), check=False)
    if completed.returncode != 0:
        raise SystemExit(f"PyInstaller 失败（退出码 {completed.returncode}）")
    if not (PACKAGE_DIR / "chaiai.exe").is_file():
        raise SystemExit(f"打包结束但没找到 {_display(PACKAGE_DIR / 'chaiai.exe')}，请检查 spec")
    _copy_notes()
    _copy_skills()
    _copy_binaries()


def _copy_notes() -> None:
    """把使用说明放到包**顶层**，并按 Windows 记事本能正确识别的编码写出。

    为什么要加 BOM 与 CRLF：这份说明是给不写代码的人双击打开的，而记事本对无 BOM 的 UTF-8
    与 LF 换行的识别随系统版本而异；统一成"UTF-8 + BOM + CRLF"后，任何记事本都能正常显示。
    """
    if not NOTES_SOURCE.is_file():
        raise SystemExit(f"缺少使用说明源文件：{_display(NOTES_SOURCE)}")
    text = NOTES_SOURCE.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
    if not text.startswith(codecs.BOM_UTF8):
        text = codecs.BOM_UTF8 + text
    (PACKAGE_DIR / NOTES_NAME).write_bytes(text)


def _copy_skills() -> None:
    """把技能目录复制到包**顶层**（与使用说明同一位置与理由）。

    为什么放顶层而不是 `_internal`：这些是**给模型读的工作手册**，使用者要能打开看、能自己往里加
    一条技能——藏在 `_internal` 里等于告诉他"别动"。源目录缺失或为空时**构建失败**：那说明
    "包里该带技能"这件事没人维护了，静默发一个不带技能的包更糟。
    """
    if not SKILLS_SOURCE.is_dir():
        raise SystemExit(f"缺少技能源目录：{_display(SKILLS_SOURCE)}")
    copied = 0
    for path in sorted(SKILLS_SOURCE.rglob("*")):
        if not path.is_file():
            continue
        target = PACKAGE_DIR / SKILLS_NAME / path.relative_to(SKILLS_SOURCE)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(path.read_bytes())
        copied += 1
    if copied == 0:
        raise SystemExit(f"技能源目录是空的：{_display(SKILLS_SOURCE)}")


def _copy_binaries() -> None:
    """把随包二进制（``../packaging/bin``）复制到 ``_internal/bin``。

    为什么不是顶层：运行期由 ``app_paths.bundled_binary()`` 按 ``_MEIPASS/bin/<name>.exe``
    解析，而 onedir 形态的 ``_MEIPASS`` 就是 ``_internal``。放错位置的表现很隐蔽——
    程序能启动，只是代码检索永远走慢路径。

    源目录或 rg 缺失时**构建失败**：这个包的"检索不退化"依赖它，静默发一个不带它的包
    等于把一条已验证的行为降级成"看运气"。
    """
    if not BIN_SOURCE.is_dir():
        raise SystemExit(f"缺少随包二进制目录：{_display(BIN_SOURCE)}")
    if not (BIN_SOURCE / "rg.exe").is_file():
        raise SystemExit(f"缺少 {_display(BIN_SOURCE / 'rg.exe')}；代码检索会退化为扫描")
    target_root = PACKAGE_DIR / "_internal" / BIN_NAME
    copied = 0
    for path in sorted(BIN_SOURCE.rglob("*")):
        if not path.is_file():
            continue
        target = target_root / path.relative_to(BIN_SOURCE)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(path.read_bytes())
        copied += 1
    if copied == 0:
        raise SystemExit(f"随包二进制目录是空的：{_display(BIN_SOURCE)}")


def _imported_dlls(path: Path) -> list[str]:
    """读出一个 PE 文件导入表里引用的 DLL 名字。"""
    pe = pefile.PE(str(path), fast_load=True)
    pe.parse_data_directories(directories=[pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_IMPORT"]])
    return [
        entry.dll.decode("ascii", "replace") for entry in getattr(pe, "DIRECTORY_ENTRY_IMPORT", [])
    ]


def _missing_dependencies() -> dict[str, list[str]]:
    """包内可执行文件**引用但没有随包**的 DLL（按引用者分组）。

    这一步只回答"在不在包里"，不判断该不该在——分类交给 :func:`_repair_and_verify_dlls`。
    """
    internal = PACKAGE_DIR / "_internal"
    missing: dict[str, list[str]] = {}
    for path in sorted(internal.rglob("*")):
        if path.suffix.lower() not in {".pyd", ".dll"} or not path.is_file():
            continue
        absent = sorted({name for name in _imported_dlls(path) if not (internal / name).exists()})
        if absent:
            missing[path.relative_to(internal).as_posix()] = absent
    return missing


def _dll_source_dirs() -> list[Path]:
    """解释器环境里可能放着依赖 DLL 的目录（按优先级）。

    conda 把 Python 用到的第三方 DLL 放在 ``<prefix>/Library/bin``，numpy 这类包把伴随 DLL 放在
    ``site-packages/*.libs``——这两处都不在 PyInstaller 默认的依赖搜索范围里，正是漏收的来源。
    """
    base_prefix = Path(sys.base_prefix)
    candidates: list[Path] = [
        Path(sys.executable).resolve().parent,
        base_prefix,
        base_prefix / "DLLs",
        base_prefix / "Library" / "bin",
        base_prefix / "bin",
    ]
    for entry in [*site.getsitepackages(), site.getusersitepackages()]:
        root = Path(entry)
        candidates.append(root)
        candidates.extend(sorted(root.glob("*.libs")))
        # pywin32 把运行库放在自己的子目录 `pywin32_system32/`（pywintypesXX.dll / pythoncomXX.dll）：
        # 既不符合 `*.libs` 的命名，也不在 site-packages 根上，所以上面两种匹配都抓不到它。
        # 2026-10-23 实测：打包分析会把 `win32/win32api.pyd` 这类扩展收进来，缺了这两个 DLL 时
        # 自包含校验直接判失败（包在干净机器上会 import 崩，而本机因 PATH 里有 pywin32 看不出来）。
        candidates.append(root / "pywin32_system32")
    return [path for path in dict.fromkeys(candidates) if path.is_dir()]


def _system_dll_dirs() -> list[Path]:
    """Windows 自带 DLL 的目录：只在这里能找到的依赖不算"包缺东西"。"""
    system_root = os.environ.get("SystemRoot")
    if not system_root:
        return []
    return [Path(system_root) / name for name in ("System32", "SysWOW64")]


def _provided_by_windows(name: str, system_dirs: list[Path]) -> bool:
    """这个依赖能不能由 Windows 自己满足（API set 契约名，或 System32/SysWOW64 里的文件）。"""
    if name.lower().startswith(API_SET_PREFIXES):
        return True
    return any((directory / name).is_file() for directory in system_dirs)


def _repair_and_verify_dlls() -> None:
    """把打包器漏收的 Python 侧依赖 DLL 补进包，然后校验这个包是**自包含**的。

    分类不猜文件名，按事实判断：

    - 包内已有 → 不动；
    - 能在解释器环境里找到（含 ``Library/bin``、``*.libs``）→ 这是 Python 侧的依赖，复制进
      ``_internal``；随包的副本优先于 System32，所以复制进来的那份一定会被加载；
    - 只在 System32 / SysWOW64 里 → Windows 自带，跳过；
    - 哪都没有 → **构建失败**：这种包在干净机器上一定会起不来。
    """
    print("[3/5] DLL 依赖补齐与自包含检查", flush=True)
    internal = PACKAGE_DIR / "_internal"
    sources = _dll_source_dirs()
    system_dirs = _system_dll_dirs()

    copied: list[str] = []
    for names in _missing_dependencies().values():
        for name in names:
            if (internal / name).exists():
                continue
            origin = next((directory / name for directory in sources if (directory / name).is_file()), None)
            if origin is None:
                continue
            shutil.copy2(origin, internal / name)
            copied.append(f"{name} ← {_display(origin)}")
            print(f"  补齐 DLL：{copied[-1]}", flush=True)

    unresolved: dict[str, list[str]] = {}
    for relative, names in _missing_dependencies().items():
        hard = [name for name in names if not _provided_by_windows(name, system_dirs)]
        if hard:
            unresolved[relative] = hard
    if unresolved:
        detail = "；".join(f"{key} → {value}" for key, value in unresolved.items())
        raise SystemExit(f"DLL 自包含检查未通过，这个包在干净机器上会启动失败：{detail}")
    print(f"  DLL 自包含检查：通过（本次补齐 {len(copied)} 个）", flush=True)


def _iter_files(root: Path) -> list[Path]:
    """包里需要校验的文件（不含本脚本生成的清单与构建信息）。"""
    return sorted(
        path
        for path in root.rglob("*")
        if path.is_file() and path.name not in GENERATED_FILES
    )


def _search(blob: bytes, patterns: tuple[re.Pattern[bytes], ...]) -> list[str]:
    """返回命中的可读描述；带一小段上下文，便于区分真密钥与随机字节。"""
    hits: list[str] = []
    for pattern in patterns:
        match = pattern.search(blob)
        if match is None:
            continue
        snippet = blob[max(0, match.start() - 12) : match.end() + 12]
        hits.append(f"{pattern.pattern!r} 命中 {snippet.decode('utf-8', 'replace')!r}")
    return hits


def _scan_file(path: Path) -> tuple[list[str], list[str]]:
    """逐块扫描一个文件，返回（命中禁用模式, 命中警告模式）的可读描述。"""
    forbidden: list[str] = []
    warned: list[str] = []
    tail = b""
    with path.open("rb") as handle:
        while chunk := handle.read(CHUNK_BYTES):
            window = tail + chunk
            forbidden += _search(window, FORBIDDEN_PATTERNS)
            warned += _search(window, WARN_PATTERNS)
            tail = window[-OVERLAP_BYTES:]
    return forbidden, warned


def _scan_package() -> tuple[list[str], list[str]]:
    print("[4/5] 敏感数据扫描", flush=True)
    forbidden: list[str] = []
    warned: list[str] = []
    for path in _iter_files(PACKAGE_DIR):
        relative = path.relative_to(PACKAGE_DIR).as_posix()
        if (path.name.lower() in FORBIDDEN_NAMES
                or (path.name.lower().startswith("mcp.json.") and path.suffix.lower() == ".tmp")):
            forbidden.append(f"{relative}（真实配置/日志不该进包）")
            continue
        if path.suffix.lower() in FORBIDDEN_SUFFIXES:
            forbidden.append(f"{relative}（数据库文件不该进包）")
            continue
        hits_forbidden, hits_warn = _scan_file(path)
        forbidden += [f"{relative} → {needle}" for needle in hits_forbidden]
        warned += [f"{relative} → {needle}" for needle in hits_warn]
    return forbidden, warned


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


def _write_manifest() -> tuple[int, int]:
    print("[5/5] 生成 MANIFEST.txt", flush=True)
    lines = ["# 相对路径\t字节数\tSHA-256"]
    total = 0
    files = _iter_files(PACKAGE_DIR)
    for path in files:
        size = path.stat().st_size
        total += size
        lines.append(f"{path.relative_to(PACKAGE_DIR).as_posix()}\t{size}\t{_sha256(path)}")
    (PACKAGE_DIR / "MANIFEST.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return len(files), total


def _tool_version(*command: str) -> str:
    """读取工具版本；解析不到可执行文件时返回 ``未知``（不编造）。

    `npm` 在 Windows 上是 `npm.cmd`，直接交给 ``subprocess`` 会找不到，所以先按 PATH 解析。
    """
    resolved = shutil.which(command[0]) or command[0]
    try:
        completed = subprocess.run(
            [resolved, *command[1:]], capture_output=True, text=True, check=False, timeout=30
        )
    except (OSError, subprocess.SubprocessError):
        return "未知"
    text = (completed.stdout or completed.stderr).strip().splitlines()
    return text[0] if text else "未知"


def _git_facts() -> tuple[str, str]:
    """返回（HEAD 提交, 未提交改动条数）。没有 Git 时如实写"未知"。"""
    try:
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
            check=False,
        )
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return "未知", "未知"
    commit = head.stdout.strip() if head.returncode == 0 else "未知"
    if status.returncode == 0:
        changed = str(len([line for line in status.stdout.splitlines() if line.strip()]))
    else:
        changed = "未知"
    return commit or "未知", changed


def _write_build_info() -> None:
    commit, changed = _git_facts()
    lines = [
        f"构建时间(UTC)：{datetime.now(timezone.utc).isoformat(timespec='seconds')}",
        f"Python：{sys.version.split()[0]}",
        f"PyInstaller：{_tool_version(sys.executable, '-m', 'PyInstaller', '--version')}",
        f"Node：{_tool_version('node', '-v')}",
        f"npm：{_tool_version('npm', '-v')}",
        f"源树 HEAD：{commit}",
        f"未提交改动条数：{changed}",
        "构建方式：scripts/build_windows_package.py（spec：scripts/chaiai.spec，onedir）",
        "数据目录口径：%LOCALAPPDATA%\\chaiai（只读资源在 _internal）",
    ]
    (PACKAGE_DIR / "BUILD_INFO.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _display(path: Path) -> str:
    try:
        return path.relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        return str(path)


def _force_utf8_console() -> None:
    """把 stdout / stderr 强制成 UTF-8（2026-10-27）。

    为什么需要：GitHub Actions 的 Windows runner 是 en-US locale，Python 的 stdout 编码是
    **cp1252**，而本脚本每条进度都是中文——第一次跑 release workflow 就崩在
    ``print("[2/5] 打包：PyInstaller onedir → …")`` 上，抛 ``UnicodeEncodeError: 'charmap'``。
    本地中文 Windows 是 cp936，看得见中文，所以这个问题永远只在别人的构建机上暴露。

    ``errors="replace"`` 是兜底：宁可少一个字形，也不要因为一句进度提示让整次构建失败
    （这条脚本的职责是"产出可靠的包"，不是"保证终端能显示中文"）。
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:  # 被重定向成非 TextIOWrapper 时无从设置
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            continue


def main(argv: list[str] | None = None) -> int:
    _force_utf8_console()
    parser = argparse.ArgumentParser(description="构建 chaiai Windows 目录式试用包（T40）")
    parser.add_argument(
        "--frontend",
        action="store_true",
        help="先在 frontend/ 执行 npm run build，再用新产物打包",
    )
    parser.add_argument(
        "--scan-only",
        action="store_true",
        help="不重新打包，只扫描并重写现有产物的清单（用来迭代扫描规则）",
    )
    args = parser.parse_args(argv)

    problem = _check_pyinstaller()
    if problem is not None:
        print(problem, file=sys.stderr)
        return 1

    if args.scan_only:
        if not PACKAGE_DIR.is_dir():
            print(f"没有可扫描的产物：{_display(PACKAGE_DIR)} 不存在", file=sys.stderr)
            return 1
    else:
        from local_agent.security.win_ipc import verify_bundle

        verify_bundle(check_sources=True)
        if args.frontend:
            _build_frontend()
        _require_frontend_dist()
        _run_pyinstaller()
        _repair_and_verify_dlls()

    from local_agent.security.win_ipc import verify_bundle

    verify_bundle(PACKAGE_DIR / "_internal/local_agent/native/windows/bin")

    forbidden, warned = _scan_package()
    count, total = _write_manifest()
    _write_build_info()

    print()
    print(f"完成：{_display(PACKAGE_DIR)}（{count} 个文件，{total / 1024 / 1024:.1f} MiB）")
    print(f"清单：{_display(PACKAGE_DIR / 'MANIFEST.txt')}；构建信息：{_display(PACKAGE_DIR / 'BUILD_INFO.txt')}")
    if warned:
        unique = sorted({item.split(" → ")[0] for item in warned})
        print(f"\n警告：{len(warned)} 处命中，来自 {len(unique)} 个文件（不阻断）")
        for name in unique[:10]:
            print(f"  - {name}")
        if len(unique) > 10:
            print(f"  ……另有 {len(unique) - 10} 个文件")
        print(
            "  说明：这些是构建机的源码绝对路径（Python 字节码的 co_filename）。彻底避免的办法是在"
            "一个不含私人信息的短路径下构建；不要假装它不存在。"
        )
    if forbidden:
        print(f"\n构建失败：包内有 {len(forbidden)} 处禁用内容（密钥/真实数据/其它项目路径）：", file=sys.stderr)
        for item in forbidden[:40]:
            print(f"  - {item}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover - 命令行入口
    raise SystemExit(main())
