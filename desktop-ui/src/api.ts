/**
 * The agent's local API. The access token arrives in the URL fragment
 * (#t=…) when the Guard shortcut opens this page — fragments never reach a
 * server or a log — and is kept in sessionStorage for the tab's lifetime.
 */

const TOKEN_KEY = "guard_setup_token";

function readToken(): string | null {
  const m = window.location.hash.match(/[#&]t=([^&]+)/);
  if (m) {
    try {
      sessionStorage.setItem(TOKEN_KEY, decodeURIComponent(m[1]));
    } catch {
      /* storage blocked — the in-memory copy below still works */
    }
    // Take it out of the address bar so it isn't screenshotted or bookmarked.
    history.replaceState(null, "", window.location.pathname);
    return decodeURIComponent(m[1]);
  }
  try {
    return sessionStorage.getItem(TOKEN_KEY);
  } catch {
    return null;
  }
}

const token = readToken();

// Phase 3: the PERSON signed in with a PIN (on top of the device token).
const SESSION_KEY = "guard_person_session";
let personSession: string | null = (() => {
  try {
    return sessionStorage.getItem(SESSION_KEY);
  } catch {
    return null;
  }
})();
export function setPersonSession(s: string | null) {
  personSession = s;
  try {
    if (s) sessionStorage.setItem(SESSION_KEY, s);
    else sessionStorage.removeItem(SESSION_KEY);
  } catch {
    /* in-memory copy still works */
  }
}

export const hasToken = () => !!token;

export class ApiError extends Error {
  status: number;
  constructor(message: string, status: number) {
    super(message);
    this.status = status;
  }
}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  let res: Response;
  try {
    res = await fetch(`/api/v1${path}`, {
      method,
      headers: {
        "Content-Type": "application/json",
        "X-Guard-Token": token ?? "",
        ...(personSession ? { "X-Guard-Session": personSession } : {}),
      },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch {
    throw new ApiError("Guard isn't running on this computer. Start Boombiz Guard and try again.", 0);
  }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const detail = (data as { detail?: unknown }).detail;
    const message =
      typeof detail === "string"
        ? detail
        : Array.isArray(detail)
          ? "Please check what you typed and try again."
          : "Something went wrong. Try again.";
    throw new ApiError(message, res.status);
  }
  return data as T;
}

export const api = {
  get: <T>(p: string) => request<T>("GET", p),
  post: <T>(p: string, body?: unknown) => request<T>("POST", p, body ?? {}),
  del: <T>(p: string) => request<T>("DELETE", p),
};

// ── types mirrored from the agent ─────────────────────────────────────
export type Compat = "UNKNOWN" | "COMPATIBLE" | "LIMITED" | "INCOMPATIBLE" | "AUTH_REQUIRED" | "OFFLINE";

export interface Device {
  id: string;
  ip_address: string;
  port: number | null;
  name: string | null;
  manufacturer: string | null;
  model: string | null;
  firmware: string | null;
  device_type: "CAMERA" | "DVR" | "NVR" | "UNKNOWN";
  adapter_type: string | null;
  compatibility: Compat;
  compatibility_reason: string | null;
  authentication_required: boolean;
  auth_error: string | null;
  source: string;
  channel_count: number;
  capabilities: Record<string, unknown>;
}

export interface Check {
  key: string;
  label: string;
  passed: boolean | null;
  mandatory: boolean;
  note: string | null;
}

export interface TestPayload {
  report: { status: Compat; summary: string; checks: Check[] };
  test: {
    connected: boolean;
    codec: string | null;
    width: number | null;
    height: number | null;
    measured_fps: number | null;
    nominal_fps: number | null;
    seconds_run: number;
    stable: boolean;
    errors: string[];
  } | null;
}

export interface StreamHealth {
  status: "STARTING" | "ONLINE" | "DEGRADED" | "OFFLINE" | "AUTH_ERROR" | "STREAM_ERROR" | "STOPPED";
  last_frame_at: string | null;
  stream_fps: number;
  decode_fps: number;
  reconnect_count: number;
  last_error: string | null;
  role: "guard" | "preview";
}

export interface Camera {
  id: string;
  device_id: string;
  channel_number: number;
  name: string;
  has_main_stream: boolean;
  has_sub_stream: boolean;
  codec_main: string | null;
  codec_sub: string | null;
  width: number | null;
  height: number | null;
  fps: number | null;
  guard_enabled: boolean;
  online: boolean;
  compatibility: Compat;
  last_test: TestPayload | null;
  health: StreamHealth | null;
}

export interface DiscoveryStatus {
  stage: "idle" | "onvif" | "network" | "identify" | "done";
  checked: number;
  total: number;
  found: number;
  error: string | null;
}

export interface Health {
  system: {
    cpu_percent: number;
    cpu_count: number;
    ram_percent: number;
    ram_total_gb: number;
    disk_free_gb: number;
    ffmpeg_available: boolean;
    ffprobe_available: boolean;
    agent_uptime_s: number;
  };
  guard_cameras: { online: number; total: number; limit: number };
  streams: Record<string, StreamHealth>;
  events: { event: string; camera_id: string; at: string }[];
}

// ── Phase 2 ───────────────────────────────────────────────────────────
export type ZoneType = "SHELF" | "EXIT" | "RESTRICTED" | "CASHIER" | "STOCKROOM" | "FIRE_RISK" | "IGNORE" | "PRIVACY";
export interface Pt {
  x: number;
  y: number;
}
export interface Zone {
  id: string;
  camera_id: string;
  name: string;
  zone_type: ZoneType;
  polygon: Pt[];
  enabled: boolean;
  sensitivity: string;
}
export interface AiEvent {
  id: string;
  camera_id: string;
  track_id: string | null;
  event_type: string;
  severity: string | null;
  confidence: string | null;
  zone_id: string | null;
  metadata: Record<string, unknown>;
  occurred_at: string;
  feedback: string | null;
}
export interface TrackBox {
  track_id: string;
  state: string;
  confidence: number;
  bbox: { x1: number; y1: number; x2: number; y2: number };
  zones: string[];
}
export interface Plan {
  level: string;
  primary_fps: number;
  secondary_fps: number;
  disabled_features: string[];
  reduced: boolean;
  message: string | null;
}
export interface AiCameraStatus {
  camera_id: string;
  name: string;
  state: string;
  target_fps: number;
  ai_fps: number;
  infer_ms: number;
  lag_ms: number;
  frames_dropped: number;
  active_tracks: number;
  features: string[];
  failed_modules: string[];
}
export interface AiStatus {
  running: boolean;
  error: string | null;
  device: string | null;
  models: Record<string, string>;
  performance: Plan;
  cameras: AiCameraStatus[];
}
export interface AiDebug {
  camera_id: string;
  tracks: TrackBox[];
  zones: Zone[];
  status: AiCameraStatus;
}
export interface AiConfig {
  person: boolean;
  shelf: boolean;
  exit: boolean;
  restricted: boolean;
  after_hours: boolean;
  fire: boolean;
  concealment: boolean;
  priority: "PRIMARY" | "NORMAL";
  fire_notice?: string;
  concealment_notice?: string;
}
export interface DayHours {
  day_of_week: number;
  opens_at: string | null;
  closes_at: string | null;
  closed: boolean;
}

export const api2 = {
  put: <T>(p: string, body: unknown) => request<T>("PUT", p, body),
};

// ── Phase 3 ───────────────────────────────────────────────────────────
export type Severity = "INFO" | "LOW" | "HIGH" | "CRITICAL";
export interface IncidentRow {
  id: string;
  ref: string;
  incident_type: string;
  title: string | null;
  severity: Severity;
  confidence: string | null;
  status: string;
  camera: string;
  camera_id: string;
  occurred_at: string;
  ended_at: string | null;
  has_snapshot: boolean;
  has_clip: boolean;
  clip_duration_seconds: number | null;
  media_status: string;
  keep_evidence: boolean;
  alarm_state: string | null;
  acknowledged_by: string | null;
  reviewed_by: string | null;
  false_alert_reason: string | null;
  resolution_note: string | null;
  description: string | null;
  timeline?: { event: string; at: string; zone_id?: string | null }[];
}
export interface Person {
  id: string;
  name: string;
  role: "OWNER" | "MANAGER" | "SECURITY";
  active?: boolean;
}
export interface AlarmOut {
  id: string;
  name: string;
  kind: string;
  enabled: boolean;
  health: string;
  last_error: string | null;
}
export interface AlarmRuleRow {
  id: string;
  incident_type: string;
  enabled: boolean;
  duration_seconds: number;
  cooldown_seconds: number;
  repeat_until_ack: boolean;
}
export interface StorageStatus {
  guard_media_bytes: number;
  ceiling_bytes: number;
  disk_free_bytes: number;
  level: string;
  warning: string | null;
  kept_evidence: number;
  buffer_ram_bytes: number;
  policies: { id: string; incident_type: string | null; severity: string | null; retention_days: number; keep_if_confirmed: boolean }[];
}

export async function incidentMedia(id: string, kind: "snapshot" | "clip" | "thumbnail"): Promise<string> {
  const r = await request<{ ticket: string }>("POST", `/incidents/${id}/media-ticket?kind=${kind}`, {});
  return `/api/v1/incidents/${id}/${kind}?ticket=${encodeURIComponent(r.ticket)}`;
}

export async function download(path: string, filename: string) {
  const res = await fetch(`/api/v1${path}`, {
    headers: { "X-Guard-Token": token ?? "", ...(personSession ? { "X-Guard-Session": personSession } : {}) },
  });
  if (!res.ok) throw new ApiError("Export failed. Try again.", res.status);
  const url = URL.createObjectURL(await res.blob());
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  a.click();
  URL.revokeObjectURL(url);
}

export async function previewTicket(cameraId: string): Promise<string> {
  const r = await api.post<{ ticket: string }>(`/cameras/${cameraId}/preview-ticket`);
  return r.ticket;
}
