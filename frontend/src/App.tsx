/** 应用外壳：侧边栏布局（Claude Code 风格暗色）。 */

import { NavLink, Route, Routes } from "react-router-dom";
import { Clapperboard, FolderOpen, ListChecks, Settings } from "lucide-react";
import UpdateNotice from "./components/UpdateNotice";
import { APP_VERSION } from "./lib/version";
import ProjectsPage from "./pages/ProjectsPage";
import ProjectPage from "./pages/ProjectPage";
import TasksPage from "./pages/TasksPage";
import SettingsPage from "./pages/SettingsPage";

const nav = [
  { to: "/", label: "项目", icon: FolderOpen, end: true },
  { to: "/tasks", label: "任务中心", icon: ListChecks },
  { to: "/settings", label: "设置", icon: Settings },
];

export default function App() {
  return (
    <div className="flex h-full">
      <aside className="flex w-52 shrink-0 flex-col border-r border-(--color-border) bg-(--color-panel)">
        <div className="flex items-center gap-2 border-b border-(--color-border) px-4 py-4">
          <Clapperboard size={18} className="text-(--color-accent)" />
          <span className="font-semibold">MangaTrans</span>
        </div>
        <nav className="flex-1 p-2">
          {nav.map(({ to, label, icon: Icon, end }) => (
            <NavLink
              key={to}
              to={to}
              end={end}
              className={({ isActive }) =>
                `flex items-center gap-2 rounded px-3 py-2 text-sm ${
                  isActive
                    ? "bg-(--color-panel2) text-(--color-accent)"
                    : "text-(--color-muted) hover:bg-(--color-panel2) hover:text-(--color-text)"
                }`
              }
            >
              <Icon size={16} /> {label}
            </NavLink>
          ))}
        </nav>
        <UpdateNotice />
        <div className="border-t border-(--color-border) px-4 py-3 font-mono text-xs text-(--color-muted)">
          v{APP_VERSION} · 本地运行
        </div>
      </aside>
      <main className="flex-1 overflow-hidden">
        <Routes>
          <Route path="/" element={<ProjectsPage />} />
          <Route path="/projects/:id" element={<ProjectPage />} />
          <Route path="/tasks" element={<TasksPage />} />
          <Route path="/settings" element={<SettingsPage />} />
        </Routes>
      </main>
    </div>
  );
}
