import { useCallback, useEffect, useRef, useState } from "react";
import { Check, Trash2, X } from "lucide-react";
import {
  api,
  api2,
  type AiConfig,
  type AiDebug,
  type AiEvent,
  type AiStatus,
  type Camera,
  type DayHours,
  type Pt,
  type Zone,
  type ZoneType,
} from "./api";
import { ErrorNote, Screen, Snapshot, Spinner } from "./ui";

/* Phase 2 screens: Zones (§47), AI test (§48–49), Events (§51), Hours (§16),
   and the per-camera AI switches (§50). Overlays are setup/debug only. */

export const ZONE_STYLE: Record<ZoneType, { label: string; stroke: string; fill: string }> = {
  SHELF: { label: "Shelf", stroke: "#FFC21A", fill: "rgba(255,194,26,0.18)" },
  EXIT: { label: "Exit", stroke: "#22C55E", fill: "rgba(34,197,94,0.18)" },
  RESTRICTED: { label: "Restricted", stroke: "#EF4444", fill: "rgba(239,68,68,0.18)" },
  CASHIER: { label: "Cashier", stroke: "#3B82F6", fill: "rgba(59,130,246,0.18)" },
  STOCKROOM: { label: "Stockroom", stroke: "#A855F7", fill: "rgba(168,85,247,0.18)" },
  FIRE_RISK: { label: "Fire risk", stroke: "#F97316", fill: "rgba(249,115,22,0.18)" },
  IGNORE: { label: "Ignore", stroke: "#9CA3AF", fill: "rgba(156,163,175,0.25)" },
  PRIVACY: { label: "Privacy mask", stroke: "#111111", fill: "rgba(0,0,0,0.65)" },
};

const EVENT_TEXT: Record<string, string> = {
  PERSON_DETECTED: "Person detected",
  ZONE_ENTRY: "Entered",
  ZONE_EXIT: "Left",
  SHELF_INTERACTION: "Shelf interaction",
  UNRESOLVED_SHELF_INTERACTION: "Unresolved shelf interaction",
  EXIT_APPROACH: "Walked to the exit",
  POSSIBLE_UNPAID_EXIT: "Possible unpaid exit",
  POSSIBLE_PRODUCT_REPLACEMENT: "Possible product swap",
  RESTRICTED_ZONE_ENTRY: "Entered a restricted area",
  AFTER_HOURS_PERSON: "Person after hours",
  POSSIBLE_SMOKE: "Possible smoke",
  POSSIBLE_FIRE: "Possible fire",
  TRACK_LOST: "Person out of view",
};

const FIRE_NOTICE = "Visual AI warning only. Not a replacement for certified fire detection systems.";

function useGuardCameras() {
  const [cams, setCams] = useState<Camera[]>([]);
  const [sel, setSel] = useState<string>("");
  useEffect(() => {
    api.get<{ cameras: Camera[] }>("/cameras").then((r) => {
      const g = r.cameras.filter((c) => c.guard_enabled);
      setCams(g);
      setSel((s) => s || g[0]?.id || "");
    });
  }, []);
  return { cams, sel, setSel };
}

function CameraPicker({ cams, sel, setSel }: ReturnType<typeof useGuardCameras>) {
  if (!cams.length) return <p className="text-sm text-slate-700">Choose your Guard cameras in Setup first.</p>;
  return (
    <div className="mb-4 flex flex-wrap gap-2" role="tablist">
      {cams.map((c) => (
        <button
          key={c.id}
          type="button"
          role="tab"
          aria-selected={c.id === sel}
          onClick={() => setSel(c.id)}
          className={c.id === sel ? "btn-dark" : "btn-outline"}
        >
          {c.name}
        </button>
      ))}
    </div>
  );
}

function ZoneLayer({ zones, draft }: { zones: Zone[]; draft?: { type: ZoneType; pts: Pt[] } }) {
  return (
    <svg className="pointer-events-none absolute inset-0 h-full w-full" viewBox="0 0 1 1" preserveAspectRatio="none">
      {zones.map((z) => {
        const s = ZONE_STYLE[z.zone_type];
        return (
          <polygon
            key={z.id}
            points={z.polygon.map((p) => `${p.x},${p.y}`).join(" ")}
            fill={s.fill}
            stroke={s.stroke}
            strokeWidth={0.004}
            vectorEffect="non-scaling-stroke"
          />
        );
      })}
      {draft && draft.pts.length > 0 && (
        <polyline
          points={draft.pts.map((p) => `${p.x},${p.y}`).join(" ")}
          fill={ZONE_STYLE[draft.type].fill}
          stroke={ZONE_STYLE[draft.type].stroke}
          strokeWidth={2}
          strokeDasharray="4 3"
          vectorEffect="non-scaling-stroke"
        />
      )}
    </svg>
  );
}

