"""T87：显式诊断，不启动服务或修复环境。"""

from __future__ import annotations

import argparse
from pathlib import Path

from local_agent import app_paths
from local_agent.diagnostics import print_diagnosis


def main() -> int:
    parser = argparse.ArgumentParser(description="只读诊断配置、静态产物和已有数据库")
    parser.add_argument("--config", type=Path, default=app_paths.default_config_path())
    return print_diagnosis(parser.parse_args().config)


if __name__ == "__main__":
    raise SystemExit(main())
