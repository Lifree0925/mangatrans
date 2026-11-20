/** 本地 API 客户端：动态解析后端地址与令牌（Tauri 环境从 invoke 获取，开发环境读 env）。 */

interface BackendConfig {
  base: string;
  token: string;
}

let _config: BackendConfig | null = null;

async function resolveConfig(): Promise<BackendConfig> {
  if (_config) return _config;
  const w = window as unknown as { __TAURI_INTERNALS__?: unknown };
  if (w.__TAURI_INTERNALS__) {
    // Tauri 桌面环境：由 Rust 侧提供动态端口与会话令牌
    const { invoke } = await import("@tauri-apps/api/core");
    const [port, token] = await invoke<[number, string]>("get_backend_config");
    _config = { base: `http://127.0.0.1:${port}`, token };
    return _config;
  }
  // 浏览器开发环境
  _config = {
    base: import.meta.env.VITE_API_BASE ?? "http://127.0.0.1:8765",
    token: import.meta.env.VITE_AUTH_TOKEN ?? "dev-token-local",
  };
  return _config;
}

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

/** 本地回环连接失败说明后端尚未就绪（sidecar 启动/解压中），自动重试。 */
async function fetchWithRetry(
  url: string,
  init?: RequestInit,
  timeoutMs = 60_000
): Promise<Response> {
  const start = Date.now();
  let lastError: unknown;
  for (;;) {
    try {
      return await fetch(url, init);
    } catch (err) {
      lastError = err;
      if (Date.now() - start >= timeoutMs) throw lastError;
      await new Promise((resolve) => setTimeout(resolve, 1200));
    }
  }
}

export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const { base, token } = await resolveConfig();
  const res = await fetchWithRetry(`${base}/api/v1${path}`, {
    ...init,
    headers: {
      "content-type": "application/json",
      "x-auth-token": token,
      ...init?.headers,
    },
  });
  if (!res.ok) {
    throw new ApiError(res.status, `${res.status}: ${await res.text()}`);
  }
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}
