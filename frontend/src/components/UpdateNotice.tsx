/** 更新提示：有新版时显示下载入口。 */

import { useEffect, useState } from "react";
import { Download } from "lucide-react";
import { APP_VERSION, checkUpdate, type UpdateInfo } from "../lib/version";

export default function UpdateNotice() {
  const [info, setInfo] = useState<UpdateInfo | null>(null);

  useEffect(() => {
    checkUpdate().then(setInfo);
  }, []);

  if (!info || !info.has_update || !info.download_url) return null;

  return (
    <a
      href={info.download_url}
      target="_blank"
      rel="noreferrer"
      className="flex items-center gap-2 border-t border-(--color-border) px-4 py-2.5 text-xs text-(--color-accent) hover:bg-(--color-panel2)"
      title={`当前 v${APP_VERSION}，最新 v${info.latest}`}
    >
      <Download size={13} />
      发现新版本 v{info.latest}，点击下载
    </a>
  );
}
