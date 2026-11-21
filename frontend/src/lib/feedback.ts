/** 应用内消息系统：Toast 通知 + 应用内确认对话框（替代浏览器 alert/confirm）。 */

import { create } from "zustand";

export type ToastType = "success" | "error" | "info";

export interface ToastItem {
  id: number;
  type: ToastType;
  message: string;
}

interface ToastState {
  toasts: ToastItem[];
  push: (type: ToastType, message: string) => void;
  remove: (id: number) => void;
}

let nextId = 1;

export const useToastStore = create<ToastState>((set, get) => ({
  toasts: [],
  push: (type, message) => {
    // 相同类型与消息的 toast 仍显示时不重复添加（多入口触发同一错误只显示一条）
    if (get().toasts.some((t) => t.type === type && t.message === message)) return;
    const id = nextId++;
    set({ toasts: [...get().toasts, { id, type, message }] });
    setTimeout(() => get().remove(id), 5000);
  },
  remove: (id) => set({ toasts: get().toasts.filter((t) => t.id !== id) }),
}));

export const toast = {
  success: (message: string) => useToastStore.getState().push("success", message),
  error: (message: string) => useToastStore.getState().push("error", message),
  info: (message: string) => useToastStore.getState().push("info", message),
};

interface ConfirmState {
  open: boolean;
  title: string;
  message: string;
  danger: boolean;
  resolve: ((value: boolean) => void) | null;
  show: (title: string, message?: string, danger?: boolean) => Promise<boolean>;
  close: (value: boolean) => void;
}

export const useConfirmStore = create<ConfirmState>((set, get) => ({
  open: false,
  title: "",
  message: "",
  danger: false,
  resolve: null,
  show: (title, message = "", danger = false) =>
    new Promise<boolean>((resolve) => {
      set({ open: true, title, message, danger, resolve });
    }),
  close: (value) => {
    get().resolve?.(value);
    set({ open: false, resolve: null });
  },
}));

/** 应用内确认框（替代 window.confirm），返回 Promise<boolean>。 */
export function confirmAction(
  title: string,
  message?: string,
  danger = false
): Promise<boolean> {
  return useConfirmStore.getState().show(title, message, danger);
}
