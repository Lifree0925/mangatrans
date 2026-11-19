"""pytest 公共配置：隔离数据目录与数据库 + 带令牌 TestClient。"""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from app.core import security
from app.core.config import settings


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
