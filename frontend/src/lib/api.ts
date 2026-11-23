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
  // 浏览器环境：默认同源相对路径（经 Vite 代理），可选 VITE_API_BASE 覆盖
  _config = {
    base: import.meta.env.VITE_API_BASE ?? "",
    token: import.meta.env.VITE_AUTH_TOKEN ?? "dev-token-local",
  };
  return _config;
}

/** HTTP 状态码 → 全中文提示（面向非技术用户）。 */
const STATUS_CN: Record<number, string> = {
  400: "请求参数有误",
  401: "未授权：会话令牌无效",
  403: "没有权限执行该操作",
  404: "请求的内容不存在",
  409: "操作冲突（可能存在进行中的任务）",
  422: "提交的数据格式不正确",
  429: "请求过于频繁，请稍后重试",
  500: "服务器内部错误",
  502: "本地服务暂时不可用",
  503: "本地服务正在启动，请稍后重试",
};

export class ApiError extends Error {
  status: number;
  /** 原始报错文本（保留给技术人员排查） */
  raw: string;
  constructor(status: number, detail: string) {
    // 报错策略：后端返回中文提示就直接显示；否则显示全中文状态提示 + 原文（供技术人员）
    const hasCn = /[\u4e00-\u9fff]/.test(detail || "");
    const friendly = hasCn
      ? detail
      : (STATUS_CN[status] ?? "请求失败") + (detail ? `（原始错误：${detail}）` : "");
    super(friendly);
    this.status = status;
    this.raw = detail || "";
  }
}

/** 把底层网络错误翻译成用户可理解的中文提示。 */
function friendlyNetworkError(err: unknown): string {
  const raw = err instanceof Error ? err.message : String(err);
  if (raw.includes("Failed to fetch")) {
    return "无法连接本地服务：请稍候重试（服务启动中）";
  }
  if (raw.includes("NetworkError")) {
    return "网络错误：请检查网络连接后重试";
  }
  if (raw.includes("timed out") || raw.includes("abort")) {
    return "请求超时：本地服务未响应";
  }
  return raw;
}

/** 本地回环连接失败说明后端尚未就绪（sidecar 启动/解压中），自动重试。 */
async function fetchWithRetry(
  url: string,
  init?: RequestInit,
  timeoutMs = 30_000
): Promise<Response> {
  const start = Date.now();
  let lastError: unknown;
  for (;;) {
    try {
      return await fetch(url, init);
    } catch (err) {
      lastError = err;
      if (Date.now() - start >= timeoutMs) {
        throw new ApiError(0, friendlyNetworkError(lastError));
      }
      await new Promise((resolve) => setTimeout(resolve, 1200));
    }
  }
}

export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const { base, token } = await resolveConfig();
  // FormData 必须由浏览器自动生成 multipart boundary，禁止强制 JSON content-type
  const isFormData = typeof FormData !== "undefined" && init?.body instanceof FormData;
  const headers: Record<string, string> = { "x-auth-token": token };
  if (!isFormData) headers["content-type"] = "application/json";
  const res = await fetchWithRetry(`${base}/api/v1${path}`, {
    ...init,
    headers: { ...headers, ...init?.headers },
  });
  if (!res.ok) {
    const body = await res.text();
    let detail = body;
    try {
      const parsed = JSON.parse(body);
      if (typeof parsed.detail === "string") detail = parsed.detail;
    } catch {
      /* 非 JSON 错误体，保留原文 */
    }
    throw new ApiError(res.status, detail);
  }
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}
