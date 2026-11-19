/** 应用版本与更新检查（检查 GitHub Releases，静默失败，一天缓存）。 */

declare const __APP_VERSION__: string;

export const APP_VERSION: string = __APP_VERSION__;

const CACHE_KEY = "mt-update-check";
const CACHE_TTL_MS = 24 * 60 * 60 * 1000;
const RELEASES_API = "https://api.github.com/repos/Lifree0925/mangatrans/releases/latest";

export interface UpdateInfo {
  current: string;
  latest: string | null;
  download_url: string | null;
  has_update: boolean;
  error?: string;
}

export async function checkUpdate(force = false): Promise<UpdateInfo> {
  const current = APP_VERSION;
  if (!force) {
    const cached = localStorage.getItem(CACHE_KEY);
    if (cached) {
      try {
        const { ts, info } = JSON.parse(cached);
        if (Date.now() - ts < CACHE_TTL_MS) return info as UpdateInfo;
      } catch {
        /* 忽略坏缓存 */
      }
    }
  }

  const info: UpdateInfo = { current, latest: null, download_url: null, has_update: false };
  try {
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), 10_000);
    const res = await fetch(RELEASES_API, { signal: ctrl.signal });
    clearTimeout(timer);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = (await res.json()) as { tag_name: string; html_url: string };
    info.latest = data.tag_name.replace(/^v/, "");
    info.download_url = data.html_url;
    info.has_update = compareVersions(info.latest, current) > 0;
  } catch (err) {
    info.error = err instanceof Error ? err.message : String(err);
  }
  try {
    localStorage.setItem(CACHE_KEY, JSON.stringify({ ts: Date.now(), info }));
  } catch {
    /* 存储不可用时忽略 */
  }
  return info;
}

/** 简单语义化版本比较：1.2.10 > 1.2.2。 */
export function compareVersions(a: string, b: string): number {
  const pa = a.split(".").map((n) => parseInt(n, 10) || 0);
  const pb = b.split(".").map((n) => parseInt(n, 10) || 0);
  for (let i = 0; i < Math.max(pa.length, pb.length); i++) {
    const x = pa[i] ?? 0;
    const y = pb[i] ?? 0;
    if (x !== y) return x - y;
  }
  return 0;
}
