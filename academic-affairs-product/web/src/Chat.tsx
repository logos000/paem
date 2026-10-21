import { useCallback, useEffect, useLayoutEffect, useRef, useState, type ReactNode } from "react";
import { answerCache, api, askStream, AskResult, Citation, EvidenceItem, RetrievedDoc, Turn } from "./api";
import { useI18n, type Lang, type T } from "./i18n";
import {
  IconAlert, IconArrowUp, IconCheck, IconCopy, IconExternal, IconClose,
  IconStop, IconThumbDown, IconThumbUp,
} from "./icons";
import Dial from "./Dial";

// ------------------------------------------------------------------ vocab

const STEPS = ["optimizing_query", "retrieving", "generating", "validating"] as const;
const stepIndex = (s: string) => (s === "done" ? STEPS.length : STEPS.indexOf(s as typeof STEPS[number]));

const TONE: Record<string, "ok" | "warn" | "muted"> = {
  sufficient: "ok", conflict: "warn", no_material: "muted", wrong_term: "muted", wrong_audience: "muted", stale_or_uncertain: "muted",
};
/** Public AnswerStatus values that OVERRIDE the decision badge — they change
 *  what the product promises (2026-10-17: an answer whose text said 证据中未包含…
 *  wore 有据可依·引用已核对 because the badge only read the builder decision). */
const STATUS_TONE: Record<string, "ok" | "warn" | "muted"> = {
  partial: "warn", refused_evidence: "muted", refused_policy: "muted",
};

function genBadge(mode: string, t: T): { text: string; tip: string; ok: boolean } | null {
  if (mode === "refusal") return null;
  if (mode === "validated") return { text: t("gen.validated"), tip: t("gen.validatedTip"), ok: true };
  if (mode === "unverified") return { text: t("gen.unverified"), tip: t("gen.unverifiedTip"), ok: false };
  if (mode === "breaker_open" || mode === "provider_fallback") return { text: t("gen.fallback"), tip: t("gen.fallbackTip"), ok: false };
  if (mode === "template") return { text: t("gen.template"), tip: t("gen.templateTip"), ok: false };
  return { text: mode, tip: "", ok: false };
}

// Sample questions are LIVE-VERIFIED against the serving corpus before they
// ship (2026-10-17: the old tuition/withdrawal samples were unanswerable on
// the SP build and the page demoed a refusal — every entry below returned a
// real answer through /v1/ask on sharepoint-only-20261015).
const EXAMPLES: Record<Lang, string[]> = {
  zh: [
    "Course Enrollment Guide Spring 2026 中，Seniors 的选课开始时间是什么？",
    "修 ECE 313 之前需要先修过哪些数学课？",
    "按 2022 年秋季学期的选课指南，退课申请最迟需要在什么时候提交并获批准？",
    "电气工程专业需要修哪些必修课？",
  ],
  en: [
    "In the Spring 2026 Course Enrollment Guide, when does enrollment open for seniors?",
    "Which math courses are prerequisites for ECE 313?",
    "What are the contact emails for the UIUC exchange program?",
    "When is the course withdrawal deadline for undergraduates in Fall 2026?",
  ],
};

function termOptions(t: T): Array<{ value: string; label: string }> {
  const now = new Date();
  const y = now.getFullYear();
  const fall = now.getMonth() >= 6;
  const seq: Array<[number, string]> = fall
    ? [[y, "spring"], [y, "fall"], [y + 1, "spring"]]
    : [[y - 1, "fall"], [y, "spring"], [y, "fall"]];
  return seq.map(([yr, s]) => ({ value: `${yr}-${s}`, label: t(s === "fall" ? "term.fall" : "term.spring", { y: yr }) }));
}

// ------------------------------------------------------------------ message model

interface Message {
  id: string;
  question: string;
  context: { audience?: string; term?: string };
  state: "streaming" | "done" | "error" | "stopped";
  stage: string;
  optimized: string[];
  followUps: string[];
  docs: RetrievedDoc[];
  nEvidence: number;
  live: string;
  result?: AskResult;
  restored?: Turn;
  error?: string;
  feedback?: "up" | "down";
}

interface Opened { evidence?: EvidenceItem; citation?: Citation; n?: number; claim?: string }

// ------------------------------------------------------------------ hooks

/** Reveal `target` at a steady reading pace; speeds up when the backlog grows,
 *  so bursty SSE chunks read as continuous typing instead of jumps. */
