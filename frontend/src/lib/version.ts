/** 应用版本与更新检查（静态 update.json，避免 GitHub API 限流 403；一天缓存）。 */

declare const __APP_VERSION__: string;

export const APP_VERSION: string = __APP_VERSION__;

const CACHE_KEY = "mt-update-check";
const CACHE_TTL_MS = 24 * 60 * 60 * 1000;
const MANIFESTS = [
  "https://raw.githubusercontent.com/Lifree0925/mangatrans/main/update.json",
  "https://cdn.jsdelivr.net/gh/Lifree0925/mangatrans@main/update.json",
];

interface UpdateManifest {
  version: string;
  download_url: string;
}

export interface UpdateInfo {
  current: string;
  latest: string | null;
  download_url: string | null;
  has_update: boolean;
  error?: string;
}

async function fetchManifest(force: boolean): Promise<UpdateManifest> {
  if (!force) {
    const cached = localStorage.getItem(CACHE_KEY);
    if (cached) {
      try {
        const { ts, manifest } = JSON.parse(cached);
        if (Date.now() - ts < CACHE_TTL_MS && manifest) return manifest as UpdateManifest;
      } catch {
        /* 忽略坏缓存 */
      }
    }
  }
  let lastError: unknown;
  for (const url of MANIFESTS) {
    try {
      const ctrl = new AbortController();
      const timer = setTimeout(() => ctrl.abort(), 8_000);
      const res = await fetch(url, { signal: ctrl.signal, cache: "no-store" });
      clearTimeout(timer);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = (await res.json()) as UpdateManifest;
      if (!data.version) throw new Error("invalid manifest");
      try {
        localStorage.setItem(CACHE_KEY, JSON.stringify({ ts: Date.now(), manifest: data }));
      } catch {
        /* 存储不可用时忽略 */
      }
      return data;
    } catch (err) {
      lastError = err;
    }
  }
  throw lastError instanceof Error ? lastError : new Error(String(lastError));
}

export async function checkUpdate(force = false): Promise<UpdateInfo> {
  const current = APP_VERSION;
  const info: UpdateInfo = { current, latest: null, download_url: null, has_update: false };
  try {
    const manifest = await fetchManifest(force);
    info.latest = manifest.version.replace(/^v/, "");
    info.download_url = manifest.download_url;
    info.has_update = compareVersions(info.latest, current) > 0;
  } catch (err) {
    info.error = err instanceof Error ? err.message : String(err);
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
