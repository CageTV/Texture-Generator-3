"""
Generic worker-thread runner for pbr_engine.py-style functions - every one
of them matches the same shape:

    fn(*args, log=callback, progress=callback, cancelled=callback, **kwargs) -> result

That's the exact contract the old Tkinter app's `_run_pbr_op` drove on a
background thread. `ThreadedJob` does the same thing here, decoupled from
Tkinter: start a callable on a worker thread, and let gui() drain whatever
it's reported so far once per frame via poll() (never blocking the render
loop). Every remaining PBR Generator tab (PBR Builder, Parallax Generator,
Complex<->PBR) can reuse this exact class - it's not specific to the JSON
Builder plugin that introduces it.
"""
import queue
import threading


class ThreadedJob:
    def __init__(self):
        self._queue = queue.Queue()
        self.thread = None
        self.running = False
        self.done = False
        self.ok = None
        self.result = None
        self.error = None
        self.progress_done = 0
        self.progress_total = 0
        self.lines = []  # list of (text, level_or_None)
        self._cancel_flag = threading.Event()

    def start(self, fn, *args, **kwargs):
        if self.running:
            return
        self.lines.clear()
        self.progress_done = 0
        self.progress_total = 0
        self.done = False
        self.ok = None
        self.result = None
        self.error = None
        self._cancel_flag.clear()
        self.running = True

        def _log(msg, level=None):
            self._queue.put(("log", msg, level))

        def _progress(done, total):
            self._queue.put(("progress", done, total))

        def _cancelled():
            return self._cancel_flag.is_set()

        def run():
            try:
                result = fn(*args, log=_log, progress=_progress,
                            cancelled=_cancelled, **kwargs)
                self._queue.put(("done", True, result))
            except Exception as e:
                self._queue.put(("done", False, e))

        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()

    def cancel(self):
        self._cancel_flag.set()

    def poll(self):
        """Call once per frame. Drains whatever the worker has queued
        since the last call; never blocks."""
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                break
            kind = item[0]
            if kind == "log":
                self.lines.append((item[1], item[2]))
            elif kind == "progress":
                self.progress_done, self.progress_total = item[1], item[2]
            elif kind == "done":
                self.running = False
                self.done = True
                self.ok = item[1]
                if self.ok:
                    self.result = item[2]
                else:
                    self.error = item[2]

    @property
    def fraction(self):
        return (self.progress_done / self.progress_total) if self.progress_total else 0.0
