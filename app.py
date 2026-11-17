"""WeatherDial: one see-through weather panel on the Windows desktop.

Serves the panel and its data on http://127.0.0.1:8791 and opens the panel in a
frameless, see-through window that sits behind your other windows, never takes
focus (safe while gaming), has no taskbar button and keeps its spot per monitor.

  python app.py            open the panel (does nothing if it's already open)
  python app.py --server   data + panel for a normal browser only:
                           http://127.0.0.1:8791/panel.html?preview
"""
import ctypes
import ctypes.wintypes
import json
import os
import subprocess
import sys
import threading
import time
import traceback
import webbrowser
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import weather

PORT = 8791
HERE = Path(__file__).resolve().parent
POSITIONS_FILE = HERE / "positions.json"   # written by the app: where the panel sits
LATEST = weather.DATA / "latest.json"
REFRESH_S = 15          # how often to re-read your station; other sources keep their own pace
APP = None              # the running Api (set in run_window)
fetch_lock = threading.Lock()


def note(*msg):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), *msg, flush=True)


# ---------------------------------------------------------------- data

def refresh(force=False):
    with fetch_lock:
        try:
            weather.fetch(force=force)
        except Exception:
            note("refresh failed:\n" + traceback.format_exc())


def refresh_loop():
    while True:
        refresh()
        time.sleep(REFRESH_S)


def tempest_listener(port=50222):
    """Listen for the Tempest hub's broadcast on the home network (no internet needed):
    a full observation every minute, wind every 3 seconds, lightning strikes as they happen.
    Saves the latest of each, today's rain total and the last 3 hours of strikes to
    data/tempest_udp.json for weather.py. The first time, Windows may ask whether Python
    may use the network: allow it on private networks."""
    import socket
    out = weather.UDP_FILE
    state = weather.load_json(out, {}) or {}
    state.pop("error", None)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind(("", port))
    except OSError as e:
        note(f"Tempest listener: can't use UDP port {port} ({e}); another program "
             "(e.g. Weather Display) is probably listening to the hub already")
        weather.save_json(out, {"error": f"UDP port {port} is in use by another program (e.g. Weather Display); "
                                         "set weather_display_dir in config.json if that program is Weather Display"})
        return
    note(f"Tempest listener: waiting for the hub on UDP port {port}")
    last_save = 0
    while True:
        try:
            data, _ = s.recvfrom(4096)
            m = json.loads(data)
        except (OSError, ValueError):
            continue
        kind, now = m.get("type"), time.time()
        if kind == "obs_st" and m.get("obs"):
            ob = m["obs"][-1]
            state["obs"], state["serial"] = ob, m.get("serial_number")
            day = time.strftime("%Y-%m-%d", time.localtime(ob[0]))
            rd = state.get("rain_day") or {}
            if rd.get("date") != day:
                rd = {"date": day, "mm": 0.0}
            if rd.get("last") != ob[0]:          # each minute's rain counted once
                rd["mm"] = round(rd["mm"] + (ob[12] if len(ob) > 12 and ob[12] else 0), 3)
                rd["last"] = ob[0]
            state["rain_day"] = rd
        elif kind == "rapid_wind" and m.get("ob"):
            state["wind"] = m["ob"]
        elif kind == "evt_strike" and m.get("evt"):
            state["strikes"] = [x for x in state.get("strikes") or [] if now - x[0] < 3 * 3600] + [m["evt"]]
        else:
            continue
        state["heard"] = now
        if now - last_save >= 3 or kind != "rapid_wind":
            weather.save_json(out, state)
            last_save = now


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(HERE), **kwargs)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/data/latest.json":
            if "force=1" in self.path:
                refresh(force=True)
            elif not LATEST.exists():
                refresh()
            body = LATEST.read_bytes() if LATEST.exists() else b"{}"
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)
        elif path.endswith((".html", ".js", ".css", ".png", ".ico")) and "/data/" not in path:
            super().do_GET()
        else:   # never serve keys.json, config.json or the cache
            self.send_error(404)

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def log_message(self, *args):
        pass


class Server(ThreadingHTTPServer):
    allow_reuse_address = False   # a second copy fails to start instead of sharing the port


