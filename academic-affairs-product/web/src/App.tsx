import { useCallback, useEffect, useState } from "react";
import Chat from "./Chat";
import Admin from "./Admin";
import Import from "./Import";
import { api, Health } from "./api";
import { useI18n, type Lang, type T } from "./i18n";
import { IconArrowLeft, IconClose, IconGauge, IconMenu, IconMoon, IconPlus, IconSettings, IconSun, IconUpload } from "./icons";

/** Two separate spaces: the reading room (chat + its history) and the
 *  console (intake + status) for whoever maintains the knowledge base. */
type Space = "chat" | "console";
type ConsoleTab = "import" | "status";
export interface Conversation { id: string; title: string; ts: number }

const CONV_KEY = "conversations";

function loadConversations(): Conversation[] {
  try {
    const list = JSON.parse(localStorage.getItem(CONV_KEY) || "[]") as Conversation[];
    // migrate the single-session key used before the conversation list existed
    const legacy = localStorage.getItem("session_id");
    if (legacy && !list.some((c) => c.id === legacy)) list.push({ id: legacy, title: "", ts: 0 });
    localStorage.removeItem("session_id");
    return list;
  } catch {
    return [];
  }
}

function saveConversations(list: Conversation[]) {
  try { localStorage.setItem(CONV_KEY, JSON.stringify(list.slice(0, 40))); } catch { /* storage off */ }
}

