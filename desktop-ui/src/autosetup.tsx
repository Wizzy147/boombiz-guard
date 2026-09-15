import { useCallback, useEffect, useRef, useState } from "react";
import {
  ArrowRight,
  Cctv,
  Check,
  CircleAlert,
  Cpu,
  ExternalLink,
  Film,
  HardDrive,
  RefreshCw,
  ShieldCheck,
  Wifi,
  Wrench,
} from "lucide-react";
import {
  api,
  type FoundDevice,
  type GuardTestStatus,
  type Recommendation,
  type SetupChecks,
  type SetupState,
} from "./api";
import { ErrorNote, Screen, Snapshot, Spinner } from "./ui";
import { PowerChecklist } from "./power";

/*
 * Auto Setup — plug-and-play (owner decision 2026-09-15).
 *
 *   Welcome → three automatic checks → "We found your CCTV system" (CCTV
 *   username/password only) → recommended cameras → where products are /
 *   where customers leave → Guard Test → YOUR BUSINESS IS PROTECTED
 *
 * Without a licence (Compatibility & Demo mode) the camera step becomes
 * "Your business is Guard Ready" → Activate Boombiz Guard → browser sign-in
 * and payment → back here, and setup carries on by itself.
 *
 * Words the merchant never sees here: RTSP, ONVIF, H.264, port, stream URL,
 * substream, IP address. Those live in Advanced Setup.
 */

type Stage = "welcome" | "checks" | "found" | "survey" | "ready" | "waiting" | "cameras" | "areas" | "test" | "protected" | "help";

type HelpReason = "no_cctv_found" | "cctv_password_unknown" | "cctv_incompatible" | "computer_too_slow" | "no_network" | "test_failed";

const HELP_TEXT: Record<HelpReason, string> = {
  no_cctv_found: "Guard couldn't find a CCTV recorder or camera on this computer's network.",
  cctv_password_unknown: "Guard needs the CCTV recorder's username and password to see the cameras.",
  cctv_incompatible: "Your CCTV doesn't give Guard a video feed on the shop network — some cloud-only and Wi-Fi cameras don't.",
  computer_too_slow: "This computer is too slow to run Guard AI alongside your POS.",
  no_network: "This computer isn't connected to the shop network.",
  test_failed: "Guard Test didn't pass on this camera.",
};

const HELP_TIPS: Record<HelpReason, string[]> = {
  no_cctv_found: [
    "Check the CCTV recorder is switched on.",
    "Check this computer and the recorder are plugged into the same router or switch.",
  ],
  cctv_password_unknown: [
    "The installer who fitted your CCTV usually has it.",
    "The recorder's own screen can reset it — follow the manufacturer's official password recovery.",
  ],
  cctv_incompatible: ["A Boombiz technician can connect it through a recorder, or suggest a compatible camera."],
  computer_too_slow: ["Guard works best on a Core i5-class computer with 8 GB of memory."],
  no_network: ["Plug this computer into the same router as the CCTV recorder, ideally with a cable."],
  test_failed: ["Check the camera's picture is clear and the areas you drew are right, then test again."],
};

async function post<T>(path: string, body?: unknown) {
  return api.post<T>(path, body);
}

function usePoll(fn: () => Promise<boolean | void>, ms: number, on: boolean) {
  const ref = useRef(fn);
  ref.current = fn;
  useEffect(() => {
    if (!on) return;
    let alive = true;
    let t: number | undefined;
    const tick = async () => {
      let stop = false;
      try {
        stop = (await ref.current()) === true;
      } catch {
        /* keep polling; the screen shows its own error */
      }
      if (alive && !stop) t = window.setTimeout(tick, ms);
    };
    void tick();
    return () => {
      alive = false;
      window.clearTimeout(t);
    };
  }, [ms, on]);
}

