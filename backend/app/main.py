"""Mangatrans 本地后端入口。

仅监听 127.0.0.1；会话令牌由 Tauri 传入或自动生成。
"""

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app import __version__
from app.api.v1 import dialogue, documents, knowledge, projects, qa, settings_api, translation
from app.core import security
from app.core.config import settings
from app.core.credentials import CredentialStoreError
from app.core.single_instance import acquire_single_instance
from app.pipeline.runner import ActiveTaskConflict

# 单实例锁：同一数据目录已有后端存活时本进程退出，避免孤儿进程堆积与并发写库。
# 注意：锁获取失败不等于「已有实例」——Windows 锁机制异常时若直接退出，
# 会导致新装用户后端不启动、前端永久连接失败。必须探测已有实例真实存活后才退出。
_LOCK_FD = acquire_single_instance()
if _LOCK_FD is None:
    import sys

    from app.core.single_instance import existing_instance_alive

    if existing_instance_alive():
        print("backend: existing instance is running; this process exits", flush=True)
        sys.exit(0)
    print("backend: single-instance lock unavailable; continuing startup", flush=True)

ALLOWED_ORIGINS = [
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "http://localhost:1420",
    "http://127.0.0.1:1420",
    "tauri://localhost",
    "https://tauri.localhost",
    "http://tauri.localhost",  # Tauri v2 打包后 WebView 的实际 Origin（Windows/Linux）
]

# 兜底：任意本机回环端口（开发/打包环境端口不固定）
ALLOWED_ORIGIN_REGEX = r"https?://(tauri\.localhost|localhost|127\.0\.0\.1)(:\d+)?"

PUBLIC_PATHS = {"/health", "/docs", "/openapi.json", "/redoc"}


def run_migrations_if_needed() -> None:
    """打包环境（PyInstaller）启动时自动执行 Alembic 迁移。"""
    import sys

    from app.core.config import settings

    if not settings.migrate_on_start:
        return
    from pathlib import Path

    from alembic import command
    from alembic.config import Config

    if getattr(sys, "frozen", False):
        base = Path(sys._MEIPASS)  # noqa: SLF001 PyInstaller 资源目录
    else:
        # 开发/容器环境：优先工作目录（backend 或 /app），回退 app/main.py 所在 backend 根。
        # 注意：main.py 位于 backend/app/main.py，backend 根是 parents[1]，不是 parents[2]。
        backend_root = Path(__file__).resolve().parents[1]
        candidates = [Path.cwd(), backend_root]
        base = next((c for c in candidates if (c / "alembic.ini").exists()), backend_root)
    ini = base / "alembic.ini"
    script_dir = base / "migrations"
    cfg = Config(str(ini))
    cfg.set_main_option("script_location", str(script_dir))
    cfg.set_main_option("sqlalchemy.url", settings.database_url)
    command.upgrade(cfg, "head")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """启动迁移、恢复已验证模型到 registry、启动持久 Worker；退出时取消 Worker。"""
    from app.pipeline.worker import worker_loop
    from app.core import credentials

    run_migrations_if_needed()
    # 历史明文凭据仅在系统凭据库写入并回读成功后删除；失败时保留原文件，
    # 正式环境后续读取会返回明确错误，不会继续使用明文。
    try:
        migrated = credentials.migrate_file_credentials()
        if migrated:
            print(f"backend: migrated {migrated} credential(s) to system store", flush=True)
    except Exception as exc:  # noqa: BLE001 启动可继续，但凭据操作会明确报错
        print(f"backend: credential migration failed: {exc}", flush=True)

    # 重启后必须从数据库恢复已验证模型到内存 registry，
    # 否则翻译任务会落到开发用 FakeProvider（返回空对象导致失败）。
    import app.core.database as dbmod

    from app.pipeline.runner import recover_threaded_tasks
    from app.pipeline.worker import recover_all_running
    from app.services.provider_service import set_default_from_db

    with dbmod.SessionLocal() as db:
        try:
            set_default_from_db(db)
        except CredentialStoreError as exc:
            # 凭据库异常不应让整个本地服务无法启动；设置页/凭据操作会返回明确错误。
            print(f"backend: credential store unavailable: {exc}", flush=True)
            from app.llm.fake import FakeProvider
            from app.llm.registry import set_provider

            set_provider(FakeProvider())
        # 崩溃恢复：把无执行线程的 running 线程型任务标记失败（可手动重试）
        recover_threaded_tasks(db)
        # 进程重启后无正在执行的窗口：全部 running 项回 pending，Worker 立即续跑
        recovered = recover_all_running(db)
        if recovered:
            print(f"backend: recovered {recovered} running windows; worker will resume", flush=True)

    worker_task = asyncio.create_task(worker_loop())
    try:
        yield
    finally:
        worker_task.cancel()
        # 正常关闭时结算当前运行段；崩溃场景由下次启动按最后写库时间恢复。
        try:
            from app.pipeline.task_timer import settle_active_task_timers

            with dbmod.SessionLocal() as db:
                settle_active_task_timers(db)
        except Exception:  # noqa: BLE001 退出清理不能阻塞窗口关闭
            pass


def create_app() -> FastAPI:
    app = FastAPI(title="Mangatrans Local API", version=__version__, lifespan=lifespan)

    @app.exception_handler(ActiveTaskConflict)
    async def _active_task_conflict(_request: Request, exc: ActiveTaskConflict):
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(CredentialStoreError)
    async def _credential_store_error(_request: Request, exc: CredentialStoreError):
        return JSONResponse(status_code=503, content={"detail": str(exc)})

    # CORS 仅允许本机前端与 Tauri 来源
    app.add_middleware(
        CORSMiddleware,
        allow_origins=ALLOWED_ORIGINS,
        allow_origin_regex=ALLOWED_ORIGIN_REGEX,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def auth_guard(request: Request, call_next):
        """所有请求（除公开路径）必须携带会话令牌。"""
        # OPTIONS 预检不携带自定义头，放行；由 CORS 中间件处理
        if request.method == "OPTIONS" or request.url.path in PUBLIC_PATHS:
            return await call_next(request)
        token = request.headers.get("x-auth-token")
        if not security.check_token(token):
            return JSONResponse(status_code=401, content={"detail": "unauthorized"})
        return await call_next(request)

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok", "version": __version__}

    app.include_router(projects.router, prefix="/api/v1")
    app.include_router(documents.router, prefix="/api/v1")
    app.include_router(knowledge.overview_router, prefix="/api/v1")
    app.include_router(knowledge.router, prefix="/api/v1")
    app.include_router(translation.router, prefix="/api/v1")
    app.include_router(qa.router, prefix="/api/v1")
    app.include_router(settings_api.router, prefix="/api/v1")
    app.include_router(dialogue.router, prefix="/api/v1")
    return app


# 会话令牌：Tauri 传入则使用，否则每次启动自动生成
security.set_token(settings.auth_token or security.generate_token())

app = create_app()


if __name__ == "__main__":
    # PyInstaller sidecar 入口：Tauri 通过 MT_PORT / MT_AUTH_TOKEN / MT_DATA_DIR 注入配置
    import uvicorn

    uvicorn.run(
        app,
        host=settings.host,
        port=settings.port,
        log_level="info",
    )