function ZoneLabels({ zones }: { zones: Zone[] }) {
  return (
    <>
      {zones.map((z) => {
        const x = Math.min(...z.polygon.map((p) => p.x));
        const y = Math.min(...z.polygon.map((p) => p.y));
        return (
          <span
            key={z.id}
            className="pointer-events-none absolute bg-guard-ink px-1 text-[11px] font-semibold text-white"
            style={{ left: `${x * 100}%`, top: `${y * 100}%` }}
          >
            {z.name}
          </span>
        );
      })}
    </>
  );
}

// ── Zones ─────────────────────────────────────────────────────────────
export function ZonesView() {
  const cam = useGuardCameras();
  const [zones, setZones] = useState<Zone[]>([]);
  const [draft, setDraft] = useState<{ type: ZoneType; pts: Pt[] } | null>(null);
  const [name, setName] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const load = useCallback(async () => {
    if (!cam.sel) return;
    const r = await api.get<{ zones: Zone[] }>(`/zones?camera_id=${encodeURIComponent(cam.sel)}`);
    setZones(r.zones);
  }, [cam.sel]);
  useEffect(() => {
    void load();
  }, [load]);

  function addPoint(e: React.MouseEvent<HTMLDivElement>) {
    if (!draft) return;
    const r = e.currentTarget.getBoundingClientRect();
    const x = Math.min(1, Math.max(0, (e.clientX - r.left) / r.width));
    const y = Math.min(1, Math.max(0, (e.clientY - r.top) / r.height));
    setDraft({ ...draft, pts: [...draft.pts, { x: +x.toFixed(4), y: +y.toFixed(4) }] });
  }

  async function save() {
    if (!draft) return;
    setErr(null);
    try {
      await api.post("/zones", {
        camera_id: cam.sel,
        name: name.trim() || ZONE_STYLE[draft.type].label,
        zone_type: draft.type,
        polygon: draft.pts,
      });
      setDraft(null);
      setName("");
      await load();
    } catch (e) {
      setErr((e as Error).message);
    }
  }

  async function remove(id: string) {
    await api.del(`/zones/${id}`);
    await load();
  }

  return (
    <Screen
      title="Monitoring zones"
      lead="Draw where the shelves, exit and private areas are. Click the picture to place each corner, then save. Guard uses where people stand, not where their heads are."
    >
      <CameraPicker {...cam} />
      {cam.sel && (
        <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_18rem]">
          <div className="min-w-0">
            <div
              className={`relative select-none overflow-hidden ${draft ? "cursor-crosshair ring-2 ring-guard-500" : ""}`}
              onClick={addPoint}
              role={draft ? "application" : undefined}
              aria-label={draft ? "Click to add zone corners" : undefined}
            >
              <Snapshot cameraId={cam.sel} live />
              <ZoneLayer zones={zones} draft={draft ?? undefined} />
              <ZoneLabels zones={zones} />
            </div>
            <div className="mt-3 flex flex-wrap gap-2">
              {(Object.keys(ZONE_STYLE) as ZoneType[]).map((t) => (
                <button key={t} type="button" className="btn-outline" onClick={() => setDraft({ type: t, pts: [] })} disabled={!!draft}>
                  <span className="inline-block h-3 w-3" style={{ background: ZONE_STYLE[t].stroke }} aria-hidden />+ {ZONE_STYLE[t].label}
                </button>
              ))}
            </div>
            {draft && (
              <div className="mt-4 flex flex-wrap items-end gap-3 border border-slate-200 p-4">
                <div className="min-w-[12rem] flex-1">
                  <label className="label" htmlFor="zn">
                    {ZONE_STYLE[draft.type].label} name
                  </label>
                  <input id="zn" className="field" value={name} onChange={(e) => setName(e.target.value)} placeholder={`e.g. ${ZONE_STYLE[draft.type].label} A`} />
                </div>
                <p className="text-sm text-slate-700">{draft.pts.length} corners{draft.pts.length < 3 ? " (at least 3)" : ""}</p>
                <button type="button" className="btn-primary" disabled={draft.pts.length < 3} onClick={save}>
                  <Check className="h-4 w-4" /> Save zone
                </button>
                <button type="button" className="btn-outline" onClick={() => setDraft({ ...draft, pts: draft.pts.slice(0, -1) })} disabled={!draft.pts.length}>
                  Undo corner
                </button>
                <button type="button" className="btn-outline" onClick={() => setDraft(null)}>
                  <X className="h-4 w-4" /> Cancel
                </button>
              </div>
            )}
            <ErrorNote message={err} />
          </div>
          <div className="space-y-6">
            <div>
              <h2 className="text-sm font-bold uppercase tracking-wider text-slate-700">Zones on this camera</h2>
              <ul className="mt-2 divide-y divide-slate-200 border border-slate-200">
                {zones.map((z) => (
                  <li key={z.id} className="flex items-center justify-between gap-2 px-3 py-2 text-sm text-guard-ink">
                    <span className="flex items-center gap-2">
                      <span className="inline-block h-3 w-3" style={{ background: ZONE_STYLE[z.zone_type].stroke }} aria-hidden />
                      {z.name}
                    </span>
                    <button type="button" onClick={() => remove(z.id)} aria-label={`Delete ${z.name}`} className="p-2 text-slate-700 hover:text-red-700">
                      <Trash2 className="h-4 w-4" />
                    </button>
                  </li>
                ))}
                {!zones.length && <li className="px-3 py-2 text-sm text-slate-700">No zones yet.</li>}
              </ul>
            </div>
            <AiSwitches cameraId={cam.sel} />
          </div>
        </div>
      )}
    </Screen>
  );
}

