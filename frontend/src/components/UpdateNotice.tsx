/** 更新提示：发现新版后直接在应用内下载、安装并重启。 */

import { Download, Loader2 } from "lucide-react";
import { useAppUpdate } from "../lib/updater";

export default function UpdateNotice() {
  const { state, install } = useAppUpdate(true);

  if (!["available", "downloading", "installing"].includes(state.phase)) return null;
  const busy = state.phase === "downloading" || state.phase === "installing";
  const progress = state.phase === "downloading" && state.progress != null
    ? ` ${state.progress}%`
    : "";
  const label = state.phase === "installing"
    ? "正在安装更新…"
    : state.phase === "downloading"
      ? `正在下载更新${progress}`
      : `发现新版本 v${state.version ?? ""}`;

  return (
    <button
      onClick={() => void install()}
      disabled={busy}
      aria-busy={busy}
      className="flex w-full items-center gap-2 border-t border-(--color-border) px-4 py-2.5 text-left text-xs text-(--color-text) hover:bg-(--color-panel2)"
      title={state.phase === "available" ? "下载并安装更新" : label}
    >
      {busy ? (
        <Loader2 size={13} className="shrink-0 animate-spin text-(--color-accent)" />
      ) : (
        <Download size={13} className="shrink-0 text-(--color-accent)" />
      )}
      <span>
        {label}
        {state.phase === "available" && (
          <span className="ml-1.5 rounded bg-(--color-accent) px-1.5 py-0.5 font-medium text-white">
            立即更新
          </span>
        )}
      </span>
    </button>
  );
}
