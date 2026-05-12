"""
KEF LSX II Controller
GUI application built on top of pykefcontrol.
"""

import os
import sys
import json
import time
import queue
import threading
import ctypes
import ctypes.wintypes
import winreg
import urllib.request
import concurrent.futures
from io import BytesIO
from pathlib import Path
from tkinter import messagebox

import customtkinter as ctk
import pystray
from PIL import Image, ImageDraw

from pykefcontrol.kef_connector import KefConnector

# ---------------------------------------------------------------------------
# Theme
# ---------------------------------------------------------------------------
ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
SOURCES = ["wifi", "bluetooth", "tv", "optical", "analog", "usb"]
SOURCE_LABELS = {
    "wifi":      "WiFi",
    "bluetooth": "Bluetooth",
    "tv":        "TV",
    "optical":   "Opt",
    "analog":    "Aux",
    "usb":        "USB",
}

PWR_ON      = "powerOn"
PWR_STANDBY = "standby"

COLOR_SRC_DEFAULT = ["#3B8ED0", "#1F6AA5"]
COLOR_PWR_ON      = ("#1a6b3c", "#25a05a")
COLOR_PWR_OFF     = ("#6b1a1a", "#a02525")

# KEF LSX II LED colors per source (verified by user)
SOURCE_COLORS = {
    "wifi":      "#FFFFFF",  # white
    "bluetooth": "#1F6AA5",  # blue
    "tv":        "#00C8AA",  # cyan green
    "optical":   "#D63384",  # magenta
    "analog":    "#FFC107",  # yellow
    "usb":       "#F8BA87",  # pastel orange
}

BTN_SIZE = 34  # diameter of source buttons in px

# ---------------------------------------------------------------------------
# Left-panel layout (absolute coordinates inside the 220×~440 left panel)
# Edit these values, save, and relaunch the app to reposition elements.
# Format: (x, y) is the top-left corner of each element.
# ---------------------------------------------------------------------------
LEFT_PANEL_W = 200   # width of the left panel
LEFT_PANEL_H = 440   # used as min height reference

LAYOUT = {
    # Header
    "title":           {"x":  60, "y":  10},

    # IP + Connect (same row)
    "ip_entry":        {"x":  10, "y":  40, "w": 100, "h": 26},
    "conn_btn":        {"x": 115, "y":  40, "w":  75, "h": 26},

    # Auto-start checkbox
    "auto_usb_check":  {"x":  10, "y":  74},

    # Separator 1
    "sep1":            {"x":  10, "y": 104, "w": 200, "h": 1},

    # Speaker name (bold)
    "name_lbl":        {"x":  10, "y": 116, "w": 200},

    # Model (left) + FW (right) on same y
    "model_lbl":       {"x":  10, "y": 138},
    "fw_lbl":          {"x": 100, "y": 138, "w":  80},

    # Separator 2
    "sep2":            {"x":  10, "y": 162, "w": 200, "h": 1},

    # Power status (left) + "Input Source" (right) on same y
    "status_dot":      {"x":  10, "y": 175},
    "status_lbl":      {"x":  28, "y": 175},
    "input_src_lbl":   {"x": 110, "y": 175},

    # Source buttons: top-left of each cell. The button is centered horizontally
    # inside a CELL_W-wide cell; the label sits just below the icon.
    "btn_grid_y":      210,    # y of the first row of buttons
    "btn_row_h":        60,    # vertical spacing between row 1 and row 2
    "btn_cols_x":     [16, 80, 144],   # x of each of the 3 columns
}

# Font sizes (in pt) for each text element. Increase a value to make text
# larger — you will likely need to adjust the matching y in LAYOUT too.
# Rough rule: +2 pt of font ≈ +3 px of height for that element.
FONTS = {
    "title":           14,   # "KEF Controller"
    "ip_entry":        11,
    "conn_btn":        11,
    "auto_usb_check":  10,
    "name_lbl":        12,   # speaker name (bold)
    "model_lbl":       10,
    "fw_lbl":          10,
    "status_dot":      13,
    "status_lbl":      11,
    "input_src_lbl":   11,   # "Input Source" header
    "btn_label":        9,   # WiFi / Bluetooth / TV ... under each button
}


def _hex_to_rgb(hex_color):
    h = hex_color.lstrip("#")
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))


