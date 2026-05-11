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
    "optical":   "Optical",
    "analog":    "Analog",
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


def _hex_to_rgb(hex_color):
    h = hex_color.lstrip("#")
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))

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
    return f'"{sys.executable}"'


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

    def __init__(self, show_event=None):
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
        self.geometry("880x600")
        self.minsize(880, 600)
        self.resizable(True, True)
        try:
            self.iconbitmap(str(ICON_PATH))
        except Exception:
            pass

        # Speaker handle — read/written via property to protect cross-thread access
        self._speaker_lock = threading.Lock()
        self._speaker      = None
        self._connected    = False

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
                json.dump({"ip": self._ip_var.get().strip()}, f)
        except Exception:
            pass

    # =========================================================================
    # UI construction
    # =========================================================================

    def _build_ui(self):
        self._left = ctk.CTkFrame(self, width=250, corner_radius=10)
        self._left.pack(side="left", fill="y", padx=(10, 5), pady=10)
        self._left.pack_propagate(False)

        self._right = ctk.CTkFrame(self, corner_radius=10)
        self._right.pack(side="right", fill="both", expand=True,
                         padx=(5, 10), pady=10)

        self._build_left_panel()
        self._build_right_panel()

    # -- Left panel -----------------------------------------------------------

    def _build_left_panel(self):
        p = self._left

        ctk.CTkLabel(p, text="KEF Controller",
                     font=ctk.CTkFont(size=16, weight="bold")).pack(
            pady=(15, 5))

        ctk.CTkLabel(p, text="Speaker IP Address",
                     font=ctk.CTkFont(size=12)).pack(pady=(8, 2))

        self._ip_var = ctk.StringVar()
        self._ip_entry = ctk.CTkEntry(
            p, textvariable=self._ip_var,
            placeholder_text="192.168.x.x", width=200)
        self._ip_entry.pack(pady=2)
        self._ip_entry.bind("<Return>", lambda _e: self._connect())

        self._conn_btn = ctk.CTkButton(
            p, text="Connect", command=self._connect, width=200)
        self._conn_btn.pack(pady=(5, 8))

        _separator(p)

        # --- Status row ---
        row = ctk.CTkFrame(p, fg_color="transparent")
        row.pack(fill="x", padx=10, pady=4)
        self._status_dot = ctk.CTkLabel(
            row, text="●", font=ctk.CTkFont(size=14), text_color="gray50")
        self._status_dot.pack(side="left")
        self._status_lbl = ctk.CTkLabel(
            row, text="Disconnected", font=ctk.CTkFont(size=12))
        self._status_lbl.pack(side="left", padx=5)

        self._name_lbl = ctk.CTkLabel(
            p, text="", wraplength=220,
            font=ctk.CTkFont(size=12, weight="bold"))
        self._name_lbl.pack(pady=1)

        self._model_lbl = ctk.CTkLabel(
            p, text="", text_color="gray70",
            font=ctk.CTkFont(size=11))
        self._model_lbl.pack(pady=1)

        self._fw_lbl = ctk.CTkLabel(
            p, text="", text_color="gray60",
            font=ctk.CTkFont(size=10))
        self._fw_lbl.pack(pady=1)

        _separator(p)

        # --- Source selection ---
        ctk.CTkLabel(p, text="Input Source",
                     font=ctk.CTkFont(size=12, weight="bold")).pack(
            pady=(0, 5))

        grid = ctk.CTkFrame(p, fg_color="transparent")
        grid.pack(fill="x", padx=10)

        self._src_btns = {}
        for i, src in enumerate(SOURCES):
            btn = ctk.CTkButton(
                grid, text=SOURCE_LABELS[src],
                width=92, height=30,
                font=ctk.CTkFont(size=11),
                state="disabled",
                command=lambda s=src: self._cmd_set_source(s),
            )
            btn.grid(row=i // 2, column=i % 2, padx=3, pady=3)
            self._src_btns[src] = btn

        _separator(p)

        # --- Power ---
        ctk.CTkLabel(p, text="Power",
                     font=ctk.CTkFont(size=12, weight="bold")).pack(
            pady=(0, 5))

        pwr = ctk.CTkFrame(p, fg_color="transparent")
        pwr.pack(fill="x", padx=10)

        self._power_on_btn = ctk.CTkButton(
            pwr, text="Power On", width=92, height=30, state="disabled",
            fg_color=COLOR_PWR_ON[0], hover_color=COLOR_PWR_ON[1],
            command=self._cmd_power_on,
        )
        self._power_on_btn.grid(row=0, column=0, padx=3)

        self._shutdown_btn = ctk.CTkButton(
            pwr, text="Shutdown", width=92, height=30, state="disabled",
            fg_color=COLOR_PWR_OFF[0], hover_color=COLOR_PWR_OFF[1],
            command=self._cmd_shutdown,
        )
        self._shutdown_btn.grid(row=0, column=1, padx=3)

    # -- Right panel ----------------------------------------------------------

    def _build_right_panel(self):
        p = self._right

        # Cover + song info
        top = ctk.CTkFrame(p, fg_color="transparent")
        top.pack(fill="x", padx=15, pady=(15, 5))

        self._cover_lbl = ctk.CTkLabel(top, text="", width=185, height=185)
        self._cover_lbl.pack(side="left", padx=(0, 15))
        self._set_no_cover()

        info_col = ctk.CTkFrame(top, fg_color="transparent")
        info_col.pack(side="left", fill="both", expand=True, anchor="n")

        self._title_lbl = ctk.CTkLabel(
            info_col, text="--",
            font=ctk.CTkFont(size=15, weight="bold"),
            wraplength=340, justify="left", anchor="w")
        self._title_lbl.pack(fill="x", pady=(20, 4))

        self._artist_lbl = ctk.CTkLabel(
            info_col, text="--",
            font=ctk.CTkFont(size=13), text_color="gray70",
            wraplength=340, justify="left", anchor="w")
        self._artist_lbl.pack(fill="x", pady=2)

        self._album_lbl = ctk.CTkLabel(
            info_col, text="--",
            font=ctk.CTkFont(size=12), text_color="gray60",
            wraplength=340, justify="left", anchor="w")
        self._album_lbl.pack(fill="x", pady=2)

        self._src_info_lbl = ctk.CTkLabel(
            info_col, text="",
            font=ctk.CTkFont(size=11), text_color="gray50")
        self._src_info_lbl.pack(fill="x", pady=(14, 0))

        # Progress bar
        pf = ctk.CTkFrame(p, fg_color="transparent")
        pf.pack(fill="x", padx=15, pady=5)

        self._progress = ctk.CTkProgressBar(pf, height=6)
        self._progress.pack(fill="x")
        self._progress.set(0)

        tf = ctk.CTkFrame(pf, fg_color="transparent")
        tf.pack(fill="x")
        self._time_cur = ctk.CTkLabel(
            tf, text="0:00", font=ctk.CTkFont(size=10), text_color="gray60")
        self._time_cur.pack(side="left")
        self._time_tot = ctk.CTkLabel(
            tf, text="0:00", font=ctk.CTkFont(size=10), text_color="gray60")
        self._time_tot.pack(side="right")

        # Playback controls
        pb = ctk.CTkFrame(p, fg_color="transparent")
        pb.pack(pady=10)

        self._prev_btn = ctk.CTkButton(
            pb, text="⏮", width=52, height=42,
            font=ctk.CTkFont(size=18), state="disabled",
            command=self._cmd_prev)
        self._prev_btn.pack(side="left", padx=5)

        self._play_btn = ctk.CTkButton(
            pb, text="⏯", width=62, height=42,
            font=ctk.CTkFont(size=20), state="disabled",
            command=self._cmd_play_pause)
        self._play_btn.pack(side="left", padx=5)

        self._next_btn = ctk.CTkButton(
            pb, text="⏭", width=52, height=42,
            font=ctk.CTkFont(size=18), state="disabled",
            command=self._cmd_next)
        self._next_btn.pack(side="left", padx=5)

        _separator(p)

        # Volume
        vf = ctk.CTkFrame(p, fg_color="transparent")
        vf.pack(fill="x", padx=15, pady=5)

        ctk.CTkLabel(vf, text="Volume",
                     font=ctk.CTkFont(size=12, weight="bold")).pack(
            side="left", padx=(0, 8))

        self._mute_btn = ctk.CTkButton(
            vf, text="Mute", width=75, height=28,
            state="disabled", command=self._cmd_toggle_mute)
        self._mute_btn.pack(side="left", padx=(0, 10))

        self._vol_lbl = ctk.CTkLabel(
            vf, text="--", width=32, font=ctk.CTkFont(size=13))
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
            img = Image.new("RGB", (185, 185), color=(52, 52, 56))
            self._no_cover_img = ctk.CTkImage(
                light_image=img, dark_image=img, size=(185, 185))
        self._cover_lbl.configure(image=self._no_cover_img)

    def _enable_controls(self, on):
        s = "normal" if on else "disabled"
        for btn in self._src_btns.values():
            btn.configure(state=s)
        self._power_on_btn.configure(state=s)
        self._shutdown_btn.configure(state=s)
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
                btn.configure(fg_color=SOURCE_COLORS.get(src, "#1a6b3c"))
            else:
                btn.configure(fg_color=COLOR_SRC_DEFAULT)
        self._update_tray_icon(active_src)

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
            self._update_power_status(pwr)
            if pwr == PWR_STANDBY:
                self._update_tray_icon(None)  # grey when off

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

        # Source
        src = state.get("source")
        if src and src != PWR_STANDBY:
            label = SOURCE_LABELS.get(src, src)
            self._src_info_lbl.configure(text=f"Source: {label}")
            self._highlight_source(src)

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
            img = Image.open(BytesIO(raw)).resize((185, 185), Image.LANCZOS)
            ctk_img = ctk.CTkImage(
                light_image=img, dark_image=img, size=(185, 185))
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

    def _cmd_power_on(self):
        def _f():
            self.speaker.power_on()
            time.sleep(0.5)
            self._bg_refresh_state()
        self._run(_f)

    def _cmd_shutdown(self):
        def _f():
            self.speaker.shutdown()
            self._ui_q.put(("state", {"speaker_status": PWR_STANDBY}))
        self._run(_f)

    def _cmd_set_source(self, src):
        def _f():
            self.speaker.source = src
            self._ui_q.put(("state", {"source": src}))
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
    def _make_tray_image(bg_hex="#1a6b3c"):
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
            "KEF LSX II", self._make_tray_image(), "KEF LSX II", menu)
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
    app = KefApp(show_event=_show_event)
    app.mainloop()
