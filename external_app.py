"""
Launches an external GUI application to edit a single file, then detects
when that app closes and whether the file changed on disk - the "round-
trip external editor" pattern (the same shape Photoshop/GIMP round-trip
integrations use in game engines and DCC tools).

This is deliberately NOT app/proc_runner.py's StreamingProcess: Pinta (and
most GUI editors) don't print anything useful to stdout and aren't meant
to be waited on - the user is actively working in them. All this needs to
know is "did it close, and did the file change while it was open", polled
cheaply once per frame.
"""
import os
import subprocess


class ExternalEditSession:
    def __init__(self):
        self.proc = None
        self.running = False
        self.done = False
        self.file_path = None
        self.file_changed = False
        self.error = None
        self._start_mtime = None

    def start(self, exe_path, file_path):
        if self.running:
            return
        self.file_path = file_path
        self.done = False
        self.file_changed = False
        self.error = None
        try:
            self._start_mtime = os.path.getmtime(file_path) if os.path.isfile(file_path) else None
            self.proc = subprocess.Popen([exe_path, file_path])
            self.running = True
        except Exception as e:
            self.error = f"{type(e).__name__}: {e}"
            self.done = True

    def poll(self):
        """Call once per frame - just checks whether the process exited,
        nothing more expensive than that."""
        if not self.running or self.proc is None:
            return
        if self.proc.poll() is not None:
            self.running = False
            self.done = True
            try:
                new_mtime = os.path.getmtime(self.file_path) if os.path.isfile(self.file_path) else None
                self.file_changed = new_mtime is not None and new_mtime != self._start_mtime
            except OSError:
                self.file_changed = False