const SWITCHES: { key: keyof AiConfig; label: string }[] = [
  { key: "person", label: "Person detection" },
  { key: "shelf", label: "Shelf interaction" },
  { key: "exit", label: "Exit monitoring" },
  { key: "restricted", label: "Restricted zones" },
  { key: "after_hours", label: "After-hours" },
  { key: "fire", label: "Smoke / fire (experimental)" },
  { key: "concealment", label: "Concealment (experimental)" },
];

function AiSwitches({ cameraId }: { cameraId: string }) {
  const [cfg, setCfg] = useState<AiConfig | null>(null);
  const [err, setErr] = useState<string | null>(null);
  useEffect(() => {
    api.get<AiConfig>(`/cameras/${cameraId}/ai-config`).then(setCfg).catch((e) => setErr(e.message));
  }, [cameraId]);
  async function save(next: AiConfig) {
    setCfg(next);
    try {
      const { fire_notice: _a, concealment_notice: _b, ...body } = next;
      setCfg(await api2.put<AiConfig>(`/cameras/${cameraId}/ai-config`, body));
    } catch (e) {
      setErr((e as Error).message);
    }
  }
  if (!cfg) return err ? <ErrorNote message={err} /> : <Spinner />;
  return (
    <div>
      <h2 className="text-sm font-bold uppercase tracking-wider text-slate-700">AI on this camera</h2>
      <ul className="mt-2 space-y-1">
        {SWITCHES.map((s) => (
          <li key={s.key}>
            <label className="flex min-h-[40px] cursor-pointer items-center gap-3 text-sm text-guard-ink">
              <input type="checkbox" className="h-5 w-5 accent-guard-ink" checked={!!cfg[s.key]} onChange={(e) => save({ ...cfg, [s.key]: e.target.checked })} />
              {s.label}
            </label>
          </li>
        ))}
      </ul>
      {cfg.fire && <p className="mt-2 border border-orange-300 bg-orange-50 px-3 py-2 text-xs text-guard-ink">{FIRE_NOTICE}</p>}
      <label className="label mt-3" htmlFor="prio">
        Priority when the computer is busy
      </label>
      <select id="prio" className="field" value={cfg.priority} onChange={(e) => save({ ...cfg, priority: e.target.value as AiConfig["priority"] })}>
        <option value="PRIMARY">High (exit / security camera)</option>
        <option value="NORMAL">Normal</option>
      </select>
    </div>
  );
}

