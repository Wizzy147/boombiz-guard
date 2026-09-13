import { useCallback, useEffect, useMemo, useState } from "react";
import {
  ArrowRight,
  Check,
  Cctv,
  CircleAlert,
  Cpu,
  HardDrive,
  MemoryStick,
  Plus,
  RefreshCw,
  Search,
  ShieldCheck,
  X,
} from "lucide-react";
import {
  api,
  hasToken,
  type Camera,
  type Device,
  type DiscoveryStatus,
  type Health,
  type TestPayload,
} from "./api";
import { CompatBadge, ErrorNote, Lockup, Screen, Snapshot, Spinner } from "./ui";
import { AiTestView, EventsView, HoursView, ZonesView } from "./phase2";
import { AlarmsView, GuardModeView, IncidentsView, SettingsView, SignInBar, usePerson } from "./phase3";

/*
 * The Phase 1 installer flow (§26): Welcome → PC check → Scan → Devices
 * (connect / add manually) → Channels → Choose Guard cameras (max 2) →
 * Connection test → Done. A Status view sits beside it for after setup.
 */

type Step =
  | "welcome" | "pc" | "scan" | "devices" | "channels" | "select" | "test" | "done" | "status"
  | "zones" | "aitest" | "events" | "hours" | "incidents" | "guardmode" | "alarms" | "settings";

const TOOLS: { key: Step; label: string }[] = [
  { key: "incidents", label: "Incidents" },
  { key: "guardmode", label: "Guard Mode" },
  { key: "zones", label: "Zones" },
  { key: "alarms", label: "Alarms" },
  { key: "settings", label: "Settings" },
  { key: "aitest", label: "Test AI" },
  { key: "events", label: "Events" },
  { key: "hours", label: "Hours" },
  { key: "status", label: "Status" },
];

const FLOW: { key: Step; label: string }[] = [
  { key: "pc", label: "Computer" },
  { key: "scan", label: "Scan" },
  { key: "devices", label: "Devices" },
  { key: "channels", label: "Channels" },
  { key: "select", label: "Choose" },
  { key: "test", label: "Test" },
];

export default function App() {
  const [step, setStep] = useState<Step>("welcome");
  const [devices, setDevices] = useState<Device[]>([]);
  const [cameras, setCameras] = useState<Camera[]>([]);
  const [limit, setLimit] = useState(2);
  const { person, refresh: refreshPerson } = usePerson();

  const refresh = useCallback(async () => {
    const [d, c] = await Promise.all([
      api.get<{ devices: Device[] }>("/devices"),
      api.get<{ cameras: Camera[]; max_guard_cameras: number }>("/cameras"),
    ]);
    setDevices(d.devices);
    setCameras(c.cameras);
    setLimit(c.max_guard_cameras);
  }, []);

  useEffect(() => {
    if (!hasToken()) return;
    refresh()
      .then(() => undefined)
      .catch(() => undefined);
  }, [refresh]);

  if (!hasToken()) {
    return (
      <Shell step={step} setStep={setStep}>
        <Screen
          title="Open Guard setup from its shortcut"
          lead="For security, this page only works when it's opened by Boombiz Guard on this computer. Close this tab and use the Boombiz Guard shortcut on the desktop or in the Start menu."
        />
      </Shell>
    );
  }

  const selected = cameras.filter((c) => c.guard_enabled);
  const personNeeded = ["incidents", "guardmode", "alarms", "settings"].includes(step);

  return (
    <Shell step={step} setStep={setStep}>
      {personNeeded && <SignInBar person={person} onChange={refreshPerson} />}
      {step === "welcome" && <Welcome onStart={() => setStep("pc")} />}
      {step === "pc" && <PcCheck onNext={() => setStep("scan")} />}
      {step === "scan" && (
        <Scan
          onDone={async () => {
            await refresh();
            setStep("devices");
          }}
          onManual={() => setStep("devices")}
        />
      )}
      {step === "devices" && (
        <Devices
          devices={devices}
          refresh={refresh}
          onRescan={() => setStep("scan")}
          onNext={() => setStep("channels")}
        />
      )}
      {step === "channels" && (
        <Channels devices={devices} cameras={cameras} refresh={refresh} onNext={() => setStep("select")} />
      )}
      {step === "select" && (
        <Select cameras={cameras} limit={limit} refresh={refresh} onNext={() => setStep("test")} />
      )}
      {step === "test" && (
        <TestStep cameras={selected} refresh={refresh} onBack={() => setStep("select")} onNext={() => setStep("done")} />
      )}
      {step === "done" && <Done cameras={selected} onStatus={() => setStep("status")} />}
      {step === "status" && <Status cameras={cameras} refresh={refresh} />}
      {step === "zones" && <ZonesView />}
      {step === "aitest" && <AiTestView />}
      {step === "events" && <EventsView />}
      {step === "hours" && <HoursView />}
      {step === "incidents" && <IncidentsView person={person} />}
      {step === "guardmode" && <GuardModeView person={person} />}
      {step === "alarms" && <AlarmsView />}
      {step === "settings" && <SettingsView person={person} />}
    </Shell>
  );
}