function initialTheme(): "light" | "dark" {
  try {
    const saved = localStorage.getItem("theme");
    if (saved === "light" || saved === "dark") return saved;
  } catch { /* storage off */ }
  return window.matchMedia?.("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

function dayGroup(ts: number, t: T): string {
  if (!ts) return t("day.earlier");
  const days = Math.floor((Date.now() - ts) / 86_400_000);
  if (days <= 0) return t("day.today");
  if (days === 1) return t("day.yesterday");
  if (days < 7) return t("day.week");
  return t("day.earlier");
}

export default function App() {
  const { t, lang, setLang } = useI18n();
  const [space, setSpace] = useState<Space>(() => (location.hash === "#console" ? "console" : "chat"));
  const [consoleTab, setConsoleTab] = useState<ConsoleTab>("import");
  const [conversations, setConversations] = useState<Conversation[]>(loadConversations);
  const [activeId, setActiveId] = useState<string>(() => conversations[0]?.id || "");
  const [chatKey, setChatKey] = useState(0);
  const [theme, setTheme] = useState<"light" | "dark">(initialTheme);
  const [health, setHealth] = useState<Health | null | "down">(null);
  const [navOpen, setNavOpen] = useState(false);

  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    try { localStorage.setItem("theme", theme); } catch { /* storage off */ }
  }, [theme]);

  useEffect(() => {
    const poll = () => api.health().then(setHealth).catch(() => setHealth("down"));
    poll();
    const timer = window.setInterval(poll, 30_000);
    return () => window.clearInterval(timer);
  }, []);

  useEffect(() => {
    history.replaceState(null, "", space === "console" ? "#console" : location.pathname + location.search);
  }, [space]);

  const openConversation = useCallback((id: string) => {
    setActiveId(id); setChatKey((k) => k + 1); setSpace("chat"); setNavOpen(false);
  }, []);
  const newConversation = useCallback(() => openConversation(""), [openConversation]);

  // Chat reports the session it created (first question) and every later ask.
  const onAsked = useCallback((id: string, question: string) => {
    setActiveId(id);
    setConversations((list) => {
      const existing = list.find((c) => c.id === id);
      const title = existing?.title || question.slice(0, 48);
      const next = [{ id, title, ts: Date.now() }, ...list.filter((c) => c.id !== id)];
      saveConversations(next);
      return next;
    });
  }, []);

  const removeConversation = (id: string) => {
    setConversations((list) => {
      const next = list.filter((c) => c.id !== id);
      saveConversations(next);
      return next;
    });
    if (id === activeId) newConversation();
  };

  const groups: Array<[string, Conversation[]]> = [];
  for (const c of conversations) {
    const label = dayGroup(c.ts, t);
    const group = groups.find(([l]) => l === label);
    if (group) group[1].push(c); else groups.push([label, [c]]);
  }

  const online = health && health !== "down";
  const healthText = online
    ? (health.runtime?.n_documents ? t("health.docs", { n: health.runtime.n_documents.toLocaleString() }) : t("health.online"))
    : health === "down" ? t("health.offline") : t("health.linking");

  const prefs = (
    <div className="prefs">
      <div className="seg" role="group" aria-label={t("lang.label")}>
        {(["zh", "en"] as Lang[]).map((l) => (
          <button key={l} className={lang === l ? "on" : ""} onClick={() => setLang(l)}>{l === "zh" ? "中" : "EN"}</button>
        ))}
      </div>
      <button className="icon-btn" onClick={() => setTheme(theme === "dark" ? "light" : "dark")} aria-label={t("theme.toggle")} title={t("theme.toggle")}>
        {theme === "dark" ? <IconSun size={16} /> : <IconMoon size={16} />}
      </button>
    </div>
  );

  const brand = (
    <button className="brand" onClick={newConversation}>
      <span className="brand-mark" aria-hidden="true" />
      <span className="brand-text"><b>{t("brand.name")}</b></span>
    </button>
  );

  return (
    <div className={`shell space-${space} ${navOpen ? "nav-open" : ""}`}>
      {/* ---------------- reading room: chat + history ---------------- */}
      <aside className="sidebar" hidden={space !== "chat"}>
        <div className="side-head">
          {brand}
          <button className="icon-btn only-mobile" onClick={() => setNavOpen(false)} aria-label={t("drawer.close")}><IconClose /></button>
        </div>

        <button className="new-chat" onClick={newConversation}>
          <IconPlus size={15} /><span>{t("nav.newChat")}</span><kbd>N</kbd>
        </button>

        <div className="side-history">
          <div className="side-caption">{t("nav.history")}</div>
          {groups.length === 0 && <p className="side-empty">{t("nav.historyEmpty")}</p>}
          {groups.map(([label, items]) => (
            <div key={label} className="side-group">
              <div className="side-group-label">{label}</div>
              {items.map((c) => (
                <div key={c.id} className={`side-item ${c.id === activeId ? "active" : ""}`}>
                  <button className="side-item-main" onClick={() => openConversation(c.id)} title={c.title}>
                    {c.title || (c.ts ? t("nav.untitled") : t("nav.earlier"))}
                  </button>
                  <button className="side-item-del" onClick={() => removeConversation(c.id)} aria-label={t("nav.removeConv")}><IconClose size={13} /></button>
                </div>
              ))}
            </div>
          ))}
        </div>

        <div className="side-foot">
          <button className="console-entry" onClick={() => { setSpace("console"); setNavOpen(false); }}>
            <IconSettings size={16} />
            <span className="console-entry-text"><b>{t("console.entry")}</b><small>{t("console.entrySub")}</small></span>
          </button>
          <div className="side-foot-row">
            <span className={`status-line ${online ? "on" : health === "down" ? "off" : ""}`} title={online ? t("health.online") : ""}>
              <span className="dot" /><span>{healthText}</span>
            </span>
            {prefs}
          </div>
        </div>
      </aside>
      <div className="scrim" onClick={() => setNavOpen(false)} />

      <main className="main" hidden={space !== "chat"}>
        <header className="mobile-bar">
          <button className="icon-btn" onClick={() => setNavOpen(true)} aria-label="menu"><IconMenu /></button>
          <span className="mobile-title">{t("brand.name")}</span>
          <button className="icon-btn" onClick={newConversation} aria-label={t("nav.newChat")}><IconPlus /></button>
        </header>
        {/* Chat stays mounted while the console is open, so an in-flight
            answer and its evidence survive the visit. */}
        <Chat key={chatKey} active={space === "chat"} initialSessionId={activeId} onAsked={onAsked} onNew={newConversation} />
      </main>

      {/* ---------------- console: intake + status ---------------- */}
      {space === "console" && (
        <div className="console">
          <header className="console-bar">
            <div className="console-left">
              <button className="back-btn" onClick={() => setSpace("chat")}><IconArrowLeft size={16} /><span>{t("console.back")}</span></button>
              <span className="console-sep" />
              <span className="console-name">{t("console.title")}</span>
            </div>
            <nav className="console-tabs">
              <button className={consoleTab === "import" ? "on" : ""} onClick={() => setConsoleTab("import")}><IconUpload size={15} />{t("console.import")}</button>
              <button className={consoleTab === "status" ? "on" : ""} onClick={() => setConsoleTab("status")}><IconGauge size={15} />{t("console.status")}</button>
            </nav>
            <div className="console-right">
              <span className={`status-line ${online ? "on" : health === "down" ? "off" : ""}`}><span className="dot" /><span>{healthText}</span></span>
              {prefs}
            </div>
          </header>
          <div className="console-body" key={consoleTab}>
            {consoleTab === "import" ? <Import /> : <Admin />}
          </div>
        </div>
      )}
    </div>
  );
}
