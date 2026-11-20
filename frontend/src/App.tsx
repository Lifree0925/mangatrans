/** 应用外壳：可收起的侧边栏（ChatGPT 风格浅色）。 */

import { useEffect, useState } from "react";
import { NavLink, Route, Routes } from "react-router-dom";
import {
  Clapperboard,
  FolderOpen,
  ListChecks,
  PanelLeftClose,
  PanelLeftOpen,
  Settings,
} from "lucide-react";
import TaskNotifier from "./components/TaskNotifier";
import Toaster from "./components/Toaster";
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

const COLLAPSE_KEY = "mt-sidebar-collapsed";

export default function App() {
  const [collapsed, setCollapsed] = useState(
    () => localStorage.getItem(COLLAPSE_KEY) === "1"
  );

  useEffect(() => {
    localStorage.setItem(COLLAPSE_KEY, collapsed ? "1" : "0");
  }, [collapsed]);

  return (
    <div className="flex h-full">
      <aside
        className={`flex ${collapsed ? "w-14" : "w-60"} shrink-0 flex-col border-r border-(--color-border) bg-(--color-panel) transition-[width] duration-200`}
      >
        <div
          className={
            collapsed
              ? "flex flex-col items-center gap-2 py-4"
              : "flex items-center justify-between px-5 py-4"
          }
        >
          {!collapsed && (
            <div className="flex items-center gap-2">
              <Clapperboard size={20} className="text-(--color-text)" />
              <span className="text-[15px] font-semibold tracking-tight">MangaTrans</span>
            </div>
          )}
          <button
            onClick={() => setCollapsed((v) => !v)}
            title={collapsed ? "展开侧边栏" : "收起侧边栏"}
            className="rounded-lg p-1.5 text-(--color-muted) hover:bg-(--color-panel2) hover:text-(--color-text)"
          >
            {collapsed ? <PanelLeftOpen size={18} /> : <PanelLeftClose size={18} />}
          </button>
        </div>
        <nav className="flex-1 p-2">
          {nav.map(({ to, label, icon: Icon, end }) => (
            <NavLink
              key={to}
              to={to}
              end={end}
              title={collapsed ? label : undefined}
              className={({ isActive }) =>
                `mb-1 flex items-center ${collapsed ? "justify-center" : "gap-2"} rounded-lg px-3 py-2 text-sm ${
                  isActive
                    ? "bg-(--color-panel2) font-medium text-(--color-text)"
                    : "text-(--color-muted) hover:bg-(--color-panel2) hover:text-(--color-text)"
                }`
              }
            >
              <Icon size={18} /> {!collapsed && label}
            </NavLink>
          ))}
        </nav>
        {!collapsed && <UpdateNotice />}
        {!collapsed && (
          <div className="px-5 py-4 text-xs text-(--color-muted)">v{APP_VERSION}</div>
        )}
      </aside>
      <main className="flex-1 overflow-hidden">
        <Routes>
          <Route path="/" element={<ProjectsPage />} />
          <Route path="/projects/:id" element={<ProjectPage />} />
          <Route path="/tasks" element={<TasksPage />} />
          <Route path="/settings" element={<SettingsPage />} />
        </Routes>
      </main>
      <TaskNotifier />
      <Toaster />
    </div>
  );
}
