"""应用配置：从环境变量读取，Windows 下由 Tauri 注入。"""

from pathlib import Path

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """本地应用配置。"""

    model_config = {"env_prefix": "MT_"}

    # 用户数据目录；Windows 正式环境由 Tauri 传入
    data_dir: Path = Path.home() / ".local" / "share" / "mangatrans"
    # 仅监听回环地址，不允许 0.0.0.0
    host: str = "127.0.0.1"
    # 动态端口：0 表示由系统分配
    port: int = 0
    # 会话令牌：为空则启动时自动生成并打印到启动日志
    auth_token: str = ""

    @property
    def database_url(self) -> str:
        db_path = self.data_dir / "database" / "app.sqlite"
        return f"sqlite:///{db_path}"


settings = Settings()
