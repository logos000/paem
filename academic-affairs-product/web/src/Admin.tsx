import { Fragment, useCallback, useEffect, useState } from "react";
import { api, ApiError } from "./api";
import AdminLock from "./AdminLock";
import { useI18n } from "./i18n";
import { IconRefresh } from "./icons";

const DECISIONS = ["sufficient", "conflict", "no_material", "wrong_term", "wrong_audience", "stale_or_uncertain"];

export default function Admin() {
  const { t } = useI18n();
  const [overview, setOverview] = useState<any>(null);
  const [rejected, setRejected] = useState<any>(null);
  const [feedback, setFeedback] = useState<any>(null);
  const [openFeedback, setOpenFeedback] = useState<string | null>(null);
  const [message, setMessage] = useState<{ ok: boolean; text: string } | null>(null);
  const [buildDir, setBuildDir] = useState("");
  const [pending, setPending] = useState("");
  const [locked, setLocked] = useState(false);

  const refresh = useCallback(async () => {
    try {
      setOverview(await api.overview());
      setRejected(await api.rejected(20));
      setFeedback(await api.feedbackReview(30));
    } catch (e: any) {
      if (e instanceof ApiError && e.status === 401) { setLocked(true); return; }
      setMessage({ ok: false, text: String(e).slice(0, 160) });
    }
  }, []);

  useEffect(() => {
    if (locked) return;
    refresh();
    const timer = window.setInterval(refresh, 5000);
    return () => window.clearInterval(timer);
  }, [refresh, locked]);

  async function act(label: string, fn: () => Promise<any>, confirmText?: string) {
    if (confirmText && !window.confirm(confirmText)) return;
    setPending(label);
    try {
      const result = await fn();
      setMessage({ ok: true, text: `${label}: ${JSON.stringify(result).slice(0, 220)}` });
      refresh();
    } catch (e: any) {
      setMessage({ ok: false, text: t("st.failedOp", { l: label, e: String(e).slice(0, 200) }) });
    } finally {
      setPending("");
    }
  }

  const product = overview?.product;
  const runtime = overview?.runtime;
  const counters: Record<string, number> = product?.counters || {};
  const lat = product?.latency_ms?.total || {};
  const breakerOpen = !!product?.llm_breaker?.open;
  const decisions = Object.entries(counters).filter(([k]) => k.startsWith("decision_")).sort((a, b) => b[1] - a[1]);
  const generations = Object.entries(counters).filter(([k]) => k.startsWith("generation_")).sort((a, b) => b[1] - a[1]);
  const fb = { up: counters.feedback_helpful || 0, down: counters.feedback_unhelpful || 0 };
  const decisionLabel = (k: string) => (DECISIONS.includes(k) ? t(`status.${k}` as any) : k);

  return (
    <div className="page">
      <div className="page-inner">
        <header className="page-head row">
          <div>
            <div className="page-kicker">{t("st.kicker")}</div>
            <h1>{t("st.title")}</h1>
            <p>{t("st.lede")}</p>
          </div>
          <button className="ghost-btn" onClick={refresh}><IconRefresh size={14} />{t("st.refresh")}</button>
        </header>

        {message && (
          <div className={`form-msg ${message.ok ? "ok" : "bad"} code`} onClick={() => setMessage(null)}>{message.text}</div>
        )}

        {locked ? <AdminLock onUnlock={() => { setLocked(false); setMessage(null); }} /> : (
          <>
            <div className="stats">
          <Stat label={t("st.requests")} value={fmt(product?.requests)} sub={t("st.errors", { n: fmt(product?.errors) })} />
          <Stat label={t("st.latency")} value={ms(lat.p50)} sub={t("st.latencySub", { a: ms(lat.p95), b: ms(lat.p99) })} />
          <Stat label={t("st.llm")} value={breakerOpen ? t("st.llmOpen") : t("st.llmOk")} tone={breakerOpen ? "warn" : "ok"}
            sub={breakerOpen ? t("st.llmOpenSub") : t("st.llmOkSub", { n: product?.llm_breaker?.failures ?? 0 })} />
          <Stat label={t("st.feedback")} value={`${fb.up} / ${fb.down}`} sub={t("st.feedbackSub")} />
        </div>

        <div className="admin-grid">
          <section className="card">
            <div className="card-head"><h2>{t("st.decisions")}</h2></div>
            <Dist rows={decisions.map(([k, v]) => [decisionLabel(k.replace("decision_", "")), v])} empty={t("st.noData")} />
            <div className="card-head sub"><h2>{t("st.generation")}</h2></div>
            <Dist rows={generations.map(([k, v]) => [k.replace("generation_", ""), v])} empty={t("st.noData")} />
          </section>

          <section className="card">
            <div className="card-head">
              <h2>{t("st.builds")}</h2>
              <span className="muted small">{t("st.arm", { a: product?.current_arm || "—" })}</span>
            </div>
            <div className="builds">
              {(runtime?.builds || []).map((b: any) => {
                const isDefault = b.key === runtime?.default;
                const state = b.loading ? t("st.loading") : b.error ? t("st.error") : isDefault ? t("st.serving") : t("st.ready");
                return (
                  <div key={b.key} className={`build ${isDefault ? "current" : ""}`}>
                    <div className="build-main">
                      <span className="build-key">{b.key}<span className={`pill ${isDefault ? "ok" : b.error ? "bad" : ""}`}>{state}</span></span>
                      <span className="build-id code" title={b.build_id}>{b.build_id}</span>
                      {b.error && <span className="error-text small">{b.error.slice(0, 80)}</span>}
                    </div>
                    <div className="build-actions">
                      <button className="ghost-btn sm" disabled={b.loading || !!b.error || !!pending}
                        onClick={() => act(`smoke ${b.key}`, () => api.adminPost(`/admin/builds/${b.key}/smoke`, {}))}>{t("st.smoke")}</button>
                      <button className="ghost-btn sm" disabled={b.loading || !!b.error || isDefault || !!pending}
                        onClick={() => act(`switch ${b.key}`, () => api.adminPost("/admin/switch", { key: b.key }), t("st.confirmSwitch", { k: b.key }))}>{t("st.makeCurrent")}</button>
                    </div>
                  </div>
                );
              })}
              {!(runtime?.builds || []).length && <p className="muted small">{t("st.noBuilds")}</p>}
            </div>
            <div className="load-row">
              <input value={buildDir} placeholder={t("st.loadPh")} onChange={(e) => setBuildDir(e.target.value)} />
              <button className="ghost-btn" disabled={!buildDir.trim() || !!pending} onClick={() => act("load", () => api.adminPost("/admin/builds", { build_dir: buildDir }))}>{t("st.load")}</button>
              <button className="ghost-btn danger" disabled={!!pending} onClick={() => act("rollback", () => api.adminPost("/admin/rollback"), t("st.confirmRollback"))}>{t("st.rollback")}</button>
            </div>
          </section>
        </div>

        <section className="card">
          <div className="card-head">
            <h2>{t("st.rejected")}</h2>
            <span className="muted small">{t("st.rejectedSub", { n: rejected?.n ?? "—" })}</span>
          </div>
          <div className="table-wrap">
            <table className="table">
              <thead><tr><th>{t("st.colSource")}</th><th>{t("st.colReason")}</th><th>{t("st.colPath")}</th></tr></thead>
              <tbody>
                {(rejected?.rows || []).slice().reverse().map((r: any, i: number) => (
                  <tr key={i}>
                    <td className="code" title={r.source_url || r.site}>{String(r.source_url || r.site || "").slice(0, 56)}</td>
                    <td>{r.reason}</td>
                    <td className="code" title={r.rel_path}>{String(r.rel_path || "").slice(0, 56)}</td>
                  </tr>
                ))}
                {!(rejected?.rows || []).length && <tr><td colSpan={3} className="muted">{t("st.queueEmpty")}</td></tr>}
              </tbody>
            </table>
          </div>
        </section>

        <section className="card">
          <div className="card-head">
            <h2>{t("fb.title")}</h2>
            <span className="muted small">{t("fb.sub")}</span>
          </div>
          <div className="table-wrap">
            <table className="table">
              <thead><tr><th>{t("fb.rating")}</th><th>{t("fb.q")}</th><th>{t("fb.comment")}</th></tr></thead>
              <tbody>
                {(feedback?.rows || []).map((r: any) => (
                  <Fragment key={r.request_id}>
                    <tr className="rowlink" onClick={() => setOpenFeedback(openFeedback === r.request_id ? null : r.request_id)}>
                      <td>{r.helpful ? "👍" : "👎"}</td>
                      <td>{String(r.question || r.request_id).slice(0, 60)}</td>
                      <td className="muted">{String(r.comment || "").slice(0, 40)}</td>
                    </tr>
                    {openFeedback === r.request_id && (
                      <tr className="fb-detail">
                        <td colSpan={3}>
                          <div className="muted small">{r.ts} · {r.status || "—"} · <span className="code">{r.build_id || "—"}</span></div>
                          <p className="fb-answer">{r.answer || "—"}</p>
                          <div className="fb-cites">
                            {(r.citations || []).length
                              ? r.citations.map((c: any, i: number) => (c.source_url
                                ? <a key={i} className="source-chip" href={c.source_url} target="_blank" rel="noreferrer">{c.title || c.doc_uid}</a>
                                : <span key={i} className="source-chip">{c.title || c.doc_uid}</span>))
                              : <span className="muted small">{t("fb.noCites")}</span>}
                          </div>
                        </td>
                      </tr>
                    )}
                  </Fragment>
                ))}
                {!(feedback?.rows || []).length && <tr><td colSpan={3} className="muted">{t("fb.empty")}</td></tr>}
              </tbody>
            </table>
          </div>
        </section>
          </>
        )}
      </div>
    </div>
  );
}