export function AutoSetup({ onAdvanced, onFinished }: { onAdvanced: () => void; onFinished: () => void }) {
  const [stage, setStage] = useState<Stage>("welcome");
  const [help, setHelp] = useState<HelpReason | null>(null);
  const [rec, setRec] = useState<Recommendation | null>(null);
  const [cloudUrl, setCloudUrl] = useState("https://guard.getboombiz.com");

  useEffect(() => {
    api.get<{ cloud_url: string }>("/cloud/status").then((s) => setCloudUrl(s.cloud_url)).catch(() => undefined);
  }, []);

  const needHelp = useCallback((reason: HelpReason) => {
    post("/setup/help", { reason }).catch(() => undefined);
    setHelp(reason);
    setStage("help");
  }, []);

  const toCameras = useCallback(async () => {
    setStage("survey");
    try {
      const r = await post<Recommendation>("/setup/survey");
      setRec(r);
      if (!r.usable) return needHelp("cctv_incompatible");
      setStage(r.licence.protects ? "cameras" : "ready");
    } catch {
      needHelp("cctv_incompatible");
    }
  }, [needHelp]);

  return (
    <>
      {stage === "welcome" && <Welcome onStart={() => setStage("checks")} onAdvanced={onAdvanced} />}
      {stage === "checks" && (
        <Checks
          onFound={() => setStage("found")}
          onNone={() => needHelp("no_cctv_found")}
          onNoNetwork={() => needHelp("no_network")}
        />
      )}
      {stage === "found" && (
        <Found onNext={toCameras} onRescan={() => setStage("checks")} onHelp={needHelp} />
      )}
      {stage === "survey" && (
        <Screen title="Looking at your cameras" lead="Guard checks each camera's picture and works out which ones matter most.">
          <Spinner label="Checking pictures… about 15 seconds" />
        </Screen>
      )}
      {stage === "ready" && rec && <Ready rec={rec} onActivate={() => setStage("waiting")} onHelp={needHelp} />}
      {stage === "waiting" && <WaitForLicence cloudUrl={cloudUrl} onLicensed={toCameras} onBack={() => setStage("ready")} />}
      {stage === "cameras" && rec && <Cameras rec={rec} onApplied={() => setStage("areas")} />}
      {stage === "areas" && <Areas onDone={() => setStage("test")} />}
      {stage === "test" && (
        <GuardTest
          onDone={async (passed) => {
            await post("/setup/complete", { test_passed: passed }).catch(() => undefined);
            setStage("protected");
          }}
          onHelp={() => needHelp("test_failed")}
          onBackToAreas={() => setStage("areas")}
        />
      )}
      {stage === "protected" && <Protected cloudUrl={cloudUrl} onFinished={onFinished} />}
      {stage === "help" && help && (
        <Help reason={help} cloudUrl={cloudUrl} onRetry={() => setStage("checks")} onAdvanced={onAdvanced} />
      )}
    </>
  );
}

// ── welcome ───────────────────────────────────────────────────────────
function Welcome({ onStart, onAdvanced }: { onStart: () => void; onAdvanced: () => void }) {
  return (
    <Screen
      title="Welcome to Boombiz Guard"
      lead="Let's turn your existing CCTV into intelligent business protection. Guard finds your cameras by itself — this usually takes 10 to 15 minutes, and nothing on your CCTV recorder changes."
      actions={
        <button type="button" className="btn-primary px-8 text-base" onClick={onStart}>
          Start Automatic Setup <ArrowRight className="h-4 w-4" />
        </button>
      }
    >
      <ul className="grid gap-3 sm:grid-cols-3">
        {[
          { icon: Cctv, t: "Works with the CCTV you already have" },
          { icon: ShieldCheck, t: "Your CCTV password stays encrypted on this computer" },
          { icon: Check, t: "Your recorder keeps recording as normal" },
        ].map(({ icon: Icon, t }) => (
          <li key={t} className="card flex items-start gap-3 p-4 text-sm text-guard-ink">
            <Icon className="mt-0.5 h-5 w-5 shrink-0" aria-hidden />
            {t}
          </li>
        ))}
      </ul>
      <p className="mt-8 text-sm text-slate-700">
        Boombiz technician or certified installer?{" "}
        <button type="button" className="font-semibold text-guard-ink underline" onClick={onAdvanced}>
          Advanced setup
        </button>
      </p>
    </Screen>
  );
}

// ── the three automatic checks ────────────────────────────────────────
function Row({ icon: Icon, label, state, detail }: {
  icon: typeof Cpu; label: string; state: "idle" | "running" | "ok" | "failed"; detail?: string | null;
}) {
  return (
    <li className="flex items-start gap-3 px-4 py-4">
      <Icon className="mt-0.5 h-5 w-5 shrink-0 text-guard-ink" aria-hidden />
      <div className="min-w-0 flex-1">
        <p className="font-semibold text-guard-ink">{label}</p>
        {detail && <p className="mt-0.5 text-sm text-slate-700">{detail}</p>}
      </div>
      <span className="shrink-0">
        {state === "ok" ? (
          <Check className="h-5 w-5 text-emerald-700" aria-label="Done" />
        ) : state === "failed" ? (
          <CircleAlert className="h-5 w-5 text-red-700" aria-label="Problem" />
        ) : (
          <Spinner />
        )}
      </span>
    </li>
  );
}

function Checks({ onFound, onNone, onNoNetwork }: { onFound: () => void; onNone: () => void; onNoNetwork: () => void }) {
  const [c, setC] = useState<SetupChecks | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const started = useRef(false);
  useEffect(() => {
    if (started.current) return;
    started.current = true;
    post<SetupChecks>("/setup/start").then(setC).catch((e) => setErr((e as Error).message));
  }, []);
  usePoll(async () => {
    const r = await api.get<SetupChecks>("/setup/checks");
    setC(r);
    if (!r.done) return false;
    window.setTimeout(() => {
      if (r.cctv.state === "ok") onFound();
      else if (r.network.state === "failed") onNoNetwork();
      else onNone();
    }, 900);
    return true;
  }, 1000, !err);

  const pct = c?.cctv.progress;
  return (
    <Screen title="Setting up Boombiz Guard" lead="Three quick checks, all at once. Guard only looks for CCTV equipment and never guesses passwords.">
      <ErrorNote message={err} />
      <ul className="card divide-y divide-slate-200">
        <Row icon={Cpu} label="Checking this computer" state={c?.computer.state ?? "running"}
          detail={c?.computer.state === "ok" || c?.computer.state === "failed" ? c.computer.message : "Measuring how many cameras it can protect"} />
        <Row icon={Wifi} label="Checking your network" state={c?.network.state ?? "running"}
          detail={c?.network.message ?? "Shop network and internet"} />
        <Row icon={Cctv} label="Searching for CCTV" state={c?.cctv.state ?? "running"}
          detail={c?.cctv.state === "ok" ? `${c.cctv.devices} CCTV ${c.cctv.devices === 1 ? "system" : "systems"} found`
            : c?.cctv.state === "failed" ? c.cctv.message : pct != null ? `Searching… ${pct}%` : "Asking cameras and recorders to answer"} />
      </ul>
    </Screen>
  );
}