// ── chrome ────────────────────────────────────────────────────────────
function Shell({ step, setStep, children }: { step: Step; setStep: (s: Step) => void; children: React.ReactNode }) {
  const idx = FLOW.findIndex((f) => f.key === step);
  return (
    <div className="flex min-h-screen flex-col">
      <header className="bg-guard-ink text-white">
        <div className="mx-auto flex max-w-4xl items-center justify-between gap-4 px-4 py-3 sm:px-6">
          <button type="button" onClick={() => setStep("welcome")} aria-label="Setup">
            <Lockup />
          </button>
          <nav className="flex flex-wrap justify-end gap-x-4 gap-y-1" aria-label="Guard tools">
            {TOOLS.map((t) => (
              <button
                key={t.key}
                type="button"
                onClick={() => setStep(t.key)}
                aria-current={step === t.key ? "page" : undefined}
                className={`text-sm font-semibold hover:text-guard-500 ${step === t.key ? "text-guard-500" : "text-white"}`}
              >
                {t.label}
              </button>
            ))}
          </nav>
        </div>
        {idx >= 0 && (
          <ol className="mx-auto flex max-w-4xl flex-wrap gap-x-4 gap-y-1 px-4 pb-3 text-xs sm:px-6" aria-label="Setup steps">
            {FLOW.map((f, i) => (
              <li
                key={f.key}
                aria-current={i === idx ? "step" : undefined}
                className={i === idx ? "font-bold text-guard-500" : i < idx ? "text-white" : "text-white/60"}
              >
                {i + 1}. {f.label}
              </li>
            ))}
          </ol>
        )}
      </header>
      <main className="flex-1">{children}</main>
      <footer className="border-t border-slate-200 py-4 text-center text-xs text-slate-600">
        Guard never sends your CCTV password or video to the internet during setup.
      </footer>
    </div>
  );
}

// ── 1. welcome ────────────────────────────────────────────────────────
function Welcome({ onStart }: { onStart: () => void }) {
  return (
    <Screen
      title="Boombiz Guard"
      lead="Connect your existing CCTV to intelligent monitoring. This takes about 10 minutes and doesn't change anything on the CCTV recorder."
      actions={
        <button type="button" className="btn-primary" onClick={onStart}>
          Start setup <ArrowRight className="h-4 w-4" />
        </button>
      }
    >
      <ul className="grid gap-3 sm:grid-cols-3">
        {[
          { icon: Cctv, t: "Uses the CCTV already in the shop" },
          { icon: ShieldCheck, t: "Password stays encrypted on this computer" },
          { icon: Check, t: "Your recorder keeps recording as normal" },
        ].map(({ icon: Icon, t }) => (
          <li key={t} className="card flex items-start gap-3 p-4 text-sm text-guard-ink">
            <Icon className="mt-0.5 h-5 w-5 shrink-0" aria-hidden />
            {t}
          </li>
        ))}
      </ul>
    </Screen>
  );
}