function Stat({ label, value, sub, tone }: { label: string; value: string; sub?: string; tone?: "ok" | "warn" }) {
  return (
    <div className={`stat ${tone ? `tone-${tone}` : ""}`}>
      <span className="stat-label">{tone && <span className="status-dot" />}{label}</span>
      <span className="stat-value">{value}</span>
      {sub && <span className="stat-sub">{sub}</span>}
    </div>
  );
}

function Dist({ rows, empty }: { rows: Array<[string, number]>; empty: string }) {
  const total = rows.reduce((s, [, v]) => s + v, 0);
  if (!rows.length) return <p className="muted small">{empty}</p>;
  return (
    <ul className="dist">
      {rows.map(([k, v]) => (
        <li key={k}>
          <span className="dist-k">{k}</span>
          <span className="dist-bar"><i style={{ width: `${total ? (v / total) * 100 : 0}%` }} /></span>
          <span className="dist-v">{v}<small>{total ? ` ${Math.round((v / total) * 100)}%` : ""}</small></span>
        </li>
      ))}
    </ul>
  );
}

const fmt = (n?: number) => (n == null ? "—" : n.toLocaleString());
const ms = (n?: number) => (n == null ? "—" : n >= 1000 ? `${(n / 1000).toFixed(1)}s` : `${Math.round(n)}ms`);
