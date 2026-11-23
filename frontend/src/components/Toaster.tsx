/** 全局消息渲染：Toast 通知与确认对话框。 */

import { AlertCircle, CheckCircle2, Info, X } from "lucide-react";
import { useConfirmStore, useToastStore } from "../lib/feedback";

const tone: Record<string, string> = {
  success: "border-emerald-200 bg-emerald-50 text-emerald-800",
  error: "border-red-200 bg-red-50 text-red-800",
  info: "border-(--color-border) bg-(--color-panel) text-(--color-text)",
};

const icons = {
  success: CheckCircle2,
  error: AlertCircle,
  info: Info,
};

export default function Toaster() {
  const toasts = useToastStore((s) => s.toasts);
  const remove = useToastStore((s) => s.remove);
  const confirm = useConfirmStore();

  return (
    <>
      <div className="pointer-events-none fixed left-1/2 top-5 z-[60] flex w-96 max-w-[90vw] -translate-x-1/2 flex-col items-center gap-2">
        {toasts.map((t) => {
          const Icon = icons[t.type];
          return (
            <div
              key={t.id}
              className={`pointer-events-auto flex items-start gap-2 rounded-xl border px-3.5 py-2.5 text-sm shadow-lg ${tone[t.type]}`}
            >
              <Icon size={16} className="mt-0.5 shrink-0" />
              <span className="flex-1 break-words">{t.message}</span>
              <button
                onClick={() => remove(t.id)}
                className="shrink-0 rounded p-0.5 opacity-60 hover:opacity-100"
              >
                <X size={14} />
              </button>
            </div>
          );
        })}
      </div>

      {confirm.open && (
        <div
          className="fixed inset-0 z-[70] flex items-center justify-center bg-black/30 p-6"
          onClick={() => confirm.close(false)}
        >
          <div
            className="flex min-h-[180px] w-full max-w-sm flex-col rounded-2xl bg-(--color-bg) p-5 shadow-2xl"
            onClick={(e) => e.stopPropagation()}
          >
            <h3 className="text-base font-semibold">{confirm.title}</h3>
            {confirm.message && (
              <p className="mt-2 text-sm leading-6 text-(--color-muted)">{confirm.message}</p>
            )}
            <div className="mt-auto flex justify-end gap-2 pt-5">
              <button
                onClick={() => confirm.close(false)}
                className="rounded-lg border border-(--color-border) bg-(--color-bg) px-4 py-1.5 text-sm hover:bg-(--color-panel2)"
              >
                取消
              </button>
              <button
                onClick={() => confirm.close(true)}
                className={`rounded-lg px-4 py-1.5 text-sm text-white ${
                  confirm.danger
                    ? "bg-red-600 hover:bg-red-700"
                    : "bg-(--color-accent) hover:bg-(--color-accent-hover)"
                }`}
              >
                确定
              </button>
            </div>
          </div>
        </div>
      )}
    </>
  );
}
