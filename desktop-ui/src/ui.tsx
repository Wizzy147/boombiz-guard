import { useEffect, useState, type ReactNode } from "react";
import { Loader2 } from "lucide-react";
import { previewTicket, type Compat } from "./api";

export function Mark({ size = 32 }: { size?: number }) {
  // PLACEHOLDER mark until the real Boombiz Guard logo is supplied.
  return (
    <svg width={size} height={size} viewBox="0 0 40 40" aria-hidden className="shrink-0">
      <rect width="40" height="40" fill="#0B0B0B" />
      <path d="M20 6 L31 10 V19 C31 26 26.5 31 20 34 C13.5 31 9 26 9 19 V10 Z" fill="#FFC21A" />
      <circle cx="20" cy="19.5" r="4.2" fill="#0B0B0B" />
      <circle cx="20" cy="19.5" r="1.7" fill="#FFC21A" />
    </svg>
  );
}

export function Lockup() {
  return (
    <div className="flex items-center gap-2.5">
      <Mark size={34} />
      <div className="flex flex-col leading-none">
        <span className="text-lg font-extrabold tracking-tight text-white">Boombiz</span>
        <span className="mt-[3px] text-[10px] font-bold uppercase tracking-[0.34em] text-guard-500">Guard</span>
      </div>
    </div>
  );
}

const COMPAT_STYLE: Record<Compat, { label: string; cls: string }> = {
  COMPATIBLE: { label: "Compatible", cls: "bg-emerald-50 text-emerald-800 border-emerald-200" },
  LIMITED: { label: "Limited", cls: "bg-guard-50 text-guard-ink border-guard-500" },
  INCOMPATIBLE: { label: "Not compatible", cls: "bg-red-50 text-red-800 border-red-200" },
  AUTH_REQUIRED: { label: "Sign-in needed", cls: "bg-slate-50 text-slate-800 border-slate-300" },
  OFFLINE: { label: "Offline", cls: "bg-slate-100 text-slate-700 border-slate-300" },
  UNKNOWN: { label: "Not tested", cls: "bg-white text-slate-700 border-slate-300" },
};

export function CompatBadge({ status }: { status: Compat }) {
  const s = COMPAT_STYLE[status] ?? COMPAT_STYLE.UNKNOWN;
  return <span className={`inline-flex items-center border px-2 py-0.5 text-xs font-semibold ${s.cls}`}>{s.label}</span>;
}

export function Spinner({ label }: { label?: string }) {
  return (
    <span className="inline-flex items-center gap-2 text-sm text-slate-700">
      <Loader2 className="h-4 w-4 animate-spin" aria-hidden />
      {label}
    </span>
  );
}

export function ErrorNote({ message }: { message: string | null }) {
  if (!message) return null;
  return (
    <p role="alert" className="border-l-4 border-red-600 bg-red-50 px-3 py-2 text-sm text-red-900">
      {message}
    </p>
  );
}

export function Screen({
  step,
  title,
  lead,
  children,
  actions,
}: {
  step?: string;
  title: string;
  lead?: ReactNode;
  children?: ReactNode;
  actions?: ReactNode;
}) {
  return (
    <section className="mx-auto w-full max-w-4xl px-4 py-8 sm:px-6 sm:py-10">
      {step && <p className="text-xs font-bold uppercase tracking-[0.2em] text-slate-500">{step}</p>}
      <h1 className="mt-1 text-2xl font-bold tracking-tight text-guard-ink sm:text-3xl">{title}</h1>
      {lead && <div className="mt-2 max-w-2xl text-[15px] leading-relaxed text-slate-700">{lead}</div>}
      <div className="mt-6">{children}</div>
      {actions && <div className="mt-8 flex flex-wrap items-center gap-3">{actions}</div>}
    </section>
  );
}

/** A still from the camera, fetched through the agent with a short ticket. */
export function Snapshot({ cameraId, live = false }: { cameraId: string; live?: boolean }) {
  const [src, setSrc] = useState<string | null>(null);
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    let alive = true;
    setFailed(false);
    previewTicket(cameraId)
      .then((t) => {
        if (!alive) return;
        const kind = live ? "preview" : "snapshot.jpg";
        setSrc(`/api/v1/cameras/${cameraId}/${kind}?ticket=${encodeURIComponent(t)}`);
      })
      .catch(() => alive && setFailed(true));
    return () => {
      alive = false;
    };
  }, [cameraId, live]);
  return (
    <div className="relative aspect-video w-full overflow-hidden bg-guard-ink">
      {src && !failed ? (
        <img src={src} alt="" draggable={false} className="h-full w-full object-cover" onError={() => setFailed(true)} />
      ) : null}
      {(!src || failed) && (
        <div className="absolute inset-0 flex items-center justify-center p-3 text-center text-xs text-white/80">
          {failed ? "No picture from this camera" : <Loader2 className="h-5 w-5 animate-spin text-guard-500" />}
        </div>
      )}
    </div>
  );
}
