import { useCallback, useEffect, useState } from "react";
import { AlertTriangle, BellRing, Check, Download, Flame, Lock, Play, ShieldCheck, Trash2, X } from "lucide-react";
import {
  api,
  api2,
  download,
  incidentMedia,
  setPersonSession,
  type AlarmOut,
  type AlarmRuleRow,
  type IncidentRow,
  type Person,
  type Severity,
  type StorageStatus,
} from "./api";
import { ErrorNote, Screen, Spinner } from "./ui";

/* Phase 3 screens: sign-in (PIN), Incidents (§33–34, §58–59), Guard Mode
   (§61–62), Alarms (§64–65), Settings (storage, retention, people, location). */

// ── who is signed in ──────────────────────────────────────────────────
export function usePerson() {
  const [person, setPerson] = useState<Person | null>(null);
  const refresh = useCallback(async () => {
    const r = await api.get<{ user: Person | null }>("/auth/me");
    setPerson(r.user);
  }, []);
  useEffect(() => {
    refresh().catch(() => undefined);
  }, [refresh]);
  return { person, refresh, setPerson };
}

export function SignInBar({ person, onChange }: { person: Person | null; onChange: () => void }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="flex flex-wrap items-center justify-between gap-3 border-b border-slate-200 bg-slate-50 px-4 py-2 text-sm text-guard-ink sm:px-6">
      {person ? (
        <span>
          Signed in: <b>{person.name}</b> · {person.role.toLowerCase()}
        </span>
      ) : (
        <span>Not signed in. Sign in to review incidents.</span>
      )}
      {person ? (
        <button
          type="button"
          className="font-semibold underline"
          onClick={async () => {
            await api.post("/auth/signout");
            setPersonSession(null);
            onChange();
          }}
        >
          Sign out
        </button>
      ) : (
        <button type="button" className="btn-dark min-h-[36px] px-3" onClick={() => setOpen(true)}>
          <Lock className="h-4 w-4" /> Sign in with PIN
        </button>
      )}
      {open && (
        <PinDialog
          onClose={() => setOpen(false)}
          onDone={() => {
            setOpen(false);
            onChange();
          }}
        />
      )}
    </div>
  );
}

function PinDialog({ onClose, onDone }: { onClose: () => void; onDone: () => void }) {
  const [users, setUsers] = useState<Person[]>([]);
  const [hasOwner, setHasOwner] = useState(true);
  const [who, setWho] = useState<string>("");
  const [pin, setPin] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [ownerName, setOwnerName] = useState("");
  useEffect(() => {
    api.get<{ users: Person[]; has_owner: boolean }>("/auth/users").then((r) => {
      setUsers(r.users);
      setHasOwner(r.has_owner);
      setWho(r.users[0]?.id ?? "");
    });
  }, []);
  async function go() {
    setBusy(true);
    setErr(null);
    try {
      if (!hasOwner) {
        const u = await api.post<Person>("/users", { name: ownerName, role: "OWNER", pin });
        const s = await api.post<{ session: string }>("/auth/signin", { user_id: u.id, pin });
        setPersonSession(s.session);
      } else {
        const s = await api.post<{ session: string }>("/auth/signin", { user_id: who, pin });
        setPersonSession(s.session);
      }
      onDone();
    } catch (e) {
      setErr((e as Error).message);
      setPin("");
    } finally {
      setBusy(false);
    }
  }
  return (
    <div className="fixed inset-0 z-50 flex items-end justify-center bg-black/50 sm:items-center" role="dialog" aria-modal="true" aria-label="Sign in">
      <div className="w-full max-w-sm bg-white p-5">
        <div className="mb-4 flex items-center justify-between">
          <h2 className="text-lg font-bold text-guard-ink">{hasOwner ? "Sign in" : "Add the shop owner"}</h2>
          <button type="button" onClick={onClose} aria-label="Close" className="p-2 text-slate-700">
            <X className="h-5 w-5" />
          </button>
        </div>
        {hasOwner ? (
          <div className="mb-3">
            <label className="label" htmlFor="who">Who are you?</label>
            <select id="who" className="field" value={who} onChange={(e) => setWho(e.target.value)}>
              {users.map((u) => (
                <option key={u.id} value={u.id}>
                  {u.name} ({u.role.toLowerCase()})
                </option>
              ))}
            </select>
          </div>
        ) : (
          <div className="mb-3">
            <p className="mb-2 text-sm text-slate-700">Nobody has been set up yet. Add the owner first; they can add managers and security staff.</p>
            <label className="label" htmlFor="on">Owner's name</label>
            <input id="on" className="field" value={ownerName} onChange={(e) => setOwnerName(e.target.value)} />
          </div>
        )}
        <label className="label" htmlFor="pin">{hasOwner ? "PIN" : "Choose a 4–6 digit PIN"}</label>
        <input
          id="pin"
          className="field text-center text-2xl tracking-[0.5em]"
          type="password"
          inputMode="numeric"
          autoComplete="off"
          maxLength={6}
          value={pin}
          onChange={(e) => setPin(e.target.value.replace(/\D/g, ""))}
          onKeyDown={(e) => e.key === "Enter" && pin.length >= 4 && go()}
        />
        <div className="mt-3 grid grid-cols-3 gap-2">
          {["1", "2", "3", "4", "5", "6", "7", "8", "9", "", "0", "⌫"].map((k) =>
            k ? (
              <button
                key={k}
                type="button"
                className="btn-outline min-h-[52px] text-lg"
                onClick={() => setPin((p) => (k === "⌫" ? p.slice(0, -1) : (p + k).slice(0, 6)))}
              >
                {k}
              </button>
            ) : (
              <span key="blank" />
            ),
          )}
        </div>
        <div className="mt-3">
          <ErrorNote message={err} />
        </div>
        <button type="button" className="btn-primary mt-3 w-full" disabled={busy || pin.length < 4 || (!hasOwner && !ownerName.trim())} onClick={go}>
          {busy ? <Spinner /> : hasOwner ? "Sign in" : "Add owner and sign in"}
        </button>
      </div>
    </div>
  );
}