// ── 2. computer check ─────────────────────────────────────────────────
function PcCheck({ onNext }: { onNext: () => void }) {
  const [h, setH] = useState<Health | null>(null);
  const [err, setErr] = useState<string | null>(null);
  useEffect(() => {
    api.get<Health>("/system/health").then(setH).catch((e) => setErr(e.message));
  }, []);
  const verdict = useMemo(() => {
    if (!h) return null;
    const s = h.system;
    if (!s.ffmpeg_available || !s.ffprobe_available) return { tone: "bad", text: "Guard's video tools are missing. Reinstall Boombiz Guard." };
    if (s.ram_total_gb < 4 || s.disk_free_gb < 5) return { tone: "bad", text: "Not recommended. This computer is too small to run Guard alongside the POS." };
    if (s.ram_total_gb < 7.5 || s.cpu_count < 4) return { tone: "warn", text: "Limited. Use one Guard camera, on its substream." };
    return { tone: "good", text: "Good. This computer can run Guard on 2 cameras." };
  }, [h]);
  return (
    <Screen
      step="Step 1"
      title="Checking this computer"
      lead="A quick check of memory, disk and Guard's video tools. The full performance benchmark comes with the AI setup."
      actions={
        <button type="button" className="btn-primary" disabled={!verdict || verdict.tone === "bad"} onClick={onNext}>
          Continue <ArrowRight className="h-4 w-4" />
        </button>
      }
    >
      <ErrorNote message={err} />
      {!h && !err && <Spinner label="Checking…" />}
      {h && (
        <div className="grid gap-3 sm:grid-cols-3">
          <Stat icon={Cpu} label="Processor" value={`${h.system.cpu_count} cores · ${Math.round(h.system.cpu_percent)}% busy`} />
          <Stat icon={MemoryStick} label="Memory" value={`${h.system.ram_total_gb} GB`} />
          <Stat icon={HardDrive} label="Free disk" value={`${h.system.disk_free_gb} GB`} />
        </div>
      )}
      {verdict && (
        <p
          className={`mt-5 border-l-4 px-3 py-2 text-sm font-semibold text-guard-ink ${
            verdict.tone === "good" ? "border-emerald-600 bg-emerald-50" : verdict.tone === "warn" ? "border-guard-500 bg-guard-50" : "border-red-600 bg-red-50"
          }`}
        >
          {verdict.text}
        </p>
      )}
    </Screen>
  );
}

function Stat({ icon: Icon, label, value }: { icon: typeof Cpu; label: string; value: string }) {
  return (
    <div className="card p-4">
      <p className="flex items-center gap-2 text-xs font-semibold uppercase tracking-wider text-slate-600">
        <Icon className="h-4 w-4" aria-hidden /> {label}
      </p>
      <p className="mt-1 text-lg font-bold text-guard-ink">{value}</p>
    </div>
  );
}

// ── 3. scan ───────────────────────────────────────────────────────────
const STAGE_TEXT: Record<DiscoveryStatus["stage"], string> = {
  idle: "Starting…",
  onvif: "Asking CCTV devices to announce themselves…",
  network: "Looking for recorders and cameras on the shop network…",
  identify: "Identifying what was found…",
  done: "Done",
};

function Scan({ onDone, onManual }: { onDone: () => void; onManual: () => void }) {
  const [status, setStatus] = useState<DiscoveryStatus | null>(null);
  const [err, setErr] = useState<string | null>(null);
  useEffect(() => {
    let alive = true;
    let timer: number | undefined;
    const poll = async () => {
      try {
        const r = await api.get<{ status: DiscoveryStatus }>("/discovery/status");
        if (!alive) return;
        setStatus(r.status);
        if (r.status.error) setErr(r.status.error);
        if (r.status.stage === "done") {
          onDone();
          return;
        }
      } catch (e) {
        if (alive) setErr((e as Error).message);
      }
      timer = window.setTimeout(poll, 700);
    };
    api
      .post("/discovery/start")
      .then(poll)
      .catch((e) => setErr(e.message));
    return () => {
      alive = false;
      window.clearTimeout(timer);
    };
  }, [onDone]);
  const pct = status && status.total ? Math.round((status.checked / status.total) * 100) : 0;
  return (
    <Screen
      step="Step 2"
      title="Searching for CCTV systems"
      lead="This normally takes less than a minute. Guard only looks for CCTV equipment and doesn't try any passwords."
      actions={
        <button type="button" className="btn-outline" onClick={onManual}>
          Add a camera manually instead
        </button>
      }
    >
      <ErrorNote message={err} />
      <div className="card p-5">
        <Spinner label={status ? STAGE_TEXT[status.stage] : "Starting…"} />
        <div className="mt-4 h-2 w-full bg-slate-100" role="progressbar" aria-valuenow={pct} aria-valuemin={0} aria-valuemax={100}>
          <div className="h-2 bg-guard-500 transition-all" style={{ width: `${status?.stage === "network" ? pct : status?.stage === "identify" ? 100 : 5}%` }} />
        </div>
      </div>
    </Screen>
  );
}