# ---------------------------------------------------------------- window helpers (as in PCDials)

user32 = ctypes.windll.user32
GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW, WS_EX_NOACTIVATE, WS_EX_APPWINDOW = 0x80, 0x8000000, 0x40000
HWND_BOTTOM = 1
SWP_NOSIZE, SWP_NOMOVE, SWP_NOACTIVATE = 0x1, 0x2, 0x10
MONITOR_DEFAULTTONULL = 0
SW_HIDE, SW_SHOWNOACTIVATE = 0, 4
RECT = ctypes.wintypes.RECT
user32.GetWindowLongW.restype = ctypes.c_long


def load_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def hwnd_of(window):
    return window.native.Handle.ToInt64()


def no_background(window):
    """Never paint a window background behind the panel (otherwise a resize or a move
    to another monitor turns the see-through area grey)."""
    from System import Action, Array, Object
    from System.Reflection import BindingFlags
    from System.Windows.Forms import ControlStyles
    form = window.native
    styles = ControlStyles.Opaque | ControlStyles.AllPaintingInWmPaint | ControlStyles.UserPaint

    def apply():
        stack = [form]
        while stack:
            c = stack.pop()
            c.GetType().GetMethod("SetStyle", BindingFlags.NonPublic | BindingFlags.Instance) \
                .Invoke(c, Array[Object]([styles, True]))
            stack.extend(list(c.Controls))
    form.Invoke(Action(apply))


def to_desktop_layer(window, size=None):
    """No taskbar button / Alt+Tab, never takes focus, sits behind normal windows.
    size=(w, h) in page pixels also sets the window size."""
    hwnd = hwnd_of(window)
    style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
    user32.SetWindowLongW(hwnd, GWL_EXSTYLE,
                          (style | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE) & ~WS_EX_APPWINDOW)
    flags = SWP_NOMOVE | SWP_NOACTIVATE
    w = h = 0
    if size:
        scale = user32.GetDpiForWindow(hwnd) / 96
        w, h = round(size[0] * scale), round(size[1] * scale)
    else:
        flags |= SWP_NOSIZE
    user32.SetWindowPos(hwnd, HWND_BOTTOM, 0, 0, w, h, flags)


def on_a_monitor(pos, w, h):
    if "x" not in pos:
        return True
    r = RECT(pos["x"], pos["y"], pos["x"] + w, pos["y"] + h)
    return bool(user32.MonitorFromRect(ctypes.byref(r), MONITOR_DEFAULTTONULL))


# ---------------------------------------------------------------- the panel window

