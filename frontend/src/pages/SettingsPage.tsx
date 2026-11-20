/** 设置：模型参数与软件更新（ChatGPT 风格分区布局）。 */

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Plus, Trash2 } from "lucide-react";
import { api } from "../lib/api";
import { APP_VERSION, checkUpdate, type UpdateInfo } from "../lib/version";
import type { ProviderInfo } from "../lib/types";

export default function SettingsPage() {
  const qc = useQueryClient();
  const [baseUrl, setBaseUrl] = useState("");
  const [model, setModel] = useState("");
  const [display, setDisplay] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [keyFor, setKeyFor] = useState<string | null>(null);
  const [updateInfo, setUpdateInfo] = useState<UpdateInfo | null>(null);

  const providers = useQuery({
    queryKey: ["providers"],
    queryFn: () => api<ProviderInfo[]>("/settings/providers"),
  });

  const create = useMutation({
    mutationFn: () =>
      api(`/settings/providers`, {
        method: "POST",
        body: JSON.stringify({
          display_name: display || model,
          base_url: baseUrl,
          model_name: model,
          is_default: (providers.data?.length ?? 0) === 0,
        }),
      }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["providers"] }),
  });

  const saveKey = useMutation({
    mutationFn: (pid: string) =>
      api(`/settings/providers/${pid}/credential`, {
        method: "PUT",
        body: JSON.stringify({ api_key: apiKey }),
      }),
    onSuccess: () => {
      setApiKey("");
      setKeyFor(null);
      qc.invalidateQueries({ queryKey: ["providers"] });
    },
  });

  const remove = useMutation({
    mutationFn: (pid: string) => api<void>(`/settings/providers/${pid}`, { method: "DELETE" }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["providers"] }),
  });

  return (
    <div className="flex h-full flex-col">
      <div className="border-b border-(--color-border) px-6 py-4">
        <h1 className="text-lg font-semibold">设置</h1>
      </div>

      <div className="flex-1 overflow-auto p-6">
        <div className="mx-auto max-w-3xl space-y-10">
          <section>
            <h2 className="text-sm font-semibold">模型服务</h2>
            <p className="mt-1 text-xs text-(--color-muted)">
              兼容 OpenAI 接口。API Key 仅保存在本机系统凭据库，不写入数据库或日志。
            </p>

            <div className="mt-4 flex flex-wrap items-end gap-2 rounded-xl bg-(--color-panel) p-4">
              <label className="text-xs text-(--color-muted)">
                名称
                <input
                  value={display}
                  onChange={(e) => setDisplay(e.target.value)}
                  placeholder="可选"
                  className="mt-1 block w-32 rounded-lg border border-(--color-border) bg-(--color-bg) px-2 py-1.5 text-sm outline-none focus:border-(--color-text)"
                />
              </label>
              <label className="text-xs text-(--color-muted)">
                Base URL
                <input
                  value={baseUrl}
                  onChange={(e) => setBaseUrl(e.target.value)}
                  placeholder="https://api.openai.com/v1"
                  className="mt-1 block w-60 rounded-lg border border-(--color-border) bg-(--color-bg) px-2 py-1.5 text-sm outline-none focus:border-(--color-text)"
                />
              </label>
              <label className="text-xs text-(--color-muted)">
                模型
                <input
                  value={model}
                  onChange={(e) => setModel(e.target.value)}
                  placeholder="gpt-4o-mini"
                  className="mt-1 block w-40 rounded-lg border border-(--color-border) bg-(--color-bg) px-2 py-1.5 text-sm outline-none focus:border-(--color-text)"
                />
              </label>
              <button
                onClick={() => create.mutate()}
                className="flex items-center gap-1.5 rounded-lg bg-(--color-accent) px-3 py-1.5 text-sm text-white hover:bg-(--color-accent-hover)"
              >
                <Plus size={15} /> 添加
              </button>
            </div>

            <div className="mt-3 space-y-2">
              {providers.data?.map((p) => (
                <div key={p.id} className="rounded-xl bg-(--color-panel) px-4 py-3">
                  <div className="flex items-center gap-2">
                    <span className="text-sm font-medium">{p.display_name || p.model_name}</span>
                    <span className="font-mono text-xs text-(--color-muted)">{p.model_name}</span>
                    {p.is_default && (
                      <span className="rounded-md bg-(--color-panel2) px-1.5 py-0.5 text-xs">
                        默认
                      </span>
                    )}
                    {p.has_credential && (
                      <span className="rounded-md bg-(--color-panel2) px-1.5 py-0.5 text-xs text-emerald-600">
                        已配置 Key
                      </span>
                    )}
                    <button
                      onClick={() => remove.mutate(p.id)}
                      className="ml-auto rounded-lg p-1 text-(--color-muted) hover:bg-(--color-panel2) hover:text-red-600"
                    >
                      <Trash2 size={14} />
                    </button>
                  </div>
                  <div className="mt-2 flex items-center gap-2">
                    <input
                      type="password"
                      value={keyFor === p.id ? apiKey : ""}
                      onChange={(e) => {
                        setKeyFor(p.id);
                        setApiKey(e.target.value);
                      }}
                      placeholder={p.has_credential ? "输入新 Key 覆盖" : "输入 API Key"}
                      className="w-64 rounded-lg border border-(--color-border) bg-(--color-bg) px-2 py-1 font-mono text-xs outline-none focus:border-(--color-text)"
                    />
                    <button
                      onClick={() => saveKey.mutate(p.id)}
                      className="rounded-lg border border-(--color-border) bg-(--color-bg) px-2 py-1 text-xs hover:bg-(--color-panel2)"
                    >
                      保存 Key
                    </button>
                  </div>
                </div>
              ))}
              {providers.data?.length === 0 && (
                <p className="text-xs text-(--color-muted)">
                  尚未配置模型服务。一键直出与真实翻译需要至少一个模型服务商。
                </p>
              )}
            </div>
          </section>

          <section>
            <h2 className="text-sm font-semibold">软件更新</h2>
            <div className="mt-3 flex items-center gap-3 rounded-xl bg-(--color-panel) px-4 py-3 text-sm">
              <span className="text-(--color-muted)">当前版本 v{APP_VERSION}</span>
              <button
                onClick={async () => setUpdateInfo(await checkUpdate(true))}
                className="rounded-lg border border-(--color-border) bg-(--color-bg) px-3 py-1 text-xs hover:bg-(--color-panel2)"
              >
                检查更新
              </button>
              {updateInfo?.has_update && updateInfo.download_url && (
                <a href={updateInfo.download_url} target="_blank" rel="noreferrer" className="text-xs underline">
                  发现新版本 v{updateInfo.latest}，去下载 →
                </a>
              )}
              {updateInfo && !updateInfo.has_update && (
                <span className="text-xs text-emerald-600">
                  {updateInfo.error ? "暂时无法检查更新（网络受限）" : "已是最新版本"}
                </span>
              )}
            </div>
          </section>
        </div>
      </div>
    </div>
  );
}