// ── 4. devices + authentication + manual add ──────────────────────────
function deviceTitle(d: Device) {
  if (d.name && d.source === "MANUAL") return d.name;
  const kind = d.device_type === "UNKNOWN" ? "CCTV device" : d.device_type === "CAMERA" ? "camera" : d.device_type;
  if (d.manufacturer) return `${d.manufacturer} ${kind}`;
  return d.device_type === "CAMERA" ? "IP camera" : kind === "CCTV device" ? "CCTV device" : kind;
}

function Devices({
  devices,
  refresh,
  onRescan,
  onNext,
}: {
  devices: Device[];
  refresh: () => Promise<void>;
  onRescan: () => void;
  onNext: () => void;
}) {
  const [connecting, setConnecting] = useState<Device | null>(null);
  const [manual, setManual] = useState(false);
  const connected = devices.filter((d) => !d.authentication_required && d.channel_count > 0);
  return (
    <Screen
      step="Step 3"
      title={devices.length ? `${devices.length} CCTV ${devices.length === 1 ? "system" : "systems"} found` : "No CCTV found yet"}
      lead={
        devices.length
          ? "Connect to the recorder (DVR/NVR) if there is one: it gives Guard every camera at once."
          : "Check that the recorder is switched on and plugged into the same router as this computer, then scan again. Or add it by its IP address."
      }
      actions={
        <>
          <button type="button" className="btn-primary" disabled={!connected.length} onClick={onNext}>
            See cameras <ArrowRight className="h-4 w-4" />
          </button>
          <button type="button" className="btn-outline" onClick={onRescan}>
            <RefreshCw className="h-4 w-4" /> Scan again
          </button>
          <button type="button" className="btn-outline" onClick={() => setManual(true)}>
            <Plus className="h-4 w-4" /> Add manually
          </button>
        </>
      }
    >
      <ul className="space-y-3">
        {devices.map((d) => (
          <li key={d.id} className="card flex flex-col gap-3 p-4 sm:flex-row sm:items-center sm:justify-between">
            <div className="min-w-0">
              <p className="font-bold text-guard-ink">{deviceTitle(d)}</p>
              <p className="text-sm text-slate-700">
                {d.ip_address}
                {d.model ? ` · ${d.model}` : ""}
                {d.channel_count ? ` · ${d.channel_count} ${d.channel_count === 1 ? "channel" : "channels"}` : ""}
              </p>
              <div className="mt-1.5">
                <CompatBadge status={d.compatibility} />
              </div>
              {d.compatibility_reason && <p className="mt-1.5 text-sm text-slate-700">{d.compatibility_reason}</p>}
              {d.auth_error && !d.compatibility_reason && <p className="mt-1.5 text-sm text-red-800">{d.auth_error}</p>}
            </div>
            {d.compatibility !== "INCOMPATIBLE" && (
              <button type="button" className={d.authentication_required ? "btn-primary" : "btn-outline"} onClick={() => setConnecting(d)}>
                {d.authentication_required ? "Connect" : "Reconnect"}
              </button>
            )}
          </li>
        ))}
      </ul>
      {connecting && (
        <AuthDialog
          device={connecting}
          onClose={() => setConnecting(null)}
          onDone={async () => {
            setConnecting(null);
            await refresh();
          }}
        />
      )}
      {manual && (
        <ManualDialog
          onClose={() => setManual(false)}
          onDone={async () => {
            setManual(false);
            await refresh();
          }}
        />
      )}
    </Screen>
  );
}

function Dialog({ title, onClose, children }: { title: string; onClose: () => void; children: React.ReactNode }) {
  useEffect(() => {
    const k = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    document.addEventListener("keydown", k);
    return () => document.removeEventListener("keydown", k);
  }, [onClose]);
  return (
    <div className="fixed inset-0 z-50 flex items-end justify-center bg-black/50 sm:items-center" role="dialog" aria-modal="true" aria-label={title}>
      <div className="max-h-[92vh] w-full max-w-md overflow-y-auto bg-white p-5 sm:p-6">
        <div className="mb-4 flex items-start justify-between gap-4">
          <h2 className="text-lg font-bold text-guard-ink">{title}</h2>
          <button type="button" onClick={onClose} aria-label="Close" className="-m-2 p-2 text-slate-700 hover:text-guard-ink">
            <X className="h-5 w-5" />
          </button>
        </div>
        {children}
      </div>
    </div>
  );
}