class Api:
    """Functions the panel page can call."""

    def __init__(self):
        self._window = None
        self._tray = None
        self._pos = load_json(POSITIONS_FILE, {})
        self._dirty = False
        self._dragging = False
        self._hidden = False
        self._size = (502, 640)

    # -- saving
    def _save_loop(self):
        while True:
            time.sleep(1)
            if self._dirty:
                self._dirty = False
                POSITIONS_FILE.write_text(json.dumps(self._pos, indent=2), encoding="utf-8")

    def _set(self, **kw):
        self._pos.update(kw)
        self._dirty = True

    # -- called from the page
    def settings(self):
        return {"locked": self._pos.get("locked", False), "compact": self._pos.get("compact", False),
                "zoom": self._pos.get("zoom", 1), "tab": self._pos.get("tab", "hourly"),
                "tabh": self._pos.get("tabh"), "pw": self._pos.get("pw"),
                "theme": self._pos.get("theme", "dark"), "acc_days": self._pos.get("acc_days", 30),
                "weighted": self._pos.get("weighted", True), "gstyle": self._pos.get("gstyle", "classic"),
                "rpaused": self._pos.get("rpaused", False)}

    def remember(self, key, value):
        if key in ("compact", "zoom", "tab", "tabh", "pw", "theme", "acc_days", "weighted", "gstyle", "rpaused"):
            self._set(**{key: value})

    def toggle_lock(self):
        self._set(locked=not self._pos.get("locked", False))
        return self._pos["locked"]

    def fit(self, w, h):
        """The page tells us its size (page pixels); make the window match."""
        size = (max(200, round(w)), max(100, round(h)))
        if size != self._size:
            self._size = size
            to_desktop_layer(self._window, size)

    def dragging(self, on):
        self._dragging = on
        if not on:
            r = RECT()
            user32.GetWindowRect(hwnd_of(self._window), ctypes.byref(r))
            self._set(x=r.left, y=r.top)   # real screen pixels: right spot on any DPI

    def open_url(self, url):
        if isinstance(url, str) and url.startswith("https://"):
            webbrowser.open(url)

    def edit_keys(self):
        subprocess.Popen(["notepad.exe", str(HERE / "keys.json")])

    def edit_location(self):
        subprocess.Popen(["notepad.exe", str(HERE / "config.json")])

    def open_folder(self):
        os.startfile(HERE)

    def refresh_now(self):
        threading.Thread(target=lambda: (refresh(force=True), self.reload()), daemon=True).start()

    def reload(self):
        if self._window:
            self._window.evaluate_js("load()")

    def set_locked_all(self, locked):
        self._set(locked=locked)
        self._window.evaluate_js(f"setLocked({'true' if locked else 'false'})")

    def startup_enabled(self):
        return startup_link().exists()

    def toggle_startup(self):
        link = startup_link()
        if link.exists():
            link.unlink()
        else:
            make_startup_link(link)
        return link.exists()

    def quit(self):
        if self._tray:
            self._tray.Visible = False
        if self._window:
            self._window.destroy()

    # -- placing
    def _place(self):
        hwnd = hwnd_of(self._window)
        r = RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(r))
        if not on_a_monitor(self._pos, r.right - r.left, r.bottom - r.top):
            if not self._hidden:
                user32.ShowWindow(hwnd, SW_HIDE)
                self._hidden = True
            return
        if "x" in self._pos and (r.left, r.top) != (self._pos["x"], self._pos["y"]):
            user32.SetWindowPos(hwnd, HWND_BOTTOM, self._pos["x"], self._pos["y"], 0, 0,
                                SWP_NOSIZE | SWP_NOACTIVATE)
        if self._hidden:
            self._hidden = False
            user32.ShowWindow(hwnd, SW_SHOWNOACTIVATE)

    def _watch(self):
        """Every 2 s: stay behind other windows; hide while its monitor is off."""
        while True:
            time.sleep(2)
            if self._dragging or not self._window:
                continue
            try:
                to_desktop_layer(self._window)
                self._place()
            except Exception:
                pass   # window closing


def startup_link():
    return Path(os.environ["APPDATA"]) / r"Microsoft\Windows\Start Menu\Programs\Startup\WeatherDial.lnk"


def make_startup_link(link):
    pyw = Path(sys.executable).with_name("pythonw.exe")
    ps = (f"$s=(New-Object -ComObject WScript.Shell).CreateShortcut('{link}');"
          f"$s.TargetPath='{pyw}';$s.Arguments='\"{HERE / 'app.py'}\"';"
          f"$s.WorkingDirectory='{HERE}';$s.Save()")
    subprocess.run(["powershell", "-NoProfile", "-Command", ps], creationflags=0x08000000)


