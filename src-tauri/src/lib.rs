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
use serde::{Deserialize, Serialize};
use tauri::{AppHandle, Manager, State};
use tauri_plugin_shell::process::{CommandChild, CommandEvent};
use tauri_plugin_shell::ShellExt;

/// 后端连接信息（前端通过 invoke 获取）。
struct BackendState {
    port: u16,
    token: String,
    data_dir: String,
    child: Mutex<Option<CommandChild>>,
}

#[tauri::command]
fn get_backend_config(state: State<'_, BackendState>) -> (u16, String) {
    (state.port, state.token.clone())
}

#[tauri::command]
fn get_storage_info(app: AppHandle, state: State<'_, BackendState>) -> serde_json::Value {
    serde_json::json!({
        "data_dir": state.data_dir,
        "config_path": config_path(&app).to_string_lossy(),
        "uses_custom_dir": load_config(&app).data_dir.is_some(),
    })
}

/// 导出保存（二进制，如 DOCX）：base64 解码后写入文件，返回保存路径。
#[tauri::command]
async fn save_file_base64(
    app: AppHandle,
    default_name: String,
    content_base64: String,
    default_dir: Option<String>,
) -> Result<Option<String>, String> {
    use base64::{engine::general_purpose::STANDARD, Engine as _};
    use tauri_plugin_dialog::DialogExt;

    let mut builder = app.dialog().file().set_file_name(&default_name);
    if let Some(dir) = default_dir {
        if !dir.is_empty() {
            builder = builder.set_directory(&dir);
        }
    }
    let Some(path) = builder.blocking_save_file() else {
        return Ok(None);
    };
    let path_buf = path.into_path().map_err(|e| e.to_string())?;
    let bytes = STANDARD
        .decode(content_base64)
        .map_err(|e| format!("解码失败: {e}"))?;
    std::fs::write(&path_buf, bytes).map_err(|e| e.to_string())?;
    Ok(Some(path_buf.to_string_lossy().to_string()))
}

/// 导出保存：弹出系统“另存为”对话框，写入文本文件，返回保存路径。
#[tauri::command]
async fn save_text_file(
    app: AppHandle,
    default_name: String,
    content: String,
    default_dir: Option<String>,
) -> Result<Option<String>, String> {
    use tauri_plugin_dialog::DialogExt;

    let mut builder = app.dialog().file().set_file_name(&default_name);
    if let Some(dir) = default_dir {
        if !dir.is_empty() {
            builder = builder.set_directory(&dir);
        }
    }
    let Some(path) = builder.blocking_save_file() else {
        return Ok(None);
    };
    let path_buf = path.into_path().map_err(|e| e.to_string())?;
    std::fs::write(&path_buf, content).map_err(|e| e.to_string())?;
    Ok(Some(path_buf.to_string_lossy().to_string()))
}

/// 通用目录选择器（仅返回路径，不持久化）：用于新建项目时选择项目文件夹。
#[tauri::command]
async fn pick_directory(app: AppHandle) -> Result<Option<String>, String> {
    use tauri_plugin_dialog::DialogExt;

    Ok(app
        .dialog()
        .file()
        .blocking_pick_folder()
        .map(|path| path.to_string()))
}

/// 打开系统目录选择器，保存自定义数据目录（重启应用后生效）。
#[tauri::command]
async fn choose_data_dir(app: AppHandle) -> Result<Option<String>, String> {
    use tauri_plugin_dialog::DialogExt;

    let picked = app.dialog().file().blocking_pick_folder();
    let Some(path) = picked else {
        return Ok(None);
    };
    let dir = path.to_string();
    let mut config = load_config(&app);
    config.data_dir = Some(dir.clone());
    save_config(&app, &config)?;
    Ok(Some(dir))
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

/// 应用配置文件（固定在系统应用数据目录，不受自定义数据目录影响）。
#[derive(Serialize, Deserialize, Default)]
struct AppConfig {
    /// 用户自定义数据目录（磁盘上的项目数据位置）
    data_dir: Option<String>,
}

fn config_path(app: &AppHandle) -> PathBuf {
    let dir = app
        .path()
        .app_data_dir()
        .unwrap_or_else(|_| PathBuf::from("."));
    let _ = std::fs::create_dir_all(&dir);
    dir.join("config.json")
}

fn load_config(app: &AppHandle) -> AppConfig {
    std::fs::read_to_string(config_path(app))
        .ok()
        .and_then(|text| serde_json::from_str(&text).ok())
        .unwrap_or_default()
}

fn save_config(app: &AppHandle, config: &AppConfig) -> Result<(), String> {
    let text = serde_json::to_string_pretty(config).map_err(|e| e.to_string())?;
    std::fs::write(config_path(app), text).map_err(|e| e.to_string())
}

/// 数据目录优先级：MT_DATA_DIR 环境变量 > config.json > 系统应用数据目录。
fn resolve_data_dir(app: &AppHandle) -> PathBuf {
    if let Ok(dir) = std::env::var("MT_DATA_DIR") {
        if !dir.is_empty() {
            return PathBuf::from(dir);
        }
    }
    if let Some(dir) = load_config(app).data_dir {
        if !dir.is_empty() {
            return PathBuf::from(dir);
        }
    }
    app.path()
        .app_data_dir()
        .unwrap_or_else(|_| PathBuf::from("."))
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
        .plugin(tauri_plugin_dialog::init())
        .setup(|app| {
            let port = find_free_port().expect("无法分配本地端口");
            let token = generate_token();
            // 数据目录：MT_DATA_DIR > 用户配置 > 系统应用数据目录
            let data_dir = resolve_data_dir(app.handle());
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
                data_dir: data_dir.to_string_lossy().to_string(),
                child: Mutex::new(child),
            });
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            get_backend_config,
            get_storage_info,
            choose_data_dir,
            pick_directory,
            save_text_file,
            save_file_base64
        ])
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