function AuthDialog({ device, onClose, onDone }: { device: Device; onClose: () => void; onDone: () => void }) {
  const [username, setUsername] = useState("admin");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [ok, setOk] = useState(false);
  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setErr(null);
    try {
      const r = await api.post<{ ok: boolean; error?: string }>(`/devices/${device.id}/authenticate`, { username, password });
      if (r.ok) {
        setOk(true);
        window.setTimeout(onDone, 700);
      } else setErr(r.error ?? "Connection failed.");
    } catch (e2) {
      setErr((e2 as Error).message);
    } finally {
      setBusy(false);
    }
  }
  return (
    <Dialog title={`Connect to ${deviceTitle(device)}`} onClose={onClose}>
      <form onSubmit={submit} className="space-y-4">
        <p className="text-sm text-slate-700">
          Use the CCTV login the business owner gave you. Guard tries it once and never guesses. If nobody knows it, use the manufacturer's official password recovery.
        </p>
        <div>
          <label className="label" htmlFor="u">Username</label>
          <input id="u" className="field" autoComplete="off" value={username} onChange={(e) => setUsername(e.target.value)} required />
        </div>
        <div>
          <label className="label" htmlFor="p">Password</label>
          <input id="p" className="field" type="password" autoComplete="off" value={password} onChange={(e) => setPassword(e.target.value)} />
        </div>
        <ErrorNote message={err} />
        {ok && <p className="text-sm font-semibold text-emerald-800">Connection successful ✓</p>}
        <button type="submit" className="btn-primary w-full" disabled={busy || ok}>
          {busy ? <Spinner label="Testing connection…" /> : "Test connection"}
        </button>
      </form>
    </Dialog>
  );
}

function ManualDialog({ onClose, onDone }: { onClose: () => void; onDone: () => void }) {
  const [f, setF] = useState({ name: "", ip_address: "", port: "", username: "admin", password: "", connection_type: "auto", rtsp_url: "" });
  const [advanced, setAdvanced] = useState(false);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const set = (k: keyof typeof f) => (e: React.ChangeEvent<HTMLInputElement | HTMLSelectElement>) => setF({ ...f, [k]: e.target.value });
  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setErr(null);
    try {
      const r = await api.post<{ ok: boolean; error?: string }>("/devices/manual", {
        ...f,
        port: f.port ? Number(f.port) : null,
        rtsp_url: advanced && f.rtsp_url ? f.rtsp_url : null,
      });
      if (r.ok) onDone();
      else setErr(r.error ?? "Connection failed.");
    } catch (e2) {
      setErr((e2 as Error).message);
    } finally {
      setBusy(false);
    }
  }
  return (
    <Dialog title="Add a camera manually" onClose={onClose}>
      <form onSubmit={submit} className="space-y-4">
        <div>
          <label className="label" htmlFor="mn">Device name</label>
          <input id="mn" className="field" value={f.name} onChange={set("name")} placeholder="e.g. Shop DVR" />
        </div>
        <div className="grid grid-cols-3 gap-3">
          <div className="col-span-2">
            <label className="label" htmlFor="mi">IP address</label>
            <input id="mi" className="field" value={f.ip_address} onChange={set("ip_address")} placeholder="192.168.1.64" inputMode="decimal" required={!advanced} />
          </div>
          <div>
            <label className="label" htmlFor="mp">Port</label>
            <input id="mp" className="field" value={f.port} onChange={set("port")} placeholder="80" inputMode="numeric" />
          </div>
        </div>
        <div>
          <label className="label" htmlFor="mt">Connection type</label>
          <select id="mt" className="field" value={f.connection_type} onChange={set("connection_type")}>
            <option value="auto">Automatic (recommended)</option>
            <option value="onvif">ONVIF</option>
            <option value="rtsp">RTSP</option>
            <option value="hikvision">Hikvision</option>
            <option value="dahua">Dahua</option>
          </select>
        </div>
        <div className="grid grid-cols-2 gap-3">
          <div>
            <label className="label" htmlFor="mu">Username</label>
            <input id="mu" className="field" autoComplete="off" value={f.username} onChange={set("username")} />
          </div>
          <div>
            <label className="label" htmlFor="mw">Password</label>
            <input id="mw" className="field" type="password" autoComplete="off" value={f.password} onChange={set("password")} />
          </div>
        </div>
        <button type="button" className="text-sm font-semibold text-guard-ink underline" onClick={() => setAdvanced((v) => !v)}>
          {advanced ? "Hide" : "I have an RTSP address"}
        </button>
        {advanced && (
          <div>
            <label className="label" htmlFor="mr">RTSP address</label>
            <input id="mr" className="field font-mono text-sm" value={f.rtsp_url} onChange={set("rtsp_url")} placeholder="rtsp://192.168.1.64:554/stream1" />
            <p className="mt-1 text-xs text-slate-600">Leave the password out of the address; put it in the password box above.</p>
          </div>
        )}
        <ErrorNote message={err} />
        <button type="submit" className="btn-primary w-full" disabled={busy}>
          {busy ? <Spinner label="Connecting…" /> : "Add and connect"}
        </button>
      </form>
    </Dialog>
  );
}