def tray_icon(api):
    """Tray icon (the panel has no taskbar button) with a menu."""
    import System.Drawing.Drawing2D as Drawing2D
    from System import Action, EventHandler
    from System.Drawing import Bitmap, Color, Graphics, Icon, Pen, SolidBrush
    from System.Windows.Forms import ContextMenuStrip, NotifyIcon, ToolStripMenuItem, ToolStripSeparator

    def draw():   # a small sun behind a cloud
        bmp = Bitmap(32, 32)
        g = Graphics.FromImage(bmp)
        g.SmoothingMode = Drawing2D.SmoothingMode.AntiAlias
        g.FillEllipse(SolidBrush(Color.FromArgb(255, 201, 74)), 12, 2, 18, 18)
        cloud = SolidBrush(Color.FromArgb(225, 230, 238))
        g.FillEllipse(cloud, 2, 14, 14, 14)
        g.FillEllipse(cloud, 9, 9, 16, 16)
        g.FillEllipse(cloud, 17, 15, 13, 13)
        g.FillRectangle(cloud, 9, 21, 15, 8)
        g.Dispose()
        return Icon.FromHandle(bmp.GetHicon())

    def later(fn):
        return EventHandler(lambda s, e: threading.Thread(target=fn, daemon=True).start())

    def create():
        menu = ContextMenuStrip()
        for text, fn in [("Refresh now", api.refresh_now),
                         ("Lock position", lambda: api.set_locked_all(True)),
                         ("Unlock position", lambda: api.set_locked_all(False)),
                         (None, None),
                         ("Edit API keys…", api.edit_keys),
                         ("Edit location…", api.edit_location),
                         ("Open WeatherDial folder", api.open_folder),
                         (None, None)]:
            if text is None:
                menu.Items.Add(ToolStripSeparator())
            else:
                menu.Items.Add(text).Click += later(fn)
        start = ToolStripMenuItem("Start with Windows")
        start.Checked = api.startup_enabled()

        def toggle(s, e):
            start.Checked = api.toggle_startup()
        start.Click += EventHandler(toggle)
        menu.Items.Add(start)
        menu.Items.Add(ToolStripSeparator())
        menu.Items.Add("Close WeatherDial").Click += later(api.quit)
        tray = NotifyIcon()
        tray.Icon = draw()
        tray.Text = "WeatherDial"
        tray.ContextMenuStrip = menu
        tray.Visible = True
        api._tray = tray

    api._window.native.Invoke(Action(create))


def run_window():
    import webview
    global APP
    api = APP = Api()
    w = webview.create_window(
        "WeatherDial", f"http://127.0.0.1:{PORT}/panel.html", js_api=api,
        width=api._size[0], height=api._size[1], x=60, y=60,
        frameless=True, easy_drag=False, transparent=True, shadow=False,
        resizable=False, focus=False)
    api._window = w

    def on_shown():
        no_background(w)
        to_desktop_layer(w, api._size)
        api._place()
        tray_icon(api)
        threading.Thread(target=api._watch, daemon=True).start()

    w.events.shown += on_shown
    threading.Thread(target=api._save_loop, daemon=True).start()
    webview.start()
    if api._dirty:
        POSITIONS_FILE.write_text(json.dumps(api._pos, indent=2), encoding="utf-8")


def log_to_file():
    """With no console (pythonw) write messages and errors to weatherdial.log."""
    log = HERE / "weatherdial.log"
    if log.exists() and log.stat().st_size > 1_000_000:
        log.replace(HERE / "weatherdial.old.log")
    sys.stdout = sys.stderr = open(log, "a", encoding="utf-8", buffering=1)
    threading.excepthook = lambda a: note("error in", a.thread.name,
                                          "".join(traceback.format_exception(a.exc_value)))


if __name__ == "__main__":
    if sys.stdout is None:
        log_to_file()
    try:
        server = Server(("127.0.0.1", PORT), Handler)
    except OSError:
        note("Not started: WeatherDial is already running.")
        sys.exit(0)
    added = weather.complete_config(HERE / "config.json")   # show every setting in config.json
    if added:
        note("config.json: added settings with their defaults:", ", ".join(added))
    added = weather.complete_keys(HERE / "keys.json")        # and every key name in keys.json
    if added:
        note("keys.json: added empty entries for:", ", ".join(added))
    cfg = weather.load_json(HERE / "config.json", {}) or {}
    if not cfg.get("weather_display_dir") and cfg.get("tempest_udp", True):
        threading.Thread(target=tempest_listener, args=(int(cfg.get("tempest_udp_port", 50222)),),
                         daemon=True).start()
    threading.Thread(target=refresh_loop, daemon=True).start()
    if "--server" in sys.argv:
        note(f"Serving http://127.0.0.1:{PORT}/panel.html?preview")
        server.serve_forever()
    else:
        note("WeatherDial started.")
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            run_window()
        except Exception:
            note("WeatherDial stopped by an error:\n" + traceback.format_exc())
            raise
        note("WeatherDial closed.")
        server.shutdown()
