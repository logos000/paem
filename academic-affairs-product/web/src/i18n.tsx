// UI strings (zh / en).  The chosen language is also sent with every ask so
// the LLM answers in it.
import { createContext, useContext, useEffect, useMemo, useState, type ReactNode } from "react";

export type Lang = "zh" | "en";

const zh = {
  "brand.name": "ZJUI 教务问答",
  "brand.sub": "Academic Evidence",
  "nav.newChat": "新对话",
  "nav.history": "对话记录",
  "nav.historyEmpty": "你提过的问题会保存在这里",
  "nav.removeConv": "从列表移除",
  "nav.untitled": "未命名对话",
  "nav.earlier": "更早的对话",
  "day.today": "今天", "day.yesterday": "昨天", "day.week": "近 7 天", "day.earlier": "更早",
  "console.entry": "控制台",
  "console.entrySub": "材料导入 · 运行状态",
  "console.title": "控制台",
  "console.back": "返回对话",
  "console.import": "材料导入",
  "console.status": "运行状态",
  "gate.title": "控制台口令",
  "gate.lede": "材料导入与知识库管理需要管理员口令；问答不受影响。",
  "gate.password": "口令",
  "gate.submit": "解锁",
  "gate.bad": "口令不正确，请重试。",
  "health.online": "知识库在线",
  "health.docs": "{n} 份文档",
  "health.offline": "服务未连接",
  "health.linking": "正在连接",
  "theme.toggle": "切换浅色 / 深色",
  "lang.label": "语言",

  "welcome.kicker": "ZJUI 教务证据问答",
  "welcome.title1": "每一个答案，",
  "welcome.title2": "都能回到原文。",
  "welcome.lede": "基于学院官方发布的校历、选课指南、课程与通知材料作答。每句话标注出处，证据冲突时并列呈现，证据不足时如实说明。",
  "welcome.try": "可以这样问",
  "welcome.principles": "工作方式",
  "p1.t": "逐句溯源", "p1.d": "正文中的编号对应具体证据段落，点开即可阅读原文、发布方与来源链接。",
  "p2.t": "冲突并列", "p2.d": "不同文件说法不一时，双方来源同时呈现，由你判断而非替你选择。",
  "p3.t": "诚实拒答", "p3.d": "学期未发布、对象不符或资料过时，系统会说明原因，不作推测。",

  "composer.placeholder": "问一个教务问题，例如退课截止、选课时间、课程先修…",
  "composer.placeholderMore": "继续提问（每个问题独立检索）",
  "composer.audience": "身份", "composer.audienceAny": "不限", "composer.ug": "本科生", "composer.grad": "研究生",
  "composer.term": "学期", "composer.termAuto": "自动识别",
  "composer.send": "发送", "composer.stop": "停止",
  "composer.hint": "Enter 发送 · Shift + Enter 换行",
  "composer.disclaimer": "回答仅依据已收录的官方材料，请以学院正式通知为准",
  "term.fall": "{y} 秋季", "term.spring": "{y} 春季",

  "step.optimizing_query": "理解问题", "step.retrieving": "检索证据", "step.generating": "生成回答", "step.validating": "核对引用",
  "dial.q": "提问", "dial.e": "证据", "dial.c": "结论", "dial.v": "核对",
  "progress.hits": "命中 {n} 段证据，来自 {d} 份原始材料",
  "progress.queries": "检索词",
  "progress.followup": "证据不足，正在补充检索",
  "await.search": "正在检索相关材料",
  "await.read": "正在研读 {n} 段证据",
  "live.writing": "正在生成，内容可能仍会调整",
  "live.verifying": "正在核对引用，结果尚未确认",

  "status.sufficient": "有据可依", "status.conflict": "证据存在分歧", "status.no_material": "未找到充分证据",
  "status.wrong_term": "所问学期暂无资料", "status.wrong_audience": "资料面向其他对象", "status.stale_or_uncertain": "资料可能已过时",
  "status.partial": "部分回答", "status.refused_evidence": "诚实拒答", "status.refused_policy": "策略拒答",
  "hint.text_guard": "所问的具体信息不在证据里，系统如实说明而不是猜测；可尝试更具体的问法或换一个学期间。",
  "d.answerStatus": "回答状态",
  "fb.title": "反馈审查",
  "fb.sub": "👎 的提问记录（问题 · 答案 · 引用）——差评可直接变成评测题",
  "fb.empty": "还没有 👎 反馈",
  "fb.rating": "评价",
  "fb.q": "问题",
  "fb.comment": "评语",
  "fb.answer": "回答",
  "fb.cites": "引用",
  "fb.noCites": "无引用",
  "hint.no_material": "知识库里还没有能直接回答这个问题的材料。可以换个更具体的说法，或在控制台中补充官方通知。",
  "hint.wrong_term": "找到的材料属于其他学期。尚未发布的学期安排，系统不会猜测。",
  "hint.wrong_audience": "找到的材料面向其他身份（本科生 / 研究生），可以切换身份后再问。",
  "hint.stale_or_uncertain": "相关材料可能已被更新版本取代，系统不愿用过时信息回答。请以学院最新发布为准。",
  "retry.ug": "以本科生身份再问", "retry.grad": "以研究生身份再问",
  "gen.validated": "引用已核对", "gen.validatedTip": "回答中的日期、数字、课程代码均能在所引原文中找到",
  "gen.unverified": "引用待人工核对", "gen.unverifiedTip": "回答未通过自动引用校验，请对照下方原始材料确认",
  "gen.fallback": "证据摘录模式", "gen.fallbackTip": "生成服务暂不可用，直接呈现证据原文",
  "gen.template": "证据摘录模式", "gen.templateTip": "直接由证据原文组织的回答",
  "conflict.note": "不同材料对此说法不一，下方并列列出了双方来源。请以学院最新正式发布为准。",
  "claims.title": "逐条依据",
  "act.copy": "复制", "act.copied": "已复制", "act.helpful": "有帮助", "act.unhelpful": "没解决问题", "act.thanks": "感谢反馈",
  "act.details": "技术详情", "act.hideDetails": "收起详情",
  "d.total": "总耗时", "d.retrieval": "检索", "d.generation": "生成", "d.provider": "生成方式", "d.build": "知识库版本", "d.request": "请求 ID", "d.decision": "决策说明", "d.validation": "校验提示",
  "err.title": "没能完成这次回答", "err.retry": "重试", "err.connect": "无法连接到问答服务，请确认启动器窗口仍在运行。",
  "err.interrupted": "连接意外中断，未收到完整回答。", "err.history": "无法读取这段对话的历史记录",
  "stopped": "已停止生成", "stopped.retry": "重新提问",
  "restored.tag": "历史记录", "restored.tip": "此浏览器没有这次回答的完整记录；重新提问可查看引用与原始材料",
  "drawer.label": "引用原文", "drawer.publisher": "发布方", "drawer.published": "发布日期", "drawer.source": "来源",
  "drawer.doc": "文档 ID", "drawer.block": "段落", "drawer.excerpt": "原文摘录", "drawer.missing": "此引用的原文片段不在本次证据包中，可通过来源链接查看。",
  "drawer.open": "打开原始文件", "drawer.close": "关闭", "cite.view": "查看原文",

  "imp.kicker": "知识库维护",
  "imp.title": "材料导入",
  "imp.lede": "上传官方附件或网页通知。系统自动完成入库、增量构建、发布门禁与原子切换；任何一步失败，正在服务的知识库都不受影响。",
  "imp.sumPublished": "已发布", "imp.sumRunning": "处理中", "imp.sumFailed": "失败",
  "imp.step1": "选择文件", "imp.step2": "填写信息", "imp.step3": "提交发布",
  "imp.drop": "拖放文件到这里，或点击选择", "imp.dropSub": "PDF · Word · Excel 附件，或保存的网页通知 HTML", "imp.remove": "移除文件",
  "imp.kind": "材料类型", "imp.kindDoc": "附件文档", "imp.kindPage": "网页通知",
  "imp.site": "来源站点", "imp.date": "发布日期", "imp.optional": "选填", "imp.required": "必填",
  "imp.titleField": "标题", "imp.titlePh": "如：2026 秋季学期校历",
  "imp.url": "来源 URL", "imp.urlHelp": "回答中的「打开原文」将指向这个地址",
  "imp.needFile": "请先选择文件", "imp.needTitle": "网页通知需要填写标题", "imp.needUrl": "网页通知需要填写来源 URL",
  "imp.submit": "导入并自动发布", "imp.uploading": "正在上传…",
  "imp.ok": "已提交（任务 {id}）。系统将自动入库、构建、过门禁并切换，通常需要 2–3 分钟。", "imp.fail": "提交失败：{e}",
  "imp.pipeline": "发布流水线", "imp.live": "每 4 秒刷新", "imp.offline": "暂时读不到导入看门狗的状态，它可能仍在启动。",
  "imp.empty": "还没有导入记录", "imp.emptySub": "提交的材料会在这里显示实时进度",
  "imp.job": "任务", "imp.switched": "已切换到知识库", "imp.unknown": "未知原因", "imp.safe": "旧知识库未受影响",
  "imp.serving": "当前服务：", "imp.historyJob": "{ts} 的历史任务，当时曾切换到",
  "track.queued": "已提交", "track.received": "入库校验", "track.placed": "构建与门禁", "track.published": "已生效",
  "site.sharepoint": "SharePoint 文档库", "site.zjui": "ZJUI 官网", "site.ugrs": "本科生院", "site.grs": "研究生院", "site.zju": "浙大主站", "site.news": "新闻网", "site.libweb": "图书馆",

  "st.kicker": "系统概览",
  "st.title": "运行状态",
  "st.lede": "服务指标、知识库版本与失败材料。数据每 5 秒自动刷新。",
  "st.refresh": "刷新",
  "st.requests": "请求", "st.errors": "错误 {n}", "st.latency": "延迟中位数", "st.latencySub": "p95 {a} · p99 {b}",
  "st.llm": "生成服务", "st.llmOk": "正常", "st.llmOpen": "已熔断", "st.llmOkSub": "连续失败 {n} 次", "st.llmOpenSub": "以证据摘录模式回答",
  "st.feedback": "用户反馈", "st.feedbackSub": "有帮助 / 未解决",
  "st.decisions": "回答决策", "st.generation": "生成方式", "st.noData": "暂无数据",
  "st.builds": "知识库版本", "st.arm": "检索配置 {a}",
  "st.serving": "服务中", "st.ready": "就绪", "st.loading": "加载中", "st.error": "错误",
  "st.smoke": "冒烟测试", "st.makeCurrent": "设为当前", "st.noBuilds": "暂无可用版本信息",
  "st.loadPh": "加载新版本目录，如 data/corpus/<build_id>", "st.load": "加载", "st.rollback": "回滚上一版",
  "st.confirmSwitch": "确认将线上知识库切换为 {k}？", "st.confirmRollback": "确认回滚到上一个知识库版本？",
  "st.failedOp": "{l} 失败：{e}",
  "st.rejected": "失败材料", "st.rejectedSub": "本版本共拒收 {n} 份，需人工处置",
  "st.colSource": "来源", "st.colReason": "原因", "st.colPath": "路径", "st.queueEmpty": "队列为空",
};