// ── 5. channels ───────────────────────────────────────────────────────
function Channels({ devices, cameras, refresh, onNext }: { devices: Device[]; cameras: Camera[]; refresh: () => Promise<void>; onNext: () => void }) {
  const [live, setLive] = useState<Camera | null>(null);
  const byDevice = devices.filter((d) => cameras.some((c) => c.device_id === d.id));
  useEffect(() => {
    refresh().catch(() => undefined);
  }, [refresh]);
  return (
    <Screen
      step="Step 4"
      title="Camera channels"
      lead="Every camera Guard can see. Tap a picture to watch it live."
      actions={
        <button type="button" className="btn-primary" onClick={onNext} disabled={!cameras.some((c) => c.online)}>
          Choose Guard cameras <ArrowRight className="h-4 w-4" />
        </button>
      }
    >
      {byDevice.map((d) => (
        <div key={d.id} className="mb-8">
          <h2 className="mb-3 text-sm font-bold uppercase tracking-wider text-slate-700">
            {deviceTitle(d)} · {d.ip_address}
          </h2>
          <ul className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
            {cameras
              .filter((c) => c.device_id === d.id)
              .map((c) => (
                <li key={c.id} className="card overflow-hidden">
                  {c.online ? (
                    <button type="button" className="block w-full" onClick={() => setLive(c)} aria-label={`Watch ${c.name} live`}>
                      <Snapshot cameraId={c.id} />
                    </button>
                  ) : (
                    <div className="flex aspect-video items-center justify-center bg-slate-100 text-sm text-slate-700">Offline</div>
                  )}
                  <div className="flex items-center justify-between gap-2 p-3">
                    <p className="truncate text-sm font-semibold text-guard-ink">
                      {c.channel_number}. {c.name}
                    </p>
                    <span className="shrink-0 text-xs text-slate-700">{c.has_sub_stream ? "Main + sub" : c.has_main_stream ? "Main only" : "—"}</span>
                  </div>
                </li>
              ))}
          </ul>
        </div>
      ))}
      {live && (
        <Dialog title={live.name} onClose={() => setLive(null)}>
          <Snapshot cameraId={live.id} live />
          <p className="mt-3 text-xs text-slate-600">Live view through Guard. The CCTV password never reaches this page.</p>
        </Dialog>
      )}
    </Screen>
  );
}

// ── 6. select ─────────────────────────────────────────────────────────
function Select({ cameras, limit, refresh, onNext }: { cameras: Camera[]; limit: number; refresh: () => Promise<void>; onNext: () => void }) {
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const chosen = cameras.filter((c) => c.guard_enabled);
  async function toggle(c: Camera) {
    setErr(null);
    setBusy(c.id);
    try {
      await api.post(`/cameras/${c.id}/${c.guard_enabled ? "disable" : "enable"}`);
      await refresh();
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(null);
    }
  }
  return (
    <Screen
      step="Step 5"
      title="Choose cameras for Guard"
      lead={`Guard Basic watches up to ${limit} cameras. Usually the main shelves and the exit. The others stay as normal CCTV.`}
      actions={
        <button type="button" className="btn-primary" disabled={!chosen.length} onClick={onNext}>
          Test {chosen.length} {chosen.length === 1 ? "camera" : "cameras"} <ArrowRight className="h-4 w-4" />
        </button>
      }
    >
      <p className="mb-3 text-sm font-semibold text-guard-ink">
        {chosen.length} of {limit} chosen
      </p>
      <ErrorNote message={err} />
      <ul className="mt-3 divide-y divide-slate-200 border border-slate-200">
        {cameras.map((c) => {
          const full = !c.guard_enabled && chosen.length >= limit;
          const unusable = !c.online || c.compatibility === "INCOMPATIBLE";
          return (
            <li key={c.id}>
              <label className={`flex min-h-[56px] items-center gap-3 px-4 py-3 ${full || unusable ? "opacity-60" : "cursor-pointer hover:bg-slate-50"}`}>
                <input
                  type="checkbox"
                  className="h-5 w-5 accent-guard-ink"
                  checked={c.guard_enabled}
                  disabled={busy === c.id || (!c.guard_enabled && (full || unusable))}
                  onChange={() => toggle(c)}
                />
                <span className="flex-1 text-[15px] font-medium text-guard-ink">{c.name}</span>
                <span className="text-xs text-slate-700">{!c.online ? "Offline" : c.guard_enabled ? "Guard on" : "CCTV only"}</span>
              </label>
            </li>
          );
        })}
      </ul>
    </Screen>
  );
}