// ── AI test mode ──────────────────────────────────────────────────────
export function AiTestView() {
  const cam = useGuardCameras();
  const [dbg, setDbg] = useState<AiDebug | null>(null);
  const [status, setStatus] = useState<AiStatus | null>(null);
  const [feed, setFeed] = useState<AiEvent[]>([]);
  const [err, setErr] = useState<string | null>(null);
  const zoneName = useRef<Record<string, string>>({});

  useEffect(() => {
    if (!cam.sel) return;
    let alive = true;
    const tick = async () => {
      try {
        const [d, s, f] = await Promise.all([
          api.get<AiDebug>(`/ai/debug/${cam.sel}`),
          api.get<AiStatus>("/ai/status"),
          api.get<{ events: AiEvent[] }>("/events/live"),
        ]);
        if (!alive) return;
        setDbg(d);
        setStatus(s);
        zoneName.current = Object.fromEntries(d.zones.map((z) => [z.id, z.name]));
        setFeed(f.events.filter((e) => e.camera_id === cam.sel && e.event_type !== "TRACK_LOST").slice(-40).reverse());
        setErr(null);
      } catch (e) {
        if (alive) setErr((e as Error).message);
      }
    };
    void tick();
    const t = window.setInterval(tick, 400);
    return () => {
      alive = false;
      window.clearInterval(t);
    };
  }, [cam.sel]);

  const cs = status?.cameras.find((c) => c.camera_id === cam.sel);
  return (
    <Screen title="Test AI" lead="Walk in front of the camera. Boxes and labels only appear here, in setup, never on the merchant's dashboard.">
      <CameraPicker {...cam} />
      {status?.performance.reduced && (
        <p className="mb-3 border border-guard-500 bg-guard-50 px-3 py-2 text-sm font-semibold text-guard-ink">{status.performance.message}</p>
      )}
      {status && !status.running && <ErrorNote message={status.error ?? "Guard AI is not running."} />}
      <ErrorNote message={err} />
      {cam.sel && (
        <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_20rem]">
          <div className="min-w-0">
            {/* overflow-hidden: labels near the frame edge clip inside the
                picture instead of widening the page (no sideways scroll). */}
            <div className="relative overflow-hidden">
              <Snapshot cameraId={cam.sel} live />
              {dbg && <ZoneLayer zones={dbg.zones} />}
              {dbg?.tracks.map((t) => (
                <div
                  key={t.track_id}
                  className="pointer-events-none absolute border-2 border-guard-500"
                  style={{
                    left: `${t.bbox.x1 * 100}%`,
                    top: `${t.bbox.y1 * 100}%`,
                    width: `${(t.bbox.x2 - t.bbox.x1) * 100}%`,
                    height: `${(t.bbox.y2 - t.bbox.y1) * 100}%`,
                    opacity: t.state === "TEMPORARILY_LOST" ? 0.4 : 1,
                  }}
                >
                  <span
                    className={`absolute -top-5 whitespace-nowrap bg-guard-500 px-1 text-[11px] font-bold text-guard-ink ${
                      t.bbox.x1 > 0.5 ? "right-0" : "left-0"
                    }`}
                  >
                    Person #{t.track_id.replace("track_", "")} · {t.confidence >= 0.7 ? "High" : t.confidence >= 0.5 ? "Medium" : "Low"}
                    {t.zones.length ? ` · ${t.zones.map((z) => zoneName.current[z] ?? z).join(", ")}` : ""}
                  </span>
                </div>
              ))}
            </div>
            {cs && (
              <p className="mt-2 text-xs text-slate-700">
                {cs.state} · AI {cs.ai_fps} fps (target {cs.target_fps}) · {cs.infer_ms} ms per frame · {cs.active_tracks} people · dropped {cs.frames_dropped}
                {cs.failed_modules.length ? ` · unavailable: ${cs.failed_modules.join(", ")}` : ""}
              </p>
            )}
          </div>
          <div>
            <h2 className="text-sm font-bold uppercase tracking-wider text-slate-700">Live events</h2>
            <ul className="mt-2 max-h-[28rem] divide-y divide-slate-200 overflow-y-auto border border-slate-200 text-sm">
              {feed.map((e, i) => (
                <li key={`${e.id}-${i}`} className={`px-3 py-2 ${e.event_type === "POSSIBLE_UNPAID_EXIT" ? "bg-guard-50 font-semibold" : ""} text-guard-ink`}>
                  <time className="mr-2 text-slate-700">{new Date(e.occurred_at).toLocaleTimeString()}</time>
                  {EVENT_TEXT[e.event_type] ?? e.event_type}
                  {e.zone_id && zoneName.current[e.zone_id] ? ` ${zoneName.current[e.zone_id]}` : ""}
                  {e.track_id ? ` · #${e.track_id.replace("track_", "")}` : ""}
                  {e.confidence && e.event_type === "POSSIBLE_UNPAID_EXIT" ? ` · ${e.confidence}` : ""}
                </li>
              ))}
              {!feed.length && <li className="px-3 py-2 text-slate-700">Waiting for activity…</li>}
            </ul>
          </div>
        </div>
      )}
    </Screen>
  );
}

