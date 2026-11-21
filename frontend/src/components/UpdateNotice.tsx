/** 更新提示：有新版时显示下载入口。 */

import { useEffect, useState } from "react";
import { Download } from "lucide-react";
import { APP_VERSION, checkUpdate, type UpdateInfo } from "../lib/version";
import { openExternal } from "../lib/open";

export default function UpdateNotice() {
  const [info, setInfo] = useState<UpdateInfo | null>(null);

  useEffect(() => {
    checkUpdate().then(setInfo);
  }, []);

  if (!info || !info.has_update || (!info.download_url && !info.installer_url)) return null;

  return (
    <button
      onClick={() => openExternal(info.installer_url || info.download_url || "")}
      className="flex w-full items-center gap-2 border-t border-(--color-border) px-4 py-2.5 text-left text-xs text-(--color-text) hover:bg-(--color-panel2)"
      title={`当前 v${APP_VERSION}，最新 v${info.latest}，点击下载安装包`}
    >
      <Download size={13} className="shrink-0 text-(--color-accent)" />
      <span>
        发现新版本 v{info.latest}
        <span className="ml-1.5 rounded bg-(--color-accent) px-1.5 py-0.5 font-medium text-white">
          点击下载
        </span>
      </span>
    </button>
  );
}
