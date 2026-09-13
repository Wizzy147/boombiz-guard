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

export async function previewTicket(cameraId: string): Promise<string> {
  const r = await api.post<{ ticket: string }>(`/cameras/${cameraId}/preview-ticket`);
  return r.ticket;
}
