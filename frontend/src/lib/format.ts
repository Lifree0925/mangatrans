/** 章节显示名：优先显示章节标题（去 markdown # 前缀），无标题时回退「第 N 章」。
 *  与项目页目录、导入时的项目展示保持一致，避免出现「第N章 第N集」式拼接。 */
export function chapterLabel(
  chapterNo: number | null | undefined,
  title: string | null | undefined,
): string {
  const t = String(title ?? "").trim().replace(/^#+\s*/, "");
  if (t) return t;
  return `第 ${chapterNo ?? "?"} 章`;
}

/** 角色显示名：去掉「未具名」前缀；「未具名女孩（描述）」显示括号内描述。
 *  例：「未具名女孩（黑森林猎杀吸血鬼的Van Helsing家族女孩）」→「黑森林猎杀吸血鬼的Van Helsing家族女孩」 */
export function characterDisplayName(name: string): string {
  const t = String(name ?? "").trim();
  const m = t.match(/^(.+?)[（(]([^（）()]*)[）)]$/);
  if (m) {
    const head = m[1].trim();
    const desc = m[2].trim();
    if (head.startsWith("未具名") && desc) return desc;
  }
  if (t.startsWith("未具名")) return t.slice(3).trim() || t;
  return t;
}
