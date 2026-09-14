# PyInstaller spec: the agent and the tray app, side by side in one folder
# (dist\BoombizGuard). Build with installer\build.ps1, not directly.
# -*- mode: python -*-
import os

from PyInstaller.utils.hooks import collect_all, collect_submodules

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))
AGENT = os.path.join(ROOT, "agent")
ICON = os.path.join(SPECPATH, "guard.ico")


def collect(*pkgs):
    datas, binaries, hidden = [], [], []
    for p in pkgs:
        d, b, h = collect_all(p)
        datas += d
        binaries += b
        hidden += h
    return datas, binaries, hidden


# ── agent (runs at boot, as SYSTEM, no desktop) ──────────────────────
a_datas, a_bins, a_hidden = collect("onnxruntime")
a_datas.append((os.path.join(ROOT, "desktop-ui", "dist"), "desktop-ui/dist"))
a_hidden += collect_submodules("app") + collect_submodules("uvicorn")

agent = Analysis(
    [os.path.join(SPECPATH, "guard_agent.py")],
    pathex=[AGENT],
    datas=a_datas,
    binaries=a_bins,
    hiddenimports=a_hidden,
    excludes=["pytest", "tkinter", "pystray", "windows_toasts"],
)
agent_exe = EXE(
    PYZ(agent.pure),
    agent.scripts,
    [],
    exclude_binaries=True,
    name="BoombizGuardAgent",
    icon=ICON,
    console=True,  # session 0 under the scheduled task: never visible
)

# ── tray (runs in each signed-in user's session) ─────────────────────
t_datas, t_bins, t_hidden = collect("windows_toasts", "winrt")
t_hidden += ["pystray._win32"]

tray = Analysis(
    [os.path.join(AGENT, "tray", "guard_tray.py")],
    pathex=[AGENT],
    datas=t_datas,
    binaries=t_bins,
    hiddenimports=t_hidden,
    excludes=["pytest", "tkinter", "onnxruntime", "cv2", "numpy"],
)
tray_exe = EXE(
    PYZ(tray.pure),
    tray.scripts,
    [],
    exclude_binaries=True,
    name="BoombizGuardTray",
    icon=ICON,
    console=False,
)

COLLECT(
    agent_exe, agent.binaries, agent.datas,
    tray_exe, tray.binaries, tray.datas,
    name="BoombizGuard",
)