// ── we found your CCTV ────────────────────────────────────────────────
function Found({ onNext, onRescan, onHelp }: { onNext: () => void; onRescan: () => void; onHelp: (r: HelpReason) => void }) {
  const [devices, setDevices] = useState<FoundDevice[] | null>(null);
  const [open, setOpen] = useState<string | null>(null);
  const load = useCallback(async () => {
    const r = await api.get<{ devices: FoundDevice[] }>("/setup/found");
    setDevices(r.devices);
    setOpen((cur) => cur ?? r.devices.find((d) => d.needs_login && !d.incompatible)?.id ?? null);
  }, []);
  useEffect(() => {
    void load();
  }, [load]);
  if (!devices) return <Screen title="We found your CCTV"><Spinner /></Screen>;

  const connected = devices.filter((d) => d.connected);
  const cams = connected.reduce((n, d) => n + d.channels, 0);
  const usable = devices.filter((d) => !d.incompatible);
  const main = usable[0] ?? devices[0];
  if (!usable.length) {
    return (
      <Screen title="We found CCTV, but Guard can't use it" lead={main?.problem ?? HELP_TEXT.cctv_incompatible}
        actions={<button type="button" className="btn-primary" onClick={() => onHelp("cctv_incompatible")}>Get help</button>} />
    );
  }

  return (
    <Screen
      title={connected.length ? `${cams} CCTV ${cams === 1 ? "camera" : "cameras"} found 🎉` : "We found your CCTV system 🎉"}
      lead={connected.length
        ? "Guard can see your cameras. Connect anything else below, or carry on."
        : "Enter your CCTV login once. Guard handles everything else."}
      actions={
        <>
          <button type="button" className="btn-primary" disabled={!connected.length} onClick={onNext}>
            Continue <ArrowRight className="h-4 w-4" />
          </button>
          <button type="button" className="btn-outline" onClick={onRescan}>
            <RefreshCw className="h-4 w-4" /> Search again
          </button>
        </>
      }
    >
      <ul className="space-y-3">
        {usable.map((d) => (
          <li key={d.id} className="card p-4 sm:p-5">
            <div className="flex flex-wrap items-center justify-between gap-3">
              <div>
                <p className="text-lg font-bold text-guard-ink">{d.title}</p>
                <p className="text-sm text-slate-700">
                  {d.connected ? `Connected · ${d.channels} ${d.channels === 1 ? "camera" : "cameras"}` : "Needs your CCTV login"}
                </p>
              </div>
              {d.connected ? (
                <span className="flex items-center gap-1.5 text-sm font-semibold text-emerald-800"><Check className="h-4 w-4" /> Connected</span>
              ) : open !== d.id ? (
                <button type="button" className="btn-primary" onClick={() => setOpen(d.id)}>Connect</button>
              ) : null}
            </div>
            {open === d.id && !d.connected && (
              <ConnectForm device={d} onConnected={async () => { setOpen(null); await load(); }}
                onForgot={() => onHelp("cctv_password_unknown")} />
            )}
          </li>
        ))}
      </ul>
      {devices.some((d) => d.incompatible) && (
        <p className="mt-4 text-sm text-slate-700">
          {devices.filter((d) => d.incompatible).length} other device{devices.filter((d) => d.incompatible).length === 1 ? "" : "s"} can't
          be used by Guard (no video on the shop network).
        </p>
      )}
    </Screen>
  );
}

function ConnectForm({ device, onConnected, onForgot }: { device: FoundDevice; onConnected: () => void; onForgot: () => void }) {
  const [username, setUsername] = useState("admin");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setErr(null);
    try {
      const r = await post<{ ok: boolean; error: string | null }>("/setup/connect", { device_id: device.id, username, password });
      if (r.ok) onConnected();
      else setErr(r.error ?? "Guard couldn't connect. Check the username and password.");
    } catch (e2) {
      setErr((e2 as Error).message);
    } finally {
      setBusy(false);
    }
  }
  return (
    <form onSubmit={submit} className="mt-4 grid gap-3 border-t border-slate-200 pt-4 sm:grid-cols-2">
      <p className="text-sm text-slate-700 sm:col-span-2">
        The login you use on the recorder's screen or its phone app. Guard tries it once and never guesses.
      </p>
      <div>
        <label className="label" htmlFor={`u-${device.id}`}>CCTV username</label>
        <input id={`u-${device.id}`} className="field" autoComplete="off" value={username} onChange={(e) => setUsername(e.target.value)} required />
      </div>
      <div>
        <label className="label" htmlFor={`p-${device.id}`}>CCTV password</label>
        <input id={`p-${device.id}`} className="field" type="password" autoComplete="off" value={password} onChange={(e) => setPassword(e.target.value)} />
      </div>
      <div className="sm:col-span-2"><ErrorNote message={err} /></div>
      <div className="flex flex-wrap items-center gap-3 sm:col-span-2">
        <button type="submit" className="btn-primary" disabled={busy}>{busy ? <Spinner label="Connecting…" /> : "Connect"}</button>
        <button type="button" className="text-sm font-semibold text-guard-ink underline" onClick={onForgot}>
          I don't know the CCTV password
        </button>
      </div>
    </form>
  );
}

