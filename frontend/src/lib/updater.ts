/** 应用更新：桌面端走 Tauri 官方 updater，浏览器开发环境回退到 update.json。 */

import { useCallback, useEffect, useRef, useState } from "react";
import type { Update } from "@tauri-apps/plugin-updater";
import { checkUpdate as checkManifestUpdate } from "./version";
import { openExternal } from "./open";
import { toast } from "./feedback";

export type UpdatePhase =
  | "idle"
  | "checking"
  | "available"
  | "latest"
  | "downloading"
  | "installing"
  | "error";

export interface AppUpdateState {
  phase: UpdatePhase;
  version?: string;
  progress?: number;
  error?: string;
  failedAction?: "check" | "install";
  manualUrl?: string;
}

function isTauri(): boolean {
  return !!(window as unknown as { __TAURI_INTERNALS__?: unknown }).__TAURI_INTERNALS__;
}

function errorText(err: unknown): string {
  return err instanceof Error ? err.message : String(err);
}

export function useAppUpdate(autoCheck = false) {
  const updateRef = useRef<Update | null>(null);
  const mountedRef = useRef(true);
  const [state, setState] = useState<AppUpdateState>({ phase: "idle" });

  const emit = useCallback((next: AppUpdateState): AppUpdateState => {
    if (mountedRef.current) setState(next);
    return next;
  }, []);

  const check = useCallback(async (): Promise<AppUpdateState> => {
    emit({ phase: "checking" });

    if (isTauri()) {
      try {
        if (updateRef.current) {
          await updateRef.current.close();
          updateRef.current = null;
        }
        const { check: checkTauriUpdate } = await import("@tauri-apps/plugin-updater");
        const update = await checkTauriUpdate({ timeout: 15_000 });
        if (!update) return emit({ phase: "latest" });
        updateRef.current = update;
        return emit({ phase: "available", version: update.version });
      } catch (err) {
        return emit({ phase: "error", error: errorText(err), failedAction: "check" });
      }
    }

    try {
      const info = await checkManifestUpdate(true);
      if (info.has_update) {
        return emit({
          phase: "available",
          version: info.latest ?? undefined,
          manualUrl: info.installer_url ?? info.download_url ?? undefined,
        });
      }
      if (info.error) return emit({ phase: "error", error: info.error, failedAction: "check" });
      return emit({ phase: "latest" });
    } catch (err) {
      return emit({ phase: "error", error: errorText(err), failedAction: "check" });
    }
  }, [emit]);

  const install = useCallback(async (): Promise<void> => {
    const update = updateRef.current;
    if (!update) {
      if (state.manualUrl) openExternal(state.manualUrl);
      return;
    }

    emit({ phase: "downloading", version: update.version, progress: 0 });
    let total = 0;
    let downloaded = 0;
    let lastProgress = -1;
    try {
      await update.downloadAndInstall((event) => {
        if (event.event === "Started") {
          total = event.data.contentLength ?? 0;
        } else if (event.event === "Progress") {
          downloaded += event.data.chunkLength;
          if (total > 0) {
            const progress = Math.min(99, Math.round((downloaded / total) * 100));
            if (progress !== lastProgress) {
              lastProgress = progress;
              emit({ phase: "downloading", version: update.version, progress });
            }
          }
        } else if (event.event === "Finished") {
          emit({ phase: "installing", version: update.version });
        }
      });

      // Windows 安装器会接管并重启应用；macOS/Linux 需要主动重启。
      try {
        const { relaunch } = await import("@tauri-apps/plugin-process");
        await relaunch();
      } catch {
        /* Windows 更新器退出应用后不会回到这里 */
      }
    } catch (err) {
      const message = errorText(err);
      toast.error(`更新失败：${message}`);
      emit({ phase: "error", error: message, failedAction: "install" });
    }
  }, [emit, state.manualUrl]);

  useEffect(() => {
    if (autoCheck) void check();
  }, [autoCheck, check]);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      const update = updateRef.current;
      updateRef.current = null;
      if (update) void update.close().catch(() => undefined);
    };
  }, []);

  return { state, check, install };
}
