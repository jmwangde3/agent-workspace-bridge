# -*- coding: utf-8 -*-
"""Double-click launcher: pythonw opens the console without a terminal window."""
import os
import runpy

runpy.run_path(os.path.join(os.path.dirname(os.path.abspath(__file__)), "awb_gui.py"),
               run_name="__main__")