// ── demo mode: Guard Ready → Activate ─────────────────────────────────
function Ready({ rec, onActivate, onHelp }: { rec: Recommendation; onActivate: () => void; onHelp: (r: HelpReason) => void }) {
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [checks, setChecks] = useState<SetupChecks | null>(null);
  useEffect(() => {
    api.get<SetupChecks>("/setup/checks").then(setChecks).catch(() => undefined);
  }, []);
  const cap = rec.capacity ?? 0;
  const top = rec.cameras.filter((c) => c.recommended).map((c) => c.name);
  const rows = [
    { icon: Cpu, label: "Computer", ok: cap > 0, text: cap > 0 ? "Compatible" : "Too slow" },
    { icon: Cctv, label: "CCTV", ok: rec.usable > 0, text: rec.usable > 0 ? "Compatible" : "Not compatible" },
    { icon: Cctv, label: "Cameras found", ok: rec.cameras.length > 0, text: String(rec.cameras.length) },
    { icon: ShieldCheck, label: "Guard AI capacity", ok: cap > 0, text: `${cap} ${cap === 1 ? "camera" : "cameras"}` },
    { icon: Wifi, label: "Internet", ok: !!checks?.network.internet, text: checks?.network.internet ? "Connected" : "Offline" },
    { icon: Film, label: "Local incident recording", ok: true, text: "Ready" },
  ];
  async function activate() {
    setBusy(true);
    setErr(null);
    try {
      const r = await post<{ link_url: string }>("/setup/link");
      window.open(r.link_url, "_blank", "noopener");
      onActivate();
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  return (
    <Screen
      title={cap > 0 ? "Your business is Guard Ready" : "Guard checked your shop"}
      lead={cap > 0
        ? `This computer can protect ${cap} ${cap === 1 ? "camera" : "cameras"} with Guard AI${top.length ? `. We'd watch ${top.slice(0, cap).join(" + ")}` : ""}.`
        : "Your CCTV works with Guard, but this computer is too slow to run Guard AI."}
      actions={cap > 0 ? (
        <button type="button" className="btn-primary px-8 text-base" disabled={busy || !checks?.network.internet} onClick={activate}>
          {busy ? <Spinner label="Opening…" /> : <>Activate Boombiz Guard <ArrowRight className="h-4 w-4" /></>}
        </button>
      ) : (
        <button type="button" className="btn-primary" onClick={() => onHelp("computer_too_slow")}>Get help</button>
      )}
    >
      <ul className="card divide-y divide-slate-200">
        {rows.map(({ icon: Icon, label, ok, text }) => (
          <li key={label} className="flex items-center justify-between gap-3 px-4 py-3">
            <span className="flex items-center gap-2 text-[15px] text-guard-ink"><Icon className="h-5 w-5" aria-hidden /> {label}</span>
            <span className="flex items-center gap-1.5 text-[15px] font-semibold text-guard-ink">
              {ok ? <Check className="h-4 w-4 text-emerald-700" aria-label="Good" /> : <CircleAlert className="h-4 w-4 text-red-700" aria-label="Problem" />}
              {text}
            </span>
          </li>
        ))}
      </ul>
      {cap > 0 && !checks?.network.internet && (
        <p className="mt-4 text-sm text-guard-ink">Connect this computer to the internet to activate Guard. It only needs it for a minute.</p>
      )}
      <ErrorNote message={err} />
      <p className="mt-4 text-sm text-slate-700">
        Activating opens your browser: sign in or create your account, link this computer, and choose a package. Nothing
        is charged until you pay there.
      </p>
    </Screen>
  );
}

function WaitForLicence({ cloudUrl, onLicensed, onBack }: { cloudUrl: string; onLicensed: () => void; onBack: () => void }) {
  const [st, setSt] = useState<SetupState | null>(null);
  usePoll(async () => {
    await api.get("/cloud/status"); // asks the cloud now while a sign-in is waiting
    const s = await api.get<SetupState>("/setup/state");
    setSt(s);
    if (s.licence.protects) {
      onLicensed();
      return true;
    }
    return false;
  }, 3000, true);
  const url = st?.cloud.link_url;
  return (
    <Screen
      title={st?.cloud.paired ? `Linked to ${st.cloud.business_name ?? "your business"}` : "Finish in your browser"}
      lead={st?.cloud.paired
        ? "Now choose your Guard package in the browser. As soon as the payment goes through, setup carries on here by itself."
        : "Sign in or create your account, then tap “Link this computer”. This screen moves on by itself."}
      actions={<button type="button" className="btn-outline" onClick={onBack}>Back</button>}
    >
      <div className="card p-4">
        <Spinner label={st?.cloud.paired ? "Waiting for your licence…" : "Waiting for you to sign in…"} />
        {url && !st?.cloud.paired && (
          <p className="mt-4 text-sm text-slate-700">
            Browser didn't open, or want to use your phone? Go to{" "}
            <a href={url} target="_blank" rel="noopener" className="inline-flex items-center gap-1 font-semibold text-guard-ink underline">
              {url.replace(/^https?:\/\//, "")} <ExternalLink className="h-3.5 w-3.5" aria-hidden />
            </a>
          </p>
        )}
        {st?.cloud.last_error && <p className="mt-3 text-sm text-red-800">{st.cloud.last_error}</p>}
      </div>
      <p className="mt-4 text-sm text-slate-700">
        Prefer someone to help?{" "}
        <a href={`${cloudUrl}/book`} target="_blank" rel="noopener" className="font-semibold text-guard-ink underline">Book a Boombiz visit</a>
      </p>
    </Screen>
  );
}

// ── recommended cameras ───────────────────────────────────────────────
function Cameras({ rec, onApplied }: { rec: Recommendation; onApplied: () => void }) {
  const initial = rec.cameras.filter((c) => c.recommended).map((c) => c.id);
  const [chosen, setChosen] = useState<string[]>(initial);
  const [changing, setChanging] = useState(false);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const limit = rec.licence.limit;
  const names = rec.cameras.filter((c) => initial.includes(c.id)).map((c) => c.name);

  async function apply(ids: string[]) {
    setBusy(true);
    setErr(null);
    try {
      await post("/setup/apply", { camera_ids: ids });
      onApplied();
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  const toggle = (id: string) =>
    setChosen((c) => (c.includes(id) ? c.filter((x) => x !== id) : c.length < limit ? [...c, id] : c));

  return (
    <Screen
      title={`${rec.cameras.length} CCTV ${rec.cameras.length === 1 ? "camera" : "cameras"} found`}
      lead={
        <>
          Your {rec.licence.name ?? "Guard"} plan protects {limit} {limit === 1 ? "camera" : "cameras"} with AI.
          {names.length > 0 && <> We recommend <b>{names.join(" + ")}</b>.</>} Your other cameras keep recording as normal CCTV.
        </>
      }
      actions={
        changing ? (
          <button type="button" className="btn-primary" disabled={busy || !chosen.length} onClick={() => apply(chosen)}>
            {busy ? <Spinner label="Starting Guard AI…" /> : <>Protect {chosen.length} {chosen.length === 1 ? "camera" : "cameras"} <ArrowRight className="h-4 w-4" /></>}
          </button>
        ) : (
          <>
            <button type="button" className="btn-primary px-8 text-base" disabled={busy || !initial.length} onClick={() => apply(initial)}>
              {busy ? <Spinner label="Starting Guard AI…" /> : <>Use Recommended Setup <ArrowRight className="h-4 w-4" /></>}
            </button>
            <button type="button" className="btn-outline" onClick={() => setChanging(true)}>Change cameras</button>
          </>
        )
      }
    >
      {rec.over_capacity && (
        <p className="mb-4 border-l-4 border-guard-500 bg-guard-50 px-3 py-2 text-sm text-guard-ink">
          Your licence covers {limit} cameras, but this computer is recommended for {rec.capacity}. Guard will protect your{" "}
          {rec.capacity} highest-risk cameras; a faster computer lets it watch all {limit}.
        </p>
      )}
      <ErrorNote message={err} />
      {changing && <p className="mb-3 text-sm font-semibold text-guard-ink">{chosen.length} of {limit} chosen</p>}
      <ul className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
        {rec.cameras.map((c) => {
          const on = changing ? chosen.includes(c.id) : c.recommended;
          const full = changing && !on && chosen.length >= limit;
          return (
            <li key={c.id} className={`card overflow-hidden ${on ? "ring-2 ring-guard-500" : ""} ${!c.usable ? "opacity-60" : ""}`}>
              {c.usable ? <Snapshot cameraId={c.id} /> : (
                <div className="flex aspect-video items-center justify-center bg-slate-100 text-sm text-slate-700">No picture</div>
              )}
              <label className={`flex items-center gap-2 p-3 ${changing && c.usable && !full ? "cursor-pointer" : ""}`}>
                {changing && (
                  <input type="checkbox" className="h-5 w-5 accent-black" checked={on} disabled={!c.usable || full} onChange={() => toggle(c.id)} />
                )}
                <span className="min-w-0 flex-1">
                  <span className="block truncate font-semibold text-guard-ink">{c.name}</span>
                  <span className="block text-xs text-slate-700">{c.reason}</span>
                </span>
                {!changing && c.recommended && (
                  <span className="shrink-0 bg-guard-500 px-1.5 py-0.5 text-xs font-bold text-guard-ink">Recommended</span>
                )}
              </label>
            </li>
          );
        })}
      </ul>
    </Screen>
  );
}

// ── where products are, where customers leave ─────────────────────────
type R = { x1: number; y1: number; x2: number; y2: number };

function DrawBox({ cameraId, rect, other, color, onChange }: {
  cameraId: string; rect: R | null; other: { r: R; label: string } | null; color: "yellow" | "red"; onChange: (r: R) => void;
}) {
  const box = useRef<HTMLDivElement>(null);
  const start = useRef<{ x: number; y: number } | null>(null);
  const [draft, setDraft] = useState<R | null>(null);
  const pos = (e: React.PointerEvent) => {
    const b = box.current!.getBoundingClientRect();
    return { x: Math.min(1, Math.max(0, (e.clientX - b.left) / b.width)), y: Math.min(1, Math.max(0, (e.clientY - b.top) / b.height)) };
  };
  const shown = draft ?? rect;
  const style = (r: R) => ({
    left: `${Math.min(r.x1, r.x2) * 100}%`, top: `${Math.min(r.y1, r.y2) * 100}%`,
    width: `${Math.abs(r.x2 - r.x1) * 100}%`, height: `${Math.abs(r.y2 - r.y1) * 100}%`,
  });
  return (
    <div
      ref={box}
      className="relative touch-none select-none"
      // The camera picture is an <img>: without this the browser starts its
      // own image drag, which cancels the pointer events and loses the box.
      onDragStart={(e) => e.preventDefault()}
      onPointerDown={(e) => {
        box.current?.setPointerCapture?.(e.pointerId);
        start.current = pos(e);
        setDraft({ x1: start.current.x, y1: start.current.y, x2: start.current.x, y2: start.current.y });
      }}
      onPointerMove={(e) => {
        if (!start.current) return;
        const p = pos(e);
        setDraft({ x1: start.current.x, y1: start.current.y, x2: p.x, y2: p.y });
      }}
      onPointerUp={(e) => {
        const s = start.current;
        start.current = null;
        setDraft(null);
        if (!s) return;
        const p = pos(e); // the release point, not the last move React rendered
        const r = { x1: s.x, y1: s.y, x2: p.x, y2: p.y };
        if (Math.abs(r.x2 - r.x1) > 0.03 && Math.abs(r.y2 - r.y1) > 0.03) onChange(r);
      }}
      onPointerCancel={() => {
        start.current = null;
        setDraft(null);
      }}
    >
      <Snapshot cameraId={cameraId} />
      {other && (
        <div className="pointer-events-none absolute border-2 border-dashed border-white" style={style(other.r)}>
          <span className="absolute left-0 top-0 bg-black/70 px-1 text-[11px] font-bold text-white">{other.label}</span>
        </div>
      )}
      {shown && (
        <div className={`pointer-events-none absolute border-[3px] ${color === "yellow" ? "border-guard-500 bg-guard-500/20" : "border-red-600 bg-red-600/20"}`} style={style(shown)} />
      )}
      <span className="pointer-events-none absolute bottom-2 left-2 bg-black/70 px-2 py-1 text-xs font-semibold text-white">
        Drag on the picture to draw a box
      </span>
    </div>
  );
}

function Areas({ onDone }: { onDone: () => void }) {
  const [cams, setCams] = useState<{ id: string; name: string }[] | null>(null);
  const [i, setI] = useState(0);
  const [part, setPart] = useState<"products" | "exit">("products");
  const [products, setProducts] = useState<R | null>(null);
  const [exit, setExit] = useState<R | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  useEffect(() => {
    api.get<{ cameras: { id: string; name: string; guard_enabled: boolean }[] }>("/cameras")
      .then((r) => setCams(r.cameras.filter((c) => c.guard_enabled)))
      .catch((e) => setErr((e as Error).message));
  }, []);
  const cam = cams?.[i];
  useEffect(() => {
    if (cams && !cam) onDone(); // no protected camera to draw on
  }, [cams, cam, onDone]);
  if (!cams || !cam) return <Screen title="Your shop's areas"><ErrorNote message={err} /><Spinner /></Screen>;
  const camId = cam.id;
  async function save(p: R | null, x: R | null) {
    setBusy(true);
    setErr(null);
    try {
      if (p || x) await post("/setup/areas", { camera_id: camId, products: p, exit: x });
      setProducts(null);
      setExit(null);
      setPart("products");
      if (i + 1 >= cams!.length) onDone();
      else setI(i + 1);
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  const counter = cams.length > 1 ? `Camera ${i + 1} of ${cams.length} · ` : "";
  return part === "products" ? (
    <Screen
      step={`${counter}${cam.name}`}
      title="Where are your products?"
      lead="Drag a box over the shelves or display where you keep goods on this camera."
      actions={
        <>
          <button type="button" className="btn-primary" disabled={!products} onClick={() => setPart("exit")}>
            Next <ArrowRight className="h-4 w-4" />
          </button>
          <button type="button" className="btn-outline" onClick={() => setPart("exit")}>No products on this camera</button>
        </>
      }
    >
      <div className="max-w-2xl"><DrawBox cameraId={cam.id} rect={products} other={null} color="yellow" onChange={setProducts} /></div>
    </Screen>
  ) : (
    <Screen
      step={`${counter}${cam.name}`}
      title="Where do customers leave?"
      lead="Drag a box over the door or gate customers walk out through."
      actions={
        <>
          <button type="button" className="btn-primary" disabled={busy || !exit} onClick={() => save(products, exit)}>
            {busy ? <Spinner label="Saving…" /> : <>Done <ArrowRight className="h-4 w-4" /></>}
          </button>
          <button type="button" className="btn-outline" disabled={busy} onClick={() => save(products, null)}>
            This camera doesn't show the exit
          </button>
          <button type="button" className="btn-outline" disabled={busy} onClick={() => setPart("products")}>Back</button>
        </>
      }
    >
      <ErrorNote message={err} />
      <div className="max-w-2xl">
        <DrawBox cameraId={cam.id} rect={exit} other={products ? { r: products, label: "Products" } : null} color="red" onChange={setExit} />
      </div>
    </Screen>
  );
}

// ── Guard Test ────────────────────────────────────────────────────────
const WALK: { key: "person" | "products" | "exit"; ask: string; done: string }[] = [
  { key: "person", ask: "Walk in front of the camera.", done: "Person detected" },
  { key: "products", ask: "Walk to the product area.", done: "Product area detected" },
  { key: "exit", ask: "Walk toward the exit.", done: "Exit detected" },
];
const CHECK_LABEL: Record<string, string> = {
  incident: "Incident created",
  snapshot: "Snapshot captured",
  clip: "15-second clip working",
  local_alert: "Local alert working",
  cloud: "Cloud connected",
  phone: "Test alert sent to your phone",
};

function GuardTest({ onDone, onHelp, onBackToAreas }: {
  onDone: (passed: boolean) => void; onHelp: () => void; onBackToAreas: () => void;
}) {
  const [cams, setCams] = useState<{ id: string; name: string }[]>([]);
  const [st, setSt] = useState<GuardTestStatus | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [cameraId, setCameraId] = useState<string | null>(null);
  const checksAsked = useRef(false);

  useEffect(() => {
    api.get<{ cameras: { id: string; name: string; guard_enabled: boolean }[] }>("/cameras").then((r) => {
      const on = r.cameras.filter((c) => c.guard_enabled);
      setCams(on);
      setCameraId(on[0]?.id ?? null);
    }).catch((e) => setErr((e as Error).message));
  }, []);

  // Start (retrying while Guard AI attaches to the camera — a few seconds after setup).
  usePoll(async () => {
    if (!cameraId) return false;
    try {
      setSt(await post<GuardTestStatus>("/setup/test/start", { camera_id: cameraId }));
      setErr(null);
      checksAsked.current = false;
      return true;
    } catch (e) {
      setErr((e as Error).message);
      return false;
    }
  }, 3000, !!cameraId && !st?.running);

  usePoll(async () => {
    const s = await api.get<GuardTestStatus>("/setup/test");
    setSt(s);
    const walked = s.steps && Object.values(s.steps).every((v) => v !== "waiting");
    if (walked && !checksAsked.current && !s.checks_running && !s.checks_done) {
      checksAsked.current = true;
      setSt(await post<GuardTestStatus>("/setup/test/checks"));
    }
    return !!s.checks_done && !s.checks_running;
  }, 1000, !!st?.running);

  const cam = cams.find((c) => c.id === cameraId);
  const current = st?.steps ? WALK.find((w) => st.steps![w.key] === "waiting") : WALK[0];

  return (
    <Screen
      step={cam ? `Guard Test · ${cam.name}` : "Guard Test"}
      title="Test your protection"
      lead={current ? current.ask : st?.checks_done ? (st.passed ? "Everything works." : "Some checks need attention.") : "Guard is checking the rest by itself…"}
      actions={st?.checks_done && !st.checks_running ? (
        <>
          <button type="button" className="btn-primary px-8 text-base" onClick={() => onDone(!!st.passed)}>
            {st.passed ? "Finish" : "Finish anyway"} <ArrowRight className="h-4 w-4" />
          </button>
          {!st.passed && (
            <>
              <button type="button" className="btn-outline" onClick={() => { setSt(null); }}>
                <RefreshCw className="h-4 w-4" /> Test again
              </button>
              <button type="button" className="btn-outline" onClick={onBackToAreas}>Redraw areas</button>
              <button type="button" className="btn-outline" onClick={onHelp}>Get help</button>
            </>
          )}
        </>
      ) : undefined}
    >
      <ErrorNote message={st?.running ? null : err} />
      {cams.length > 1 && !st?.checks_running && !st?.checks_done && (
        <div className="mb-4 flex flex-wrap items-center gap-2 text-sm text-guard-ink">
          Testing
          <select className="field w-auto py-1.5" value={cameraId ?? ""} onChange={(e) => { setSt(null); setCameraId(e.target.value); }}>
            {cams.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
          </select>
        </div>
      )}
      <div className="grid gap-5 lg:grid-cols-[minmax(0,1.3fr)_minmax(0,1fr)]">
        {cameraId && <Snapshot cameraId={cameraId} live />}
        <div>
          <ul className="card divide-y divide-slate-200">
            {WALK.map((w) => {
              const s = st?.steps?.[w.key] ?? "waiting";
              return (
                <li key={w.key} className="flex items-center justify-between gap-3 px-4 py-3">
                  <span className="flex items-center gap-2 text-[15px] text-guard-ink">
                    {s === "ok" ? <Check className="h-5 w-5 text-emerald-700" aria-label="Done" /> : s === "skipped" ? <span className="w-5 text-center text-slate-500">–</span> : <Spinner />}
                    {s === "ok" ? w.done : w.ask}
                  </span>
                  {s === "waiting" && st?.running && current?.key === w.key && (
                    <button type="button" className="shrink-0 text-xs font-semibold text-guard-ink underline"
                      onClick={() => post<GuardTestStatus>("/setup/test/skip", { step: w.key }).then(setSt).catch(() => undefined)}>
                      Skip
                    </button>
                  )}
                </li>
              );
            })}
          </ul>
          {st?.checks && (st.checks_running || st.checks_done) && (
            <ul className="card mt-4 divide-y divide-slate-200">
              {Object.entries(st.checks).map(([k, c]) => (
                <li key={k} className="px-4 py-2.5 text-[15px] text-guard-ink">
                  <span className="flex items-center gap-2">
                    {c.state === "ok" ? <Check className="h-5 w-5 text-emerald-700" aria-label="Done" />
                      : c.state === "failed" ? <CircleAlert className="h-5 w-5 text-red-700" aria-label="Problem" />
                      : c.state === "skipped" ? <span className="w-5 text-center text-slate-500">–</span> : <Spinner />}
                    {CHECK_LABEL[k]}
                  </span>
                  {c.note && <span className="ml-7 block text-xs text-slate-700">{c.note}</span>}
                </li>
              ))}
            </ul>
          )}
        </div>
      </div>
    </Screen>
  );
}

// ── the finish ────────────────────────────────────────────────────────
function Protected({ cloudUrl, onFinished }: { cloudUrl: string; onFinished: () => void }) {
  const [s, setS] = useState<SetupState | null>(null);
  useEffect(() => {
    api.get<SetupState>("/setup/state").then(setS).catch(() => undefined);
  }, []);
  const remote = s?.cloud.paired && s.cloud.online !== false;
  const rows = s ? [
    `${s.cameras} CCTV ${s.cameras === 1 ? "camera" : "cameras"} connected`,
    `${s.protected_cameras} ${s.protected_cameras === 1 ? "camera" : "cameras"} protected by Guard AI`,
    "Local protection: Active",
    `Remote alerts: ${remote ? "Active" : !s.cloud.paired ? "Not set up — link this computer in Settings" : "Waiting for internet"}`,
    "Incident recording: Active",
  ] : [];
  return (
    <section className="bg-guard-ink text-white">
      <div className="mx-auto w-full max-w-4xl px-4 py-12 sm:px-6 sm:py-16">
        <ShieldCheck className="h-14 w-14 text-guard-500" aria-hidden />
        <h1 className="mt-4 text-4xl font-extrabold tracking-tight sm:text-5xl">YOUR BUSINESS IS PROTECTED</h1>
        <ul className="mt-8 space-y-3">
          {rows.map((r) => (
            <li key={r} className="flex items-center gap-3 text-lg font-semibold text-white">
              <span className="h-3 w-3 shrink-0 rounded-full bg-emerald-500" aria-hidden /> {r}
            </li>
          ))}
        </ul>
        <div className="mt-10 flex flex-wrap gap-3">
          <a href={`${cloudUrl}/dashboard`} target="_blank" rel="noopener" className="btn-primary px-8 text-base">
            Open Guard Dashboard <ExternalLink className="h-4 w-4" />
          </a>
          <button type="button" className="btn border border-white/40 text-white hover:border-white" onClick={onFinished}>
            View incidents on this computer
          </button>
        </div>
      </div>
      <div className="bg-white">
        <div className="mx-auto w-full max-w-4xl px-4 pb-10 sm:px-6"><PowerChecklist /></div>
      </div>
    </section>
  );
}

function Help({ reason, cloudUrl, onRetry, onAdvanced }: {
  reason: HelpReason; cloudUrl: string; onRetry: () => void; onAdvanced: () => void;
}) {
  return (
    <Screen
      title="⚠️ Technical assistance required"
      lead={HELP_TEXT[reason]}
      actions={
        <>
          <a href={`${cloudUrl}/book`} target="_blank" rel="noopener" className="btn-primary">
            <Wrench className="h-4 w-4" /> Book a Boombiz technician
          </a>
          <button type="button" className="btn-outline" onClick={onRetry}><RefreshCw className="h-4 w-4" /> Try again</button>
          <button type="button" className="btn-outline" onClick={onAdvanced}><HardDrive className="h-4 w-4" /> Advanced setup</button>
        </>
      }
    >
      <ul className="space-y-2">
        {HELP_TIPS[reason].map((t) => (
          <li key={t} className="flex items-start gap-2 text-[15px] text-guard-ink">
            <Check className="mt-0.5 h-4 w-4 shrink-0" aria-hidden /> {t}
          </li>
        ))}
      </ul>
    </Screen>
  );
}
