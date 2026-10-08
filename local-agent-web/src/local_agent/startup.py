"""冻结形态的启动入口：先装好"失败也要留下证据"的兜底，再进入官方入口（T40）。

**为什么必须是独立入口**：模型与运行时的导入发生在 ``local_agent.api.__main__`` **被导入的那一刻**，
那时 ``main()`` 还没开始执行——把 try/except 写在 ``main()`` 里救不了"导入阶段就崩"的情况。而
"双击后 cmd 一闪而过"恰恰属于这一类：报错出现在启动横幅之前，窗口一关就什么都不剩，用户和作者
都拿不到任何线索。2026-10-08 的试用包就是这个形态（包内缺 OpenSSL 运行库，只有恰好装了它的机器
才能起来），所以这一层不是防御性编程，是修复的一部分。

**为什么源码形态不用它**：源码入口仍然是 ``python -m local_agent.api``（§15.1）；本模块只作为
打包产物的 ``chaiai.exe`` 入口，因此不改变任何开发路径的行为。

**失败时做什么**：把 traceback 与环境摘要写到数据目录下的 ``启动错误.log``（同时仍打到控制台），
再以非零码退出。日志留在本机，可能含路径与堆栈——它不是给人公开上传的文件。
"""

from __future__ import annotations

import os
import sys
import tempfile
import traceback
from datetime import datetime, timezone
from pathlib import Path

#: 日志文件名。与 ``app_paths`` 的数据目录口径一致；下面那份冗余计算只服务于"连 app_paths 都
#: 导入不了"的最坏情况（那时正是最需要日志的时候）。
LOG_NAME = "启动错误.log"
APP_DIR_NAME = "chaiai"

#: 失败退出码：与入口自己的 1（装配失败）区分开，便于从截图判断崩在哪一步。
FAILURE_EXIT_CODE = 2


def _candidate_dirs() -> list[Path]:
    """日志可能落地的目录，按优先级排列；逐个尝试直到写入成功。"""
    candidates: list[Path] = []
    base: Path | None = None
    try:
        from local_agent import app_paths

        base = app_paths.data_dir()
    except BaseException:  # noqa: BLE001 - 最坏情况下仍要留下证据，绝不能二次抛出
        base = None
    if base is not None:
        candidates.append(Path(base))
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        candidates.append(Path(local_app_data) / APP_DIR_NAME)
    candidates.append(Path(sys.executable).resolve().parent)
    candidates.append(Path(tempfile.gettempdir()) / APP_DIR_NAME)
    return candidates


def _write_log(text: str) -> Path | None:
    """把日志写到第一个写得进去的目录；全都失败时返回 ``None``（不抛异常）。"""
    for directory in _candidate_dirs():
        path = directory / LOG_NAME
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(text)
        except OSError:
            continue
        return path
    return None


def _summary() -> str:
    return (
        f"程序：{sys.executable}\n"
        f"Python：{sys.version.split()[0]}\n"
        f"是否打包：{bool(getattr(sys, 'frozen', False))}\n"
        f"候选日志目录：{', '.join(str(path) for path in _candidate_dirs())}\n"
    )


def report_failure(exc: BaseException, *, note: str = "启动失败") -> None:
    """把失败写进日志与控制台。日志写不了也**不能**吞掉原始错误。"""
    detail = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    text = f"\n[{stamp} UTC] {note}\n{_summary()}{detail}\n"
    print(text, file=sys.stderr, flush=True)
    path = _write_log(text)
    if path is None:
        print("（写日志也失败了：请截图本窗口的内容）", file=sys.stderr, flush=True)
    else:
        print(f"已把上面的原因追加写入：{path}", file=sys.stderr, flush=True)


def main() -> int:
    """打包产物入口：导入并运行官方入口，任何异常都留下证据后非零退出。"""
    try:
        from local_agent.api.__main__ import main as api_main

        code = api_main()
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException as exc:  # noqa: BLE001 - 启动阶段任何异常都要留证据
        report_failure(exc)
        return FAILURE_EXIT_CODE
    if code != 0:
        # 友好的启动失败（配置损坏、已有实例在跑……）已经在控制台说明过原因；这里再留一份，
        # 免得用户关掉窗口之后什么都不剩。
        report_failure(RuntimeError(f"入口以退出码 {code} 结束"), note=f"启动失败（退出码 {code}）")
    return code


if __name__ == "__main__":  # pragma: no cover - 由打包后的 exe 触发
    raise SystemExit(main())