function useTypewriter(target: string, enabled: boolean): number {
  const [shown, setShown] = useState(enabled ? 0 : target.length);
  const targetRef = useRef(target);
  const posRef = useRef(enabled ? 0 : target.length);
  targetRef.current = target;

  useEffect(() => {
    if (!enabled) { posRef.current = targetRef.current.length; setShown(posRef.current); return; }
    let raf = 0;
    let last = performance.now();
    const tick = (now: number) => {
      const dt = Math.min(now - last, 64);
      last = now;
      const total = targetRef.current.length;
      if (posRef.current < total) {
        const backlog = total - posRef.current;
        const cps = 40 + backlog * 1.6;
        posRef.current = Math.min(total, posRef.current + (cps * dt) / 1000);
        setShown(Math.floor(posRef.current));
      } else if (posRef.current > total) {
        posRef.current = total; setShown(total);
      }
      raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [enabled]);

  return Math.min(shown, target.length);
}

// ------------------------------------------------------------------ answer text rendering

const CITE_RE = /\[\s*[Ee]?\s*(\d{1,2})\s*\]/g;
const PARTIAL_TAIL_RE = /(\[\s*[Ee]?\s*\d{0,2}\s*|\*{1,2}[^*\n]{0,40})$/;
/** A whitespace-separated run of citation markers, e.g. "[E1][E3] [E5]". */
const CITE_RUN_RE = /(\[\s*[Ee]?\s*\d{1,2}\s*\])(?:\s*\[\s*[Ee]?\s*\d{1,2}\s*\])*/g;
const MAX_MARKS_PER_RUN = 3;

function inline(text: string, onCite: ((n: number) => void) | null, keyBase: string, viewLabel: string, resolveDoc?: (n: number) => string): ReactNode[] {
  const out: ReactNode[] = [];
  text.split(/(\*\*[^*]+\*\*)/g).forEach((chunk, ci) => {
    if (/^\*\*[^*]+\*\*$/.test(chunk)) { out.push(<strong key={`${keyBase}b${ci}`}>{chunk.slice(2, -2)}</strong>); return; }
    let last = 0;
    CITE_RUN_RE.lastIndex = 0;
    let m: RegExpExecArray | null;
    while ((m = CITE_RUN_RE.exec(chunk)) !== null) {
      if (m.index > last) out.push(chunk.slice(last, m.index));
      const nums = (m[0].match(/\d+/g) || []).map(Number);
      // A row of markers is noise (reviewer round 1, item 3): dedupe by source
      // document, cap the visible chips, and keep the folded ones reachable —
      // the "+N" chip opens the first folded citation's drawer.
      let visible = nums;
      if (onCite && resolveDoc) {
        const seenDocs = new Set<string>();
        visible = nums.filter((n) => {
          const doc = resolveDoc(n) || `#${n}`;
          if (seenDocs.has(doc)) return false;
          seenDocs.add(doc);
          return true;
        });
      }
      const shown = visible.slice(0, MAX_MARKS_PER_RUN);
      const folded = visible.length - shown.length;
      shown.forEach((n, k) => {
        out.push(onCite
          ? <button key={`${keyBase}c${ci}-${m!.index}-${k}`} className="cite-mark" onClick={() => onCite(n)} title={viewLabel}>{n}</button>
          : <span key={`${keyBase}c${ci}-${m!.index}-${k}`} className="cite-mark pending">{n}</span>);
      });
      if (folded > 0 && onCite) {
        out.push(<button key={`${keyBase}x${ci}-${m!.index}`} className="cite-mark more" onClick={() => onCite(visible[MAX_MARKS_PER_RUN])} title={viewLabel}>+{folded}</button>);
      }
      last = m.index + m[0].length;
    }
    if (last < chunk.length) out.push(chunk.slice(last));
  });
  return out;
}

/** Light markdown: paragraphs, "-"/"•"/"1." lists, **bold**, inline [En] markers. */
function RichText({ text, onCite, caret, resolveDoc }: { text: string; onCite: ((n: number) => void) | null; caret?: boolean; resolveDoc?: (n: number) => string }) {
  const { t } = useI18n();
  const view = t("cite.view");
  const blocks: ReactNode[] = [];
  let list: { ordered: boolean; items: string[] } | null = null;
  let para: string[] = [];
  const flushPara = () => {
    if (para.length) blocks.push(<p key={`p${blocks.length}`}>{inline(para.join("\n"), onCite, `p${blocks.length}`, view, resolveDoc)}</p>);
    para = [];
  };
  const flushList = () => {
    if (!list) return;
    const items = list.items.map((it, i) => <li key={i}>{inline(it, onCite, `l${blocks.length}-${i}`, view, resolveDoc)}</li>);
    blocks.push(list.ordered ? <ol key={`o${blocks.length}`}>{items}</ol> : <ul key={`u${blocks.length}`}>{items}</ul>);
    list = null;
  };
  for (const raw of text.split("\n")) {
    const line = raw.trimEnd();
    const bullet = line.match(/^\s*(?:[-•*·]|(\d+)[.、)])\s+(.*)$/);
    if (bullet) {
      flushPara();
      const ordered = !!bullet[1];
      if (!list || list.ordered !== ordered) { flushList(); list = { ordered, items: [] }; }
      list.items.push(bullet[2]);
    } else if (!line.trim()) {
      flushPara(); flushList();
    } else {
      flushList();
      para.push(line.replace(/^#{1,4}\s+/, ""));
    }
  }
  flushPara(); flushList();
  return <div className={`rich ${caret ? "with-caret" : ""}`}>{blocks}</div>;
}

// ------------------------------------------------------------------ component

let idSeq = 0;

export default function Chat({ active = true, initialSessionId, onAsked, onNew }: {
  active?: boolean;
  initialSessionId: string;
  onAsked: (sessionId: string, question: string) => void;
  onNew: () => void;
}) {
  const { t, lang } = useI18n();
  const [sessionId, setSessionId] = useState(initialSessionId);
  const [messages, setMessages] = useState<Message[]>([]);
  const [question, setQuestion] = useState("");
  const [audience, setAudience] = useState(() => localStorage.getItem("ctx_audience") || "");
  const [term, setTerm] = useState(() => localStorage.getItem("ctx_term") || "");
  const [opened, setOpened] = useState<Opened | null>(null);
  const [loadingHistory, setLoadingHistory] = useState(!!initialSessionId);
  const [historyFailed, setHistoryFailed] = useState(false);
  const abortRef = useRef<AbortController | null>(null);
  const threadRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const stickRef = useRef(true);

  const busy = messages.some((m) => m.state === "streaming");

  useEffect(() => { try { localStorage.setItem("ctx_audience", audience); } catch { /* storage off */ } }, [audience]);
  useEffect(() => { try { localStorage.setItem("ctx_term", term); } catch { /* storage off */ } }, [term]);

  useEffect(() => {
    if (!initialSessionId) { inputRef.current?.focus(); return; }
    api.history(initialSessionId)
      .then(({ turns }) => setMessages(turns.map((turn) => ({
        id: `h${idSeq++}`, question: turn.question, context: {}, state: "done", stage: "done",
        optimized: [], followUps: [], docs: [], nEvidence: 0, live: "", restored: turn,
        result: answerCache.get(turn.request_id),
      }))))
      .catch(() => setHistoryFailed(true))
      .finally(() => setLoadingHistory(false));
    return () => abortRef.current?.abort();
    // Mount-only: App remounts Chat (new key) to switch conversations; the id
    // it echoes back after our first ask must not reload or abort the stream.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Keep the thread pinned to the bottom while streaming unless the reader scrolled up.
  const onScroll = () => {
    const el = threadRef.current;
    if (el) stickRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 140;
  };
  const pin = useCallback(() => {
    const el = threadRef.current;
    if (el && stickRef.current && el.querySelector(".turn")) el.scrollTop = el.scrollHeight;
  }, []);
  useLayoutEffect(pin, [messages, pin]);

  // Auto-grow composer.
  useLayoutEffect(() => {
    const el = inputRef.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 220)}px`;
  }, [question]);

  // Esc closes the evidence drawer; "/" focuses the composer; "N" new conversation.
  useEffect(() => {
    if (!active) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpened(null);
      const typing = (e.target as HTMLElement)?.closest("input, textarea, select");
      if (e.key === "/" && !typing) { e.preventDefault(); inputRef.current?.focus(); }
      if ((e.key === "n" || e.key === "N") && !typing && !e.ctrlKey && !e.metaKey && !e.altKey) onNew();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onNew, active]);

  const patch = useCallback((id: string, fn: (m: Message) => Partial<Message>) => {
    setMessages((list) => list.map((m) => (m.id === id ? { ...m, ...fn(m) } : m)));
  }, []);

  async function send(text?: string, audienceOverride?: string) {
    const q = (text ?? question).trim();
    if (!q || busy) return;
    setQuestion("");
    stickRef.current = true;
    const id = `m${idSeq++}`;
    const aud = audienceOverride ?? audience;
    const context = { audience: aud || undefined, term: term || undefined };
    setMessages((list) => [...list, {
      id, question: q, context, state: "streaming", stage: "optimizing_query",
      optimized: [], followUps: [], docs: [], nEvidence: 0, live: "",
    }]);

    let sid = sessionId;
    try {
      if (!sid) {
        sid = (await api.newSession()).session_id;
        setSessionId(sid);
      }
      onAsked(sid, q);
    } catch {
      patch(id, () => ({ state: "error", error: t("err.connect") }));
      return;
    }

    const user_context: Record<string, unknown> = {};
    if (context.audience) user_context.audience = context.audience;
    if (context.term) user_context.term = context.term;
    // Follow-up context (reviewer round 1, item 4): the PREVIOUS question rides
    // along for retrieval only — the backend merges it when this one is a short
    // deictic follow-up ("那研究生呢？"), so pronoun-questions still retrieve.
    const prevQuestion = [...messages].reverse().find((m) => m.question && m.question !== q)?.question;
    const controller = new AbortController();
    abortRef.current = controller;
    try {
      await askStream(
        { question: q, user_context: Object.keys(user_context).length ? user_context : undefined, session_id: sid, language: lang,
          context_question: prevQuestion && prevQuestion !== q ? prevQuestion : undefined },
        {
          stage: (s) => patch(id, (m) => ({ stage: s === "done" && m.stage === "validating" ? "validating" : s })),
          queryOptimized: (d) => patch(id, () => ({ optimized: d.queries || [] })),
          retrieved: (d) => patch(id, () => ({ docs: d.documents || [], nEvidence: d.n_evidence })),
          followUp: (d) => patch(id, () => ({ followUps: d.queries || [], stage: "retrieving", live: "" })),
          answerDelta: (txt) => patch(id, (m) => ({ live: m.live + txt, stage: "generating" })),
          result: (r) => { answerCache.put(r); patch(id, () => ({ result: r, state: "done" })); },
          error: (e) => patch(id, () => ({ state: "error", error: e })),
        },
        controller.signal,
      );
      patch(id, (m) => (m.state === "streaming" ? { state: "error", error: t("err.interrupted") } : {}));
    } catch (e: any) {
      if (e?.name === "AbortError") patch(id, () => ({ state: "stopped" }));
      else patch(id, () => ({ state: "error", error: String(e).slice(0, 200) }));
    } finally {
      abortRef.current = null;
    }
  }

  const stop = () => abortRef.current?.abort();

  async function sendFeedback(m: Message, helpful: boolean) {
    const rid = m.result?.request_id || m.restored?.request_id;
    if (!rid || m.feedback) return;
    patch(m.id, () => ({ feedback: helpful ? "up" : "down" }));
    await api.feedback(rid, helpful).catch(() => {});
  }

  const empty = messages.length === 0 && !loadingHistory;
  const terms = termOptions(t);

  return (
    <div className={`chat ${opened ? "drawer-open" : ""} ${empty ? "is-empty" : ""}`}>
      <div className="thread" ref={threadRef} onScroll={onScroll}>
        {empty ? (
          <Welcome onPick={(q) => send(q)} />
        ) : (
          <div className="thread-inner">
            {loadingHistory && <div className="skeleton-block" />}
            {historyFailed && <div className="inline-notice">{t("err.history")}</div>}
            {messages.map((m) => (
              <MessageView key={m.id} m={m}
                onTick={pin}
                onOpen={setOpened}
                onFeedback={(h) => sendFeedback(m, h)}
                onRetry={() => send(m.question)}
                onAudience={(a) => { setAudience(a); send(m.question, a); }} />
            ))}
          </div>
        )}
      </div>

      <div className="composer-dock">
        <div className={`composer ${busy ? "is-busy" : ""}`}>
          <textarea
            ref={inputRef}
            value={question}
            rows={1}
            placeholder={empty ? t("composer.placeholder") : t("composer.placeholderMore")}
            onChange={(e) => setQuestion(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) { e.preventDefault(); send(); }
            }}
          />
          <div className="composer-bar">
            <div className="ctx">
              <label className={`ctx-pill ${audience ? "set" : ""}`}>
                <span>{t("composer.audience")}</span>
                <select value={audience} onChange={(e) => setAudience(e.target.value)} aria-label={t("composer.audience")}>
                  <option value="">{t("composer.audienceAny")}</option>
                  <option value="undergraduate">{t("composer.ug")}</option>
                  <option value="graduate">{t("composer.grad")}</option>
                </select>
              </label>
              <label className={`ctx-pill ${term ? "set" : ""}`}>
                <span>{t("composer.term")}</span>
                <select value={term} onChange={(e) => setTerm(e.target.value)} aria-label={t("composer.term")}>
                  <option value="">{t("composer.termAuto")}</option>
                  {terms.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
                  {term && !terms.some((o) => o.value === term) && <option value={term}>{term}</option>}
                </select>
              </label>
            </div>
            {busy ? (
              <button className="send stop" onClick={stop} aria-label={t("composer.stop")} title={t("composer.stop")}><IconStop size={14} /></button>
            ) : (
              <button className="send" onClick={() => send()} disabled={!question.trim()} aria-label={t("composer.send")} title={t("composer.send")}>
                <IconArrowUp size={17} />
              </button>
            )}
          </div>
        </div>
        <div className="composer-hint">
          <span>{t("composer.hint")}</span>
          <span>{t("composer.disclaimer")}</span>
        </div>
      </div>

      <EvidenceDrawer opened={opened} onClose={() => setOpened(null)} />
    </div>
  );
}

// ------------------------------------------------------------------ welcome

function Welcome({ onPick }: { onPick: (q: string) => void }) {
  const { t, lang } = useI18n();
  return (
    <div className="welcome" key={lang}>
      <div className="hero">
        <div>
          <div className="welcome-kicker rise" style={{ animationDelay: "40ms" }}>{t("welcome.kicker")}</div>
          <h1 className="rise" style={{ animationDelay: "100ms" }}>
            {t("welcome.title1")}<br /><span className="dim">{t("welcome.title2")}</span>
          </h1>
          <p className="lede rise" style={{ animationDelay: "180ms" }}>{t("welcome.lede")}</p>
        </div>
        <div className="hero-dial"><Dial /></div>
      </div>

      <div className="examples rise" style={{ animationDelay: "260ms" }}>
        <div className="label">{t("welcome.try")}</div>
        {EXAMPLES[lang].map((q) => (
          <button key={q} className="example" onClick={() => onPick(q)}>
            <span>{q}</span>
            <span className="example-go" aria-hidden="true">→</span>
          </button>
        ))}
      </div>

      <div className="principles rise" style={{ animationDelay: "340ms" }}>
        {(["p1", "p2", "p3"] as const).map((k, i) => (
          <div className="principle" key={k}>
            <span className="principle-n">{String(i + 1).padStart(2, "0")}</span>
            <h3>{t(`${k}.t` as any)}</h3>
            <p>{t(`${k}.d` as any)}</p>
          </div>
        ))}
      </div>
    </div>
  );
}

// ------------------------------------------------------------------ one Q/A turn

function MessageView({ m, onTick, onOpen, onFeedback, onRetry, onAudience }: {
  m: Message;
  onTick: () => void;
  onOpen: (o: Opened) => void;
  onFeedback: (helpful: boolean) => void;
  onRetry: () => void;
  onAudience: (a: string) => void;
}) {
  const { t } = useI18n();
  const r = m.result;
  const live = !m.restored;
  // Stream target: deltas while generating; the final answer once it arrives.
  const target = r ? r.answer : m.live;
  const shown = useTypewriter(target, live);
  const caughtUp = shown >= target.length;
  const [settled, setSettled] = useState(!live);
  useEffect(() => { if (r && caughtUp) setSettled(true); }, [r, caughtUp]);
  useEffect(onTick, [shown, settled, onTick]);

  const halted = m.state === "error" || m.state === "stopped";
  const writing = !settled && !halted;
  const text = target.slice(0, shown).replace(PARTIAL_TAIL_RE, "");
  const verifying = writing && !!target && (!!r || m.stage === "validating");

  const ctxBits = [
    m.context.audience === "undergraduate" ? t("composer.ug") : m.context.audience === "graduate" ? t("composer.grad") : "",
    m.context.term || "",
  ].filter(Boolean);

  return (
    <article className="turn">
      <div className="q-row">
        <div className="q-text">{m.question}</div>
        {ctxBits.length > 0 && <div className="q-ctx">{ctxBits.join(" · ")}</div>}
      </div>

      <div className={`a-col ${writing ? "is-live" : ""}`}>
        {live && (writing || halted) && <Progress m={m} />}

        {writing && !target && (
          <div className="awaiting">
            <span className="awaiting-text">{m.docs.length ? t("await.read", { n: m.nEvidence }) : t("await.search")}</span>
            <div className="ghost-lines"><i /><i /><i /></div>
          </div>
        )}

        {writing && target && (
          <>
            <div className={`live-status ${verifying ? "verifying" : ""}`}>
              <span className="pulse" />{verifying ? t("live.verifying") : t("live.writing")}
            </div>
            <div className="answer-body streaming"><RichText text={text} onCite={null} caret={!verifying} /></div>
          </>
        )}

        {m.state === "error" && (
          <div className="a-error">
            <IconAlert size={17} />
            <div><b>{t("err.title")}</b><p>{m.error}</p></div>
            <button className="ghost-btn sm" onClick={onRetry}>{t("err.retry")}</button>
          </div>
        )}
        {m.state === "stopped" && (
          <>
            {text && <div className="answer-body"><RichText text={text} onCite={null} /></div>}
            <div className="a-stopped">{t("stopped")} · <button className="link-btn" onClick={onRetry}>{t("stopped.retry")}</button></div>
          </>
        )}

        {settled && r && <Answer r={r} m={m} animate={live} onOpen={onOpen} onFeedback={onFeedback} onAudience={onAudience} />}
        {settled && !r && m.restored && <RestoredAnswer turn={m.restored} />}
      </div>
    </article>
  );
}

function Progress({ m }: { m: Message }) {
  const { t } = useI18n();
  const now = stepIndex(m.stage === "done" ? "validating" : m.stage);
  const halted = m.state === "error" || m.state === "stopped";
  return (
    <div className="progress">
      <div className="progress-head">
        <Dial compact step={Math.max(now, 0)} halted={halted} size={38} />
        <ol className="steps">
          {STEPS.map((s, i) => (
            <li key={s} className={i < now ? "past" : i === now ? "now" : ""}>
              <span className="step-n">{String(i + 1).padStart(2, "0")}</span>{t(`step.${s}` as any)}
            </li>
          ))}
        </ol>
      </div>
      {m.optimized.length > 0 && <div className="pd-line">{t("progress.queries")}: {m.optimized.join(" · ")}</div>}
      {m.followUps.length > 0 && <div className="pd-line">{t("progress.followup")}: {m.followUps.join(" ; ")}</div>}
      {m.docs.length > 0 && (
        <div className="found">
          <div className="pd-line">{t("progress.hits", { n: m.nEvidence, d: m.docs.length })}</div>
          <ul className="found-list">
            {m.docs.map((d, i) => (
              <li key={`${d.doc_uid}-${i}`} style={{ animationDelay: `${i * 50}ms` }}>
                <span className="found-title">{d.title}</span>
                {d.source_url
                  ? <a className="found-link" href={d.source_url} target="_blank" rel="noreferrer">{hostOf(d.source_url)}<IconExternal size={11} /></a>
                  : null}
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}

// ------------------------------------------------------------------ settled answer

function Answer({ r, m, animate, onOpen, onFeedback, onAudience }: {
  r: AskResult; m: Message; animate: boolean;
  onOpen: (o: Opened) => void;
  onFeedback: (helpful: boolean) => void;
  onAudience: (a: string) => void;
}) {
  const { t } = useI18n();
  const [details, setDetails] = useState(false);
  const [copied, setCopied] = useState(false);
  const evidenceById = new Map<string, EvidenceItem>((r.evidence_pack?.evidence || []).map((e) => [e.evidence_id, e]));
  const cmap = r.citation_map || {};
  const numberOf = new Map<string, number>(Object.entries(cmap)
    .filter(([k]) => /^E\d+$/.test(k)).map(([k, v]) => [v, Number(k.slice(1))]));
  const claimTextFor = (eid?: string) => (eid ? r.claims.find((c) => c.evidence_ids?.includes(eid))?.text : undefined);

  const openByNumber = (n: number) => {
    const eid = cmap[`E${n}`];
    const evidence = eid ? evidenceById.get(eid) : undefined;
    const citation = r.citations.find((c) => c.id === eid);
    if (evidence || citation) onOpen({ evidence, citation, n, claim: claimTextFor(eid) });
  };
  const openCitation = (c: Citation) => onOpen({ evidence: evidenceById.get(c.id), citation: c, n: numberOf.get(c.id), claim: claimTextFor(c.id) });

  const d = r.decision;
  // The public AnswerStatus outranks the builder decision badge for the states
  // that change the promise; absent (pre-2026-10-17 cached results) the old
  // decision-driven behavior stands.
  const st = r.status || "";
  const badgeKey = STATUS_TONE[st] ? st : d.status;
  const tone = STATUS_TONE[st] || TONE[d.status] || "muted";
  const badgeLabel = t(`status.${badgeKey}` as any);
  const refusal = d.abstain || st === "refused_evidence" || st === "refused_policy";
  const gen = genBadge(r.generation.mode, t);
  const hintKey = refusal && !d.abstain ? "hint.text_guard" : `hint.${d.status}`;
  const hint = t(hintKey as any);

  const copy = async () => {
    const plain = r.answer.replace(CITE_RE, "").replace(/\*\*/g, "");
    try { await navigator.clipboard.writeText(plain); setCopied(true); window.setTimeout(() => setCopied(false), 1600); } catch { /* denied */ }
  };

  return (
    <div className={`answer ${animate ? "settle" : ""}`}>
      <div className="answer-head">
        <span className={`status tone-${tone}`}><span className="status-dot" />{badgeLabel === `status.${badgeKey}` ? badgeKey : badgeLabel}</span>
        {gen && <span className={`gen ${gen.ok ? "ok" : ""}`} title={gen.tip}>{gen.ok ? <IconCheck size={12} /> : <IconAlert size={12} />}{gen.text}</span>}
      </div>

      {refusal ? (
        <div className="refusal">
          <RichText text={r.answer.replace(CITE_RE, "")} onCite={null} />
          {hint !== hintKey && <p className="refusal-hint">{hint}</p>}
          {d.status === "wrong_audience" && (
            <div className="refusal-actions">
              <button className="ghost-btn sm" onClick={() => onAudience("undergraduate")}>{t("retry.ug")}</button>
              <button className="ghost-btn sm" onClick={() => onAudience("graduate")}>{t("retry.grad")}</button>
            </div>
          )}
        </div>
      ) : (
        <>
          <div className="answer-body"><RichText text={r.answer} onCite={openByNumber} resolveDoc={(n) => evidenceById.get(cmap[`E${n}`] || "")?.doc_id || ""} /></div>
          {d.status === "conflict" && <div className="conflict">{t("conflict.note")}</div>}

          {r.claims.length > 0 && (
            <section className="claims fade-in" style={{ animationDelay: "60ms" }}>
              <h4 className="sec-title">{t("claims.title")}</h4>
              {r.claims.map((c, i) => (
                <div className="claim" key={i}>
                  <span className="claim-n">{i + 1}</span>
                  <div className="claim-body">
                    <p>{c.text.replace(CITE_RE, "")}</p>
                    <ClaimSources sources={c.sources} numberOf={numberOf} openCitation={openCitation} />
                  </div>
                </div>
              ))}
            </section>
          )}
        </>
      )}

      <div className="answer-actions fade-in" style={{ animationDelay: "160ms" }}>
        <button className={`act ${copied ? "done" : ""}`} onClick={copy} title={t("act.copy")}>
          {copied ? <IconCheck size={14} /> : <IconCopy size={14} />}<span>{copied ? t("act.copied") : t("act.copy")}</span>
        </button>
        <button className={`act ${m.feedback === "up" ? "picked" : ""}`} onClick={() => onFeedback(true)} disabled={!!m.feedback} title={t("act.helpful")}>
          <IconThumbUp size={14} />
        </button>
        <button className={`act ${m.feedback === "down" ? "picked" : ""}`} onClick={() => onFeedback(false)} disabled={!!m.feedback} title={t("act.unhelpful")}>
          <IconThumbDown size={14} />
        </button>
        {m.feedback && <span className="act-note">{t("act.thanks")}</span>}
        <span className="act-spacer" />
        <button className="act" onClick={() => setDetails((v) => !v)}><span>{details ? t("act.hideDetails") : t("act.details")}</span></button>
      </div>

      {details && (
        <dl className="details">
          <div><dt>{t("d.total")}</dt><dd>{fmtMs(r.timing.total_ms)}</dd></div>
          <div><dt>{t("d.retrieval")}</dt><dd>{fmtMs(r.timing.builder_ms ?? r.timing.retrieval_ms)}</dd></div>
          <div><dt>{t("d.generation")}</dt><dd>{fmtMs(r.timing.llm_ms)}</dd></div>
          <div><dt>{t("d.provider")}</dt><dd>{r.provider}</dd></div>
          <div><dt>{t("d.build")}</dt><dd className="code">{r.build_id}</dd></div>
          <div><dt>{t("d.request")}</dt><dd className="code">{r.request_id}</dd></div>
          {st && <div><dt>{t("d.answerStatus")}</dt><dd className="code">{st}</dd></div>}
          {d.reason && <div className="wide"><dt>{t("d.decision")}</dt><dd>{d.reason}</dd></div>}
          {r.generation.problems && r.generation.problems.length > 0 && (
            <div className="wide"><dt>{t("d.validation")}</dt><dd>{r.generation.problems.join("；")}</dd></div>
          )}
        </dl>
      )}
    </div>
  );
}

/** Per-claim source chips, deduped by document and capped like inline markers
 *  (reviewer round 1, item 3); "+N" opens the first folded source. */
function ClaimSources({ sources, numberOf, openCitation }: {
  sources: Citation[]; numberOf: Map<string, number>; openCitation: (c: Citation) => void;
}) {
  const seen = new Set<string>();
  const uniq = sources.filter((s) => {
    const key = s.doc_uid || s.id;
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
  const shown = uniq.slice(0, MAX_MARKS_PER_RUN);
  const folded = uniq.length - shown.length;
  return (
    <div className="claim-sources">
      {shown.map((s, j) => (
        <button key={j} className="source-chip" onClick={() => openCitation(s)}>
          <span className="source-n">{numberOf.get(s.id) ?? j + 1}</span>
          <span className="source-t">{s.title || s.doc_uid.slice(0, 16)}</span>
        </button>
      ))}
      {folded > 0 && (
        <button className="source-chip more" onClick={() => openCitation(uniq[MAX_MARKS_PER_RUN])}>
          <span className="source-n">+{folded}</span>
        </button>
      )}
    </div>
  );
}

function RestoredAnswer({ turn }: { turn: Turn }) {
  const { t } = useI18n();
  const status = turn.decision?.status;
  const st = turn.status || "";
  const badgeKey = STATUS_TONE[st] ? st : status;
  const tone = STATUS_TONE[st] || (status ? TONE[status] : undefined);
  const refusal = !!turn.decision?.abstain || st === "refused_evidence" || st === "refused_policy";
  const label = badgeKey ? t(`status.${badgeKey}` as any) : "";
  return (
    <div className="answer restored">
      <div className="answer-head">
        {badgeKey && <span className={`status tone-${tone || "muted"}`}><span className="status-dot" />{label === `status.${badgeKey}` ? badgeKey : label}</span>}
        <span className="gen ok" title={t("restored.tip")}>{t("restored.tag")}</span>
      </div>
      <div className="answer-body"><RichText text={refusal ? (turn.answer || "").replace(CITE_RE, "") : turn.answer || ""} onCite={null} /></div>
    </div>
  );
}

// ------------------------------------------------------------------ evidence drawer

/** Fact-token shapes (mirrors the backend validator's fact kinds loosely):
 *  the drawer highlights tokens shared by the citing claim and the block. */
const FACT_RES = [
  /\d{4}\s*[-/年.]\s*\d{1,2}\s*[-/月.]\s*\d{1,2}\s*日?/g,
  /\d{1,2}\s*月\s*\d{1,2}\s*日/g,
  /\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2}(?:,?\s+\d{4})?/gi,
  /\d{1,2}:\d{2}\s*(?:[ap]m)?/gi,
  /\b[A-Z]{2,4}\s?\d{3}\b/g,
  /\d+(?:\.\d+)?\s*(?:%|％|学分|credits?|周|weeks?|天|days?|小时|hours?)/gi,
];

function factTokens(text: string): string[] {
  const out = new Set<string>();
  for (const re of FACT_RES) {
    re.lastIndex = 0;
    let m: RegExpExecArray | null;
    while ((m = re.exec(text)) !== null) out.add(m[0].trim());
  }
  return [...out];
}

function Highlighted({ text, tokens }: { text: string; tokens: string[] }) {
  const present = tokens.filter((t) => t.length >= 2 && text.toLowerCase().includes(t.toLowerCase()));
  if (!present.length) return <>{text}</>;
  const pattern = present.map((t) => t.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("|");
  const parts = text.split(new RegExp(`(${pattern})`, "gi"));
  // split-with-capture: odd indices are the highlighted matches
  return <>{parts.map((p, i) => (i % 2 ? <mark key={i}>{p}</mark> : p))}</>;
}

function EvidenceDrawer({ opened, onClose }: { opened: Opened | null; onClose: () => void }) {
  const { t } = useI18n();
  const [last, setLast] = useState<Opened | null>(opened);
  useEffect(() => { if (opened) setLast(opened); }, [opened]);
  const o = opened || last;
  const ev = o?.evidence;
  const p = ev?.provenance;
  const url = o?.citation?.source_url || p?.source_url || "";
  const title = ev?.title || o?.citation?.title || "";
  const docId = o?.citation?.doc_uid || ev?.doc_id || "";

  return (
    <>
      <div className={`drawer-scrim ${opened ? "show" : ""}`} onClick={onClose} />
      <aside className={`drawer ${opened ? "show" : ""}`} aria-hidden={!opened} aria-label={t("drawer.label")}>
        <div className="drawer-head">
          <span className="drawer-kicker">{o?.n != null && <span className="cite-mark">{o.n}</span>}{t("drawer.label")}</span>
          <button className="icon-btn" onClick={onClose} aria-label={t("drawer.close")}><IconClose size={17} /></button>
        </div>
        <h3 className="drawer-title">{title}</h3>
        <blockquote className="drawer-quote" key={ev?.evidence_id || "none"}>
          {ev?.text ? <Highlighted text={ev.text} tokens={factTokens(o?.claim || "")} /> : <span className="muted">{t("drawer.missing")}</span>}
        </blockquote>
        <dl className="drawer-meta">
          {p?.publisher && <div><dt>{t("drawer.publisher")}</dt><dd>{p.publisher}</dd></div>}
          {p?.published && <div><dt>{t("drawer.published")}</dt><dd>{String(p.published).slice(0, 10)}</dd></div>}
          {url && <div><dt>{t("drawer.source")}</dt><dd><a href={url} target="_blank" rel="noreferrer">{hostOf(url)}</a></dd></div>}
          {docId && <div><dt>{t("drawer.doc")}</dt><dd className="code">{docId}</dd></div>}
          {p?.block_id && <div><dt>{t("drawer.block")}</dt><dd className="code">{p.block_id}</dd></div>}
        </dl>
        {url && <a className="primary-btn" href={url} target="_blank" rel="noreferrer">{t("drawer.open")}<IconExternal size={14} /></a>}
      </aside>
    </>
  );
}

// ------------------------------------------------------------------ utils

function hostOf(url: string): string {
  try { return new URL(url).hostname.replace(/^www\./, ""); } catch { return ""; }
}

function fmtMs(ms?: number): string {
  if (ms == null || Number.isNaN(ms)) return "—";
  return ms >= 1000 ? `${(ms / 1000).toFixed(1)} s` : `${Math.round(ms)} ms`;
}
