//! MangaTrans 桌面壳：管理 Python sidecar 生命周期（动态端口 + 随机会话令牌）。
//!
//! 启动策略：窗口立即显示（不等后端就绪），前端静默重试请求；后端通过
//! backend.lock 单实例锁（数据目录隔离），App 崩溃遗留的孤儿后端会被下一次
//! 启动复用，不产生进程堆积。

use std::io::Write;
use std::net::TcpListener;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
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
    /// sidecar 进程是否已退出（Terminated 事件置位）
    child_exited: Arc<AtomicBool>,
    /// 复用已有后端时的进程 pid（退出时经 pid 结束，保证下次启动运行新版本）
    reused_pid: Option<u32>,
}

/// 用系统默认浏览器打开外部链接（更新下载等）。仅允许 GitHub 相关域名白名单。
#[tauri::command]
fn open_external(app: AppHandle, url: String) -> Result<(), String> {
    const ALLOWED_PREFIXES: [&str; 4] = [
        "https://github.com/",
        "https://raw.githubusercontent.com/",
        "https://objects.githubusercontent.com/",
        "https://cdn.jsdelivr.net/",
    ];
    if !ALLOWED_PREFIXES.iter().any(|p| url.starts_with(p)) {
        return Err(format!("不允许打开的链接: {url}"));
    }
    app.shell()
        .open(url, None)
        .map_err(|e| e.to_string())
}

#[tauri::command]
fn get_backend_config(state: State<'_, BackendState>) -> (u16, String) {
    (state.port, state.token.clone())
}

/// 后端诊断状态：进程存活、端口、sidecar 日志尾部（前端连接失败时展示，便于用户/技术人员定位）。
#[derive(Serialize)]
pub struct BackendStatus {
    alive: bool,
    port: u16,
    log_tail: String,
}

fn read_log_tail(path: &Path, max_bytes: usize) -> String {
    let Ok(data) = std::fs::read(path) else {
        return String::new();
    };
    let start = data.len().saturating_sub(max_bytes);
    String::from_utf8_lossy(&data[start..]).to_string()
}

