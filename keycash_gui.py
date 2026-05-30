import argparse
import io
import json
import os
import re
import subprocess
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk
from typing import Any, Dict, List, Optional

from keycash_auto import build_arg_parser

DEFAULT_SETTINGS = {
    "profile_folder": "./keycash_profile",
    "game_url": "https://keycash.pro/?c=math",
    "pause_seconds": "1.0",
    "questions": "0",
    "allow_login": True,
    "emoji_answer_delay": "",
    "window_geometry": "820x680",
}

SETTINGS_FILENAME = "keycash_gui_settings.json"
COINS_PER_PESO = 100
MAX_ACCOUNTS = 4
TARGET_COINS = 500 * COINS_PER_PESO  # ₱500 = 50,000 coins


# ---------------------------------------------------------------------------
# Per-account tab
# ---------------------------------------------------------------------------

class AccountTab:
    """One tab = one account running in its own subprocess."""

    def __init__(self, notebook: ttk.Notebook, tab_index: int, settings_dir: Path):
        self.tab_index = tab_index
        self.settings_dir = settings_dir
        self.process: Optional[subprocess.Popen] = None
        self.reader_thread: Optional[threading.Thread] = None
        self.running = False
        self.start_time: Optional[float] = None
        self.timer_job: Optional[str] = None
        self.question_count = 0
        self._coins: Optional[int] = None
        self._gained: Optional[int] = None

        # Build the frame for this tab
        self.frame = ttk.Frame(notebook, padding=8)
        notebook.add(self.frame, text=f"Account {tab_index + 1}")

        self._build_ui()
        self._set_running(False)
        self._load_tab_settings(show_message=False)

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self):
        f = self.frame

        # Stats grid
        stats = ttk.LabelFrame(f, text="Session Stats", padding=8)
        stats.pack(fill=tk.X, pady=(0, 8))
        for col in (1, 3):
            stats.columnconfigure(col, weight=1)

        self.timer_var        = tk.StringVar(value="00:00:00")
        self.questions_var    = tk.StringVar(value="0")
        self.coins_var        = tk.StringVar(value="--")
        self.gained_var       = tk.StringVar(value="--")
        self.est_10m_var      = tk.StringVar(value="--")
        self.est_1h_var       = tk.StringVar(value="--")
        self.est_500p_var     = tk.StringVar(value="--")

        def stat(row, col, label, var):
            ttk.Label(stats, text=label).grid(row=row, column=col,   sticky=tk.W, padx=(0, 6), pady=3)
            ttk.Label(stats, textvariable=var, font=("Segoe UI", 10, "bold")).grid(
                row=row, column=col+1, sticky=tk.W, pady=3
            )

        stat(0, 0, "Timer",              self.timer_var)
        stat(0, 2, "Questions answered", self.questions_var)
        stat(1, 0, "Coins",              self.coins_var)
        stat(1, 2, "Coins gained",       self.gained_var)
        stat(2, 0, "Est. 10 min",        self.est_10m_var)
        stat(2, 2, "Est. 1 hr",          self.est_1h_var)
        stat(3, 0, "Est. time to ₱500",  self.est_500p_var)

        # Settings
        settings = ttk.LabelFrame(f, text="Settings", padding=8)
        settings.pack(fill=tk.X, pady=(0, 8))
        settings.columnconfigure(1, weight=1)

        self.profile_var    = tk.StringVar(value=f"./keycash_profile_{self.tab_index + 1}")
        self.url_var        = tk.StringVar(value=DEFAULT_SETTINGS["game_url"])
        self.pause_var      = tk.StringVar(value=DEFAULT_SETTINGS["pause_seconds"])
        self.iterations_var = tk.StringVar(value=DEFAULT_SETTINGS["questions"])
        self.login_var      = tk.BooleanVar(value=DEFAULT_SETTINGS["allow_login"])
        self.emoji_delay_var = tk.StringVar(value=DEFAULT_SETTINGS["emoji_answer_delay"])

        self._add_entry(settings, "Profile folder", self.profile_var, 0)
        self._add_entry(settings, "Game URL", self.url_var, 1)
        self._add_entry(settings, "Pause (seconds)", self.pause_var, 2)
        self._add_entry(settings, "Questions (0 = unlimited)", self.iterations_var, 3)
        self._add_entry(settings, "Emoji answer delay (s, blank = use Pause)", self.emoji_delay_var, 4)

        login_row = ttk.Frame(settings)
        login_row.grid(row=5, column=0, columnspan=2, sticky=tk.W, pady=(6, 0))
        ttk.Checkbutton(login_row, text="Allow login if needed", variable=self.login_var).pack(side=tk.LEFT)

        btn_row = ttk.Frame(settings)
        btn_row.grid(row=6, column=0, columnspan=2, sticky=tk.W, pady=(8, 0))
        ttk.Button(btn_row, text="Save Settings", command=self.save_settings, width=14).pack(side=tk.LEFT)
        ttk.Button(btn_row, text="Load Settings", command=self.load_settings, width=14).pack(
            side=tk.LEFT, padx=(6, 0)
        )

        # Controls
        controls = ttk.Frame(f)
        controls.pack(fill=tk.X, pady=(0, 8))

        self.start_btn = ttk.Button(controls, text="Start", command=self.start, width=12)
        self.start_btn.pack(side=tk.LEFT)
        self.stop_btn = ttk.Button(controls, text="Stop", command=self.stop, width=12)
        self.stop_btn.pack(side=tk.LEFT, padx=(6, 0))
        self.status_var = tk.StringVar(value="Idle")
        ttk.Label(controls, textvariable=self.status_var).pack(side=tk.LEFT, padx=(12, 0))

        # Log
        log_frame = ttk.LabelFrame(f, text="Log", padding=6)
        log_frame.pack(fill=tk.BOTH, expand=True)
        self.log_text = scrolledtext.ScrolledText(
            log_frame, wrap=tk.WORD, height=10, font=("Consolas", 9), state=tk.DISABLED
        )
        self.log_text.pack(fill=tk.BOTH, expand=True)

    def _add_entry(self, parent, label, variable, row):
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky=tk.W, pady=3)
        ttk.Entry(parent, textvariable=variable, width=50).grid(
            row=row, column=1, sticky=tk.EW, padx=(6, 0), pady=3
        )

    # ------------------------------------------------------------------
    # Settings persistence
    # ------------------------------------------------------------------

    @property
    def _settings_path(self) -> Path:
        return self.settings_dir / f"keycash_gui_acc{self.tab_index + 1}.json"

    def _collect(self) -> Dict[str, Any]:
        return {
            "profile_folder": self.profile_var.get().strip(),
            "game_url": self.url_var.get().strip(),
            "pause_seconds": self.pause_var.get().strip(),
            "questions": self.iterations_var.get().strip(),
            "allow_login": self.login_var.get(),
            "emoji_answer_delay": self.emoji_delay_var.get().strip(),
        }

    def _apply(self, data: Dict[str, Any]):
        self.profile_var.set(str(data.get("profile_folder", f"./keycash_profile_{self.tab_index + 1}")))
        self.url_var.set(str(data.get("game_url", DEFAULT_SETTINGS["game_url"])))
        self.pause_var.set(str(data.get("pause_seconds", DEFAULT_SETTINGS["pause_seconds"])))
        self.iterations_var.set(str(data.get("questions", DEFAULT_SETTINGS["questions"])))
        self.login_var.set(bool(data.get("allow_login", DEFAULT_SETTINGS["allow_login"])))
        self.emoji_delay_var.set(str(data.get("emoji_answer_delay", DEFAULT_SETTINGS["emoji_answer_delay"])))

    def save_settings(self, show_message: bool = True):
        try:
            self._settings_path.write_text(json.dumps(self._collect(), indent=2), encoding="utf-8")
            if show_message:
                messagebox.showinfo("Saved", f"Settings saved to:\n{self._settings_path}")
        except OSError as exc:
            if show_message:
                messagebox.showerror("Save failed", str(exc))

    def load_settings(self, show_message: bool = True):
        self._load_tab_settings(show_message=show_message)

    def _load_tab_settings(self, show_message: bool = True):
        path = self._settings_path
        if not path.exists():
            # Fall back to the legacy single-account settings file
            legacy = self.settings_dir / SETTINGS_FILENAME
            if legacy.exists():
                path = legacy
            else:
                return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            self._apply(data)
            if show_message:
                messagebox.showinfo("Loaded", f"Settings loaded from:\n{path}")
        except Exception as exc:
            if show_message:
                messagebox.showerror("Load failed", str(exc))

    # ------------------------------------------------------------------
    # Automation control (subprocess-based)
    # ------------------------------------------------------------------

    def _build_cmd(self) -> List[str]:
        """Build the command line to launch keycash_auto.py as a subprocess."""
        try:
            pause = float(self.pause_var.get().strip())
            iterations = int(self.iterations_var.get().strip())
        except ValueError as exc:
            raise ValueError(f"Invalid settings: {exc}")

        emoji_delay_raw = self.emoji_delay_var.get().strip()
        emoji_delay: Optional[float] = None
        if emoji_delay_raw:
            try:
                emoji_delay = float(emoji_delay_raw)
            except ValueError:
                raise ValueError("Emoji answer delay must be a number or blank.")

        profile = self.profile_var.get().strip()
        url = self.url_var.get().strip()
        if not profile:
            raise ValueError("Profile folder cannot be empty.")
        if not url:
            raise ValueError("Game URL cannot be empty.")

        cmd = [
            sys.executable,
            str(Path(__file__).resolve().parent / "keycash_auto.py"),
            "--url", url,
            "--user-data-dir", profile,
            "--pause", str(pause),
            "--iterations", str(iterations),
            "--close",
        ]
        if self.login_var.get():
            cmd.append("--login")
        if emoji_delay is not None:
            cmd += ["--emoji-answer-delay", str(emoji_delay)]

        return cmd

    def start(self):
        if self.running:
            return
        try:
            cmd = self._build_cmd()
        except ValueError as exc:
            messagebox.showerror("Invalid settings", str(exc))
            return

        self.save_settings(show_message=False)
        self.question_count = 0
        self._coins = None
        self._gained = None
        self.questions_var.set("0")
        self.coins_var.set("--")
        self.gained_var.set("--")
        self.est_10m_var.set("--")
        self.est_1h_var.set("--")
        self.est_500p_var.set("--")
        self._set_running(True)
        self._append_log(f"\n--- Starting: {' '.join(cmd)} ---\n")

        try:
            env = os.environ.copy()
            env["PYTHONIOENCODING"] = "utf-8"
            self.process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                env=env,
            )
        except Exception as exc:
            self._append_log(f"\n--- Failed to start process: {exc} ---\n")
            self._set_running(False)
            return

        self.reader_thread = threading.Thread(target=self._read_output, daemon=True)
        self.reader_thread.start()

    def stop(self):
        if not self.running:
            return
        self.status_var.set("Stopping...")
        self._append_log("\n--- Stop requested ---\n")
        if self.process and self.process.poll() is None:
            try:
                self.process.terminate()
            except Exception:
                pass

    def _read_output(self):
        """Read subprocess stdout in a background thread, parse [STATUS] lines."""
        try:
            for line in self.process.stdout:
                # Parse structured status line emitted by keycash_auto.py
                if line.startswith("[STATUS]"):
                    self._parse_status_line(line)
                else:
                    self._append_log(line)
        except Exception:
            pass
        finally:
            self.frame.after(0, self._on_process_done)

    def _parse_status_line(self, line: str):
        """Parse: [STATUS] coins=12345 gained=100 questions=5"""
        coins_m   = re.search(r"coins=(\d+)", line)
        gained_m  = re.search(r"gained=(-?\d+)", line)
        quest_m   = re.search(r"questions=(\d+)", line)

        if coins_m:
            self._coins = int(coins_m.group(1))
        if gained_m:
            self._gained = int(gained_m.group(1))
        if quest_m:
            self.question_count = int(quest_m.group(1))

        self.frame.after(0, self._refresh_stats)

    def _fmt_elapsed(self, seconds: int) -> str:
        h, r = divmod(seconds, 3600)
        m, s = divmod(r, 60)
        return f"{h:02d}:{m:02d}:{s:02d}"

    def _fmt_coins(self, coins: int, sign: str = "") -> str:
        peso = coins / COINS_PER_PESO
        return f"{sign}{coins:,} (₱{peso:,.2f})"

    def _refresh_stats(self):
        if self._coins is not None:
            self.coins_var.set(self._fmt_coins(self._coins))
        if self._gained is not None:
            sign = "+" if self._gained > 0 else ("-" if self._gained < 0 else "")
            self.gained_var.set(self._fmt_coins(abs(self._gained), sign=sign))
        self.questions_var.set(str(self.question_count))
        self._refresh_estimates()

    def _refresh_estimates(self):
        if self.start_time is None or self._gained is None or self._gained <= 0:
            self.est_10m_var.set("--")
            self.est_1h_var.set("--")
            self.est_500p_var.set("--")
            return

        elapsed = time.time() - self.start_time
        if elapsed < 5:
            self.est_10m_var.set("--")
            self.est_1h_var.set("--")
            self.est_500p_var.set("--")
            return

        rate = self._gained / elapsed          # coins per second
        est_10m = int(round(rate * 600))
        est_1h  = int(round(rate * 3600))
        self.est_10m_var.set(self._fmt_coins(est_10m))
        self.est_1h_var.set(self._fmt_coins(est_1h))

        remaining = TARGET_COINS - self._gained
        if remaining <= 0:
            self.est_500p_var.set("Reached ₱500!")
        else:
            secs = remaining / rate
            self.est_500p_var.set(self._fmt_elapsed(int(secs)))

    def _on_process_done(self):
        rc = self.process.returncode if self.process else None
        self._append_log(f"\n--- Process exited (code {rc}) ---\n")
        self._set_running(False)

    # ------------------------------------------------------------------
    # UI helpers
    # ------------------------------------------------------------------

    def _set_running(self, running: bool):
        self.running = running
        self.start_btn.configure(state=tk.DISABLED if running else tk.NORMAL)
        self.stop_btn.configure(state=tk.NORMAL if running else tk.DISABLED)
        self.status_var.set("Running" if running else "Idle")
        if running:
            self._start_timer()
        else:
            self._stop_timer()

    def _append_log(self, text: str):
        def update():
            self.log_text.configure(state=tk.NORMAL)
            self.log_text.insert(tk.END, text)
            self.log_text.see(tk.END)
            self.log_text.configure(state=tk.DISABLED)
        self.frame.after(0, update)

    def _start_timer(self):
        self.start_time = time.time()
        self._tick_timer()

    def _tick_timer(self):
        if not self.running or self.start_time is None:
            return
        elapsed = int(time.time() - self.start_time)
        h, r = divmod(elapsed, 3600)
        m, s = divmod(r, 60)
        self.timer_var.set(f"{h:02d}:{m:02d}:{s:02d}")
        self.timer_job = self.frame.after(1000, self._tick_timer)

    def _stop_timer(self):
        if self.timer_job is not None:
            try:
                self.frame.after_cancel(self.timer_job)
            except Exception:
                pass
            self.timer_job = None

    def on_close(self):
        self.stop()
        self.save_settings(show_message=False)


