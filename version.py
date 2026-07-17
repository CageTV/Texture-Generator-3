"""
Single source of truth for TG3's version number. Both texture_generator.py
(Tkinter) and main_gl.py (the GL shell) import this so the number only
ever needs to change in one place.

Bump this on every change from here on: 1.1.6 -> 1.1.7 -> ...
"""
VERSION = "1.1.12"