#[tauri::command]
fn get_backend_status(state: State<'_, BackendState>) -> BackendStatus {
    let alive = !state.child_exited.load(Ordering::SeqCst);
    let log_tail = read_log_tail(
        &Path::new(&state.data_dir).join("logs").join("sidecar.log"),
        4096,
    );
    BackendStatus {
        alive,
        port: state.port,
        log_tail,
    }
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

/// 后端单实例锁文件：{data_dir}/backend.lock（JSON：pid/port/token）。
fn backend_lock_path(data_dir: &Path) -> PathBuf {
    data_dir.join("backend.lock")
}

/// 读取既有后端实例信息（不存在/损坏返回 None）。
fn read_backend_lock(data_dir: &Path) -> Option<(u16, String, u32)> {
    let path = backend_lock_path(data_dir);
    let text = std::fs::read_to_string(&path).ok()?;
    let value: serde_json::Value = serde_json::from_str(&text).ok()?;
    let port = value.get("port")?.as_u64()?;
    let token = value.get("token")?.as_str()?.to_string();
    let pid = value.get("pid").and_then(|v| v.as_u64()).unwrap_or(0) as u32;
    if port == 0 || port > 65535 {
        return None;
    }
    Some((port as u16, token, pid))
}

/// 探测既有后端是否存活（最多约 2 秒）。存活则复用其端口、令牌与 pid。
fn try_reuse_existing_backend(data_dir: &Path) -> Option<(u16, String, u32)> {
    let (port, token, pid) = read_backend_lock(data_dir)?;
    let url = format!("http://127.0.0.1:{port}/health");
    for _ in 0..4 {
        if reqwest::blocking::get(&url)
            .map(|r| r.status().is_success())
            .unwrap_or(false)
        {
            return Some((port, token, pid));
        }
        std::thread::sleep(Duration::from_millis(500));
    }
    // 锁文件存在但进程不可达：死进程残留，清理锁文件
    let _ = std::fs::remove_file(backend_lock_path(data_dir));
    None
}

/// 后台等待后端健康检查就绪（最多 8 秒；sidecar 正常 2-3 秒就绪）。
fn wait_until_healthy(port: u16, log_path: &Path) {
    let url = format!("http://127.0.0.1:{port}/health");
    for _ in 0..80 {
        if reqwest::blocking::get(&url)
            .map(|r| r.status().is_success())
            .unwrap_or(false)
        {
            append_log(log_path, "[health] backend ready");
            return;
        }
        std::thread::sleep(Duration::from_millis(100));
    }
    append_log(log_path, "[health] timeout after 8s");
}

pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_dialog::init())
        // 单实例锁：重复启动时聚焦已有窗口，避免 sidecar 后端进程堆积
        .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| {
            if let Some(window) = app.get_webview_window("main") {
                let _ = window.unminimize();
                let _ = window.set_focus();
            }
        }))
        .setup(|app| {
            // 数据目录：MT_DATA_DIR > 用户配置 > 系统应用数据目录
            let data_dir = resolve_data_dir(app.handle());
            let log_path = sidecar_log_path(&data_dir);
            let main_window = app
                .get_webview_window("main")
                .expect("main window missing");

            // 复用既有后端：上次 App 崩溃遗留的孤儿 sidecar 仍健康时直接复用，
            // 不新拉进程（消除多后端进程堆积与并发写库）
            if let Some((existing_port, existing_token, existing_pid)) =
                try_reuse_existing_backend(&data_dir)
            {
                append_log(
                    &log_path,
                    &format!("[reuse] 复用已有后端 http://127.0.0.1:{existing_port}"),
                );
                app.manage(BackendState {
                    port: existing_port,
                    token: existing_token,
                    data_dir: data_dir.to_string_lossy().to_string(),
                    child: Mutex::new(None),
                    child_exited: Arc::new(AtomicBool::new(false)),
                    reused_pid: Some(existing_pid),
                });
                // 立即显示窗口：用户打开即见主界面，不等任何连接
                let _ = main_window.show();
                let _ = main_window.set_focus();
                return Ok(());
            }

            let port = find_free_port().expect("无法分配本地端口");
            let token = generate_token();

            let child_exited = Arc::new(AtomicBool::new(false));
            let exit_flag = child_exited.clone();

            let sidecar = app
                .shell()
                .sidecar("mangatrans-backend")
                .expect("sidecar 未打包")
                .env("MT_PORT", port.to_string())
                .env("MT_AUTH_TOKEN", &token)
                .env("MT_DATA_DIR", data_dir.to_str().unwrap_or_default())
                .env("MT_MIGRATE_ON_START", "1")
                // Windows 管道默认可能是 cp1252，Python 中文日志会直接抛 UnicodeEncodeError。
                // 强制 UTF-8，保证任何后端诊断输出都不会中断启动/退出流程。
                .env("PYTHONIOENCODING", "utf-8")
                .env("PYTHONUTF8", "1");

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
                                CommandEvent::Terminated(payload) => {
                                    exit_flag.store(true, Ordering::SeqCst);
                                    append_log(
                                        &event_log,
                                        &format!("[terminated] code={:?}", payload.code),
                                    );
                                }
                                _ => {}
                            }
                        }
                    });
                    Some(child)
                }
                Err(err) => {
                    // spawn 失败时没有子进程，必须把状态标记为已退出；
                    // 否则前端诊断会误报「后端进程运行中，正在初始化」，掩盖真正原因。
                    child_exited.store(true, Ordering::SeqCst);
                    append_log(&log_path, &format!("[spawn-error] {err}"));
                    None
                }
            };

            // 窗口立即显示：不等健康检查（前端静默重试，连接失败展示诊断信息）
            let _ = main_window.show();
            let _ = main_window.set_focus();
            // 健康检查后台执行，仅写日志供诊断
            let health_log = log_path.clone();
            tauri::async_runtime::spawn_blocking(move || {
                wait_until_healthy(port, &health_log);
            });

            app.manage(BackendState {
                port,
                token,
                data_dir: data_dir.to_string_lossy().to_string(),
                child: Mutex::new(child),
                child_exited,
                reused_pid: None,
            });
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            open_external,
            get_backend_config,
            get_backend_status,
            get_storage_info,
            choose_data_dir,
            pick_directory,
            save_text_file,
            save_file_base64
        ])
        .build(tauri::generate_context!())
        .expect("Tauri 构建失败")
        .run(|app, event| {
            // 退出时回收后端进程：新 spawn 的 child 直接 kill；
            // 复用的孤儿后端按 pid 结束（保证下次启动运行当前版本后端）
            if let tauri::RunEvent::ExitRequested { .. } | tauri::RunEvent::Exit = event {
                if let Some(state) = app.try_state::<BackendState>() {
                    if let Some(pid) = state.reused_pid {
                        #[cfg(target_os = "windows")]
                        let _ = std::process::Command::new("taskkill")
                            .args(["/F", "/PID", &pid.to_string()])
                            .output();
                        #[cfg(not(target_os = "windows"))]
                        let _ = std::process::Command::new("kill")
                            .arg(pid.to_string())
                            .output();
                    }
                    if let Ok(mut guard) = state.child.lock() {
                        if let Some(child) = guard.take() {
                            let _ = child.kill();
                        }
                    }
                }
            }
        });
}
