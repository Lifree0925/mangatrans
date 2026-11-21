/** 用系统浏览器打开外部链接：Tauri 内走 Rust 命令（白名单校验），开发模式用 window.open。 */

export async function openExternal(url: string): Promise<void> {
  if ((window as unknown as { __TAURI_INTERNALS__?: unknown }).__TAURI_INTERNALS__) {
    const { invoke } = await import("@tauri-apps/api/core");
    await invoke("open_external", { url });
  } else {
    window.open(url, "_blank", "noopener");
  }
}