// ── 7. connection test ────────────────────────────────────────────────
function TestStep({ cameras, refresh, onBack, onNext }: { cameras: Camera[]; refresh: () => Promise<void>; onBack: () => void; onNext: () => void }) {
  const [results, setResults] = useState<Record<string, TestPayload | { error: string }>>({});
  const [running, setRunning] = useState(false);
  const run = useCallback(async () => {
    setRunning(true);
    setResults({});
    await Promise.all(
      cameras.map(async (c) => {
        try {
          const r = await api.post<TestPayload>(`/cameras/${c.id}/test-stream`, {});
          setResults((s) => ({ ...s, [c.id]: r }));
        } catch (e) {
          setResults((s) => ({ ...s, [c.id]: { error: (e as Error).message } }));
        }
      }),
    );
    await refresh();
    setRunning(false);
  }, [cameras, refresh]);
  useEffect(() => {
    void run();
    // run once on arrival; re-run is manual
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  const allGood =
    cameras.length > 0 &&
    cameras.every((c) => {
      const r = results[c.id];
      return r && "report" in r && (r.report.status === "COMPATIBLE" || r.report.status === "LIMITED");
    });
  return (
    <Screen
      step="Step 6"
      title="Connection test"
      lead="Guard watches each stream for about 20 seconds to make sure it's steady before relying on it."
      actions={
        <>
          <button type="button" className="btn-primary" disabled={running || !allGood} onClick={onNext}>
            Finish <ArrowRight className="h-4 w-4" />
          </button>
          <button type="button" className="btn-outline" disabled={running} onClick={run}>
            <RefreshCw className="h-4 w-4" /> Test again
          </button>
          <button type="button" className="btn-outline" disabled={running} onClick={onBack}>
            Change cameras
          </button>
        </>
      }
    >
      <ul className="grid gap-4 sm:grid-cols-2">
        {cameras.map((c) => {
          const r = results[c.id];
          return (
            <li key={c.id} className="card p-4">
              <div className="flex items-center justify-between gap-2">
                <p className="font-bold text-guard-ink">{c.name}</p>
                {r && "report" in r && <CompatBadge status={r.report.status} />}
              </div>
              {!r && <div className="mt-3"><Spinner label="Testing… about 25 seconds" /></div>}
              {r && "error" in r && <div className="mt-3"><ErrorNote message={r.error} /></div>}
              {r && "report" in r && (
                <>
                  <ul className="mt-3 space-y-1.5 text-sm">
                    {r.report.checks
                      .filter((k) => k.mandatory || k.key === "substream")
                      .map((k) => (
                        <li key={k.key} className="flex items-start gap-2 text-guard-ink">
                          {k.passed ? (
                            <Check className="mt-0.5 h-4 w-4 shrink-0 text-emerald-700" aria-label="passed" />
                          ) : (
                            <CircleAlert className="mt-0.5 h-4 w-4 shrink-0 text-red-700" aria-label="failed" />
                          )}
                          <span>
                            {k.label}
                            {k.note && <span className="block text-xs text-slate-700">{k.note}</span>}
                          </span>
                        </li>
                      ))}
                  </ul>
                  {r.test?.connected && (
                    <p className="mt-3 text-xs text-slate-700">
                      {r.test.width}×{r.test.height} · {r.test.measured_fps} fps over {r.test.seconds_run}s
                    </p>
                  )}
                  <p className="mt-2 text-sm text-guard-ink">{r.report.summary}</p>
                </>
              )}
            </li>
          );
        })}
      </ul>
    </Screen>
  );
}

// ── 8. done ───────────────────────────────────────────────────────────
function Done({ cameras, onStatus }: { cameras: Camera[]; onStatus: () => void }) {
  return (
    <Screen
      title="CCTV setup completed"
      lead={`${cameras.length} ${cameras.length === 1 ? "camera is" : "cameras are"} ready for Boombiz Guard AI. Guard keeps these streams connected, and reconnects by itself after a power cut or restart.`}
      actions={
        <button type="button" className="btn-dark" onClick={onStatus}>
          View status
        </button>
      }
    >
      <ul className="space-y-2">
        {cameras.map((c) => (
          <li key={c.id} className="flex items-center gap-2 text-[15px] font-semibold text-guard-ink">
            <Check className="h-5 w-5 text-emerald-700" aria-hidden /> {c.name}
          </li>
        ))}
      </ul>
    </Screen>
  );
}

// ── status ────────────────────────────────────────────────────────────
function Status({ cameras, refresh }: { cameras: Camera[]; refresh: () => Promise<void> }) {
  const [h, setH] = useState<Health | null>(null);
  const [err, setErr] = useState<string | null>(null);
  useEffect(() => {
    let alive = true;
    const tick = () =>
      api
        .get<Health>("/system/health")
        .then((r) => alive && (setH(r), setErr(null)))
        .catch((e) => alive && setErr(e.message));
    void tick();
    void refresh();
    const t = window.setInterval(tick, 3000);
    return () => {
      alive = false;
      window.clearInterval(t);
    };
  }, [refresh]);
  const guard = cameras.filter((c) => c.guard_enabled);
  const name = (id: string) => cameras.find((c) => c.id === id)?.name ?? "Camera";
  return (
    <Screen title="Guard status" lead="Live connection health for the cameras Guard watches.">
      <ErrorNote message={err} />
      {h && (
        <>
          <div className="grid gap-3 sm:grid-cols-3">
            <Stat icon={Cctv} label="Guard cameras" value={`${h.guard_cameras.online} / ${h.guard_cameras.total} online`} />
            <Stat icon={Cpu} label="CPU" value={`${Math.round(h.system.cpu_percent)}%`} />
            <Stat icon={MemoryStick} label="Memory" value={`${Math.round(h.system.ram_percent)}% used`} />
          </div>
          <ul className="mt-6 space-y-3">
            {guard.map((c) => {
              const s = h.streams[c.id];
              return (
                <li key={c.id} className="card flex flex-col gap-1 p-4 sm:flex-row sm:items-center sm:justify-between">
                  <p className="font-bold text-guard-ink">{c.name}</p>
                  <p className="text-sm text-guard-ink">
                    {s ? `${s.status} · ${s.decode_fps} fps · ${s.reconnect_count} reconnects` : "Starting…"}
                    {s?.last_error && <span className="block text-xs text-red-800">{s.last_error}</span>}
                  </p>
                </li>
              );
            })}
            {!guard.length && <li className="text-sm text-slate-700">No cameras chosen for Guard yet.</li>}
          </ul>
          {h.events.length > 0 && (
            <>
              <h2 className="mt-8 text-sm font-bold uppercase tracking-wider text-slate-700">Recent events</h2>
              <ul className="mt-2 divide-y divide-slate-200 border border-slate-200 text-sm">
                {[...h.events].reverse().slice(0, 12).map((e, i) => (
                  <li key={i} className="flex justify-between gap-3 px-3 py-2 text-guard-ink">
                    <span>
                      {e.event.replace(/_/g, " ").toLowerCase()} · {name(e.camera_id)}
                    </span>
                    <time className="shrink-0 text-slate-700">{new Date(e.at).toLocaleTimeString()}</time>
                  </li>
                ))}
              </ul>
            </>
          )}
        </>
      )}
      {!h && !err && <Spinner label="Loading…" />}
      <div className="mt-8">
        <a href="#" className="inline-flex items-center gap-1 text-sm font-semibold text-guard-ink underline" onClick={(e) => (e.preventDefault(), window.location.reload())}>
          <Search className="h-4 w-4" aria-hidden /> Back to setup
        </a>
      </div>
    </Screen>
  );
}
