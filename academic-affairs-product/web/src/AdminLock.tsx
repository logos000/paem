import { useState } from "react";
import { useI18n } from "./i18n";
import { adminAuth, api } from "./api";

/** Password card shown when an owner-only surface answers 401.  Browsers
 *  don't raise the native Basic-auth dialog for fetch(), so the console
 *  collects the password here once per browser session, verifies it against
 *  /admin/overview and hands control back to the page. */
export default function AdminLock({ onUnlock }: { onUnlock: () => void }) {
  const { t } = useI18n();
  const [password, setPassword] = useState("");
  const [state, setState] = useState<"idle" | "busy" | "bad">("idle");

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    if (!password || state === "busy") return;
    setState("busy");
    adminAuth.set(password);
    try {
      await api.overview(); // cheapest gated probe
      onUnlock();
    } catch {
      adminAuth.clear();
      setState("bad");
    }
  }

  return (
    <section className="card lock-card">
      <div className="card-head"><h2>{t("gate.title")}</h2></div>
      <p className="muted">{t("gate.lede")}</p>
      <form className="lock-form" onSubmit={submit}>
        <input
          type="password"
          value={password}
          placeholder={t("gate.password")}
          onChange={(e) => { setPassword(e.target.value); setState("idle"); }}
          autoFocus
        />
        <button className="primary-btn" disabled={!password || state === "busy"}>{t("gate.submit")}</button>
      </form>
      {state === "bad" && <p className="error-text">{t("gate.bad")}</p>}
    </section>
  );
}
