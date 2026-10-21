// Shared API client + SSE reader for the product backend.

/** Full answers (citations + evidence pack) keyed by request_id.  The server's
 *  session history keeps only answer text, so this is what lets a reopened
 *  conversation still show its sources.  Best-effort localStorage, LRU-capped. */
const CACHE_KEY = "answer_cache_v1";
const CACHE_MAX = 40;
const memoryCache = new Map<string, AskResult>();

function readCache(): Array<[string, AskResult]> {
  try { return JSON.parse(localStorage.getItem(CACHE_KEY) || "[]"); } catch { return []; }
}

export const answerCache = {
  get(requestId?: string): AskResult | undefined {
    if (!requestId) return undefined;
    if (memoryCache.has(requestId)) return memoryCache.get(requestId);
    const hit = readCache().find(([id]) => id === requestId)?.[1];
    if (hit) memoryCache.set(requestId, hit);
    return hit;
  },
  put(result: AskResult) {
    if (!result.request_id) return;
    memoryCache.set(result.request_id, result);
    let entries = [[result.request_id, result] as [string, AskResult], ...readCache().filter(([id]) => id !== result.request_id)].slice(0, CACHE_MAX);
    // Evidence packs can be large: drop the oldest entries until it fits the quota.
    while (entries.length) {
      try { localStorage.setItem(CACHE_KEY, JSON.stringify(entries)); return; } catch { entries = entries.slice(0, -1); }
    }
  },
};

export interface Citation { id: string; title: string; source_url: string; doc_uid: string; block_id?: string }
export interface Claim { text: string; evidence_ids: string[]; sources: Citation[] }
export interface Decision { status: string; abstain: boolean; reason: string }
export interface Generation { mode: string; validated: boolean; problems?: string[]; schema_version?: number }
export interface EvidenceItem {
  evidence_id: string; doc_id: string; title: string; text: string; blocking: boolean;
  provenance: { source_url?: string; block_id?: string; doc_uid?: string; publisher?: string; published?: string; modified?: string; site?: string };
}
export interface RetrievedDoc { title: string; doc_uid: string; source_url?: string; publisher?: string; published?: string }
export interface AskResult {
  ok: boolean; request_id: string; build_id: string; config: string;
  /** Public AnswerStatus (answered/partial/refused_evidence/…) — optional
   *  because results cached in localStorage before 2026-10-17 lack it. */
  status?: string; answer_disposition?: string;
  decision: Decision; query_ir: Record<string, unknown>;
  answer: string; provider: string; claims: Claim[]; generation: Generation;
  citation_map?: Record<string, string>;
  citations: Citation[]; evidence_pack: { evidence: EvidenceItem[]; usable_evidence_ids: string[] };
  timing: { retrieval_ms: number; builder_ms: number; llm_ms: number; total_ms: number };
}
export interface Turn {
  request_id?: string; ts?: string; question: string; answer?: string; provider?: string; build_id?: string;
  decision?: Decision; status?: string;
}
export interface Health { ok: boolean; arm?: string; llm_enabled?: boolean; runtime?: { build_id?: string; n_documents?: number; n_units?: number } }

async function json<T>(url: string, init?: RequestInit, admin = false): Promise<T> {
  const response = await fetch(url, {
    ...init,
    headers: { "Content-Type": "application/json", ...(admin ? adminAuth.header() : {}), ...(init?.headers || {}) },
  });
  if (!response.ok) throw new ApiError(response.status, `${response.status}: ${(await response.text()).slice(0, 200)}`);
  return response.json() as Promise<T>;
}

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) { super(message); this.status = status; }
}

const ADMIN_AUTH_KEY = "admin_auth_v1";

/** Basic-auth credentials for owner-only surfaces (/admin/*, /v1/materials).
 *  The server gate 401s those paths, but browsers never raise the native
 *  Basic dialog for fetch() — the console renders a password card instead and
 *  stores the accepted value here for the rest of the browser session. */
export const adminAuth = {
  set(password: string) { sessionStorage.setItem(ADMIN_AUTH_KEY, btoa(`admin:${password}`)); },
  clear() { sessionStorage.removeItem(ADMIN_AUTH_KEY); },
  header(): Record<string, string> {
    const raw = sessionStorage.getItem(ADMIN_AUTH_KEY);
    return raw ? { Authorization: `Basic ${raw}` } : {};
  },
};

export const api = {
  health: () => json<Health>("/health"),
  newSession: () => json<{ session_id: string }>("/v1/session", { method: "POST" }),
  history: (id: string) => json<{ turns: Turn[] }>(`/v1/session/${id}`),
  feedback: (request_id: string, helpful: boolean, comment = "") =>
    json<{ ok: boolean }>("/v1/feedback", { method: "POST", body: JSON.stringify({ request_id, helpful, comment }) }),
  overview: () => json<Record<string, any>>("/admin/overview", undefined, true),
  builds: () => json<Record<string, any>>("/admin/builds", undefined, true),
  adminPost: (path: string, body: unknown = {}) => json<Record<string, any>>(path, { method: "POST", body: JSON.stringify(body) }, true),
  rejected: (limit = 30) => json<Record<string, any>>(`/admin/rejected?limit=${limit}`, undefined, true),
  feedbackReview: (limit = 50) => json<Record<string, any>>(`/admin/feedback?limit=${limit}`, undefined, true),
  materials: () => json<{ items: unknown[] }>("/v1/materials", undefined, true),
  uploadMaterials: (form: FormData) =>
    fetch("/v1/materials", { method: "POST", body: form, headers: adminAuth.header() }),
};

export interface AskBody { question: string; today?: string; user_context?: Record<string, unknown>; session_id?: string; language?: "zh" | "en"; context_question?: string }

/** POST /v1/ask/stream and dispatch SSE events until result/error. */
export interface StreamHooks {
  stage?: (s: string) => void;
  queryOptimized?: (d: { queries: string[] }) => void;
  retrieved?: (d: { documents: RetrievedDoc[]; n_evidence: number; decision: unknown }) => void;
  followUp?: (q: { queries: string[] }) => void;
  answerDelta?: (t: string) => void;
  result?: (r: AskResult) => void;
  error?: (e: string) => void;
}

export async function askStream(body: AskBody, hooks: StreamHooks, signal?: AbortSignal) {
  const response = await fetch("/v1/ask/stream", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body), signal,
  });
  if (!response.ok || !response.body) {
    hooks.error?.(`${response.status}: ${(await response.text()).slice(0, 200)}`);
    return;
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let boundary: number;
    while ((boundary = buffer.indexOf("\n\n")) >= 0) {
      const frame = buffer.slice(0, boundary);
      buffer = buffer.slice(boundary + 2);
      const line = frame.split("\n").find((l) => l.startsWith("data: "));
      if (!line) continue;
      const event = JSON.parse(line.slice(6));
      switch (event.type) {
        case "stage": hooks.stage?.(event.name); break;
        case "query_optimized": hooks.queryOptimized?.(event); break;
        case "retrieved": hooks.retrieved?.(event); break;
        case "follow_up": hooks.followUp?.(event); break;
        case "answer_delta": hooks.answerDelta?.(event.text || ""); break;
        case "result": hooks.result?.(event.payload as AskResult); break;
        case "error": hooks.error?.(event.error || event.detail || `error ${event.status}`); break;
      }
    }
  }
}