def _draw_wifi(draw, size, color):
    """Draw an official WiFi icon (3 arcs + dot) centered in the image."""
    cx, cy = size // 2, size // 2 + size // 10
    lw = max(2, size // 14)
    for r in [size // 3, size // 4 - 1, size // 6 - 1]:
        if r <= 0:
            continue
        draw.arc([cx - r, cy - r, cx + r, cy + r],
                 start=225, end=315, fill=color, width=lw)
    # Dot
    dr = max(1, size // 16)
    dot_y = cy + size // 4
    draw.ellipse([cx - dr, dot_y - dr, cx + dr, dot_y + dr], fill=color)


def _draw_bluetooth(draw, size, color):
    """Draw an official Bluetooth icon (rune shape) centered in the image."""
    cx, cy = size // 2, size // 2
    lw = max(2, size // 14)
    h = size // 3
    w = size // 5
    top = cy - h
    bot = cy + h
    left = cx - w
    right = cx + w
    # Bluetooth rune: 8 line segments forming the classic shape
    pts = [
        ((cx, top), (right, cy)),
        ((right, cy), (cx, bot)),
        ((cx, top), (cx, bot)),
        ((left, cy - h // 2), (right, cy + h // 2)),
        ((left, cy + h // 2), (right, cy - h // 2)),
    ]
    for a, b in pts:
        draw.line([a, b], fill=color, width=lw)


def _make_source_btn_image(src, active, size=BTN_SIZE):
    """Render a circular button image with the source's icon/text.

    Uses 4× supersampling: everything is drawn on a canvas 4 times bigger,
    then resized down with LANCZOS for smooth antialiased edges.
    """
    SS = 4                       # supersampling factor
    big = size * SS
    img = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    if active:
        bg = SOURCE_COLORS.get(src, "#FFFFFF")
        fg = "#1a1a1a"
    else:
        bg = "#3a3a3a"
        fg = "#bbbbbb"

    bg_rgb = _hex_to_rgb(bg)
    fg_rgb = _hex_to_rgb(fg)

    # Circle background (drawn at 4× resolution → smooth after downscale)
    draw.ellipse([0, 0, big - 1, big - 1], fill=bg_rgb)

    if src == "wifi":
        _draw_wifi(draw, big, fg_rgb)
    elif src == "bluetooth":
        _draw_bluetooth(draw, big, fg_rgb)
    else:
        try:
            from PIL import ImageFont
            font = ImageFont.truetype("arialbd.ttf", big // 3)
        except Exception:
            font = None
        text = {"tv": "TV", "optical": "OPT",
                "analog": "AUX", "usb": "USB"}.get(src, src.upper()[:3])
        if font:
            bbox = draw.textbbox((0, 0), text, font=font)
            tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
            draw.text(((big - tw) // 2 - bbox[0],
                       (big - th) // 2 - bbox[1]),
                      text, fill=fg_rgb, font=font)
        else:
            draw.text((big // 4, big // 3), text, fill=fg_rgb)

    # Downscale with high-quality LANCZOS filter → antialiased result
    return img.resize((size, size), Image.LANCZOS)


POLL_INTERVAL_S = 1    # seconds between poll cycles

# Store config in %APPDATA%\KefLSXController so it works even when the .exe
# lives in a write-protected location like C:\Program Files\.
# Fall back to the .exe / source directory if APPDATA is unavailable.
def _resolve_config_path():
    appdata = os.environ.get("APPDATA")
    if appdata:
        cfg_dir = Path(appdata) / "KefLSXController"
        try:
            cfg_dir.mkdir(parents=True, exist_ok=True)
            return cfg_dir / "config.json"
        except Exception:
            pass
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent / "config.json"
    return Path(__file__).parent / "config.json"


CONFIG_PATH = _resolve_config_path()


def _resource_path(rel):
    """Locate a bundled resource both in dev mode and inside a PyInstaller exe."""
    if getattr(sys, "frozen", False):
        base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    else:
        base = Path(__file__).parent
    return base / rel


ICON_PATH = _resource_path("Kef-LSX-HP.ico")

# Low-level mouse hook (scroll over the tray icon)
_WH_MOUSE_LL   = 14
_WM_MOUSEWHEEL = 0x020A

class _MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("pt",          ctypes.wintypes.POINT),
        ("mouseData",   ctypes.wintypes.DWORD),
        ("flags",       ctypes.wintypes.DWORD),
        ("time",        ctypes.wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]

class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_ulong),
        ("Data2", ctypes.c_ushort),
        ("Data3", ctypes.c_ushort),
        ("Data4", ctypes.c_ubyte * 8),
    ]

class _NOTIFYICONIDENTIFIER(ctypes.Structure):
    _fields_ = [
        ("cbSize",   ctypes.wintypes.DWORD),
        ("hWnd",     ctypes.wintypes.HWND),
        ("uID",      ctypes.wintypes.UINT),
        ("guidItem", _GUID),
    ]


# ---------------------------------------------------------------------------
# Windows startup registry helpers
# ---------------------------------------------------------------------------
_STARTUP_REG_KEY  = r"Software\Microsoft\Windows\CurrentVersion\Run"
_STARTUP_REG_NAME = "KEF LSX Controller"


def _startup_exe_path():
    if getattr(sys, "frozen", False):
        # PyInstaller .exe
        return f'"{sys.executable}" --startup'
    else:
        # Script mode - use absolute path
        script_path = Path(__file__).resolve()
        return f'"{sys.executable}" "{script_path}" --startup'


def _is_startup_enabled():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _STARTUP_REG_KEY,
                            0, winreg.KEY_READ) as key:
            val, _ = winreg.QueryValueEx(key, _STARTUP_REG_NAME)
            return val == _startup_exe_path()
    except Exception:
        return False


def _set_startup(enabled):
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _STARTUP_REG_KEY,
                            0, winreg.KEY_WRITE) as key:
            if enabled:
                winreg.SetValueEx(key, _STARTUP_REG_NAME, 0,
                                  winreg.REG_SZ, _startup_exe_path())
            else:
                winreg.DeleteValue(key, _STARTUP_REG_NAME)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Helper widget
# ---------------------------------------------------------------------------
def _separator(parent):
    ctk.CTkFrame(parent, height=1, fg_color="gray40").pack(
        fill="x", padx=10, pady=8
    )


# ---------------------------------------------------------------------------
# Main application
# ---------------------------------------------------------------------------
class KefApp(ctk.CTk):

    def __init__(self, show_event=None, is_startup=False):
        # DPI-aware BEFORE any window creation, otherwise the mouse hook
        # coordinates won't match what GetWindowRect returns.
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            try:
                ctypes.windll.user32.SetProcessDPIAware()
            except Exception:
                pass

        # Custom AppUserModelID so Windows groups the taskbar entry under
        # our app instead of grouping it under python.exe.
        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                "KEF.LSX.Controller")
        except Exception:
            pass

        super().__init__()
        self.title("KEF LSX II Controller")
        self.geometry("700x380")
        self.resizable(False, False)
        try:
            self.iconbitmap(str(ICON_PATH))
        except Exception:
            pass

        # UI zoom (Ctrl + mouse wheel)
        self._ui_scale = 1.0
        self.bind_all("<Control-MouseWheel>", self._on_ctrl_wheel)

        # Speaker handle — read/written via property to protect cross-thread access
        self._speaker_lock = threading.Lock()
        self._speaker      = None
        self._connected    = False
        self._is_startup   = is_startup

        # Background worker pool (3 threads: connect, poll, cover)
        self._executor = concurrent.futures.ThreadPoolExecutor(max_workers=3)

        # Thread-safe queue for pushing updates to the UI thread
        self._ui_q = queue.Queue()

        # Local state mirrors
        self._poll_running   = False
        self._muted          = False
        self._song_length_ms = 0
        self._cover_url      = None
        self._no_cover_img   = None
        self._volume         = 30
        self._tray_icon      = None
        self._active_source  = None
        self._speaker_status = None

        self._hook      = None
        self._hook_proc = None

        if show_event:
            threading.Thread(
                target=self._wait_show_event, args=(show_event,),
                daemon=True).start()

        self._build_ui()
        has_config = self._load_config()
        self._setup_tray()
        self._setup_mouse_hook()

        # Drain the UI queue every 200 ms
        self.after(200, self._drain_queue)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        # First launch (no config yet): show the window so the user can
        # enter the speaker IP. Otherwise stay hidden in the tray.
        if has_config:
            self.withdraw()

    # =========================================================================
    # Thread-safe speaker property
    # =========================================================================

    @property
    def speaker(self):
        with self._speaker_lock:
            return self._speaker

    @speaker.setter
    def speaker(self, value):
        with self._speaker_lock:
            self._speaker = value

    # =========================================================================
    # Config persistence
    # =========================================================================

    def _load_config(self):
        """Returns True if a usable config (with an IP) was loaded."""
        try:
            with CONFIG_PATH.open() as f:
                data = json.load(f)
            ip = data.get("ip", "")
            self._auto_start_usb_var.set(data.get("auto_start_usb", False))
            if ip:
                self._ip_var.set(ip)
                # Auto-connect once the window is ready
                self.after(300, self._connect)
                return True
        except Exception:
            pass
        return False

    def _save_config(self):
        try:
            with CONFIG_PATH.open("w") as f:
                json.dump({
                    "ip": self._ip_var.get().strip(),
                    "auto_start_usb": self._auto_start_usb_var.get()
                }, f)
        except Exception:
            pass

    # =========================================================================
    # UI construction
    # =========================================================================

    def _build_ui(self):
        self._left = ctk.CTkFrame(self, width=LEFT_PANEL_W, corner_radius=8)
        self._left.pack(side="left", fill="y", padx=(8, 4), pady=8)
        self._left.pack_propagate(False)

        self._right = ctk.CTkFrame(self, corner_radius=8)
        self._right.pack(side="right", fill="both", expand=True,
                         padx=(4, 8), pady=8)

        self._build_left_panel()
        self._build_right_panel()

    # -- Left panel -----------------------------------------------------------

    def _build_left_panel(self):
        p = self._left
        L = LAYOUT  # shorthand

        F = FONTS  # shorthand

        # --- Title ---
        ctk.CTkLabel(p, text="KEF Controller",
                     font=ctk.CTkFont(size=F["title"], weight="bold")
                     ).place(x=L["title"]["x"], y=L["title"]["y"])

        # --- IP entry ---
        self._ip_var = ctk.StringVar()
        self._ip_entry = ctk.CTkEntry(
            p, textvariable=self._ip_var,
            placeholder_text="192.168.x.x",
            width=L["ip_entry"]["w"], height=L["ip_entry"]["h"],
            font=ctk.CTkFont(size=F["ip_entry"]))
        self._ip_entry.place(x=L["ip_entry"]["x"], y=L["ip_entry"]["y"])
        self._ip_entry.bind("<Return>", lambda _e: self._connect())

        # --- Connect button ---
        self._conn_btn = ctk.CTkButton(
            p, text="Connect", command=self._connect,
            width=L["conn_btn"]["w"], height=L["conn_btn"]["h"],
            font=ctk.CTkFont(size=F["conn_btn"]))
        self._conn_btn.place(x=L["conn_btn"]["x"], y=L["conn_btn"]["y"])

        # --- Auto-start USB checkbox ---
        self._auto_start_usb_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(
            p, text="Auto start USB on boot",
            variable=self._auto_start_usb_var,
            command=self._save_config,
            font=ctk.CTkFont(size=F["auto_usb_check"]),
            checkbox_width=16, checkbox_height=16,
        ).place(x=L["auto_usb_check"]["x"], y=L["auto_usb_check"]["y"])

        # --- Separator 1 ---
        ctk.CTkFrame(p, width=L["sep1"]["w"], height=L["sep1"]["h"],
                     fg_color="gray40").place(
            x=L["sep1"]["x"], y=L["sep1"]["y"])

        # --- Speaker name (bold) ---
        self._name_lbl = ctk.CTkLabel(
            p, text="", anchor="w", width=L["name_lbl"]["w"],
            font=ctk.CTkFont(size=F["name_lbl"], weight="bold"))
        self._name_lbl.place(x=L["name_lbl"]["x"], y=L["name_lbl"]["y"])

        # --- Model (left) ---
        self._model_lbl = ctk.CTkLabel(
            p, text="", text_color="gray70",
            font=ctk.CTkFont(size=F["model_lbl"]), anchor="w")
        self._model_lbl.place(x=L["model_lbl"]["x"], y=L["model_lbl"]["y"])

        # --- Firmware (right of model) ---
        self._fw_lbl = ctk.CTkLabel(
            p, text="", text_color="gray60", width=L["fw_lbl"]["w"],
            font=ctk.CTkFont(size=F["fw_lbl"]), anchor="e")
        self._fw_lbl.place(x=L["fw_lbl"]["x"], y=L["fw_lbl"]["y"])

        # --- Separator 2 ---
        ctk.CTkFrame(p, width=L["sep2"]["w"], height=L["sep2"]["h"],
                     fg_color="gray40").place(
            x=L["sep2"]["x"], y=L["sep2"]["y"])

        # --- Power status dot + label (left) ---
        self._status_dot = ctk.CTkLabel(
            p, text="●", font=ctk.CTkFont(size=F["status_dot"]),
            text_color="gray50")
        self._status_dot.place(x=L["status_dot"]["x"], y=L["status_dot"]["y"])

        self._status_lbl = ctk.CTkLabel(
            p, text="Disconnected", anchor="w",
            font=ctk.CTkFont(size=F["status_lbl"]))
        self._status_lbl.place(x=L["status_lbl"]["x"], y=L["status_lbl"]["y"])

        # --- "Input Source" label (right) ---
        ctk.CTkLabel(p, text="Input Source",
                     font=ctk.CTkFont(size=F["input_src_lbl"], weight="bold")
                     ).place(x=L["input_src_lbl"]["x"],
                             y=L["input_src_lbl"]["y"])

        # --- Source buttons (3 columns × 2 rows) ---
        self._src_btns = {}
        self._src_imgs = {"on": {}, "off": {}}
        for i, src in enumerate(SOURCES):
            off_pil = _make_source_btn_image(src, active=False)
            on_pil  = _make_source_btn_image(src, active=True)
            self._src_imgs["off"][src] = ctk.CTkImage(
                light_image=off_pil, dark_image=off_pil,
                size=(BTN_SIZE, BTN_SIZE))
            self._src_imgs["on"][src] = ctk.CTkImage(
                light_image=on_pil, dark_image=on_pil,
                size=(BTN_SIZE, BTN_SIZE))

            row, col = i // 3, i % 3
            cx = L["btn_cols_x"][col]
            cy = L["btn_grid_y"] + row * L["btn_row_h"]

            btn = ctk.CTkLabel(
                p, text="", image=self._src_imgs["off"][src],
                width=BTN_SIZE, height=BTN_SIZE,
                fg_color="transparent", cursor="hand2",
            )
            btn.place(x=cx, y=cy)
            btn.bind("<Button-1>",
                     lambda _e, s=src: self._on_src_click(s))

            lbl = ctk.CTkLabel(
                p, text=SOURCE_LABELS[src],
                font=ctk.CTkFont(size=F["btn_label"]),
                text_color="gray70")
            lbl.place(x=cx + BTN_SIZE // 2, y=cy + BTN_SIZE + 2,
                      anchor="n")

            self._src_btns[src] = btn

    # -- Right panel ----------------------------------------------------------

    def _build_right_panel(self):
        p = self._right

        # Cover + song info
        top = ctk.CTkFrame(p, fg_color="transparent")
        top.pack(fill="x", padx=12, pady=(12, 4))

        self._cover_lbl = ctk.CTkLabel(top, text="", width=140, height=140)
        self._cover_lbl.pack(side="left", padx=(0, 12))
        self._set_no_cover()

        info_col = ctk.CTkFrame(top, fg_color="transparent")
        info_col.pack(side="left", fill="both", expand=True, anchor="n")

        self._title_lbl = ctk.CTkLabel(
            info_col, text="--",
            font=ctk.CTkFont(size=13, weight="bold"),
            wraplength=280, justify="left", anchor="w")
        self._title_lbl.pack(fill="x", pady=(8, 2))

        self._artist_lbl = ctk.CTkLabel(
            info_col, text="--",
            font=ctk.CTkFont(size=11), text_color="gray70",
            wraplength=280, justify="left", anchor="w")
        self._artist_lbl.pack(fill="x", pady=1)

        self._album_lbl = ctk.CTkLabel(
            info_col, text="--",
            font=ctk.CTkFont(size=10), text_color="gray60",
            wraplength=280, justify="left", anchor="w")
        self._album_lbl.pack(fill="x", pady=1)

        self._src_info_lbl = ctk.CTkLabel(
            info_col, text="",
            font=ctk.CTkFont(size=10), text_color="gray50",
            anchor="w")
        self._src_info_lbl.pack(fill="x", pady=(8, 0))

        # Progress bar
        pf = ctk.CTkFrame(p, fg_color="transparent")
        pf.pack(fill="x", padx=12, pady=4)

        self._progress = ctk.CTkProgressBar(pf, height=5)
        self._progress.pack(fill="x")
        self._progress.set(0)

        tf = ctk.CTkFrame(pf, fg_color="transparent")
        tf.pack(fill="x")
        self._time_cur = ctk.CTkLabel(
            tf, text="0:00", font=ctk.CTkFont(size=9), text_color="gray60")
        self._time_cur.pack(side="left")
        self._time_tot = ctk.CTkLabel(
            tf, text="0:00", font=ctk.CTkFont(size=9), text_color="gray60")
        self._time_tot.pack(side="right")

        # Playback controls
        pb = ctk.CTkFrame(p, fg_color="transparent")
        pb.pack(pady=8)

        self._prev_btn = ctk.CTkButton(
            pb, text="⏮", width=44, height=34,
            font=ctk.CTkFont(size=15), state="disabled",
            command=self._cmd_prev)
        self._prev_btn.pack(side="left", padx=4)

        self._play_btn = ctk.CTkButton(
            pb, text="⏯", width=54, height=34,
            font=ctk.CTkFont(size=17), state="disabled",
            command=self._cmd_play_pause)
        self._play_btn.pack(side="left", padx=4)

        self._next_btn = ctk.CTkButton(
            pb, text="⏭", width=44, height=34,
            font=ctk.CTkFont(size=15), state="disabled",
            command=self._cmd_next)
        self._next_btn.pack(side="left", padx=4)

        _separator(p)

        # Volume
        vf = ctk.CTkFrame(p, fg_color="transparent")
        vf.pack(fill="x", padx=12, pady=4)

        ctk.CTkLabel(vf, text="Volume",
                     font=ctk.CTkFont(size=11, weight="bold")).pack(
            side="left", padx=(0, 6))

        self._mute_btn = ctk.CTkButton(
            vf, text="Mute", width=60, height=24,
            font=ctk.CTkFont(size=10),
            state="disabled", command=self._cmd_toggle_mute)
        self._mute_btn.pack(side="left", padx=(0, 8))

        self._vol_lbl = ctk.CTkLabel(
            vf, text="--", width=26, font=ctk.CTkFont(size=11))
        self._vol_lbl.pack(side="right")

        self._vol_slider = ctk.CTkSlider(
            vf, from_=0, to=100, number_of_steps=100,
            state="disabled", command=self._on_vol_drag)
        self._vol_slider.pack(side="left", fill="x", expand=True)
        self._vol_slider.set(30)
        # Mouse wheel over the slider tweaks the volume
        self._vol_slider.bind("<MouseWheel>", self._on_slider_scroll)

    # =========================================================================
    # UI helpers
    # =========================================================================

    def _set_no_cover(self):
        if self._no_cover_img is None:
            img = Image.new("RGB", (140, 140), color=(52, 52, 56))
            self._no_cover_img = ctk.CTkImage(
                light_image=img, dark_image=img, size=(140, 140))
        self._cover_lbl.configure(image=self._no_cover_img)

    def _enable_controls(self, on):
        s = "normal" if on else "disabled"
        self._src_enabled = on
        for btn in self._src_btns.values():
            btn.configure(cursor="hand2" if on else "arrow")
        self._prev_btn.configure(state=s)
        self._play_btn.configure(state=s)
        self._next_btn.configure(state=s)
        self._mute_btn.configure(state=s)
        self._vol_slider.configure(state=s)

    def _update_power_status(self, status):
        if status == PWR_ON:
            self._status_dot.configure(text_color="#2ecc71")
            self._status_lbl.configure(text="Power On")
        elif status == PWR_STANDBY:
            self._status_dot.configure(text_color="#e67e22")
            self._status_lbl.configure(text="Standby")
        else:
            self._status_dot.configure(text_color="gray50")
            self._status_lbl.configure(text=status or "Unknown")

    def _highlight_source(self, active_src):
        for src, btn in self._src_btns.items():
            if src == active_src:
                btn.configure(image=self._src_imgs["on"][src])
            else:
                btn.configure(image=self._src_imgs["off"][src])
        self._update_tray_icon(active_src)

    def _on_src_click(self, src):
        if getattr(self, "_src_enabled", False):
            self._cmd_set_source(src)

    @staticmethod
    def _ms_to_str(ms):
        if not ms:
            return "0:00"
        s = int(ms) // 1000
        return f"{s // 60}:{s % 60:02d}"

    def _reset_now_playing(self):
        self._title_lbl.configure(text="--")
        self._artist_lbl.configure(text="--")
        self._album_lbl.configure(text="--")
        self._src_info_lbl.configure(text="")
        self._progress.set(0)
        self._time_cur.configure(text="0:00")
        self._time_tot.configure(text="0:00")
        self._set_no_cover()
        self._cover_url = None
        self._song_length_ms = 0

    # =========================================================================
    # Queue drain — runs on main thread every 200 ms
    # =========================================================================

    def _drain_queue(self):
        try:
            while not self._ui_q.empty():
                kind, data = self._ui_q.get_nowait()
                if kind == "connected":
                    self._on_connected(data)
                elif kind == "state":
                    self._apply_state(data)
                elif kind == "cover":
                    self._cover_lbl.configure(image=data)
                elif kind == "error":
                    messagebox.showerror(
                        "Connection Error",
                        f"Could not connect to speaker:\n{data}")
                    self._conn_btn.configure(
                        state="normal", text="Connect",
                        command=self._connect)
                elif kind == "lost":
                    self._on_connection_lost(data)
                elif kind == "cmd_error":
                    # Show briefly in status label; next poll overwrites it
                    self._status_lbl.configure(text=f"Err: {data[:28]}")
                elif kind == "scroll_volume":
                    self._scroll_volume(data)
                elif kind == "show":
                    self._show_main_window()
        except Exception:
            pass
        self.after(200, self._drain_queue)

    # =========================================================================
    # Connection
    # =========================================================================

    def _connect(self):
        ip = self._ip_var.get().strip()
        if not ip:
            messagebox.showwarning(
                "Missing IP", "Please enter the speaker IP address.")
            return
        self._conn_btn.configure(state="disabled", text="Connecting...")
        self._executor.submit(self._bg_connect, ip)

    def _bg_connect(self, ip):
        try:
            spk = KefConnector(ip)
            self.speaker = spk
            self._save_config()
            self._ui_q.put(("connected", {
                "status":   spk.status,
                "name":     spk.speaker_name,
                "model":    spk.speaker_model,
                "firmware": spk.firmware_version,
            }))
            self._bg_refresh_state()
        except Exception as exc:
            self.speaker = None
            self._ui_q.put(("error", str(exc)))

    def _on_connected(self, info):
        self._connected = True
        self._conn_btn.configure(
            state="normal", text="Disconnect",
            command=self._disconnect)
        self._update_power_status(info["status"])
        self._name_lbl.configure(text=info.get("name") or "Unknown")
        self._model_lbl.configure(text=info.get("model") or "")
        fw = info.get("firmware") or ""
        self._fw_lbl.configure(text=f"FW: {fw}" if fw else "")
        self._enable_controls(True)
        self._start_polling()

        # Auto-activate USB on startup if configured
        if self._is_startup and self._auto_start_usb_var.get():
            self.after(1500, lambda: self._cmd_set_source("usb"))

    def _disconnect(self):
        self._poll_running = False
        self._connected = False
        self.speaker = None
        self._conn_btn.configure(
            state="normal", text="Connect", command=self._connect)
        self._status_dot.configure(text_color="gray50")
        self._status_lbl.configure(text="Disconnected")
        self._name_lbl.configure(text="")
        self._model_lbl.configure(text="")
        self._fw_lbl.configure(text="")
        self._active_source = None
        self._speaker_status = None
        self._enable_controls(False)
        self._reset_now_playing()
        self._update_tray_icon(None)
        self._update_tray_title()

    def _on_connection_lost(self, reason):
        if self._connected:
            self._disconnect()
            messagebox.showwarning(
                "Connection Lost",
                f"Lost connection to speaker:\n{reason}")

    # =========================================================================
    # Background state fetch (full refresh)
    # =========================================================================

    def _bg_refresh_state(self):
        spk = self.speaker
        if not spk:
            return
        try:
            state = {
                "speaker_status": spk.status,
                "volume":         spk.volume,
                "source":         spk.source,
            }
            if state["speaker_status"] == PWR_ON:
                try:
                    state["song_info"]   = spk.get_song_information()
                    state["song_length"] = spk.song_length
                    state["song_status"] = spk.song_status
                    state["is_playing"]  = spk.is_playing
                except Exception:
                    pass
            self._ui_q.put(("state", state))
        except Exception as exc:
            self._ui_q.put(("cmd_error", f"Refresh: {exc}"))

    # =========================================================================
    # Polling loop (background thread)
    # =========================================================================

    def _start_polling(self):
        self._poll_running = True
        threading.Thread(target=self._poll_loop, daemon=True).start()

    def _poll_loop(self):
        while self._poll_running and self.speaker:
            try:
                changes = self.speaker.poll_speaker(
                    timeout=3, poll_song_status=True)
                if changes:
                    self._ui_q.put(("state", changes))
            except Exception:
                time.sleep(2)
                continue
            time.sleep(POLL_INTERVAL_S)

    # =========================================================================
    # State application (main thread)
    # =========================================================================

    def _apply_state(self, state):
        # Power status (powerOn / standby)
        pwr = state.get("speaker_status")
        if pwr:
            self._speaker_status = pwr
            self._update_power_status(pwr)
            if pwr == PWR_STANDBY:
                self._update_tray_icon(None)  # grey when off
                self._active_source = None
                for src, btn in self._src_btns.items():
                    btn.configure(image=self._src_imgs["off"][src])

        # Player state (playing / paused / stopped / buffering …)
        player_state = state.get("status")
        if player_state:
            playing = (player_state == "playing")
            self._play_btn.configure(text="⏸" if playing else "▶")

        # is_playing from initial refresh
        if "is_playing" in state:
            self._play_btn.configure(
                text="⏸" if state["is_playing"] else "▶")

        # Volume
        vol = state.get("volume")
        if vol is not None:
            v = int(vol)
            self._volume = v
            self._vol_slider.set(v)
            self._vol_lbl.configure(text=str(v))
            self._update_tray_title(v)

        # Mute (hardware mute flag from speaker)
        muted = state.get("mute")
        if muted is not None:
            self._muted = bool(muted)
            self._mute_btn.configure(
                text="Unmute" if self._muted else "Mute")

        # Source - update before potential auto-USB activation
        src = state.get("source")
        if src and src != PWR_STANDBY:
            self._active_source = src
            label = SOURCE_LABELS.get(src, src)
            self._src_info_lbl.configure(text=f"Source: {label}")
            self._highlight_source(src)
        elif src == PWR_STANDBY:
            self._update_tray_icon(None)

        # Song info
        song_info = state.get("song_info")
        if song_info:
            self._title_lbl.configure(
                text=song_info.get("title") or "--")
            self._artist_lbl.configure(
                text=song_info.get("artist") or "--")
            self._album_lbl.configure(
                text=song_info.get("album") or "--")
            cover = song_info.get("cover_url")
            if cover and cover != self._cover_url:
                self._cover_url = cover
                self._executor.submit(self._bg_fetch_cover, cover)

        # Song length
        song_length = state.get("song_length")
        if song_length is not None:
            self._song_length_ms = song_length or 0
            self._time_tot.configure(
                text=self._ms_to_str(self._song_length_ms))

        # Song progress
        song_pos = state.get("song_status")
        if song_pos is not None and self._song_length_ms > 0:
            ratio = max(0.0, min(1.0, song_pos / self._song_length_ms))
            self._progress.set(ratio)
            self._time_cur.configure(text=self._ms_to_str(song_pos))

    # =========================================================================
    # Cover art fetch (background thread)
    # =========================================================================

    def _bg_fetch_cover(self, url):
        try:
            with urllib.request.urlopen(url, timeout=5) as resp:
                raw = resp.read()
            img = Image.open(BytesIO(raw)).resize((140, 140), Image.LANCZOS)
            ctk_img = ctk.CTkImage(
                light_image=img, dark_image=img, size=(140, 140))
            self._ui_q.put(("cover", ctk_img))
        except Exception:
            pass

    # =========================================================================
    # Speaker commands — fire-and-forget in executor
    # =========================================================================

    def _run(self, fn):
        if self.speaker and self._connected:
            def _safe():
                try:
                    fn()
                except Exception as exc:
                    self._ui_q.put(("cmd_error", str(exc)))
            self._executor.submit(_safe)

    def _cmd_set_source(self, src):
        if self._active_source == src and self._speaker_status == PWR_ON:
            def _f():
                self.speaker.shutdown()
                self._ui_q.put(("state", {"speaker_status": PWR_STANDBY}))
            self._run(_f)
        else:
            def _f():
                self.speaker.source = src
                self._ui_q.put(("state", {"source": src, "speaker_status": PWR_ON}))
            self._run(_f)

    def _cmd_play_pause(self):
        spk = self.speaker
        if spk and self._connected:
            self._run(spk.toggle_play_pause)

    def _cmd_prev(self):
        spk = self.speaker
        if spk and self._connected:
            self._run(spk.previous_track)

    def _cmd_next(self):
        spk = self.speaker
        if spk and self._connected:
            self._run(spk.next_track)

    def _cmd_toggle_mute(self):
        def _f():
            if self._muted:
                self.speaker.unmute()
                self._ui_q.put(("state", {"mute": False}))
            else:
                self.speaker.mute()
                self._ui_q.put(("state", {"mute": True}))
        self._run(_f)

    def _on_vol_drag(self, value):
        v = int(value)
        if v == self._volume:
            return  # filter duplicate steps to avoid spamming the speaker
        self._volume = v
        self._vol_lbl.configure(text=str(v))
        # Live send (no debounce)
        self._run(lambda: self.speaker.set_volume(v))
        self._update_tray_title(v)

    def _on_slider_scroll(self, event):
        step = 1 if event.delta > 0 else -1
        self._scroll_volume(step)

    def _on_ctrl_wheel(self, event):
        """Ctrl + mouse wheel → zoom the whole UI in/out (debounced)."""
        step = 0.05 if event.delta > 0 else -0.05
        new_scale = round(self._ui_scale + step, 2)
        new_scale = max(0.6, min(1.6, new_scale))
        if new_scale == self._ui_scale:
            return
        self._ui_scale = new_scale
        # Debounce: cancel any pending apply so rapid scrolls coalesce
        # into a single window resize at the end, killing the flash.
        if getattr(self, "_zoom_after_id", None):
            try:
                self.after_cancel(self._zoom_after_id)
            except Exception:
                pass
        self._zoom_after_id = self.after(80, self._apply_zoom)

    def _apply_zoom(self):
        """Apply the pending UI scale to widgets and window."""
        self._zoom_after_id = None
        scale = self._ui_scale
        try:
            # set_widget_scaling scales all CTk widgets (size, fonts, and
            # place() x/y) automatically. We do NOT pre-multiply widget
            # widths — CTk would scale them a second time → distortion.
            ctk.set_widget_scaling(scale)
            # geometry() is a raw tk method, NOT scaled by CTk → we apply
            # the scale ourselves so the window matches the widgets.
            base_w, base_h = 700, 380
            self.geometry(f"{int(base_w * scale)}x{int(base_h * scale)}")
        except Exception:
            pass

    # =========================================================================
    # System tray
    # =========================================================================

    def _setup_mouse_hook(self):
        # The hook MUST run in a dedicated thread with its own message loop,
        # otherwise it interrupts tkinter at moments where the GIL is not
        # available -> Fatal Python error.
        threading.Thread(target=self._hook_thread_proc, daemon=True).start()

    def _wait_show_event(self, event_handle):
        kernel32 = ctypes.windll.kernel32
        kernel32.WaitForSingleObject.argtypes = [
            ctypes.c_void_p, ctypes.wintypes.DWORD]
        kernel32.WaitForSingleObject.restype = ctypes.wintypes.DWORD
        INFINITE     = 0xFFFFFFFF
        WAIT_OBJECT_0 = 0x00000000
        while True:
            if kernel32.WaitForSingleObject(event_handle, INFINITE) == WAIT_OBJECT_0:
                self._ui_q.put(("show", None))

    def _hook_thread_proc(self):
        user32  = ctypes.windll.user32
        shell32 = ctypes.windll.shell32

        WPARAM_T  = ctypes.c_size_t
        LPARAM_T  = ctypes.c_ssize_t
        LRESULT_T = ctypes.c_ssize_t

        user32.CallNextHookEx.argtypes = [
            ctypes.c_void_p, ctypes.c_int, WPARAM_T, LPARAM_T]
        user32.CallNextHookEx.restype = LRESULT_T

        shell32.Shell_NotifyIconGetRect.argtypes = [
            ctypes.POINTER(_NOTIFYICONIDENTIFIER),
            ctypes.POINTER(ctypes.wintypes.RECT)]
        shell32.Shell_NotifyIconGetRect.restype = ctypes.c_long

        HOOKPROC = ctypes.WINFUNCTYPE(
            LRESULT_T, ctypes.c_int, WPARAM_T, LPARAM_T)

        user32.SetWindowsHookExW.argtypes = [
            ctypes.c_int, HOOKPROC, ctypes.c_void_p, ctypes.wintypes.DWORD]
        user32.SetWindowsHookExW.restype = ctypes.c_void_p

        user32.GetMessageW.argtypes = [
            ctypes.POINTER(ctypes.wintypes.MSG),
            ctypes.c_void_p, ctypes.wintypes.UINT, ctypes.wintypes.UINT]
        user32.GetMessageW.restype = ctypes.c_int

        def _on_icon(x, y):
            try:
                hwnd = getattr(self._tray_icon, "_hwnd", None)
                if not hwnd:
                    return False
                nid = _NOTIFYICONIDENTIFIER()
                nid.cbSize = ctypes.sizeof(_NOTIFYICONIDENTIFIER)
                nid.hWnd   = hwnd
                nid.uID    = 0
                rect = ctypes.wintypes.RECT()
                if shell32.Shell_NotifyIconGetRect(
                        ctypes.byref(nid), ctypes.byref(rect)) != 0:
                    return False
                return (rect.left <= x <= rect.right
                        and rect.top <= y <= rect.bottom)
            except Exception:
                return False

        def _handler(nCode, wParam, lParam):
            try:
                if nCode >= 0 and wParam == _WM_MOUSEWHEEL:
                    info = ctypes.cast(
                        lParam, ctypes.POINTER(_MSLLHOOKSTRUCT)).contents
                    if _on_icon(info.pt.x, info.pt.y):
                        delta = ctypes.c_short(info.mouseData >> 16).value
                        step = 1 if delta > 0 else -1
                        # Never call tkinter directly from this thread;
                        # push to the UI queue drained by the main thread.
                        self._ui_q.put(("scroll_volume", step))
                        return 1  # block the wheel event
            except Exception:
                pass
            return user32.CallNextHookEx(None, nCode, wParam, lParam)

        self._hook_proc = HOOKPROC(_handler)
        self._hook = user32.SetWindowsHookExW(
            _WH_MOUSE_LL, self._hook_proc, None, 0)

        # Message pump: without this, low-level hooks are never invoked
        msg = ctypes.wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

    def _scroll_volume(self, step):
        if not self._connected:
            return
        new_vol = max(0, min(100, self._volume + step))
        self._volume = new_vol
        self._vol_slider.set(new_vol)
        self._vol_lbl.configure(text=str(new_vol))
        # Live - send immediately without debounce
        self._run(lambda: self.speaker.set_volume(new_vol))
        self._update_tray_title(new_vol)

    def _update_tray_title(self, vol=None):
        if not self._tray_icon:
            return
        if not self._connected:
            self._tray_icon.title = "KEF LSX II (disconnected)"
            return
        if vol is None:
            vol = self._volume
        self._tray_icon.title = f"KEF LSX II ({vol}%)"

    @staticmethod
    def _make_tray_image(bg_hex="#898d8b"):
        # Solid square - no ellipse anti-aliasing so the displayed color
        # in the tray exactly matches the UI button color.
        size = 64
        img = Image.new("RGB", (size, size), bg_hex)
        draw = ImageDraw.Draw(img)
        r, g, b = _hex_to_rgb(bg_hex)
        luma = 0.299 * r + 0.587 * g + 0.114 * b
        fg = "black" if luma > 160 else "white"
        lw = 8
        draw.line([(18, 10), (18, 54)], fill=fg, width=lw)
        draw.line([(18, 32), (50, 10)], fill=fg, width=lw)
        draw.line([(18, 32), (50, 54)], fill=fg, width=lw)
        return img

    def _update_tray_icon(self, source=None):
        if not self._tray_icon:
            return
        bg_hex = SOURCE_COLORS.get(source, "#5A5A5A")
        try:
            self._tray_icon.icon = self._make_tray_image(bg_hex)
        except Exception:
            pass

    def _setup_tray(self):
        menu = pystray.Menu(
            pystray.MenuItem("USB", self._tray_activate_usb, default=True),
            pystray.MenuItem("Open", self._tray_show_main),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                "Run with Windows",
                self._tray_toggle_startup,
                checked=lambda _: _is_startup_enabled(),
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", self._tray_quit),
        )
        self._tray_icon = pystray.Icon(
            "KEF LSX II", self._make_tray_image("#5A5A5A"), "KEF LSX II", menu)
        threading.Thread(target=self._tray_icon.run, daemon=True).start()

    def _tray_activate_usb(self, _icon, _item):
        self.after(0, lambda: self._cmd_set_source("usb"))

    def _tray_show_main(self, _icon, _item):
        self.after(0, self._show_main_window)

    def _tray_toggle_startup(self, *_):
        _set_startup(not _is_startup_enabled())

    def _tray_quit(self, _icon, _item):
        self.after(0, self._quit_app)

    def _show_main_window(self):
        self.deiconify()
        self.lift()
        self.focus_force()

    # =========================================================================
    # Cleanup
    # =========================================================================

    def _quit_app(self):
        self._poll_running = False
        if self._hook:
            ctypes.windll.user32.UnhookWindowsHookEx(self._hook)
        if self._tray_icon:
            self._tray_icon.stop()
        self._executor.shutdown(wait=False)
        self.destroy()

    def _on_close(self):
        self.withdraw()  # minimize to tray instead of quitting


# ---------------------------------------------------------------------------
# Single-instance guard (named Windows mutex + named event)
# ---------------------------------------------------------------------------
_SINGLE_INSTANCE_MUTEX = "Local\\KEF.LSX.Controller.SingleInstance"
_SINGLE_INSTANCE_EVENT = "Local\\KEF.LSX.Controller.ShowWindow"

_EVENT_MODIFY_STATE = 0x0002


def _acquire_single_instance():
    """Returns the mutex handle if this is the first instance, else signals the
    existing instance to show its window and returns None.

    Keep the returned handle alive for the process lifetime.
    """
    ERROR_ALREADY_EXISTS = 183
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateMutexW.argtypes = [
        ctypes.c_void_p, ctypes.wintypes.BOOL, ctypes.c_wchar_p]
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    handle = kernel32.CreateMutexW(None, False, _SINGLE_INSTANCE_MUTEX)
    if not handle:
        return None
    if ctypes.GetLastError() == ERROR_ALREADY_EXISTS:
        kernel32.CloseHandle(handle)
        kernel32.OpenEventW.argtypes = [
            ctypes.wintypes.DWORD, ctypes.wintypes.BOOL, ctypes.c_wchar_p]
        kernel32.OpenEventW.restype = ctypes.c_void_p
        ev = kernel32.OpenEventW(_EVENT_MODIFY_STATE, False, _SINGLE_INSTANCE_EVENT)
        if ev:
            kernel32.SetEvent(ev)
            kernel32.CloseHandle(ev)
        return None
    return handle


def _create_show_event():
    """Creates the named auto-reset event the first instance listens on."""
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateEventW.argtypes = [
        ctypes.c_void_p, ctypes.wintypes.BOOL, ctypes.wintypes.BOOL, ctypes.c_wchar_p]
    kernel32.CreateEventW.restype = ctypes.c_void_p
    return kernel32.CreateEventW(None, False, False, _SINGLE_INSTANCE_EVENT)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    _instance_handle = _acquire_single_instance()
    if _instance_handle is None:
        sys.exit(0)
    _show_event = _create_show_event()
    _is_startup = "--startup" in sys.argv
    app = KefApp(show_event=_show_event, is_startup=_is_startup)
    app.mainloop()
