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


def fake_translation_entries(content: str, tag: str, *, stage: str = "") -> list[dict]:
    """从翻译 Prompt 中读取 [segment_id] 原文，生成结构合法的假译文。

    假译文保留说话人/场景号/△/【】锚点，供管线级测试验证结构校验，
    而不是绕过真实生产约束。
    """
    import re as _re

    pattern = _re.compile(
        r"^\[([0-9a-f]{32})\]\s(.*?)(?=^\[[0-9a-f]{32}\]\s|\Z)",
        _re.MULTILINE | _re.DOTALL,
    )
    dialogue = _re.compile(
        r"^\s*([^（(：:]+?)\s*(?:[（(]([^）)]*)[）)])?\s*[:：]\s*(.+)$"
    )
    entries: list[dict] = []
    for sid, source in pattern.findall(content):
        target_lines: list[str] = []
        for line in [x.strip() for x in source.splitlines() if x.strip()]:
            m = dialogue.match(line)
            if m:
                cue = m.group(2) or ""
                cue_part = f" ({cue})" if cue else ""
                target_lines.append(f"Speaker{cue_part}: {tag} {sid[:8]} {stage}".strip())
                continue
            scene = _re.match(r"^(\d+)\s*[-–]\s*(\d+)", line)
            if scene:
                target_lines.append(f"{scene.group(1)}-{scene.group(2)} {tag} {sid[:8]} {stage}".strip())
                continue
            if line.startswith("△"):
                target_lines.append(f"△ {tag} {sid[:8]} {stage}".strip())
                continue
            if line.startswith("【"):
                target_lines.append(f"[Sound effect: {tag} {sid[:8]} {stage}]".strip())
                continue
            target_lines.append(f"{tag} {sid[:8]} {stage}".strip())
        entries.append({
            "segment_id": sid,
            "target_text": "\n".join(target_lines),
            "notes": [],
            "ambiguity": [],
        })
    return entries