// ── shared bits ───────────────────────────────────────────────────────
const SEV_STYLE: Record<Severity, string> = {
  CRITICAL: "bg-red-700 text-white",
  HIGH: "bg-guard-500 text-guard-ink",
  LOW: "bg-slate-200 text-guard-ink",
  INFO: "border border-slate-300 bg-white text-guard-ink",
};
export function SevBadge({ s }: { s: Severity }) {
  return <span className={`inline-block px-2 py-0.5 text-xs font-bold ${SEV_STYLE[s]}`}>{s}</span>;
}
const STATUS_TEXT: Record<string, string> = {
  UNREVIEWED: "Unreviewed", ACKNOWLEDGED: "Acknowledged", CONFIRMED: "Confirmed", FALSE_ALERT: "False alert",
  RESOLVED: "Resolved", ESCALATED: "Escalated", ARCHIVED: "Archived",
};
const EVENT_TEXT: Record<string, string> = {
  PERSON_DETECTED: "Person detected", ZONE_ENTRY: "Entered a zone", SHELF_INTERACTION: "Shelf interaction",
  UNRESOLVED_SHELF_INTERACTION: "Interaction unresolved", EXIT_APPROACH: "Exit entered",
  POSSIBLE_UNPAID_EXIT: "Incident triggered", POSSIBLE_CONCEALMENT: "Possible concealment (experimental)",
  RESTRICTED_ZONE_ENTRY: "Entered restricted area", AFTER_HOURS_PERSON: "Person after hours",
  POSSIBLE_FIRE: "Possible fire", POSSIBLE_SMOKE: "Possible smoke", ZONE_EXIT: "Left a zone",
};
const time = (s: string) => new Date(s).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });

function Thumb({ inc, big = false }: { inc: IncidentRow; big?: boolean }) {
  const [src, setSrc] = useState<string | null>(null);
  useEffect(() => {
    if (!inc.has_snapshot) return;
    incidentMedia(inc.id, big ? "snapshot" : "thumbnail").then(setSrc).catch(() => undefined);
  }, [inc.id, inc.has_snapshot, big]);
  return (
    <div className="flex aspect-video w-full items-center justify-center overflow-hidden bg-guard-ink text-xs text-white/70">
      {src ? <img src={src} alt="" className="h-full w-full object-cover" /> : inc.media_status === "PENDING" ? "Saving video…" : "No picture"}
    </div>
  );
}

function IncidentCard({ inc, onOpen }: { inc: IncidentRow; onOpen: () => void }) {
  const fire = inc.incident_type === "POSSIBLE_FIRE" || inc.incident_type === "POSSIBLE_SMOKE";
  return (
    <li className={`card overflow-hidden ${fire ? "border-2 border-red-700" : ""}`}>
      <Thumb inc={inc} />
      <div className="space-y-1 p-3">
        <div className="flex items-center justify-between gap-2">
          <SevBadge s={inc.severity} />
          <span className="text-xs font-semibold text-slate-700">{STATUS_TEXT[inc.status] ?? inc.status}</span>
        </div>
        <p className="flex items-center gap-1.5 font-bold text-guard-ink">
          {fire && <Flame className="h-4 w-4 text-red-700" aria-hidden />}
          {inc.title}
        </p>
        <p className="text-sm text-slate-700">
          {inc.camera} · {time(inc.occurred_at)}
          {inc.clip_duration_seconds ? ` · ${Math.round(inc.clip_duration_seconds)} sec clip` : ""}
        </p>
        {fire && inc.alarm_state === "TRIGGERED" && <p className="text-xs font-semibold text-red-800">Local alarm activated</p>}
        {inc.keep_evidence && <p className="text-xs font-semibold text-guard-ink">Evidence retained</p>}
        <button type="button" className="btn-dark mt-2 w-full" onClick={onOpen}>
          View incident
        </button>
      </div>
    </li>
  );
}

