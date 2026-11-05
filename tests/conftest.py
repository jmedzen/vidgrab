"""全域測試隔離。

為什麼要有這個檔：`projects_root()` 沒設 `VIDGRAB_HOME` 時會退回
`cwd()/projects`，也就是「你真正的素材庫」。以前 101 個測試各靠自己
monkeypatch，只要漏掉一個，那個測試就會對著真實資料跑 —— 實際發生過
一次資料夾被清空。這裡改成一律指到 tmp，任何測試都不可能碰到真的資料。
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_vidgrab_home(tmp_path, monkeypatch):
    """每個測試都自動在暫存目錄裡跑；要自己的路徑再自行覆寫即可。"""
    home = tmp_path / "projects"
    auth = tmp_path / "auth"
    home.mkdir(parents=True, exist_ok=True)
    auth.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("VIDGRAB_HOME", str(home))
    monkeypatch.setenv("VIDGRAB_AUTH_DIR", str(auth))
    # 有些測試會 chdir；一律固定在 tmp，避免相對路徑意外指到 repo
    monkeypatch.chdir(tmp_path)
    yield
