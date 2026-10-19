"""Choix de la source a diffuser : liste des fenetres et selecteur de zone a la souris."""
import ctypes
from ctypes import wintypes


def list_windows() -> list[str]:
    """Titres des fenetres visibles (non reduites), utilisables par gdigrab (title=...)."""
    user32, dwm = ctypes.windll.user32, ctypes.windll.dwmapi
    titles: set[str] = set()

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def visit(hwnd, _):
        if user32.IsWindowVisible(hwnd) and not user32.IsIconic(hwnd):
            n = user32.GetWindowTextLengthW(hwnd)
            cloaked = wintypes.DWORD(0)
            dwm.DwmGetWindowAttribute(hwnd, 14, ctypes.byref(cloaked), 4)   # fenetres UWP "fantomes"
            if n and not cloaked.value:
                buf = ctypes.create_unicode_buffer(n + 1)
                user32.GetWindowTextW(hwnd, buf, n + 1)
                if buf.value != "Program Manager":
                    titles.add(buf.value)
        return True

    user32.EnumWindows(visit, 0)
    return sorted(titles, key=str.lower)


def pick_region() -> dict | None:
    """Voile plein ecran : l'utilisateur trace un rectangle. Renvoie x, y, w, h (pairs) ou None."""
    import tkinter as tk

    root = tk.Tk()
    root.attributes("-fullscreen", True, "-topmost", True, "-alpha", 0.35)
    root.configure(bg="black", cursor="crosshair")
    cv = tk.Canvas(root, bg="black", highlightthickness=0)
    cv.pack(fill="both", expand=True)
    cv.create_text(root.winfo_screenwidth() // 2, 70, fill="white", font=("Segoe UI", 22),
                   text="Tracez la zone à diffuser  —  Échap pour annuler")
    start, rect, result = {}, {}, {}

    def down(e):
        start.update(x=e.x_root, y=e.y_root)
        rect["id"] = cv.create_rectangle(e.x, e.y, e.x, e.y, outline="#ff3b30", width=3)

    def move(e):
        if rect:
            cv.coords(rect["id"], start["x"], start["y"], e.x_root, e.y_root)

    def up(e):
        if start:
            x, y = min(start["x"], e.x_root), min(start["y"], e.y_root)
            w, h = abs(e.x_root - start["x"]) // 2 * 2, abs(e.y_root - start["y"]) // 2 * 2
            if w >= 64 and h >= 64:
                result.update(x=x, y=y, w=w, h=h)
        root.destroy()

    cv.bind("<ButtonPress-1>", down)
    cv.bind("<B1-Motion>", move)
    cv.bind("<ButtonRelease-1>", up)
    root.bind("<Escape>", lambda _: root.destroy())
    root.focus_force()
    root.mainloop()
    return result or None


def list_monitors() -> list[dict]:
    """Ecrans actifs (reels ou virtuels) avec leur position dans le bureau etendu."""
    user32 = ctypes.windll.user32

    class MonitorInfo(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT), ("rcWork", wintypes.RECT),
                    ("dwFlags", wintypes.DWORD), ("szDevice", wintypes.WCHAR * 32)]

    found: list[dict] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HMONITOR, wintypes.HDC, ctypes.POINTER(wintypes.RECT), wintypes.LPARAM)
    def visit(handle, _dc, _rect, _param):
        mi = MonitorInfo()
        mi.cbSize = ctypes.sizeof(MonitorInfo)
        user32.GetMonitorInfoW(handle, ctypes.byref(mi))
        r = mi.rcMonitor
        found.append(dict(device=mi.szDevice, x=r.left, y=r.top, w=(r.right - r.left) // 2 * 2,
                          h=(r.bottom - r.top) // 2 * 2, primary=bool(mi.dwFlags & 1)))
        return True

    user32.EnumDisplayMonitors(0, 0, visit, 0)
    found.sort(key=lambda m: (not m["primary"], m["x"], m["y"]))
    for i, m in enumerate(found, 1):
        m["name"] = f"Écran {i}" + (" (principal)" if m["primary"] else "")
    return found


VDD_DIR = r"C:\VirtualDisplayDriver"


def vdd_installed() -> bool:
    import os
    return os.path.isdir(VDD_DIR)


def install_vdd():
    """Installe le pilote d'ecran virtuel (MIT) via winget ; Windows demande lui-meme la confirmation."""
    import subprocess
    subprocess.Popen(["winget", "install", "--id=VirtualDrivers.Virtual-Display-Driver", "-e",
                      "--accept-package-agreements", "--accept-source-agreements"],
                     creationflags=subprocess.CREATE_NEW_CONSOLE)


def extend_displays():
    import subprocess
    subprocess.Popen(["DisplaySwitch.exe", "/extend"])