// ── Incidents ─────────────────────────────────────────────────────────
export function IncidentsView({ person, initialOpen = null }: { person: Person | null; initialOpen?: string | null }) {
  const [rows, setRows] = useState<IncidentRow[]>([]);
  const [reasons, setReasons] = useState<Record<string, string>>({});
  const [f, setF] = useState({ period: "7d", severity: "", status: "", q: "", kept: "" });
  // A tray pop-up opens straight onto its incident.
  const [open, setOpen] = useState<string | null>(initialOpen);
  const [err, setErr] = useState<string | null>(null);
  const load = useCallback(async () => {
    const p = new URLSearchParams();
    if (f.period) p.set("period", f.period);
    if (f.severity) p.set("severity", f.severity);
    if (f.status) p.set("status", f.status);
    if (f.q) p.set("q", f.q);
    if (f.kept) p.set("kept", "true");
    try {
      const r = await api.get<{ incidents: IncidentRow[]; false_alert_reasons: Record<string, string> }>(`/incidents?${p}`);
      setRows(r.incidents);
      setReasons(r.false_alert_reasons);
      setErr(null);
    } catch (e) {
      setErr((e as Error).message);
    }
  }, [f]);
  useEffect(() => {
    void load();
    const t = window.setInterval(load, 5000);
    return () => window.clearInterval(t);
  }, [load]);
  const set = (k: keyof typeof f) => (e: React.ChangeEvent<HTMLInputElement | HTMLSelectElement>) => setF({ ...f, [k]: e.target.value });
  return (
    <Screen title="Incidents" lead="Things Guard thinks a person should look at. Nothing here is proof of theft until someone reviews it.">
      <div className="mb-4 grid gap-2 sm:grid-cols-5">
        <select aria-label="Period" className="field" value={f.period} onChange={set("period")}>
          <option value="today">Today</option>
          <option value="yesterday">Yesterday</option>
          <option value="7d">Last 7 days</option>
          <option value="">All</option>
        </select>
        <select aria-label="Severity" className="field" value={f.severity} onChange={set("severity")}>
          <option value="">Any severity</option>
          <option value="CRITICAL">Critical</option>
          <option value="HIGH">High</option>
          <option value="LOW">Low</option>
        </select>
        <select aria-label="Status" className="field" value={f.status} onChange={set("status")}>
          <option value="">Any status</option>
          {Object.entries(STATUS_TEXT).filter(([k]) => k !== "ARCHIVED").map(([k, v]) => (
            <option key={k} value={k}>{v}</option>
          ))}
        </select>
        <select aria-label="Kept evidence" className="field" value={f.kept} onChange={set("kept")}>
          <option value="">All incidents</option>
          <option value="1">Kept evidence only</option>
        </select>
        <input aria-label="Search" className="field" placeholder="Search ID, camera, reviewer…" value={f.q} onChange={set("q")} />
      </div>
      <ErrorNote message={err} />
      <ul className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
        {rows.map((i) => (
          <IncidentCard key={i.id} inc={i} onOpen={() => setOpen(i.id)} />
        ))}
      </ul>
      {!rows.length && !err && <p className="text-sm text-slate-700">No incidents in this period.</p>}
      {open && <IncidentDetail id={open} person={person} reasons={reasons} onClose={() => { setOpen(null); void load(); }} />}
    </Screen>
  );
}

const CAN: Record<string, string[]> = {
  OWNER: ["acknowledge", "confirm", "false_alert", "resolve", "escalate", "reopen", "keep", "export", "delete"],
  MANAGER: ["acknowledge", "confirm", "false_alert", "resolve", "escalate", "reopen", "keep", "export"],
  SECURITY: ["acknowledge", "confirm", "false_alert", "escalate"],
};

