# -*- mode: python ; coding: utf-8 -*-
"""chaiai Windows 试用包（T40）的 PyInstaller 规格：**目录式（onedir）**、单入口。

入口固定为 ``src/local_agent/startup.py``（冻结形态兜底）：它转调 §15.1 的官方入口
``api/__main__.py``，并在导入官方入口**之前**装好"失败也要留下证据"的钩子——包内缺 DLL 这类崩溃
发生在任何输出之前，没有这一层，双击者只能看到窗口一闪而过。目录选择器的自我 spawn 参数
``--pick-directory`` 仍由 ``api/__main__.py`` 在 argparse 之前接走，argv 原样透传。

三条边界（与计划 §T40 一致）：

1. **只读资源进包、可写数据不进包**：这里只放前端构建产物 ``frontend/dist``；配置、SQLite 与
   工作区由入口按 ``LOCALAPPDATA`` 解析（``local_agent.app_paths``），与解压位置无关。所以包
   里**不应该**出现 config.yaml / .env / *.sqlite，构建脚本会扫描并拦截。
2. **不收集 Streamlit 界面**：``local_agent/web.py`` 与 ``local_agent/ui/*`` 不在 FastAPI 入口
   链上，显式排除；不排就会把 pandas / pyarrow / altair 一起拖进来。
3. **中期记忆的向量库随包，本地 ONNX 不随包**（2026-10-19 调整）：带 ``chromadb`` 本体与它的
   Rust 绑定，这样"中期记忆 + 兼容 API 的 Embedding"开箱可用；``fastembed`` / ``onnxruntime`` /
   ``huggingface_hub`` 仍然排除——它们只服务本地 ONNX（含权重下载），体积大且首次使用还要联网。
   缺依赖时仍走既有的"降级 + 如实提示"路径，不因为这次改动改变降级语义。

构建请走 ``scripts/build_windows_package.py``（它负责先构建前端、再调本文件、最后做敏感数据
扫描与清单校验），不要直接手敲 pyinstaller 命令。
"""

from pathlib import Path

from PyInstaller.utils.hooks import collect_all, copy_metadata

PROJECT_ROOT = Path(SPECPATH).resolve().parent  # noqa: F821 - SPECPATH 由 PyInstaller 注入
FRONTEND_DIST = PROJECT_ROOT / "frontend" / "dist"
ENTRY_SCRIPT = PROJECT_ROOT / "src" / "local_agent" / "startup.py"
UI_ICON = PROJECT_ROOT / "frontend" / "public" / "chaiai.ico"  # 目前不存在，见下面的注释

datas = [(str(FRONTEND_DIST), "frontend/dist")]
NATIVE_SOURCE = PROJECT_ROOT / "src/local_agent/native/windows"
datas += [(str(path), "local_agent/native/windows") for pattern in ("*.c", "*.h")
          for path in NATIVE_SOURCE.glob(pattern)]
datas += [(str(NATIVE_SOURCE / "bin" / name), "local_agent/native/windows/bin")
          for name in ("manifest.json", "ipc-compat-x64.dll", "ipc-compat-x86.dll",
                       "ipc-loader-x64.exe", "ipc-loader-x86.exe")]

# langchain / langgraph 家族在运行时用 importlib.metadata 读自己的版本；不带上 .dist-info
# 时，冻结产物里会拿不到版本信息。逐个调用并容忍缺失：这些包的可选子包在不同环境里不一定装。
for _distribution in (
    "langchain-core",
    "langchain-openai",
    "langgraph",
    "langgraph-checkpoint",
    "langgraph-checkpoint-sqlite",
    "fastapi",
    "uvicorn",
    "pydantic",
    "pydantic-settings",
):
    try:
        datas += copy_metadata(_distribution)
    except Exception:  # noqa: BLE001 - 没装就不带，不影响启动
        pass

binaries: list = []
hiddenimports = [
    # 目录选择器的 tkinter 在**函数内**延迟导入（directory_picker._ask_directory），
    # 显式登记，避免静态分析漏掉之后"点选择目录没反应"。
    "tkinter",
    "tkinter.filedialog",
    # uvicorn 按字符串动态加载这些实现模块，静态分析看不到。
    "uvicorn.logging",
    "uvicorn.loops",
    "uvicorn.loops.auto",
    "uvicorn.protocols",
    "uvicorn.protocols.http",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan",
    "uvicorn.lifespan.on",
    "uvicorn.lifespan.off",
]

# 中期记忆的向量库（2026-10-19）：只带 chromadb 本体与它的 Rust 绑定。
# 不带的三个仍是可选项——fastembed / onnxruntime 是**本地 ONNX**路径（含权重下载），
# huggingface_hub 只服务那两条；中期记忆改用「兼容 API」的 Embedding 即可。
# 用 collect_all 而不是逐个 hiddenimports：chromadb 有 migrations/日志配置/sqlite 实现
# 与大量运行期动态导入，漏一个的表现是"启动正常、一开记忆就 ImportError"。
for _package in ("chromadb", "chromadb_rust_bindings"):
    try:
        _data, _bin, _hidden = collect_all(_package)
    except Exception:
        continue
    datas += _data
    binaries += _bin
    hiddenimports += [name for name in _hidden if not name.startswith("chromadb.test")]

# 桌面窗口（2026-10-23）：pywebview 的 Windows 后端是"按平台字符串导入"的
# （`webview.platforms.winforms` / `edgechromium`），静态分析看不到；它自带的
# WebView2 程序集（Microsoft.Web.WebView2.*.dll、WebBrowserInterop.*.dll）也在 `webview/lib/` 下，
# 必须整包带上，否则冻结后表现为"窗口打不开、退回浏览器"。
# 没装 pywebview 时保持无窗口（源码形态、CI 都不需要它）。
for _package in ("webview",):
    try:
        _data, _bin, _hidden = collect_all(_package)
    except Exception:
        continue
    datas += _data
    binaries += _bin
    hiddenimports += _hidden
hiddenimports += [
    "webview.platforms.winforms",
    "webview.platforms.edgechromium",
]


a = Analysis(
    [str(ENTRY_SCRIPT)],
    pathex=[str(PROJECT_ROOT / "src")],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Streamlit 调试界面（见文件头第 2 条）。
        "streamlit",
        "pandas",
        "pyarrow",
        "altair",
        "matplotlib",
        "IPython",
        "pytest",
        # 本地 ONNX Embedding（见文件头第 3 条）：权重不随包，改用兼容 API。
        # chromadb 本体**不排除**（2026-10-19），见下面的 collect_all。
        "fastembed",
        "onnxruntime",
        "huggingface_hub",
    ],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="chaiai",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    # 首次构建刻意保留控制台：启动失败的原因（端口占用、配置损坏、数据目录不可写）得看得见。
    # 隐藏控制台 + 改用原生弹窗提示是 T40 的待定项，见构建脚本输出里的"待定"一节。
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(UI_ICON) if UI_ICON.is_file() else None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="chaiai",
)