# ---------------------------------------------------------------------------
# Main app window
# ---------------------------------------------------------------------------

class KeycashControlApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("Keycash Math Auto — Multi-Account")
        self.root.geometry("820x640")
        self.root.minsize(640, 500)

        self.settings_dir = Path(__file__).resolve().parent
        self.tabs: List[AccountTab] = []

        self._build_ui()
        self._load_window_geometry()

    def _build_ui(self):
        top = ttk.Frame(self.root, padding=(10, 8, 10, 4))
        top.pack(fill=tk.X)

        ttk.Label(top, text="Keycash Math Automation", font=("Segoe UI", 14, "bold")).pack(side=tk.LEFT)

        btn_frame = ttk.Frame(top)
        btn_frame.pack(side=tk.RIGHT)
        ttk.Button(btn_frame, text="Start All", command=self.start_all, width=10).pack(side=tk.LEFT)
        ttk.Button(btn_frame, text="Stop All", command=self.stop_all, width=10).pack(side=tk.LEFT, padx=(6, 0))
        ttk.Button(btn_frame, text="+ Add Account", command=self.add_account, width=14).pack(
            side=tk.LEFT, padx=(6, 0)
        )

        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 10))

        # Start with 2 account tabs by default
        for i in range(2):
            self._add_tab(i)

    def _add_tab(self, index: int):
        tab = AccountTab(self.notebook, index, self.settings_dir)
        self.tabs.append(tab)

    def add_account(self):
        if len(self.tabs) >= MAX_ACCOUNTS:
            messagebox.showinfo("Limit reached", f"Maximum {MAX_ACCOUNTS} accounts supported.")
            return
        self._add_tab(len(self.tabs))

    def start_all(self):
        for tab in self.tabs:
            if not tab.running:
                tab.start()

    def stop_all(self):
        for tab in self.tabs:
            if tab.running:
                tab.stop()

    def _load_window_geometry(self):
        # Try to restore window size from any existing settings file
        legacy = self.settings_dir / SETTINGS_FILENAME
        if legacy.exists():
            try:
                data = json.loads(legacy.read_text(encoding="utf-8"))
                geometry = data.get("window_geometry")
                if isinstance(geometry, str) and "x" in geometry:
                    self.root.geometry(geometry)
            except Exception:
                pass

    def on_close(self):
        for tab in self.tabs:
            tab.on_close()
        # Save window geometry to first account settings
        if self.tabs:
            path = self.settings_dir / f"keycash_gui_acc1.json"
            try:
                data = {}
                if path.exists():
                    data = json.loads(path.read_text(encoding="utf-8"))
                data["window_geometry"] = self.root.geometry()
                path.write_text(json.dumps(data, indent=2), encoding="utf-8")
            except Exception:
                pass
        self.root.destroy()


def main():
    root = tk.Tk()
    app = KeycashControlApp(root)
    root.protocol("WM_DELETE_WINDOW", app.on_close)
    root.mainloop()


if __name__ == "__main__":
    main()
