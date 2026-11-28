import { useEffect, useRef, useState } from "react";
import { api } from "./api";
import logoMark from "./assets/sourceloom-mark.png";
import ChatView from "./views/ChatView";
import LibraryView from "./views/LibraryView";
import HealthView from "./views/HealthView";
import ProjectsView from "./views/ProjectsView";
import SearchView from "./views/SearchView";
import Icon, { type IconName } from "./Icon";

type View = "search" | "chat" | "projects" | "library" | "health";

const SIDEBAR_COLLAPSED_KEY = "shiyuan.sidebar.collapsed";

const NAV: { key: View; label: string; hint: string; icon: IconName }[] = [
  { key: "search", label: "检索", hint: "全文 · 证据定位", icon: "search" },
  { key: "library", label: "数据", hint: "授权 · 同步", icon: "library" },
  { key: "projects", label: "项目", hint: "知识空间 · 外部接口", icon: "library" },
  { key: "chat", label: "对话", hint: "知识库问答", icon: "chat" },
  { key: "health", label: "更新", hint: "索引 · 覆盖", icon: "updates" },
];

export default function App() {
  const [view, setView] = useState<View>("search");
  const [online, setOnline] = useState<boolean | null>(null);
  const [version, setVersion] = useState("");
  const [sidebarCollapsed, setSidebarCollapsed] = useState(
    () => window.localStorage.getItem(SIDEBAR_COLLAPSED_KEY) === "1",
  );
  const mainRef = useRef<HTMLElement>(null);

  const changeView = (next: View) => {
    setView(next);
    if (mainRef.current) mainRef.current.scrollTop = 0;
  };

  useEffect(() => {
    window.localStorage.setItem(SIDEBAR_COLLAPSED_KEY, sidebarCollapsed ? "1" : "0");
  }, [sidebarCollapsed]);

  useEffect(() => {
    let alive = true;
    const check = async () => {
      try {
        const health = await api.health();
        if (!alive) return;
        setOnline(true);
        setVersion(health.version);
      } catch {
        if (alive) setOnline(false);
      }
    };
    void check();
    const timer = window.setInterval(check, 10000);
    return () => {
      alive = false;
      window.clearInterval(timer);
    };
  }, []);

  return (
    <div className={`app${sidebarCollapsed ? " sidebar-collapsed" : ""}`}>
      <aside className="sidebar">
        <div className="brand">
          <img className="brand-mark" src={logoMark} alt="拾源 SourceLoom" title="拾源 SourceLoom" />
          <div className="brand-text">
            <div className="brand-name">拾源</div>
            <div className="brand-sub">SourceLoom</div>
          </div>
          <button
            className="sidebar-toggle"
            onClick={() => setSidebarCollapsed((value) => !value)}
            aria-label={sidebarCollapsed ? "展开导航" : "收起导航"}
            title={sidebarCollapsed ? "展开导航" : "收起导航"}
          >
            <Icon name={sidebarCollapsed ? "chevron-right" : "chevron-left"} size={16} />
          </button>
        </div>

        <nav className="nav" aria-label="主导航">
          {NAV.map((item) => (
            <button
              key={item.key}
              className={`nav-item${view === item.key ? " active" : ""}`}
              onClick={() => changeView(item.key)}
              title={item.label}
            >
              <span className="nav-icon" aria-hidden="true"><Icon name={item.icon} size={18} /></span>
              <span className="nav-text">
                <span className="nav-label">{item.label}</span>
                <span className="nav-hint">{item.hint}</span>
              </span>
            </button>
          ))}
        </nav>

        <div className="sidebar-foot">
          <span className={`status-dot ${online ? "ok" : online === false ? "bad" : ""}`} />
          <div>
            <div className="foot-line">
              {online === null ? "连接本地核心…" : online ? "本地核心已就绪" : "本地核心未启动"}
            </div>
            <div className="foot-sub">v{version || "0.4.0"} · 默认离线</div>
          </div>
        </div>
      </aside>

      <main className="main" ref={mainRef}>
        {view === "search" ? (
          <div className="page">
            <SearchView />
          </div>
        ) : view === "chat" ? (
          <ChatView />
        ) : view === "projects" ? (
          <div className="page">
            <ProjectsView />
          </div>
        ) : view === "library" ? (
          <div className="page">
            <LibraryView />
          </div>
        ) : (
          <div className="page">
            <HealthView />
          </div>
        )}
      </main>
    </div>
  );
}
