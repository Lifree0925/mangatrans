"""pytest 公共配置：隔离数据目录与数据库 + 带令牌 TestClient。"""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from app.core import security
from app.core.config import settings


@pytest.fixture()
def wait_task():
    """等待线程型任务完成并返回任务 JSON（parse 等已任务化）。"""
    import time

    def _wait(c, task_id: str, timeout: float = 30) -> dict:
        t0 = time.time()
        while time.time() - t0 < timeout:
            r = c.get(f"/api/v1/tasks/{task_id}")
            if r.status_code == 200:
                data = r.json()
                if data["status"] in ("completed", "failed"):
                    return data
            time.sleep(0.2)
        raise AssertionError(f"任务超时未完成: {task_id}")

    return _wait


@pytest.fixture()
def client(tmp_path: Path):
    """每个测试独立数据库，会话令牌固定。"""
    import app.core.database as db

    settings.data_dir = tmp_path
    test_engine = db.make_engine(f"sqlite:///{tmp_path / 'database' / 'app.sqlite'}")
    db.engine = test_engine
    db.SessionLocal = sessionmaker(
        bind=test_engine, autoflush=False, expire_on_commit=False
    )

    from app.main import app  # 先导入以注册所有模型到 metadata

    db.Base.metadata.drop_all(test_engine)
    db.Base.metadata.create_all(test_engine)

    token = security.generate_token()
    security.set_token(token)

    c = TestClient(app)
    c.headers["x-auth-token"] = token
    yield c
    test_engine.dispose()
