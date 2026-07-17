"""
Streams a subprocess's stdout line-by-line on a worker thread, without
blocking the render loop - the external-process counterpart to
job_runner.ThreadedJob. Used here for the PBR JSON Builder's PowerShell
side (Step1.ps1 / Step2.ps1), and it's the same shape any future
external-process bridge will want (Upscayl's CLI, Blender's
`--background --python`) - reuse this rather than writing another
subprocess+queue pattern from scratch.

Recognizes the same "__SKYKING_TOTAL__=" / "__SKYKING_PROGRESS__=n/total"
markers the PowerShell scripts already print, matching the old Tkinter
app's `_run_ps_script` parsing exactly.
"""
import os
import queue
import subprocess
import threading


class StreamingProcess:
    def __init__(self):
        self._queue = queue.Queue()
        self.proc = None
        self.running = False
        self.done = False
        self.returncode = None
        self.lines = []
        self.progress_done = 0
        self.progress_total = 0

    def start(self, cmd, cwd=None):
        if self.running:
            return
        self.lines.clear()
        self.progress_done = 0
        self.progress_total = 0
        self.done = False
        self.returncode = None
        self.running = True

        def run():
            try:
                creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
                self.proc = subprocess.Popen(
                    cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, creationflags=creationflags)
                for raw in self.proc.stdout:
                    line = raw.rstrip("\n")
                    if not line:
                        continue
                    if line.startswith("__SKYKING_TOTAL__="):
                        continue
                    if line.startswith("__SKYKING_PROGRESS__="):
                        try:
                            done, total = line.split("=", 1)[1].split("/")
                            self._queue.put(("progress", int(done), int(total)))
                        except ValueError:
                            pass
                        continue
                    level = ("error" if line.startswith(("[ERROR]", "ERROR")) else
                             "warn" if line.startswith("[WARN]") else
                             "success" if line.startswith("[DONE]") else None)
                    self._queue.put(("log", line, level))
                self.proc.wait()
                self._queue.put(("done", self.proc.returncode))
            except Exception as e:
                self._queue.put(("log", f"Error: {e}", "error"))
                self._queue.put(("done", -1))

        threading.Thread(target=run, daemon=True).start()

    def poll(self):
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
                self.returncode = item[1]

    @property
    def fraction(self):
        return (self.progress_done / self.progress_total) if self.progress_total else 0.0

    @property
    def ok(self):
        return self.returncode == 0