// ── Events + feedback ─────────────────────────────────────────────────
export function EventsView() {
  const [events, setEvents] = useState<AiEvent[]>([]);
  const [securityOnly, setSecurityOnly] = useState(true);
  const load = useCallback(async () => {
    const r = await api.get<{ events: AiEvent[] }>(`/events?security_only=${securityOnly}&limit=200`);
    setEvents(r.events);
  }, [securityOnly]);
  useEffect(() => {
    void load();
  }, [load]);
  async function feedback(id: string, fb: string) {
    await api.post(`/events/${id}/feedback`, { feedback: fb });
    await load();
  }
  return (
    <Screen title="AI events" lead="Mark each event so Guard can be tuned from real shop data. Nothing here is an accusation. Every event is a possibility to check.">
      <label className="mb-4 flex items-center gap-2 text-sm text-guard-ink">
        <input type="checkbox" className="h-5 w-5 accent-guard-ink" checked={securityOnly} onChange={(e) => setSecurityOnly(e.target.checked)} />
        Security events only
      </label>
      <ul className="divide-y divide-slate-200 border border-slate-200">
        {events.map((e) => (
          <li key={e.id} className="flex flex-col gap-2 px-4 py-3 sm:flex-row sm:items-center sm:justify-between">
            <div className="text-sm text-guard-ink">
              <p className="font-semibold">
                {EVENT_TEXT[e.event_type] ?? e.event_type}
                {e.confidence ? ` · ${e.confidence}` : ""}
              </p>
              <p className="text-slate-700">
                {new Date(e.occurred_at).toLocaleString()}
                {e.track_id ? ` · person #${e.track_id.replace("track_", "")}` : ""}
              </p>
              {(e.event_type === "POSSIBLE_FIRE" || e.event_type === "POSSIBLE_SMOKE") && <p className="text-xs text-slate-700">{FIRE_NOTICE}</p>}
            </div>
            <div className="flex flex-wrap gap-2" role="group" aria-label="Was this right?">
              {[
                ["ACCURATE", "Accurate"],
                ["FALSE_EVENT", "False event"],
                ["UNSURE", "Unsure"],
              ].map(([k, l]) => (
                <button key={k} type="button" className={e.feedback === k ? "btn-dark" : "btn-outline"} onClick={() => feedback(e.id, k)}>
                  {l}
                </button>
              ))}
            </div>
          </li>
        ))}
        {!events.length && <li className="px-4 py-3 text-sm text-slate-700">No events yet.</li>}
      </ul>
    </Screen>
  );
}

// ── Business hours ────────────────────────────────────────────────────
const DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"];

export function HoursView() {
  const [days, setDays] = useState<DayHours[]>(
    DAYS.map((_, i) => ({ day_of_week: i, opens_at: "08:00", closes_at: "20:00", closed: i === 6 })),
  );
  const [msg, setMsg] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  useEffect(() => {
    api.get<{ days: DayHours[] }>("/schedules").then((r) => r.days.length && setDays(r.days));
  }, []);
  const set = (i: number, patch: Partial<DayHours>) => setDays(days.map((d, j) => (j === i ? { ...d, ...patch } : d)));
  async function save() {
    setErr(null);
    setMsg(null);
    try {
      await api2.put("/schedules", { days });
      setMsg("Saved. Outside these hours, anyone Guard sees raises an after-hours alert.");
    } catch (e) {
      setErr((e as Error).message);
    }
  }
  return (
    <Screen
      title="Business hours"
      lead="When the shop is closed, a person seen for more than 2 seconds in a watched area is an after-hours alert."
      actions={
        <button type="button" className="btn-primary" onClick={save}>
          Save hours
        </button>
      }
    >
      <ul className="divide-y divide-slate-200 border border-slate-200">
        {days.map((d, i) => (
          <li key={d.day_of_week} className="flex flex-wrap items-center gap-3 px-4 py-3">
            <span className="w-28 text-sm font-semibold text-guard-ink">{DAYS[d.day_of_week]}</span>
            <input aria-label={`${DAYS[d.day_of_week]} opens`} type="time" className="field w-32" value={d.opens_at ?? ""} disabled={d.closed} onChange={(e) => set(i, { opens_at: e.target.value })} />
            <span className="text-sm text-slate-700">to</span>
            <input aria-label={`${DAYS[d.day_of_week]} closes`} type="time" className="field w-32" value={d.closes_at ?? ""} disabled={d.closed} onChange={(e) => set(i, { closes_at: e.target.value })} />
            <label className="flex items-center gap-2 text-sm text-guard-ink">
              <input type="checkbox" className="h-5 w-5 accent-guard-ink" checked={d.closed} onChange={(e) => set(i, { closed: e.target.checked })} />
              Closed all day
            </label>
          </li>
        ))}
      </ul>
      {msg && <p className="mt-3 text-sm font-semibold text-emerald-800">{msg}</p>}
      <ErrorNote message={err} />
    </Screen>
  );
}
