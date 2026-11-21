"""任务执行器（Sprint 3 版）：进程内 asyncio 执行，状态持久化。

Sprint 5 将替换为持久化 Worker 循环 + 崩溃恢复；本版保证任务创建与状态流转正确。
"""

import asyncio
import json
import traceback

from sqlalchemy import select

import app.core.database as dbmod
from app.llm.registry import get_provider
from app.models.task import Task
from app.services import express, knowledge_service, style_guide_service


def _sess_factory():
    """运行时解析 SessionLocal：测试替换 engine 后仍指向当前库。"""
    return dbmod.SessionLocal()


def create_task(db, project_id: str, task_type: str, payload: dict) -> Task:
    task = Task(
        project_id=project_id,
        task_type=task_type,
        config_snapshot_json=json.dumps(payload, ensure_ascii=False),
    )
    db.add(task)
    db.commit()
    db.refresh(task)
    return task


def launch(task_id: str) -> None:
    """Fire-and-forget：独立线程执行任务（Sprint 5 由持久 Worker 替代）。"""
    import threading

    threading.Thread(target=lambda: asyncio.run(_run(task_id)), daemon=True).start()


async def _run(task_id: str) -> None:
    from datetime import datetime, timezone

    with _sess_factory() as db:
        task = db.get(Task, task_id)
        if task is None:
            return
        task.status = "running"
        task.started_at = datetime.now(timezone.utc)
        task.error_message = None
        db.commit()
        payload = json.loads(task.config_snapshot_json or "{}")

    try:
        if task.task_type == "knowledge":
            result = await knowledge_service.extract_knowledge_for_document(
                _sess_factory,
                project_id=task.project_id,
                source_document_id=payload["source_document_id"],
                provider=get_provider(),
                model=payload.get("model", "fake-model"),
                mode=payload.get("mode", "guided"),
            )
        elif task.task_type == "express":
            result = await express.run_express(
                _sess_factory,
                project_id=task.project_id,
                source_document_id=payload["source_document_id"],
                model=payload.get("model", "fake-model"),
                task_id=task_id,
            )
        elif task.task_type == "parse":
            from app.models.document import SourceDocument
            from app.models.project import Project
            from app.services import importer, parse_service
            from sqlalchemy import select as sa_select

            with _sess_factory() as db:
                project = db.get(Project, task.project_id)
                running = db.scalar(
                    sa_select(Task.id).where(
                        Task.project_id == task.project_id,
                        Task.status.in_(["running", "queued"]),
                        Task.id != task.id,
                    ).limit(1)
                )
                if running:
                    raise RuntimeError("项目有进行中的任务，无法重新解析")
                doc = db.get(SourceDocument, payload["source_document_id"])
                text = importer.read_normalized(doc)
                result = parse_service.parse_and_persist(db, doc, text)
                project.active_source_document_id = doc.id
                db.commit()
            result = {
                "coverage_ok": result.report.coverage_ok,
                "total_chars": result.report.total_chars,
                "covered_chars": result.report.covered_chars,
                "chapter_count": result.report.chapter_count,
                "scene_count": result.report.scene_count,
                "dialogue_count": result.report.dialogue_count,
                "narrative_count": result.report.narrative_count,
                "other_count": result.report.other_count,
                "anomalies": result.report.anomalies,
            }
        elif task.task_type == "translate" and payload.get("kind") == "trial":
            result = await style_guide_service.run_trial(
                _sess_factory,
                project_id=task.project_id,
                source_document_id=payload["source_document_id"],
                provider=get_provider(),
                model=payload.get("model", "fake-model"),
            )
        else:
            raise ValueError(f"未知任务类型: {task.task_type}")
    except Exception as exc:  # noqa: BLE001 任务失败必须记录
        with _sess_factory() as db:
            task = db.get(Task, task_id)
            task.status = "failed"
            task.error_message = f"{type(exc).__name__}: {exc}"[:2000]
            task.started_at = None  # 无执行线程
            db.commit()
        return

    with _sess_factory() as db:
        task = db.get(Task, task_id)
        if task.status == "paused":
            # 用户暂停：不覆盖为 completed（保留进度，resume 重新调度续跑）
            task.started_at = None
            task.progress_json = json.dumps(result, ensure_ascii=False)
            db.commit()
            return
        task.status = "completed"
        task.started_at = None
        task.progress_json = json.dumps(result, ensure_ascii=False)
        db.commit()


def recover_threaded_tasks(db) -> int:
    """启动时回收：running 且无 task_items（线程型任务）→ failed，可手动重试。

    task_items 型任务由常驻 Worker 继续执行，不受进程重启影响。
    """
    from sqlalchemy import select

    from app.models.task import TaskItem

    zombie_ids = db.scalars(
        select(Task.id).where(
            Task.status == "running",
            ~Task.id.in_(select(TaskItem.task_id)),
        )
    ).all()
    for tid in zombie_ids:
        task = db.get(Task, tid)
        task.status = "failed"
        task.error_message = "应用中断：任务未完成，请点击重试。"
        task.started_at = None
    db.commit()
    return len(zombie_ids)
