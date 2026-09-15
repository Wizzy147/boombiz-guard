import { useState } from "react";
import { PlugZap } from "lucide-react";

// Power checklist (owner decision 2026-09-14). If this computer is off, Guard
// isn't watching — in Nigeria that mostly means a power cut. The installer
// already stopped Windows sleeping; these are the steps software can't do.
// Ticks are a reminder, not saved anywhere. Shown at the end of both Auto
// Setup and Advanced Setup.
const POWER_STEPS = [
  "A UPS or inverter powers this computer, the CCTV recorder and the Wi-Fi router, so a power cut doesn't switch Guard off.",
  "In the computer's BIOS, \"Restore on AC power loss\" (or \"After power failure\") is set to Power On, so it turns itself back on when light returns.",
  "Business hours are set in Guard. The siren for a damaged or covered camera only sounds after closing, and without hours Guard treats the shop as always open.",
  "Staff know to tell the manager before moving or cleaning a camera — a covered or disconnected camera alerts the owner, manager and security.",
];

export function PowerChecklist() {
  const [done, setDone] = useState<boolean[]>(() => POWER_STEPS.map(() => false));
  return (
    <section className="mt-6 border border-guard-ink/15 p-4">
      <h3 className="flex items-center gap-2 font-bold text-guard-ink">
        <PlugZap className="h-5 w-5" aria-hidden /> Before you leave the shop
      </h3>
      <p className="mt-1 text-sm text-slate-700">
        Guard stops watching when this computer is off. This computer won't sleep or hibernate now — check the rest:
      </p>
      <ul className="mt-3 space-y-2">
        {POWER_STEPS.map((s, i) => (
          <li key={s}>
            <label className="flex items-start gap-2 text-[15px] text-guard-ink">
              <input
                type="checkbox"
                className="mt-1 h-4 w-4 shrink-0 accent-black"
                checked={done[i]}
                onChange={() => setDone((d) => d.map((v, j) => (j === i ? !v : v)))}
              />
              {s}
            </label>
          </li>
        ))}
      </ul>
    </section>
  );
}
