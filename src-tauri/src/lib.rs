//! MangaTrans 桌面壳：管理 Python sidecar 生命周期（动态端口 + 随机会话令牌）。
//!
//! 启动策略：sidecar 启动后立即显示窗口，健康检查在后台任务中进行，
//! 不阻塞窗口创建（one-file sidecar 首次启动解压可能需要数秒到数十秒）。

use std::io::Write;
use std::net::TcpListener;
use std::path::{Path, PathBuf};
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

/// sidecar 日志路径：用户数据目录/logs/sidecar.log。
fn sidecar_log_path(data_dir: &Path) -> PathBuf {
    let dir = data_dir.join("logs");
    let _ = std::fs::create_dir_all(&dir);
    dir.join("sidecar.log")
}

fn append_log(path: &Path, line: &str) {
    if let Ok(mut file) = std::fs::OpenOptions::new().create(true).append(true).open(path) {
        let _ = writeln!(file, "{line}");
    }
}

/// 后台等待后端健康检查就绪（最多 60 秒，one-file sidecar 首次启动需解压）。
fn wait_until_healthy(port: u16, log_path: &Path) {
    let url = format!("http://127.0.0.1:{port}/health");
    for _ in 0..600 {
        if reqwest::blocking::get(&url)
            .map(|r| r.status().is_success())
            .unwrap_or(false)
        {
            append_log(log_path, "[health] backend ready");
            return;
        }
        std::thread::sleep(Duration::from_millis(100));
    }
    append_log(log_path, "[health] timeout after 60s");
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
            let log_path = sidecar_log_path(&data_dir);

            let sidecar = app
                .shell()
                .sidecar("mangatrans-backend")
                .expect("sidecar 未打包")
                .env("MT_PORT", port.to_string())
                .env("MT_AUTH_TOKEN", &token)
                .env("MT_DATA_DIR", data_dir.to_str().unwrap_or_default())
                .env("MT_MIGRATE_ON_START", "1");

            // sidecar 启动失败时不崩溃：记录日志，窗口照常显示，由前端提示错误
            let child = match sidecar.spawn() {
                Ok((mut rx, child)) => {
                    let event_log = log_path.clone();
                    tauri::async_runtime::spawn(async move {
                        while let Some(event) = rx.recv().await {
                            match event {
                                CommandEvent::Stdout(line) => append_log(
                                    &event_log,
                                    &format!("[stdout] {}", String::from_utf8_lossy(&line)),
                                ),
                                CommandEvent::Stderr(line) => append_log(
                                    &event_log,
                                    &format!("[stderr] {}", String::from_utf8_lossy(&line)),
                                ),
                                CommandEvent::Error(err) => {
                                    append_log(&event_log, &format!("[error] {err}"))
                                }
                                CommandEvent::Terminated(payload) => append_log(
                                    &event_log,
                                    &format!("[terminated] code={:?}", payload.code),
                                ),
                                _ => {}
                            }
                        }
                    });
                    Some(child)
                }
                Err(err) => {
                    append_log(&log_path, &format!("[spawn-error] {err}"));
                    None
                }
            };

            // 健康检查后台执行：窗口先显示，前端带重试等待后端就绪
            let health_log = log_path.clone();
            tauri::async_runtime::spawn_blocking(move || {
                wait_until_healthy(port, &health_log);
            });

            app.manage(BackendState {
                port,
                token,
                child: Mutex::new(child),
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
