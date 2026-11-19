/** 本地 API 客户端：统一前缀、会话令牌、错误处理。 */

export const TOKEN = import.meta.env.VITE_AUTH_TOKEN ?? "dev-token-local";
export const API_BASE = import.meta.env.VITE_API_BASE ?? "http://127.0.0.1:8765";

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${API_BASE}/api/v1${path}`, {
    ...init,
    headers: {
      "content-type": "application/json",
      "x-auth-token": TOKEN,
      ...init?.headers,
    },
  });
  if (!res.ok) {
    throw new ApiError(res.status, `${res.status}: ${await res.text()}`);
  }
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}
