"""
KEF LSX II Controller
GUI application built on top of pykefcontrol.
"""

import time
import queue
import threading
import urllib.request
import concurrent.futures
from io import BytesIO
from tkinter import messagebox

import customtkinter as ctk
from PIL import Image

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

COLOR_SRC_ACTIVE  = "#1a6b3c"
COLOR_SRC_DEFAULT = ["#3B8ED0", "#1F6AA5"]
COLOR_PWR_ON      = ("#1a6b3c", "#25a05a")
COLOR_PWR_OFF     = ("#6b1a1a", "#a02525")


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

    def __init__(self):
        super().__init__()
        self.title("KEF LSX II Controller")
        self.geometry("880x600")
        self.resizable(False, False)

        # Speaker handle
        self.speaker = None
        self._connected = False

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
        self._vol_debounce   = None

        self._build_ui()

        # Drain the UI queue every 200 ms
        self.after(200, self._drain_queue)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

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
            btn.configure(
                fg_color=COLOR_SRC_ACTIVE if src == active_src
                else COLOR_SRC_DEFAULT)

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
        except Exception:
            pass

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

    # =========================================================================
    # State application (main thread)
    # =========================================================================

    def _apply_state(self, state):
        # Power status (powerOn / standby)
        pwr = state.get("speaker_status")
        if pwr:
            self._update_power_status(pwr)

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
            self._vol_slider.set(v)
            self._vol_lbl.configure(text=str(v))

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
        """Submit fn to thread pool only when connected."""
        if self.speaker and self._connected:
            def _safe():
                try:
                    fn()
                except Exception:
                    pass
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
        self._vol_lbl.configure(text=str(v))
        if self._vol_debounce:
            self.after_cancel(self._vol_debounce)
        # Apply 300 ms after the user stops dragging
        self._vol_debounce = self.after(
            300,
            lambda: self._run(lambda: self.speaker.set_volume(v))
            if self.speaker else None,
        )

    # =========================================================================
    # Cleanup
    # =========================================================================

    def _on_close(self):
        self._poll_running = False
        self._executor.shutdown(wait=False)
        self.destroy()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    app = KefApp()
    app.mainloop()