export function IncidentDetail({ id, person, reasons, onClose }: { id: string; person: Person | null; reasons: Record<string, string>; onClose: () => void }) {
  const [inc, setInc] = useState<IncidentRow | null>(null);
  const [clip, setClip] = useState<string | null>(null);
  const [note, setNote] = useState("");
  const [reason, setReason] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const load = useCallback(async () => setInc(await api.get<IncidentRow>(`/incidents/${id}`)), [id]);
  useEffect(() => {
    load().catch((e) => setErr(e.message));
  }, [load]);
  const can = (a: string) => !!person && (CAN[person.role] ?? []).includes(a) && !(person.role === "SECURITY" && inc?.severity === "CRITICAL" && (a === "confirm" || a === "false_alert"));
  async function act(action: string) {
    setErr(null);
    try {
      setInc(await api.post<IncidentRow>(`/incidents/${id}/${action.replace("_", "-")}`, { note: note || null, reason: reason || null }));
    } catch (e) {
      setErr((e as Error).message);
    }
  }
  return (
    <div className="fixed inset-0 z-50 flex items-stretch justify-center bg-black/60 sm:items-center" role="dialog" aria-modal="true" aria-label="Incident">
      <div className="max-h-full w-full max-w-3xl overflow-y-auto bg-white p-5 sm:max-h-[92vh]">
        <div className="mb-3 flex items-start justify-between gap-3">
          <div>
            <p className="font-mono text-xs text-slate-700">{inc?.ref}</p>
            <h2 className="text-xl font-bold text-guard-ink">{inc?.title}</h2>
          </div>
          <button type="button" onClick={onClose} aria-label="Close" className="p-2 text-slate-700">
            <X className="h-5 w-5" />
          </button>
        </div>
        {!inc ? (
          <Spinner label="Loading…" />
        ) : (
          <div className="grid gap-5 md:grid-cols-[minmax(0,1.3fr)_minmax(0,1fr)]">
            <div className="min-w-0">
              {clip ? (
                <video src={clip} controls autoPlay playsInline className="aspect-video w-full bg-black" />
              ) : (
                <Thumb inc={inc} big />
              )}
              {inc.has_clip && !clip && (
                <button type="button" className="btn-primary mt-3 w-full" onClick={async () => setClip(await incidentMedia(inc.id, "clip"))}>
                  <Play className="h-4 w-4" /> Play {Math.round(inc.clip_duration_seconds ?? 15)} sec clip
                </button>
              )}
              {!inc.has_clip && inc.media_status !== "PENDING" && <p className="mt-2 text-sm text-slate-700">Video clip unavailable for this incident.</p>}
              {(inc.incident_type === "POSSIBLE_FIRE" || inc.incident_type === "POSSIBLE_SMOKE") && (
                <p className="mt-3 border border-red-300 bg-red-50 px-3 py-2 text-xs text-guard-ink">
                  Visual AI warning only. Not a replacement for certified fire detection systems.
                </p>
              )}
            </div>
            <div className="min-w-0 space-y-3 text-sm text-guard-ink">
              <dl className="grid grid-cols-[7rem_1fr] gap-y-1">
                <dt className="text-slate-700">Severity</dt>
                <dd><SevBadge s={inc.severity} /></dd>
                <dt className="text-slate-700">AI confidence</dt>
                <dd>{inc.confidence ? inc.confidence[0] + inc.confidence.slice(1).toLowerCase() : "—"}</dd>
                <dt className="text-slate-700">Camera</dt>
                <dd>{inc.camera}</dd>
                <dt className="text-slate-700">Occurred</dt>
                <dd>{new Date(inc.occurred_at).toLocaleString()}</dd>
                <dt className="text-slate-700">Status</dt>
                <dd className="font-semibold">{STATUS_TEXT[inc.status] ?? inc.status}</dd>
                {inc.acknowledged_by && (<><dt className="text-slate-700">Acknowledged</dt><dd>{inc.acknowledged_by}</dd></>)}
                {inc.reviewed_by && (<><dt className="text-slate-700">Reviewed by</dt><dd>{inc.reviewed_by}</dd></>)}
                {inc.false_alert_reason && (<><dt className="text-slate-700">Reason</dt><dd>{reasons[inc.false_alert_reason] ?? inc.false_alert_reason}</dd></>)}
                {inc.resolution_note && (<><dt className="text-slate-700">Note</dt><dd>{inc.resolution_note}</dd></>)}
              </dl>
              {!!inc.timeline?.length && (
                <div>
                  <h3 className="text-xs font-bold uppercase tracking-wider text-slate-700">AI sequence</h3>
                  <ol className="mt-1 space-y-0.5">
                    {inc.timeline.filter((t) => t.event !== "ZONE_EXIT").map((t, i) => (
                      <li key={i}>
                        <time className="mr-2 text-slate-700">{t.at ? new Date(t.at).toLocaleTimeString() : ""}</time>
                        {EVENT_TEXT[t.event] ?? t.event}
                      </li>
                    ))}
                  </ol>
                </div>
              )}
              {!person && <p className="border border-slate-300 bg-slate-50 px-3 py-2">Sign in with your PIN to review this incident.</p>}
              {person && (
                <div className="space-y-2 border-t border-slate-200 pt-3">
                  <textarea aria-label="Note" className="field min-h-[64px]" placeholder="Note (optional)" value={note} onChange={(e) => setNote(e.target.value)} />
                  <div className="flex flex-wrap gap-2">
                    {can("acknowledge") && inc.status === "UNREVIEWED" && <button type="button" className="btn-outline" onClick={() => act("acknowledge")}><Check className="h-4 w-4" /> Acknowledge</button>}
                    {can("confirm") && ["UNREVIEWED", "ACKNOWLEDGED", "ESCALATED"].includes(inc.status) && <button type="button" className="btn-dark" onClick={() => act("confirm")}>Confirm incident</button>}
                    {can("escalate") && ["UNREVIEWED", "ACKNOWLEDGED", "CONFIRMED"].includes(inc.status) && <button type="button" className="btn-outline" onClick={() => act("escalate")}>Escalate</button>}
                    {can("resolve") && ["UNREVIEWED", "ACKNOWLEDGED", "CONFIRMED", "ESCALATED"].includes(inc.status) && <button type="button" className="btn-outline" onClick={() => act("resolve")}>Resolve</button>}
                    {can("reopen") && ["FALSE_ALERT", "RESOLVED"].includes(inc.status) && <button type="button" className="btn-outline" onClick={() => act("reopen")}>Reopen</button>}
                  </div>
                  {can("false_alert") && ["UNREVIEWED", "ACKNOWLEDGED", "ESCALATED"].includes(inc.status) && (
                    <div className="flex flex-wrap gap-2">
                      <select aria-label="False alert reason" className="field flex-1" value={reason} onChange={(e) => setReason(e.target.value)}>
                        <option value="">Why was it a false alert?</option>
                        {Object.entries(reasons).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
                      </select>
                      <button type="button" className="btn-outline" disabled={!reason} onClick={() => act("false_alert")}>False alert</button>
                    </div>
                  )}
                  <div className="flex flex-wrap gap-2">
                    {can("keep") && (
                      <button type="button" className={inc.keep_evidence ? "btn-dark" : "btn-outline"} onClick={async () => setInc(await api.post<IncidentRow>(`/incidents/${id}/keep`, { keep: !inc.keep_evidence }))}>
                        <ShieldCheck className="h-4 w-4" /> {inc.keep_evidence ? "Evidence retained" : "Keep evidence"}
                      </button>
                    )}
                    {can("export") && (
                      <button type="button" className="btn-outline" onClick={() => download(`/incidents/${id}/export`, `${inc.ref}.zip`).catch((e) => setErr(e.message))}>
                        <Download className="h-4 w-4" /> Export
                      </button>
                    )}
                    {can("delete") && (
                      <button
                        type="button"
                        className="btn-outline text-red-800"
                        onClick={async () => {
                          if (!window.confirm("Delete this incident and its video? This can't be undone.")) return;
                          await api.del(`/incidents/${id}`);
                          onClose();
                        }}
                      >
                        <Trash2 className="h-4 w-4" /> Delete
                      </button>
                    )}
                  </div>
                </div>
              )}
              <ErrorNote message={err} />
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

// ── Guard Mode ────────────────────────────────────────────────────────
interface GuardModeData {
  protection: { running: boolean; cameras_online: number; cameras_total: number; reduced: boolean };
  latest: IncidentRow | null;
  unreviewed: number;
  fullscreen: IncidentRow[];
}

export function GuardModeView({ person }: { person: Person | null }) {
  const [d, setD] = useState<GuardModeData | null>(null);
  const [open, setOpen] = useState<string | null>(null);
  const [fullscreenOn, setFullscreenOn] = useState(() => {
    try {
      return localStorage.getItem("guard_fullscreen_alerts") === "1";
    } catch {
      return false;
    }
  });
  const [dismissed, setDismissed] = useState<string | null>(null);
  useEffect(() => {
    let alive = true;
    const tick = () => api.get<GuardModeData>("/guard-mode").then((r) => alive && setD(r)).catch(() => undefined);
    void tick();
    const t = window.setInterval(tick, 2000);
    return () => {
      alive = false;
      window.clearInterval(t);
    };
  }, []);
  const active = d?.protection.running && d.protection.cameras_online > 0;
  const alert = fullscreenOn ? d?.fullscreen.find((i) => i.id !== dismissed) : undefined;
  return (
    <Screen title="Guard Mode" lead="A simple view for security staff. No settings, no camera passwords.">
      <div className={`flex items-center gap-3 p-4 text-lg font-bold ${active ? "bg-emerald-50 text-emerald-900" : "bg-red-50 text-red-900"}`}>
        <span className={`h-4 w-4 rounded-full ${active ? "bg-emerald-600" : "bg-red-600"}`} aria-hidden />
        {active ? "Protection active" : "Protection not running"}
        {d && <span className="ml-auto text-sm font-semibold">{d.protection.cameras_online}/{d.protection.cameras_total} cameras</span>}
      </div>
      {d?.protection.reduced && <p className="mt-2 border border-guard-500 bg-guard-50 px-3 py-2 text-sm font-semibold text-guard-ink">Guard is operating in reduced-performance mode.</p>}
      <h2 className="mt-6 text-sm font-bold uppercase tracking-wider text-slate-700">Latest security alert{d?.unreviewed ? ` · ${d.unreviewed} unreviewed` : ""}</h2>
      {d?.latest ? (
        <div className="mt-2 max-w-md">
          <IncidentCard inc={d.latest} onOpen={() => setOpen(d.latest!.id)} />
        </div>
      ) : (
        <p className="mt-2 text-sm text-slate-700">No alerts.</p>
      )}
      <label className="mt-6 flex items-center gap-2 text-sm text-guard-ink">
        <input
          type="checkbox"
          className="h-5 w-5 accent-guard-ink"
          checked={fullscreenOn}
          onChange={(e) => {
            setFullscreenOn(e.target.checked);
            try {
              localStorage.setItem("guard_fullscreen_alerts", e.target.checked ? "1" : "0");
            } catch {
              /* ignore */
            }
          }}
        />
        Show a full-screen alert for High and Critical incidents on this screen (not recommended on the checkout PC)
      </label>
      {alert && (
        <div className="fixed inset-0 z-40 flex flex-col items-center justify-center gap-4 bg-guard-ink p-6 text-center text-white" role="alertdialog" aria-label="Guard alert">
          <AlertTriangle className="h-16 w-16 text-guard-500" aria-hidden />
          <p className="text-sm font-bold uppercase tracking-[0.3em] text-guard-500">Boombiz Guard alert</p>
          <p className="text-3xl font-extrabold sm:text-5xl">{alert.title}</p>
          <p className="text-xl">{alert.camera} · {time(alert.occurred_at)}</p>
          <div className="flex flex-wrap justify-center gap-3">
            <button type="button" className="btn-primary" onClick={() => { setOpen(alert.id); setDismissed(alert.id); }}>View incident</button>
            <button type="button" className="btn-outline" onClick={() => setDismissed(alert.id)}>Dismiss</button>
          </div>
        </div>
      )}
      {open && <IncidentDetail id={open} person={person} reasons={{}} onClose={() => setOpen(null)} />}
    </Screen>
  );
}

// ── Alarms ────────────────────────────────────────────────────────────
const HEALTH: Record<string, string> = { AVAILABLE: "Ready", UNAVAILABLE: "Not responding", ERROR: "Error", UNKNOWN: "Not tested" };

export function AlarmsView() {
  const [outs, setOuts] = useState<AlarmOut[]>([]);
  const [rules, setRules] = useState<AlarmRuleRow[]>([]);
  const [msg, setMsg] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [relay, setRelay] = useState({ name: "Door buzzer", on_url: "", off_url: "" });
  const load = useCallback(async () => {
    const r = await api.get<{ outputs: AlarmOut[]; rules: AlarmRuleRow[] }>("/alarms");
    setOuts(r.outputs);
    setRules(r.rules);
  }, []);
  useEffect(() => {
    void load();
  }, [load]);
  async function test(id: string) {
    setErr(null);
    setMsg(null);
    try {
      const r = await api.post<{ ok: boolean }>("/alarms/test", { output_id: id, seconds: 2 });
      setMsg(r.ok ? "Alarm sounded for 2 seconds." : "The alarm didn't respond.");
      await load();
    } catch (e) {
      setErr((e as Error).message);
    }
  }
  async function saveRule(r: AlarmRuleRow, patch: Partial<AlarmRuleRow>) {
    try {
      await api2.put(`/alarms/rules/${r.id}`, patch);
      await load();
    } catch (e) {
      setErr((e as Error).message);
    }
  }
  return (
    <Screen title="Local alarms" lead="What sounds in the shop when Guard raises an incident. Incidents are always recorded, even if an alarm doesn't respond.">
      <ErrorNote message={err} />
      {msg && <p className="mb-3 text-sm font-semibold text-emerald-800">{msg}</p>}
      <h2 className="text-sm font-bold uppercase tracking-wider text-slate-700">Alarm outputs</h2>
      <ul className="mt-2 divide-y divide-slate-200 border border-slate-200">
        {outs.map((o) => (
          <li key={o.id} className="flex flex-wrap items-center justify-between gap-3 px-4 py-3 text-sm text-guard-ink">
            <span className="flex items-center gap-2"><BellRing className="h-4 w-4" aria-hidden /> {o.name}</span>
            <span className={o.health === "AVAILABLE" ? "font-semibold text-emerald-800" : o.health === "ERROR" ? "font-semibold text-red-800" : "text-slate-700"}>
              {HEALTH[o.health] ?? o.health}
              {o.last_error ? ` · ${o.last_error}` : ""}
            </span>
            <button type="button" className="btn-outline" onClick={() => test(o.id)}>Test alarm</button>
          </li>
        ))}
      </ul>
      <details className="mt-3 border border-slate-200 p-3">
        <summary className="cursor-pointer text-sm font-semibold text-guard-ink">Add a network relay</summary>
        <div className="mt-3 grid gap-2 sm:grid-cols-3">
          <input aria-label="Name" className="field" value={relay.name} onChange={(e) => setRelay({ ...relay, name: e.target.value })} />
          <input aria-label="On address" className="field" placeholder="http://192.168.1.50/relay/on" value={relay.on_url} onChange={(e) => setRelay({ ...relay, on_url: e.target.value })} />
          <input aria-label="Off address" className="field" placeholder="http://192.168.1.50/relay/off" value={relay.off_url} onChange={(e) => setRelay({ ...relay, off_url: e.target.value })} />
        </div>
        <button
          type="button"
          className="btn-primary mt-3"
          onClick={async () => {
            setErr(null);
            try {
              await api.post("/alarms/outputs", { name: relay.name, kind: "NETWORK_RELAY", config: { on_url: relay.on_url, off_url: relay.off_url } });
              await load();
            } catch (e) {
              setErr((e as Error).message);
            }
          }}
        >
          Add relay
        </button>
      </details>
      <h2 className="mt-6 text-sm font-bold uppercase tracking-wider text-slate-700">When to sound</h2>
      <div className="mt-2 overflow-x-auto">
        <table className="w-full min-w-[520px] border border-slate-200 text-sm text-guard-ink">
          <thead className="bg-slate-50 text-left">
            <tr><th className="px-3 py-2">Incident</th><th className="px-3 py-2">Alarm</th><th className="px-3 py-2">Seconds</th><th className="px-3 py-2">Quiet for (s)</th></tr>
          </thead>
          <tbody>
            {rules.map((r) => (
              <tr key={r.id} className="border-t border-slate-200">
                <td className="px-3 py-2">{r.incident_type.replace(/_/g, " ").toLowerCase()}{r.repeat_until_ack ? " · repeats until acknowledged" : ""}</td>
                <td className="px-3 py-2"><input type="checkbox" aria-label={`Alarm for ${r.incident_type}`} className="h-5 w-5 accent-guard-ink" checked={r.enabled} onChange={(e) => saveRule(r, { enabled: e.target.checked })} /></td>
                <td className="px-3 py-2"><input type="number" aria-label="Seconds" className="field w-20" min={1} max={120} defaultValue={r.duration_seconds} onBlur={(e) => saveRule(r, { duration_seconds: Number(e.target.value) })} /></td>
                <td className="px-3 py-2"><input type="number" aria-label="Cooldown" className="field w-24" min={5} max={3600} defaultValue={r.cooldown_seconds} onBlur={(e) => saveRule(r, { cooldown_seconds: Number(e.target.value) })} /></td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Screen>
  );
}

// ── Phone alerts: link this Guard PC to the business (pairing) ────────
interface CloudStatus {
  paired: boolean;
  business_name: string | null;
  location_name: string | null;
  online: boolean | null;
  last_error: string | null;
  pairing_code: string | null;
  pairing_expires_at: string | null;
  health_status: "ONLINE" | "DEGRADED" | "OFFLINE" | "UNKNOWN" | null;
  health_reasons: string[];
  last_heartbeat_at: string | null;
  queued: number;
  cloud_url: string;
}

function PhoneAlertsSection({ canLink }: { canLink: boolean }) {
  const [st, setSt] = useState<CloudStatus | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [now, setNow] = useState(Date.now());
  const [code, setCode] = useState("");
  const load = useCallback(async () => {
    try {
      setSt(await api.get<CloudStatus>("/cloud/status"));
    } catch (e) {
      setErr((e as Error).message);
    }
  }, []);
  useEffect(() => {
    void load();
  }, [load]);
  // Poll quickly only while a code is on screen, waiting to be claimed.
  const waiting = !!st?.pairing_code && !st.paired;
  useEffect(() => {
    if (!waiting) return;
    const t = window.setInterval(() => {
      setNow(Date.now());
      void load();
    }, 4000);
    return () => window.clearInterval(t);
  }, [waiting, load]);
  const secondsLeft = st?.pairing_expires_at ? Math.max(0, Math.round((new Date(st.pairing_expires_at).getTime() - now) / 1000)) : 0;
  const site = st?.cloud_url.replace(/^https?:\/\//, "") ?? "guard.getboombiz.com";

  async function pair() {
    setBusy(true);
    setErr(null);
    try {
      await api.post("/cloud/pair");
      await load();
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  async function unpair() {
    if (!window.confirm("Disconnect this computer from Boombiz? Phone alerts and remote monitoring stop. Guard keeps protecting the shop locally.")) return;
    await api.post("/cloud/unpair");
    await load();
  }
  async function activate() {
    setBusy(true);
    setErr(null);
    try {
      await api.post("/cloud/activate", { code });
      setCode("");
      await load();
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  const codeChars = code.toUpperCase().replace(/^GARD[\s-]*/, "").replace(/[^A-Z0-9]/g, "").length;

  return (
    <section className="card mt-4 p-4">
      <h2 className="font-bold text-guard-ink">Boombiz cloud</h2>
      {!st ? (
        <Spinner />
      ) : st.paired ? (
        <div className="mt-1 space-y-2 text-sm text-guard-ink">
          <p className="font-semibold text-emerald-800">
            Connected to {st.business_name ?? "your business"}{st.location_name ? ` — ${st.location_name}` : ""}.
          </p>
          <p>
            High and critical incidents go to the phones that turned on alerts at {site}, and the owner can see this
            computer's health there.
            {st.online === false ? " This computer is offline right now — alerts are waiting and will send when it's back." : ""}
          </p>
          {st.health_status === "DEGRADED" && st.health_reasons.length > 0 && (
            <div className="border-l-4 border-amber-500 bg-amber-50 px-3 py-2 text-guard-ink">
              <p className="font-semibold">Boombiz shows this location as degraded:</p>
              <ul className="list-disc pl-5">{st.health_reasons.map((r) => <li key={r}>{r}</li>)}</ul>
            </div>
          )}
          {st.last_heartbeat_at && (
            <p className="text-slate-700">Last check-in with Boombiz: {new Date(st.last_heartbeat_at).toLocaleTimeString()}.</p>
          )}
          {st.queued > 0 && <p>{st.queued} alert{st.queued === 1 ? "" : "s"} waiting to send.</p>}
          {canLink && <button type="button" className="btn-outline" onClick={unpair}>Unlink this computer</button>}
        </div>
      ) : waiting && secondsLeft > 0 ? (
        <div className="mt-2 space-y-3 text-sm text-guard-ink">
          <p className="font-mono text-4xl font-extrabold tracking-[0.15em] text-guard-ink" aria-label="Pairing code">{st.pairing_code}</p>
          <ol className="list-decimal space-y-1 pl-5">
            <li>On the owner's phone, open <b>{site}</b> and sign in.</li>
            <li>Under <b>Shop computers</b>, type this code and tap <b>Link</b>.</li>
            <li>Tap <b>Turn on alerts</b> on each phone that should get pop-ups.</li>
          </ol>
          <p className="text-slate-700">
            Code expires in {Math.floor(secondsLeft / 60)}:{String(secondsLeft % 60).padStart(2, "0")}. This screen updates by itself once it's linked.
          </p>
        </div>
      ) : (
        <div className="mt-1 space-y-2 text-sm text-guard-ink">
          <p>
            Connect this computer to Boombiz so high and critical incidents reach the owner's and manager's phones and
            the owner can check it remotely. Needs internet; alerts wait here while it's offline. Guard protects the shop
            locally either way.
          </p>
          {st.last_error && <p className="text-red-800">{st.last_error}</p>}
          {canLink ? (
            <>
              <div>
                <label htmlFor="gard-code" className="mb-1 block font-semibold">Activation code</label>
                <p className="mb-2 text-slate-700">Made in Boombiz Guard → Locations → Activate a Guard computer.</p>
                <div className="flex flex-wrap gap-2">
                  <input id="gard-code" value={code} onChange={(e) => setCode(e.target.value.toUpperCase())}
                         placeholder="GARD-XXXX-XXXX" maxLength={16} autoCapitalize="characters" autoComplete="off"
                         className="input w-52 font-mono" />
                  <button type="button" className="btn-primary" disabled={busy || codeChars !== 8} onClick={activate}>
                    {busy ? <Spinner /> : "Activate"}
                  </button>
                </div>
              </div>
              <p className="pt-1 text-slate-700">No activation code? The owner can link this computer from their phone instead:</p>
              <button type="button" className="btn-outline" disabled={busy} onClick={pair}>
                {waiting ? "Get a new phone code" : "Link with the owner's phone"}
              </button>
            </>
          ) : (
            <p className="text-slate-700">Sign in as the owner to connect this computer to Boombiz.</p>
          )}
        </div>
      )}
      <ErrorNote message={err} />
    </section>
  );
}

// ── Settings: storage, retention, people, location ────────────────────
const gb = (b: number) => `${(b / 1024 ** 3).toFixed(1)} GB`;

export function SettingsView({ person }: { person: Person | null }) {
  const [st, setSt] = useState<StorageStatus | null>(null);
  const [people, setPeople] = useState<Person[]>([]);
  const [code, setCode] = useState("");
  const [np, setNp] = useState({ name: "", role: "SECURITY", pin: "" });
  const [msg, setMsg] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const owner = person?.role === "OWNER";
  const load = useCallback(async () => {
    const [s, u, c] = await Promise.all([
      api.get<StorageStatus>("/storage/status"),
      api.get<{ users: Person[] }>("/auth/users"),
      api.get<{ code: string }>("/settings/location-code"),
    ]);
    setSt(s);
    setPeople(u.users);
    setCode(c.code);
  }, []);
  useEffect(() => {
    void load();
  }, [load]);
  async function run(fn: () => Promise<unknown>, ok: string) {
    setErr(null);
    setMsg(null);
    try {
      await fn();
      setMsg(ok);
      await load();
    } catch (e) {
      setErr((e as Error).message);
    }
  }
  const def = st?.policies.find((p) => !p.incident_type && !p.severity);
  return (
    <Screen title="Settings" lead="Storage, how long incidents are kept, and who can review them.">
      <ErrorNote message={err} />
      {msg && <p className="mb-3 text-sm font-semibold text-emerald-800">{msg}</p>}
      {st && (
        <section className="card p-4">
          <h2 className="font-bold text-guard-ink">Local storage</h2>
          <p className="mt-1 text-sm text-guard-ink">{gb(st.guard_media_bytes)} of {gb(st.ceiling_bytes)} used by Guard · {gb(st.disk_free_bytes)} free on this computer</p>
          <div className="mt-2 h-2 w-full bg-slate-100" role="progressbar" aria-valuenow={Math.round((100 * st.guard_media_bytes) / st.ceiling_bytes)} aria-valuemin={0} aria-valuemax={100}>
            <div className="h-2 bg-guard-ink" style={{ width: `${Math.min(100, (100 * st.guard_media_bytes) / st.ceiling_bytes)}%` }} />
          </div>
          {st.warning && <p className={`mt-2 border px-3 py-2 text-sm font-semibold ${st.level === "CRITICAL" ? "border-red-300 bg-red-50 text-red-900" : "border-guard-500 bg-guard-50 text-guard-ink"}`}>{st.warning}</p>}
          <p className="mt-2 text-xs text-slate-700">Guard never records continuously. Only incident snapshots and clips of up to 15 seconds are kept, encrypted. {st.kept_evidence} kept as evidence.</p>
          {owner && <button type="button" className="btn-outline mt-3" onClick={() => run(() => api.post("/storage/cleanup"), "Old incidents cleaned up.")}>Clean up now</button>}
        </section>
      )}
      <section className="card mt-4 p-4">
        <h2 className="font-bold text-guard-ink">How long incidents are kept</h2>
        <p className="mt-1 text-sm text-guard-ink">Normal incidents: {def?.retention_days ?? 30} days. Incidents marked "Keep evidence" stay until the owner deletes them.</p>
        {owner && def && (
          <div className="mt-2 flex flex-wrap items-center gap-2">
            <label htmlFor="days" className="text-sm text-guard-ink">Keep for</label>
            <input id="days" type="number" min={1} max={30} className="field w-24" defaultValue={def.retention_days} onBlur={(e) => run(() => api2.put("/retention", { retention_days: Number(e.target.value), keep_if_confirmed: def.keep_if_confirmed }), "Saved.")} />
            <span className="text-sm text-guard-ink">days (maximum 30)</span>
          </div>
        )}
      </section>
      <section className="card mt-4 p-4">
        <h2 className="font-bold text-guard-ink">People who can review</h2>
        <ul className="mt-2 divide-y divide-slate-200 text-sm text-guard-ink">
          {people.map((p) => <li key={p.id} className="py-2">{p.name} · {p.role.toLowerCase()}</li>)}
          {!people.length && <li className="py-2">Nobody yet. Use "Sign in with PIN" to add the owner.</li>}
        </ul>
        {owner && (
          <div className="mt-3 grid gap-2 sm:grid-cols-4">
            <input aria-label="Name" className="field" placeholder="Name" value={np.name} onChange={(e) => setNp({ ...np, name: e.target.value })} />
            <select aria-label="Role" className="field" value={np.role} onChange={(e) => setNp({ ...np, role: e.target.value })}>
              <option value="MANAGER">Manager</option>
              <option value="SECURITY">Security</option>
              <option value="OWNER">Owner</option>
            </select>
            <input aria-label="PIN" className="field" type="password" inputMode="numeric" placeholder="PIN (4–6 digits)" value={np.pin} onChange={(e) => setNp({ ...np, pin: e.target.value.replace(/\D/g, "").slice(0, 6) })} />
            <button type="button" className="btn-primary" onClick={() => run(() => api.post("/users", np).then(() => setNp({ name: "", role: "SECURITY", pin: "" })), "Person added.")}>Add person</button>
          </div>
        )}
      </section>
      <PhoneAlertsSection canLink={owner} />
      <section className="card mt-4 p-4">
        <h2 className="font-bold text-guard-ink">Location code</h2>
        <p className="mt-1 text-sm text-guard-ink">Used in incident IDs, e.g. BG-{code || "LOC"}-20260913-000184.</p>
        <div className="mt-2 flex gap-2">
          <input aria-label="Location code" className="field w-24 uppercase" maxLength={3} value={code} onChange={(e) => setCode(e.target.value.toUpperCase())} />
          <button type="button" className="btn-outline" onClick={() => run(() => api2.put("/settings/location-code", { code }), "Saved.")}>Save</button>
        </div>
      </section>
      <section className="mt-4 text-xs text-slate-700">
        <p>Privacy: no continuous Guard recording · no facial recognition · no video leaves this computer in this version · incidents can be deleted by the owner.</p>
      </section>
    </Screen>
  );
}
