import logging
import threading
import time
from gi.repository import Gtk, GLib

logger = logging.getLogger(__name__)

try:
    import psutil
except Exception:
    psutil = None

try:
    import GPUtil
except Exception:
    GPUtil = None

class DebugConsoleWindow(Gtk.Window):
    def __init__(self):
        super().__init__(title="Lada Debug Console")
        self.set_default_size(800, 400)

        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.set_child(vbox)

        self.stats_label = Gtk.Label(label="Stats: initializing...")
        self.stats_label.set_halign(Gtk.Align.START)
        vbox.append(self.stats_label)

        scrolled = Gtk.ScrolledWindow()
        vbox.append(scrolled)

        self.textview = Gtk.TextView()
        self.textview.set_editable(False)
        scrolled.set_child(self.textview)

        self._stop = False
        self._thread = threading.Thread(target=self._poll_stats, daemon=True)
        self._thread.start()
        # attach logging handler
        self._log_handler = DebugLogHandler(self)
        logging.getLogger().addHandler(self._log_handler)

    def append_text(self, text: str):
        GLib.idle_add(self._append_idle, text)

    def _append_idle(self, text: str):
        buffer = self.textview.get_buffer()
        buffer.insert(buffer.get_end_iter(), text + "\n")

    def _poll_stats(self):
        while not self._stop:
            stats = []
            try:
                if psutil:
                    cpu = psutil.cpu_percent(interval=None)
                    ram = psutil.virtual_memory().percent
                    stats.append(f"CPU: {cpu:.1f}%")
                    stats.append(f"RAM: {ram:.1f}%")
                if GPUtil:
                    gpus = GPUtil.getGPUs()
                    if gpus:
                        gpu = gpus[0]
                        stats.append(f"GPU: {gpu.load*100:.1f}%")
                        stats.append(f"VRAM: {gpu.memoryUtil*100:.1f}%")
            except Exception as e:
                stats.append(f"stats-error: {e}")
            stats_text = " | ".join(stats) if stats else "stats-unavailable"
            GLib.idle_add(self.stats_label.set_text, stats_text)
            time.sleep(1)

    def present(self):
        try:
            super().present()
        except Exception:
            pass

    def close(self):
        self._stop = True
        try:
            logging.getLogger().removeHandler(self._log_handler)
        except Exception:
            pass
        try:
            super().close()
        except Exception:
            pass


class DebugLogHandler(logging.Handler):
    def __init__(self, window: DebugConsoleWindow):
        super().__init__()
        self.window = window

    def emit(self, record):
        try:
            msg = self.format(record)
            self.window.append_text(msg)
        except Exception:
            pass
