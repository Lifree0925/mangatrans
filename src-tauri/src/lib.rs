//! MangaTrans 桌面壳：管理 Python sidecar 生命周期（动态端口 + 随机会话令牌）。

use std::net::TcpListener;
use std::sync::Mutex;
use std::time::Duration;

use rand::distributions::Alphanumeric;
use rand::Rng;
use tauri::{Manager, State};
use tauri_plugin_shell::process::{CommandChild, CommandEvent};
use tauri_plugin_shell::ShellExt;

/// 后端连接信息（前端通过 invoke 获取）。
struct BackendState {
    port: u16,
    token: String,
    child: Mutex<Option<CommandChild>>,
}

#[tauri::command]
fn get_backend_config(state: State<'_, BackendState>) -> (u16, String) {
    (state.port, state.token.clone())
}

/// 由系统分配空闲端口（绑定 0 后立即释放，交给 sidecar 使用）。
fn find_free_port() -> Option<u16> {
    TcpListener::bind(("127.0.0.1", 0))
        .ok()
        .and_then(|l| l.local_addr().ok())
        .map(|a| a.port())
}

/// 每次启动生成高强度随机会话令牌（32 字符）。
fn generate_token() -> String {
    rand::thread_rng()
        .sample_iter(&Alphanumeric)
        .take(32)
        .map(char::from)
        .collect()
}

/// 等待后端健康检查就绪（最多 30 秒，one-file sidecar 首次启动需解压）。
fn wait_until_healthy(port: u16) -> bool {
    let url = format!("http://127.0.0.1:{port}/health");
    for _ in 0..300 {
        if reqwest::blocking::get(&url)
            .map(|r| r.status().is_success())
            .unwrap_or(false)
        {
            return true;
        }
        std::thread::sleep(Duration::from_millis(100));
    }
    false
}

pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .setup(|app| {
            let port = find_free_port().expect("无法分配本地端口");
            let token = generate_token();
            // MT_DATA_DIR 可覆盖数据目录（便携测试隔离用），否则用系统应用数据目录
            let data_dir = std::env::var("MT_DATA_DIR")
                .map(std::path::PathBuf::from)
                .unwrap_or_else(|_| app.path().app_data_dir().expect("无法获取应用数据目录"));

            let sidecar = app
                .shell()
                .sidecar("mangatrans-backend")
                .expect("sidecar 未打包")
                .env("MT_PORT", port.to_string())
                .env("MT_AUTH_TOKEN", &token)
                .env("MT_DATA_DIR", data_dir.to_str().unwrap_or_default())
                .env("MT_MIGRATE_ON_START", "1");

            let (mut rx, child) = sidecar.spawn().expect("启动 sidecar 失败");
            // 消费输出流，避免管道阻塞
            tauri::async_runtime::spawn(async move {
                while let Some(event) = rx.recv().await {
                    if let CommandEvent::Error(err) = event {
                        eprintln!("[sidecar] {err}");
                    }
                }
            });

            let ready = wait_until_healthy(port);
            if !ready {
                eprintln!("[sidecar] 健康检查超时");
            }
            app.manage(BackendState {
                port,
                token,
                child: Mutex::new(Some(child)),
            });
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![get_backend_config])
        .build(tauri::generate_context!())
        .expect("Tauri 构建失败")
        .run(|app, event| {
            // 退出时回收 sidecar 进程
            if let tauri::RunEvent::ExitRequested { .. } | tauri::RunEvent::Exit = event {
                if let Some(state) = app.try_state::<BackendState>() {
                    if let Ok(mut guard) = state.child.lock() {
                        if let Some(child) = guard.take() {
                            let _ = child.kill();
                        }
                    }
                }
            }
        });
}
