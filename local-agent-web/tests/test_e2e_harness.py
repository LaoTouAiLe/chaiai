"""假模型验收服务必须使用独立数据根，不能继承开发机的数据位置。"""
import os
import runpy
import sys
from pathlib import Path

import pytest

from local_agent import app_paths


def test_fake_server_pins_data_and_credentials_before_opening_runtime(tmp_path, monkeypatch):
    script = Path(__file__).resolve().parents[1] / "scripts" / "serve_fake.py"
    main = runpy.run_path(str(script))["main"]
    dist = tmp_path / "frontend"
    dist.mkdir()
    (dist / "index.html").write_text("test", encoding="utf-8")
    monkeypatch.setenv("CHAIAI_HOME", str(tmp_path / "inherited-user-data"))
    monkeypatch.setattr(sys, "argv", [str(script), "--data", str(tmp_path), "--dist", str(dist)])
    captured = {}

    class RuntimeReached(Exception):
        pass

    def capture(config, **kwargs):
        captured["root"] = app_paths.data_root_of(config)
        captured["credential_store"] = config.model_settings.credential_store
        captured["load_profile"] = config.terminal.load_profile
        raise RuntimeReached

    monkeypatch.setitem(main.__globals__, "open_runtime", capture)
    with pytest.raises(RuntimeReached):
        main()
    assert Path(os.environ["CHAIAI_HOME"]) == tmp_path / "home"
    assert captured["root"] == tmp_path / "home"
    assert captured["credential_store"] == "memory"
    assert captured["load_profile"] is False
