import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError } from "./api";
import AdminLock from "./AdminLock";
import { useI18n } from "./i18n";
import { IconAlert, IconCheck, IconDoc, IconUpload, IconClose } from "./icons";

interface MaterialItem {
  job_id: string; file: string; title?: string; kind?: string;
  stage: "queued" | "received" | "placed" | "published" | "failed";
  ts?: string; build_id?: string; detail?: string;
}

// The watcher's stages, in pipeline order; "failed" can happen at any point.
const PIPELINE: Array<MaterialItem["stage"]> = ["queued", "received", "placed", "published"];
const SITES = ["sharepoint", "zjui", "ugrs", "grs", "zju", "news", "libweb"];

// Watcher timestamps are UTC ISO strings; show them in the viewer's local time.
function localTime(ts?: string): string {
  if (!ts) return "";
  const d = new Date(ts);
  if (Number.isNaN(d.getTime())) return ts.slice(5, 16).replace("T", " ");
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

export default function Import() {
  const { t } = useI18n();
  const [file, setFile] = useState<File | null>(null);
  const [kind, setKind] = useState<"pdf" | "page">("pdf");
  const [site, setSite] = useState("sharepoint");
  const [title, setTitle] = useState("");
  const [url, setUrl] = useState("");
  const [published, setPublished] = useState("");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<{ ok: boolean; text: string } | null>(null);
  const [items, setItems] = useState<MaterialItem[]>([]);
  const [drag, setDrag] = useState(false);
  const [offline, setOffline] = useState(false);
  const [currentBuild, setCurrentBuild] = useState("");
  const [locked, setLocked] = useState(false);
  const fileRef = useRef<HTMLInputElement>(null);

  const refresh = useCallback(async () => {
    try {
      const data = await api.materials();
      setItems((data.items as MaterialItem[]) || []);
      setOffline(false);
    } catch (e: any) {
      if (e instanceof ApiError && e.status === 401) { setLocked(true); return; }
      setOffline(true);
    }
    try {
      const health = await fetch("/health/ready");
      if (health.ok) setCurrentBuild((await health.json())?.runtime?.build_id || "");
    } catch { /* health is best-effort here */ }
  }, []);

  useEffect(() => {
    if (locked) return;
    refresh();
    const timer = window.setInterval(refresh, 4000);
    return () => window.clearInterval(timer);
  }, [refresh, locked]);

  const pageMode = kind === "page";
  const missing = !file ? t("imp.needFile") : pageMode && !title.trim() ? t("imp.needTitle") : pageMode && !url.trim() ? t("imp.needUrl") : "";

  function pick(f: File | null) {
    setFile(f); setMessage(null);
    if (f && /\.html?$/i.test(f.name)) setKind("page");
    if (f && !title) setTitle(f.name.replace(/\.[^.]+$/, ""));
  }

  async function submit() {
    if (missing || busy || !file) return;
    setBusy(true); setMessage(null);
    const form = new FormData();
    form.append("file", file);
    form.append("kind", kind);
    form.append("site", site);
    form.append("title", title);
    form.append("url", url);
    form.append("published", published);
    try {
      const response = await api.uploadMaterials(form);
      if (response.status === 401) { setLocked(true); return; }
      const body = await response.json();
      if (!response.ok) throw new Error(body.detail || response.status);
      setMessage({ ok: true, text: t("imp.ok", { id: body.job_id }) });
      setFile(null); setTitle(""); setUrl(""); setPublished("");
      if (fileRef.current) fileRef.current.value = "";
      refresh();
    } catch (error: any) {
      setMessage({ ok: false, text: t("imp.fail", { e: String(error?.message || error).slice(0, 200) }) });
    } finally {
      setBusy(false);
    }
  }

  const count = (pred: (i: MaterialItem) => boolean) => items.filter(pred).length;
  const nPublished = count((i) => i.stage === "published");
  const nFailed = count((i) => i.stage === "failed");
  const nRunning = items.length - nPublished - nFailed;

  return (
    <div className="page">
      <div className="page-inner">
        <header className="page-head">
          <div className="page-kicker">{t("imp.kicker")}</div>
          <h1>{t("imp.title")}</h1>
          <p>{t("imp.lede")}</p>
        </header>

        {locked ? <AdminLock onUnlock={() => setLocked(false)} /> : (
          <>
            <div className="summary">
          <div><span>{t("imp.sumPublished")}</span><b>{nPublished}</b></div>
          <div><span>{t("imp.sumRunning")}</span><b className={nRunning ? "live" : ""}>{nRunning}</b></div>
          <div><span>{t("imp.sumFailed")}</span><b className={nFailed ? "bad" : ""}>{nFailed}</b></div>
        </div>

        <div className="import-grid">
          <section className="card form-card">
            <div className="step-head"><span className="step-num">1</span>{t("imp.step1")}</div>
            <div
              className={`dropzone ${drag ? "drag" : ""} ${file ? "has-file" : ""}`}
              onDragOver={(e) => { e.preventDefault(); setDrag(true); }}
              onDragLeave={() => setDrag(false)}
              onDrop={(e) => { e.preventDefault(); setDrag(false); pick(e.dataTransfer.files?.[0] || null); }}
              onClick={() => !file && fileRef.current?.click()}
              role="button" tabIndex={0}
              onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") fileRef.current?.click(); }}
            >
              <input ref={fileRef} type="file" hidden onChange={(e) => pick(e.target.files?.[0] || null)} />
              {file ? (
                <div className="dz-file">
                  <span className="dz-icon"><IconDoc size={18} /></span>
                  <span className="dz-name">{file.name}<small>{(file.size / 1024).toFixed(0)} KB</small></span>
                  <button className="icon-btn" onClick={(e) => { e.stopPropagation(); pick(null); if (fileRef.current) fileRef.current.value = ""; }} aria-label={t("imp.remove")}><IconClose size={15} /></button>
                </div>
              ) : (
                <>
                  <span className="dz-icon"><IconUpload size={18} /></span>
                  <b>{t("imp.drop")}</b>
                  <small>{t("imp.dropSub")}</small>
                </>
              )}
            </div>

            <div className="step-head"><span className="step-num">2</span>{t("imp.step2")}</div>
            <div className="field">
              <span className="field-label">{t("imp.kind")}</span>
              <div className="seg wide">
                <button className={!pageMode ? "on" : ""} onClick={() => setKind("pdf")}>{t("imp.kindDoc")}</button>
                <button className={pageMode ? "on" : ""} onClick={() => setKind("page")}>{t("imp.kindPage")}</button>
              </div>
            </div>
            <div className="field-row">
              <label className="field">
                <span className="field-label">{t("imp.site")}</span>
                <select value={site} onChange={(e) => setSite(e.target.value)}>
                  {SITES.map((s) => <option key={s} value={s}>{t(`site.${s}` as any)}</option>)}
                </select>
              </label>
              <label className="field">
                <span className="field-label">{t("imp.date")} <i>{t("imp.optional")}</i></span>
                <input type="date" value={published} onChange={(e) => setPublished(e.target.value)} />
              </label>
            </div>
            <label className="field">
              <span className="field-label">{t("imp.titleField")} <i className={pageMode ? "req" : ""}>{pageMode ? t("imp.required") : t("imp.optional")}</i></span>
              <input value={title} placeholder={t("imp.titlePh")} onChange={(e) => setTitle(e.target.value)} />
            </label>
            <label className="field">
              <span className="field-label">{t("imp.url")} <i className={pageMode ? "req" : ""}>{pageMode ? t("imp.required") : t("imp.optional")}</i></span>
              <input value={url} placeholder="https://" onChange={(e) => setUrl(e.target.value)} />
              <span className="field-help">{t("imp.urlHelp")}</span>
            </label>

            <div className="step-head"><span className="step-num">3</span>{t("imp.step3")}</div>
            <button className="primary-btn wide" onClick={submit} disabled={!!missing || busy}>
              {busy ? t("imp.uploading") : missing || t("imp.submit")}
            </button>
            {message && (
              <div className={`form-msg ${message.ok ? "ok" : "bad"}`}>
                {message.ok ? <IconCheck size={15} /> : <IconAlert size={15} />}<span>{message.text}</span>
              </div>
            )}
          </section>

          <section className="card">
            <div className="card-head">
              <h2>{t("imp.pipeline")}</h2>
              <span className="live-tag"><span className="dot" />{t("imp.live")}</span>
            </div>
            {currentBuild && (
              <div className="inline-notice" style={{ marginBottom: 10 }}>
                {t("imp.serving")} <span className="code">{currentBuild}</span>
              </div>
            )}
            {offline && <div className="inline-notice">{t("imp.offline")}</div>}
            {items.length === 0 && !offline && (
              <div className="empty-state">
                <IconDoc size={22} />
                <p>{t("imp.empty")}</p>
                <small>{t("imp.emptySub")}</small>
              </div>
            )}
            <ul className="jobs">
              {items.map((item) => <JobRow key={item.job_id} item={item} currentBuild={currentBuild} />)}
            </ul>
          </section>
            </div>
          </>
        )}
      </div>
    </div>
  );
}

function JobRow({ item, currentBuild }: { item: MaterialItem; currentBuild: string }) {
  const { t } = useI18n();
  const failed = item.stage === "failed";
  const at = PIPELINE.indexOf(item.stage);
  // A published job that switched to a build OTHER than the one now serving is
  // history (e.g. the 2026-10-14 demo jobs) — label it so it can't read as a
  // switch that just happened.
  const historical = !!item.build_id && item.stage === "published" && item.build_id !== currentBuild;
  return (
    <li className={`job ${failed ? "failed" : item.stage === "published" ? "done" : "running"}`}>
      <div className="job-top">
        <span className="job-title">{item.title || item.file || <span className="muted">{t("imp.job")} <span className="code">{item.job_id}</span></span>}</span>
        <span className="job-time">{localTime(item.ts)}</span>
      </div>
      <div className="job-track">
        {PIPELINE.map((p, i) => (
          <span key={p} className={`track-step ${failed ? "" : i < at || (i === at && p === "published") ? "past" : i === at ? "now" : ""}`}>
            <i />{t(`track.${p}` as any)}
          </span>
        ))}
      </div>
      {item.build_id && (
        <div className={`job-note ${historical ? "muted" : "ok"}`}>
          {historical ? t("imp.historyJob", { ts: localTime(item.ts) }) : t("imp.switched")} <span className="code">{item.build_id.slice(0, 24)}</span>
        </div>
      )}
      {failed && <div className="job-note bad">{(item.detail || t("imp.unknown")).slice(0, 200)} · {t("imp.safe")}</div>}
    </li>
  );
}