type Key = keyof typeof zh;

const en: Record<Key, string> = {
  "brand.name": "ZJUI Academic Q&A",
  "brand.sub": "Academic Evidence",
  "nav.newChat": "New chat",
  "nav.history": "Conversations",
  "nav.historyEmpty": "Your questions will be kept here",
  "nav.removeConv": "Remove from list",
  "nav.untitled": "Untitled",
  "nav.earlier": "Earlier conversation",
  "day.today": "Today", "day.yesterday": "Yesterday", "day.week": "Past 7 days", "day.earlier": "Earlier",
  "console.entry": "Console",
  "console.entrySub": "Intake · System status",
  "console.title": "Console",
  "console.back": "Back to chat",
  "console.import": "Material intake",
  "console.status": "System status",
  "gate.title": "Console password",
  "gate.lede": "Material intake and knowledge-base management need the admin password; Q&A is unaffected.",
  "gate.password": "Password",
  "gate.submit": "Unlock",
  "gate.bad": "Wrong password — try again.",
  "health.online": "Knowledge base online",
  "health.docs": "{n} documents",
  "health.offline": "Service unreachable",
  "health.linking": "Connecting",
  "theme.toggle": "Toggle light / dark",
  "lang.label": "Language",

  "welcome.kicker": "ZJUI Academic Evidence",
  "welcome.title1": "Every answer,",
  "welcome.title2": "traceable to its source.",
  "welcome.lede": "Answers draw only on the institute's official calendars, enrollment guides, course listings and notices. Each statement is cited; conflicting sources are shown side by side; when evidence is insufficient, it says so.",
  "welcome.try": "Try asking",
  "welcome.principles": "How it works",
  "p1.t": "Sentence-level citations", "p1.d": "Each marker points to a specific passage — open it to read the original text, publisher and source link.",
  "p2.t": "Conflicts, side by side", "p2.d": "When documents disagree, both sources are presented so you can judge for yourself.",
  "p3.t": "Honest abstention", "p3.d": "Unpublished terms, the wrong audience or outdated material — it explains why rather than guessing.",

  "composer.placeholder": "Ask an academic question — withdrawal deadlines, enrollment times, prerequisites…",
  "composer.placeholderMore": "Ask another question (each is retrieved independently)",
  "composer.audience": "Role", "composer.audienceAny": "Any", "composer.ug": "Undergraduate", "composer.grad": "Graduate",
  "composer.term": "Term", "composer.termAuto": "Auto-detect",
  "composer.send": "Send", "composer.stop": "Stop",
  "composer.hint": "Enter to send · Shift + Enter for a new line",
  "composer.disclaimer": "Answers rely only on indexed official material — official notices prevail",
  "term.fall": "Fall {y}", "term.spring": "Spring {y}",

  "step.optimizing_query": "Understanding", "step.retrieving": "Retrieving", "step.generating": "Composing", "step.validating": "Verifying",
  "dial.q": "QUERY", "dial.e": "EVIDENCE", "dial.c": "CLAIM", "dial.v": "VERIFY",
  "progress.hits": "{n} passages from {d} source documents",
  "progress.queries": "Queries",
  "progress.followup": "Evidence thin — running a follow-up search",
  "await.search": "Searching the knowledge base",
  "await.read": "Reading {n} passages of evidence",
  "live.writing": "Composing — wording may still change",
  "live.verifying": "Verifying citations — not yet confirmed",

  "status.sufficient": "Grounded", "status.conflict": "Sources disagree", "status.no_material": "Insufficient evidence",
  "status.wrong_term": "No material for that term", "status.wrong_audience": "Material targets another audience", "status.stale_or_uncertain": "Material may be outdated",
  "status.partial": "Partially answered", "status.refused_evidence": "Honest refusal", "status.refused_policy": "Policy refusal",
  "hint.text_guard": "The asked fact is not in the evidence, and the system says so rather than guessing. Try a more specific phrasing or another term.",
  "d.answerStatus": "Answer status",
  "fb.title": "Feedback review",
  "fb.sub": "👎 rated asks (question · answer · citations) — a rating can become an eval question",
  "fb.empty": "No 👎 feedback yet",
  "fb.rating": "Rating",
  "fb.q": "Question",
  "fb.comment": "Comment",
  "fb.answer": "Answer",
  "fb.cites": "Citations",
  "fb.noCites": "no citations",
  "hint.no_material": "Nothing in the knowledge base answers this directly. Try a more specific phrasing, or add the official notice via the Console.",
  "hint.wrong_term": "The material found belongs to another term. Schedules that are not yet published are never guessed.",
  "hint.wrong_audience": "The material found targets another audience (undergraduate / graduate). Switch role and ask again.",
  "hint.stale_or_uncertain": "The relevant material may have been superseded, so it is not used. Please check the latest official notice.",
  "retry.ug": "Ask again as undergraduate", "retry.grad": "Ask again as graduate",
  "gen.validated": "Citations verified", "gen.validatedTip": "Every date, number and course code in the answer appears in the cited text",
  "gen.unverified": "Citations need review", "gen.unverifiedTip": "The answer did not pass automatic citation checks — compare with the source material below",
  "gen.fallback": "Evidence excerpt mode", "gen.fallbackTip": "Generation is unavailable; the evidence is shown directly",
  "gen.template": "Evidence excerpt mode", "gen.templateTip": "Answer assembled directly from the evidence text",
  "conflict.note": "Sources disagree on this; both are listed below. The latest official notice prevails.",
  "claims.title": "Claim by claim",
  "act.copy": "Copy", "act.copied": "Copied", "act.helpful": "Helpful", "act.unhelpful": "Not helpful", "act.thanks": "Thanks for the feedback",
  "act.details": "Details", "act.hideDetails": "Hide details",
  "d.total": "Total", "d.retrieval": "Retrieval", "d.generation": "Generation", "d.provider": "Provider", "d.build": "Build", "d.request": "Request ID", "d.decision": "Decision", "d.validation": "Validation",
  "err.title": "This answer could not be completed", "err.retry": "Retry", "err.connect": "Cannot reach the service — is the launcher window still running?",
  "err.interrupted": "The connection dropped before the answer finished.", "err.history": "Could not load this conversation's history",
  "stopped": "Generation stopped", "stopped.retry": "Ask again",
  "restored.tag": "From history", "restored.tip": "This browser has no full record of this answer; ask again to see citations and sources",
  "drawer.label": "Cited passage", "drawer.publisher": "Publisher", "drawer.published": "Published", "drawer.source": "Source",
  "drawer.doc": "Document", "drawer.block": "Block", "drawer.excerpt": "Excerpt", "drawer.missing": "This passage is not in the current evidence pack — use the source link instead.",
  "drawer.open": "Open original file", "drawer.close": "Close", "cite.view": "View source",

  "imp.kicker": "Knowledge base",
  "imp.title": "Material intake",
  "imp.lede": "Upload official attachments or web notices. Ingestion, incremental build, release gate and atomic switch run automatically; if any step fails, the live knowledge base is untouched.",
  "imp.sumPublished": "Published", "imp.sumRunning": "In progress", "imp.sumFailed": "Failed",
  "imp.step1": "Choose file", "imp.step2": "Describe", "imp.step3": "Publish",
  "imp.drop": "Drop a file here, or click to browse", "imp.dropSub": "PDF · Word · Excel attachments, or a saved web notice (HTML)", "imp.remove": "Remove file",
  "imp.kind": "Type", "imp.kindDoc": "Attachment", "imp.kindPage": "Web notice",
  "imp.site": "Source site", "imp.date": "Published on", "imp.optional": "optional", "imp.required": "required",
  "imp.titleField": "Title", "imp.titlePh": "e.g. Fall 2026 Academic Calendar",
  "imp.url": "Source URL", "imp.urlHelp": "\"Open\" links in answers will point here",
  "imp.needFile": "Choose a file first", "imp.needTitle": "Web notices need a title", "imp.needUrl": "Web notices need a source URL",
  "imp.submit": "Import and publish", "imp.uploading": "Uploading…",
  "imp.ok": "Submitted (job {id}). Ingest, build, gate and switch will run automatically — usually 2–3 minutes.", "imp.fail": "Submission failed: {e}",
  "imp.pipeline": "Release pipeline", "imp.live": "Refreshes every 4 s", "imp.offline": "The intake watcher is not reporting yet — it may still be starting.",
  "imp.empty": "No imports yet", "imp.emptySub": "Submitted material will show live progress here",
  "imp.job": "Job", "imp.switched": "Switched to build", "imp.unknown": "Unknown reason", "imp.safe": "live knowledge base unaffected",
  "imp.serving": "Now serving:", "imp.historyJob": "Historical job from {ts}; had switched to",
  "track.queued": "Submitted", "track.received": "Ingest check", "track.placed": "Build & gate", "track.published": "Live",
  "site.sharepoint": "SharePoint library", "site.zjui": "ZJUI website", "site.ugrs": "Undergraduate School", "site.grs": "Graduate School", "site.zju": "ZJU main site", "site.news": "ZJU News", "site.libweb": "Library",

  "st.kicker": "Overview",
  "st.title": "System status",
  "st.lede": "Service metrics, knowledge-base builds and rejected material. Refreshes every 5 seconds.",
  "st.refresh": "Refresh",
  "st.requests": "Requests", "st.errors": "{n} errors", "st.latency": "Median latency", "st.latencySub": "p95 {a} · p99 {b}",
  "st.llm": "Generation", "st.llmOk": "Healthy", "st.llmOpen": "Tripped", "st.llmOkSub": "{n} consecutive failures", "st.llmOpenSub": "Answering in excerpt mode",
  "st.feedback": "Feedback", "st.feedbackSub": "helpful / not helpful",
  "st.decisions": "Answer decisions", "st.generation": "Generation modes", "st.noData": "No data yet",
  "st.builds": "Knowledge-base builds", "st.arm": "Retrieval arm {a}",
  "st.serving": "Serving", "st.ready": "Ready", "st.loading": "Loading", "st.error": "Error",
  "st.smoke": "Smoke test", "st.makeCurrent": "Make current", "st.noBuilds": "No build information",
  "st.loadPh": "Load a build directory, e.g. data/corpus/<build_id>", "st.load": "Load", "st.rollback": "Roll back",
  "st.confirmSwitch": "Switch the live knowledge base to {k}?", "st.confirmRollback": "Roll back to the previous knowledge-base build?",
  "st.failedOp": "{l} failed: {e}",
  "st.rejected": "Rejected material", "st.rejectedSub": "{n} rejected in this build — needs manual review",
  "st.colSource": "Source", "st.colReason": "Reason", "st.colPath": "Path", "st.queueEmpty": "Queue is empty",
};

