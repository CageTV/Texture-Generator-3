"""
Native folder/file pickers for the ImGui shell.

Dear ImGui doesn't ship a native file dialog, and pulling in a whole extra
dependency (tkinter.filedialog does the exact same OS dialog Tkinter's
version already used) just for this would be redundant - Tkinter already
ships with Python and the rest of TG3 already depends on it, so a single
hidden Tk root here is the cheapest correct answer.
"""
import tkinter as tk
from tkinter import filedialog

_root = None


def _get_root():
    global _root
    if _root is None:
        _root = tk.Tk()
        _root.withdraw()
    return _root


def pick_folder(initial=None):
    r = _get_root()
    r.attributes("-topmost", True)
    d = filedialog.askdirectory(initialdir=initial or ".", parent=r)
    return d or None


def pick_file(initial=None, filetypes=None):
    r = _get_root()
    r.attributes("-topmost", True)
    f = filedialog.askopenfilename(initialdir=initial or ".", parent=r,
                                    filetypes=filetypes or [("All files", "*.*")])
    return f or None


def save_file(initial=None, default_ext=".png", filetypes=None):
    r = _get_root()
    r.attributes("-topmost", True)
    f = filedialog.asksaveasfilename(initialdir=initial or ".", parent=r,
                                      defaultextension=default_ext,
                                      filetypes=filetypes or [("All files", "*.*")])
    return f or None
