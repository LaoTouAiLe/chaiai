"""Build or verify the pinned Windows IPC bundle; never downloads a compiler."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "src/local_agent/native/windows"
OUTPUT = SOURCE / "bin"


def main() -> int:
    """Require an explicit Zig executable, or verify existing distributable files."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--zig", type=Path)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.check:
        from local_agent.security.win_ipc import verify_bundle

        verify_bundle(OUTPUT, check_sources=True)
        print("Windows IPC bundle verified")
        return 0
    if args.zig is None or not args.zig.is_file():
        parser.error("--zig must point to an explicitly installed compiler")
    compiler = args.zig.resolve()
    version = subprocess.check_output([str(compiler), "version"], text=True).strip()
    OUTPUT.mkdir(exist_ok=True)
    env = {**os.environ, "ZIG_GLOBAL_CACHE_DIR": str(ROOT / "build/zig-cache")}
    for arch, target in (("x64", "x86_64-windows-gnu"), ("x86", "x86-windows-gnu")):
        common = [str(compiler), "cc", "-target", target, "-O2", "-Wall", "-Wextra", "-Werror",
                  "-U_WIN32_WINNT", "-D_WIN32_WINNT=0x0a00", "-DPSAPI_VERSION=2",
                  f"-ffile-prefix-map={ROOT}=.", "-I", str(SOURCE)]
        subprocess.run([*common, "-shared", str(SOURCE / "ipc_compat.c"),
                        str(SOURCE / "ipc_common.c"), "-ladvapi32", "-o",
                        str(OUTPUT / f"ipc-compat-{arch}.dll")], check=True, env=env)
        subprocess.run([*common, "-municode", str(SOURCE / "ipc_loader.c"),
                        str(SOURCE / "ipc_common.c"), "-o",
                        str(OUTPUT / f"ipc-loader-{arch}.exe")], check=True, env=env)
    files = {name: hashlib.sha256((OUTPUT / name).read_bytes()).hexdigest()
             for arch in ("x64", "x86")
             for name in (f"ipc-compat-{arch}.dll", f"ipc-loader-{arch}.exe")}
    sources = {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
               for path in sorted(SOURCE.iterdir()) if path.suffix in {".c", ".h"}}
    manifest = {"schema_version": 1, "compiler": f"zig {version}",
                "files": files, "sources": sources}
    (OUTPUT / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"compiler": version, "files": files}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