const TABLE: Record<Lang, Record<Key, string>> = { zh, en };

export type T = (key: Key, vars?: Record<string, string | number>) => string;

interface Ctx { lang: Lang; setLang: (l: Lang) => void; t: T }
const I18n = createContext<Ctx | null>(null);

function initialLang(): Lang {
  try {
    const saved = localStorage.getItem("lang");
    if (saved === "zh" || saved === "en") return saved;
  } catch { /* storage off */ }
  return navigator.language?.toLowerCase().startsWith("zh") ? "zh" : "en";
}

export function I18nProvider({ children }: { children: ReactNode }) {
  const [lang, setLang] = useState<Lang>(initialLang);
  useEffect(() => {
    document.documentElement.lang = lang === "zh" ? "zh-CN" : "en";
    try { localStorage.setItem("lang", lang); } catch { /* storage off */ }
  }, [lang]);
  const value = useMemo<Ctx>(() => ({
    lang, setLang,
    t: (key, vars) => {
      let s = TABLE[lang][key] ?? zh[key] ?? key;
      if (vars) for (const [k, v] of Object.entries(vars)) s = s.replace(`{${k}}`, String(v));
      return s;
    },
  }), [lang]);
  return <I18n.Provider value={value}>{children}</I18n.Provider>;
}

export function useI18n(): Ctx {
  const ctx = useContext(I18n);
  if (!ctx) throw new Error("useI18n outside I18nProvider");
  return ctx;
}
