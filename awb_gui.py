"""AWB console for local session continuity and shared workspace memory."""
import os
import sys
import json
import queue
import shutil
import ctypes
import logging
import logging.handlers
import datetime
import threading
import subprocess
import traceback
from pathlib import Path

FROZEN = getattr(sys, "frozen", False)
BASE = os.path.dirname(sys.executable) if FROZEN else os.path.dirname(os.path.abspath(__file__))
REPO = os.environ.get("AWB_ROOT", BASE)
if REPO not in sys.path:
    sys.path.insert(0, REPO)

import tkinter as tk
from tkinter import ttk, messagebox, filedialog, font as tkfont

import awb_config as C
from awb_session_panel import SessionPanel

VERSION = "1.8.0-rc.1"
APP_TITLE = "AWB 控制台 · 会话与共享记忆"
OFFSCREEN = "--offscreen" in sys.argv   # 无打扰的窗口测试：把窗口放到屏幕外

LOG_DIR = os.path.join(C.DATA_DIR, "logs")
LOG_FILE = os.path.join(LOG_DIR, "awb_gui.log")
SELFTEST_FILE = Path(C.DATA_DIR) / "diagnostics" / "awb_selftest.json"
NATIVE_SYNC = "experiments/native_session_sync/workspace_sync.py"
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


# ---------------------------------------------------------------- 基础工具

def _find_python():
    """打包成 exe 后需要外部解释器跑 CLI 脚本；AWB_PYTHON 可显式指定。"""
    if not FROZEN:
        executable = sys.executable
        if os.path.basename(executable).lower() == "pythonw.exe":
            console_python = os.path.join(os.path.dirname(executable), "python.exe")
            if os.path.isfile(console_python):
                return console_python
        return executable
    for cand in (os.environ.get("AWB_PYTHON"), shutil.which("python"), shutil.which("py")):
        if cand and os.path.isfile(cand):
            return cand
    return "python"


PY = _find_python()
PS = "powershell.exe"


def script(name):
    return os.path.join(REPO, name)


def shell_open(path, log=False):
    """用 ShellExecuteW 打开；.log 交给记事本，避免无关联时静默失败。"""
    try:
        if log:
            code = ctypes.windll.shell32.ShellExecuteW(None, "open", "notepad.exe", subprocess.list2cmdline([path]), None, 1)
        else:
            code = ctypes.windll.shell32.ShellExecuteW(None, "open", path, None, None, 1)
        if code <= 32:
            raise OSError("ShellExecute failed (%s)" % code)
    except Exception as e:
        messagebox.showwarning(APP_TITLE, "打不开文件：\n%s\n\n%s" % (path, e))


class Tooltip:
    """悬停提示：每个列头、参数标签、技能名都带说明。"""

    def __init__(self, widget, text):
        self.w = widget
        self.text = text
        self.tip = None
        self.job = None
        widget.bind("<Enter>", self._schedule)
        widget.bind("<Leave>", self._hide)
        widget.bind("<ButtonPress>", self._hide, add="+")
        widget.bind("<Destroy>", self._hide, add="+")

    def _schedule(self, _e):
        self.job = self.w.after(420, self._show)

    def _show(self):
        if self.tip:
            return
        try:
            x = self.w.winfo_rootx() + 12
            y = self.w.winfo_rooty() + self.w.winfo_height() + 4
            self.tip = tk.Toplevel(self.w)
            self.tip.wm_overrideredirect(True)
            self.tip.wm_geometry("+%d+%d" % (x, y))
            tk.Label(self.tip, text=self.text() if callable(self.text) else self.text, justify="left", anchor="w",
                     relief="solid", bd=1, bg="#ffffe0", fg="#202020",
                     font=("Microsoft YaHei UI", 9), wraplength=380,
                     padx=8, pady=6).pack()
        except tk.TclError:
            self.tip = None

    def _hide(self, _e=None):
        if self.job:
            try:
                self.w.after_cancel(self.job)
            except tk.TclError:
                pass
            self.job = None
        if self.tip:
            try:
                self.tip.destroy()
            except tk.TclError:
                pass
            self.tip = None


class Runner:
    """One serialized job chain; only successful chains call the success callback."""
    def __init__(self, root, log_fn, status_fn, busy_fn=None):
        self.root, self.log, self.status = root, log_fn, status_fn
        self.busy_fn = busy_fn or (lambda busy: None)
        self.q = queue.Queue()
        self.busy = False
        self.pending = []
        self.final = self.failed = None
        self.private_line = None
        self.notice = None
        self.label = ""

    def run_many(self, jobs, final=None, failed=None, private_line=None, notice=None):
        if self.busy:
            return False
        self.pending = list(jobs)
        self.final, self.failed = final, failed
        self.private_line = private_line
        self.notice = notice
        self.busy = True
        self.busy_fn(True)
        self._next()
        return True

    def run(self, label, argv, enc="utf-8", after=None, failed=None, private_line=None, notice=None):
        return self.run_many([(label, argv, enc)], final=after, failed=failed, private_line=private_line, notice=notice)

    def _finish(self, rc):
        callback = self.final if rc == 0 else self.failed
        self.final = self.failed = None
        self.private_line = None
        self.notice = None
        self.pending = []
        self.busy = False
        self.busy_fn(False)
        if callback:
            callback() if rc == 0 else callback(rc)

    def _next(self):
        if not self.pending:
            self._finish(0)
            return
        self.label, argv, enc = self.pending.pop(0)
        self.status("正在执行：" + self.label)
        self.log("$ " + subprocess.list2cmdline([str(a) for a in argv]))
        threading.Thread(target=self._worker, args=(argv, enc), daemon=True).start()
        self.root.after(60, self._poll)

    def _worker(self, argv, enc):
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1", PYTHONUNBUFFERED="1")
        try:
            with subprocess.Popen(argv, cwd=REPO, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                  env=env, text=True, encoding=enc, errors="replace",
                                  creationflags=CREATE_NO_WINDOW) as process:
                for line in process.stdout:
                    self.q.put(("line", line.rstrip("\r\n")))
                self.q.put(("done", process.wait()))
        except Exception as error:
            self.q.put(("line", "ERROR: " + str(error)))
            self.q.put(("done", 2))

    def _poll(self):
        try:
            while True:
                kind, value = self.q.get_nowait()
                if kind == "line":
                    if not self.private_line or not self.private_line(value):
                        self.log(value)
                elif value == 1 and self.notice:
                    self.status(self.notice)
                    self._finish(value)
                    return
                elif value != 0:
                    self.status("失败：%s（退出码 %d），请查看日志" % (self.label, value))
                    self.log("操作失败；后续步骤已停止。")
                    self._finish(value)
                    return
                else:
                    self.status("成功：" + self.label)
                    self.root.after(10, self._next)
                    return
        except queue.Empty:
            pass
        if self.busy:
            self.root.after(60, self._poll)


class App:
    def __init__(self, root):
        self.root = root
        self.all_ws = C.list_workspaces()
        self.ws_var = tk.StringVar()
        self.status_var = tk.StringVar(value="就绪")
        self._status_full = "就绪"
        self.operation_status = "就绪"
        self._disabled_widgets = []
        root.title("%s v%s" % (APP_TITLE, VERSION))
        root.geometry("1180x800-4000-4000" if OFFSCREEN else "1180x800")
        root.minsize(1020, 680)
        self._init_logging()
        self._build_ui()
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.log("AWB 控制台 v%s 已启动" % VERSION)

    def _build_ui(self):
        self.nb = ttk.Notebook(self.root)
        self.nb.pack(fill="both", expand=True, padx=10, pady=(8, 0))
        self.session_panel = SessionPanel(self.nb, C.WORKSPACE_ROOT, self.all_ws)
        self.nb.add(self.session_panel, text=" 会话接力 ")
        self.tab_sync = ttk.Frame(self.nb, padding=12)
        self.nb.add(self.tab_sync, text=" 原生会话同步 ")
        self.tab_share = ttk.Frame(self.nb)
        self.nb.add(self.tab_share, text=" 共享记忆 ")
        self.tab_rules = ttk.Frame(self.nb)
        self.nb.add(self.tab_rules, text=" 规则同步 ")
        self._build_sync_tab()
        self._build_share_tab()
        self._build_rules_tab()
        self._build_footer()

    def _build_sync_tab(self):
        ttk.Label(self.tab_sync, text="选择要同步的工作空间，可多选").pack(anchor="w")
        self.sync_list = tk.Listbox(self.tab_sync, selectmode="extended", height=8, exportselection=False)
        self.sync_list.pack(fill="x", pady=6)
        for name in self.all_ws:
            self.sync_list.insert("end", name)
        bar = ttk.Frame(self.tab_sync)
        bar.pack(fill="x", pady=6)
        self.sync_blocked = set()
        self.sync_writes = []
        for text, action in (("只读预览", "preview"), ("开始常驻监控", "monitor"), ("合并后打开两端", "launch")):
            button = ttk.Button(bar, text=text, command=lambda mode=action: self.sync_action(mode))
            button.pack(side="left", padx=(0, 8))
            if action != "preview":
                self.sync_writes.append(button)
        delete = ttk.Button(bar, text="删除两端副本…", command=self.review_delete)
        delete.pack(side="left", padx=(0, 8))
        self.sync_writes.append(delete)
        ttk.Button(bar, text="兼容与恢复诊断", command=lambda: self.sync_action("diagnose")).pack(side="left")
        self.sync_list.bind("<<ListboxSelect>>", lambda _: self.refresh_sync_writes())
        ttk.Label(self.tab_sync, text="两端退出后同步正文和归档状态。工具、附件和归档冲突会暂缓。删除前请在监控终端按 Ctrl+C。",
                  wraplength=850).pack(anchor="w", pady=6)

    def selected_sync_workspaces(self):
        return [os.path.join(C.WORKSPACE_ROOT, self.sync_list.get(index))
                for index in self.sync_list.curselection()]

    def refresh_sync_writes(self):
        blocked = any(os.path.normcase(os.path.realpath(path)) in self.sync_blocked
                      for path in self.selected_sync_workspaces())
        for button in self.sync_writes:
            button.configure(state="disabled" if blocked else "normal")

    def review_delete(self):
        if self.runner.busy:
            return
        indices = self.sync_list.curselection()
        if len(indices) != 1:
            messagebox.showinfo(APP_TITLE, "请选择一个工作空间，再预览要删除的会话。", parent=self.root)
            return
        name = self.sync_list.get(indices[0])
        workspace = os.path.join(C.WORKSPACE_ROOT, name)
        command = [PY, "-B", "-X", "utf8", script(NATIVE_SYNC)]
        rows = []

        def read_private(line):
            if line.startswith("DELETE_CANDIDATES "):
                rows.extend(json.loads(line.removeprefix("DELETE_CANDIDATES ")))
                return True
            return False

        self.runner.run("读取可删除的会话", command + ["delete-preview", workspace],
                        after=lambda: self.choose_delete(name, workspace, command, rows), private_line=read_private)

    def choose_delete(self, name, workspace, command, rows):
        if not rows:
            messagebox.showinfo(APP_TITLE, "这个工作空间没有可安全删除的已配对会话。", parent=self.root)
            return
        dialog = tk.Toplevel(self.root)
        dialog.title("删除两端副本")
        dialog.transient(self.root)
        frame = ttk.Frame(dialog, padding=12)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="工作空间 · " + name).pack(anchor="w")
        ttk.Label(frame, text="请选择一条会话。执行前须停止监控，并彻底退出两端客户端。").pack(anchor="w", pady=(4, 8))
        columns = ("title", "codex", "workbuddy")
        choices = ttk.Treeview(frame, columns=columns, show="headings", selectmode="browse", height=10)
        for col, label, width in zip(columns, ("会话", "Codex", "WorkBuddy"), (380, 85, 85)):
            choices.heading(col, text=label)
            choices.column(col, width=width, stretch=col == "title")
        scroll = ttk.Scrollbar(frame, command=choices.yview)
        choices.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        choices.pack(fill="both", expand=True)
        labels = {"active": "正常", "archived": "已归档", "deleted": "回收站", "missing": "缺失"}
        for index, row in enumerate(rows):
            choices.insert("", "end", iid=str(index), values=[row["title"], *(labels[state] for state in row["states"])])

        def confirm():
            selected = choices.selection()
            if not selected:
                return
            row = rows[int(selected[0])]
            prompt = (f"{name}\n{row['title']}\n\n"
                      "现有副本将先备份。Codex 移除本地会话，WorkBuddy 将会话移入回收站。")
            if messagebox.askyesno("确认删除两端副本", prompt, parent=dialog, default="no"):
                dialog.destroy()
                self.runner.run("备份并删除两端副本", command + ["delete", workspace, "--reviewed", row["token"]])

        button = ttk.Button(frame, text="继续确认…", command=confirm, state="disabled")
        button.pack(anchor="e", pady=(8, 0))
        choices.bind("<<TreeviewSelect>>", lambda _: button.configure(state="normal" if choices.selection() else "disabled"))
        dialog.grab_set()

    def sync_action(self, action):
        if self.runner.busy:
            return
        selected = self.selected_sync_workspaces()
        if not selected:
            messagebox.showinfo(APP_TITLE, "先选择一个或多个工作空间。")
            return
        argv = [PY, "-B", "-X", "utf8", script(NATIVE_SYNC), action]
        argv.extend(selected)
        argv.append("--compatible-only")
        if action == "diagnose":
            seen, blocked = set(), set()
            current = None

            def inspect_line(line):
                nonlocal current
                if line.startswith("WORKSPACE "):
                    current = os.path.normcase(os.path.realpath(line.removeprefix("WORKSPACE ")))
                    seen.add(current)
                elif current and (line.startswith("RECOVERY_REQUIRED ")
                                  or (line.startswith("SCHEMA ") and "READ_WRITE_SUPPORTED" not in line)):
                    blocked.add(current)
                return False

            def update():
                self.sync_blocked.difference_update(seen)
                self.sync_blocked.update(blocked)
                self.refresh_sync_writes()
                if blocked:
                    self.set_status("部分工作空间禁止写入，请查看诊断和操作 ID。")

            self.runner.run("会话兼容与恢复诊断", argv, after=update, private_line=inspect_line)
        elif action in ("preview", "launch"):
            label = "会话同步预览" if action == "preview" else "等待两端退出 · 合并后打开"
            self.runner.run(label, argv)
        else:
            try:
                subprocess.Popen(argv, cwd=REPO, creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0))
            except OSError as error:
                self.set_status("无法启动会话同步")
                self.log(str(error))
            else:
                self.set_status("会话同步终端已启动")

    def _build_share_tab(self):
        bar = ttk.Frame(self.tab_share, padding=10)
        bar.pack(fill="x")
        ttk.Label(bar, text="工作空间").pack(side="left")
        self.ws_combo = ttk.Combobox(bar, textvariable=self.ws_var, width=34, state="readonly",
                                     values=self._ws_values())
        self.ws_combo.pack(side="left", padx=6)
        self.ws_combo.set(self.all_ws[0] if self.all_ws else "(全部)")
        ttk.Button(bar, text="刷新列表", command=self._refresh_ws_combos).pack(side="left")
        actions = ttk.Frame(self.tab_share, padding=10)
        actions.pack(fill="x")
        for text, action in (("建立记忆链接", self.share_build), ("状态", self.share_status),
                             ("检查", self.share_check), ("落盘验证", self.share_probe),
                             ("保存链接状态", self.share_snapshot), ("按记录修复链接", self.share_restore)):
            ttk.Button(actions, text=text, command=action).pack(side="left", padx=4)
        backup_bar = ttk.Frame(self.tab_share, padding=(10, 0, 10, 10))
        backup_bar.pack(fill="x")
        ttk.Button(backup_bar, text="备份记忆正文", command=self.share_backup).pack(side="left", padx=4)
        ttk.Button(backup_bar, text="预览并恢复记忆…", command=self.share_recover).pack(side="left", padx=4)
        ttk.Label(self.tab_share, text="共享位置 .shared/memory，WorkBuddy 通过 .workbuddy/memory 读取。").pack(anchor="w", padx=12)

    def _refresh_ws_combos(self):
        self.all_ws = C.list_workspaces()
        self.session_panel.set_workspaces(self.all_ws)
        self.ws_combo.configure(values=self._ws_values())
        selected = {self.sync_list.get(index) for index in self.sync_list.curselection()}
        self.sync_list.delete(0, "end")
        for index, name in enumerate(self.all_ws):
            self.sync_list.insert("end", name)
            if name in selected:
                self.sync_list.selection_set(index)
        self.refresh_sync_writes()

    def refresh_status(self):
        self._status_full = self.operation_status
        self._render_status()

    def on_close(self):
        if self.runner.busy:
            messagebox.showinfo(APP_TITLE, "正在执行文件操作，请等待完成后退出。")
            return
        self.log("退出")
        self.root.destroy()

    def _init_logging(self):
        os.makedirs(LOG_DIR, exist_ok=True)
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(levelname)s %(message)s",
            handlers=[logging.handlers.RotatingFileHandler(
                LOG_FILE, maxBytes=512 * 1024, backupCount=2, encoding="utf-8")],
        )
        logging.info("Configuration source: %s", C.SOURCE or "defaults")

    def _build_footer(self):
        bar = ttk.Frame(self.root, padding=(10, 6))
        bar.pack(fill="x")
        for label, fn in [("打开日志", lambda: shell_open(LOG_FILE, log=True)), ("清空日志", self.clear_log)]:
            ttk.Button(bar, text=label, command=fn, width=10).pack(side="right", padx=3)
        self.progress = ttk.Progressbar(bar, mode="indeterminate", length=90)
        self.progress.pack(side="right", padx=8)
        self.status_lbl = ttk.Label(bar, textvariable=self.status_var, anchor="w")
        self.status_lbl.pack(side="left", fill="x", expand=True)
        Tooltip(self.status_lbl, lambda: self._status_full)
        self.status_lbl.bind("<Configure>", lambda _e: self._render_status())
        logbox = ttk.LabelFrame(self.root, text=" 运行输出 ", padding=(6, 4))
        logbox.pack(fill="both", padx=10, pady=(0, 8))
        self.log_text = tk.Text(logbox, height=8, wrap="none", state="disabled",
                                font=("Consolas", 9), bg="#fbfbfb", fg="#222")
        vertical = ttk.Scrollbar(logbox, orient="vertical", command=self.log_text.yview)
        horizontal = ttk.Scrollbar(logbox, orient="horizontal", command=self.log_text.xview)
        self.log_text.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        self.log_text.grid(row=0, column=0, sticky="nsew")
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal.grid(row=1, column=0, sticky="ew")
        logbox.columnconfigure(0, weight=1)
        logbox.rowconfigure(0, weight=1)
        self.runner = Runner(self.root, self.log, self.set_status, self.set_busy)

    def _ws_values(self):
        return ["(全部)"] + list(self.all_ws)

    def _ws_args(self, allow_all=False):
        w = self.ws_var.get().strip()
        if not w or w == "(全部)":
            return ["--all"] if allow_all else None
        return ["--workspace", w]

    def share_build(self):
        a = self._ws_args(allow_all=True)
        if not a:
            messagebox.showinfo(APP_TITLE, "先选一个工作空间。")
            return
        self.runner.run("建立共享层", [PY, script("share_workspace.py")] + a)

    def share_status(self):
        a = self._ws_args()
        if not a:
            messagebox.showinfo(APP_TITLE, "先选一个工作空间。")
            return
        self.runner.run("共享层状态", [PY, script("share_workspace.py"), "--status"] + a)

    def share_probe(self):
        a = self._ws_args()
        if not a:
            messagebox.showinfo(APP_TITLE, "先选一个工作空间。")
            return
        self.runner.run("落盘即见验证", [PY, script("share_workspace.py"), "--probe"] + a)

    def share_check(self):
        a = self._ws_args()
        if not a:
            messagebox.showinfo(APP_TITLE, "先选一个工作空间。")
            return
        self.runner.run("漂移检查", [PY, script("share_workspace.py"), "--check"] + a)

    def share_snapshot(self):
        a = self._ws_args()
        if not a:
            messagebox.showinfo(APP_TITLE, "先选一个工作空间。")
            return
        self.runner.run("保存链接状态", [PY, script("share_workspace.py"), "--snapshot"] + a)

    def share_restore(self):
        a = self._ws_args()
        if not a:
            messagebox.showinfo(APP_TITLE, "先选一个工作空间。")
            return
        ws = self.ws_var.get().strip()
        start = os.path.join(C.WORKSPACE_ROOT, ws, ".shared")
        p = filedialog.askopenfilename(title="选择链接状态记录", initialdir=start,
                                       filetypes=[("快照 JSON", "snapshot-*.json"), ("JSON", "*.json")])
        if not p:
            return
        self.runner.run("修复记忆链接", [PY, script("share_workspace.py"), "--restore", p] + a)

    def share_backup(self):
        args = self._ws_args()
        if not args:
            messagebox.showinfo(APP_TITLE, "先选一个工作空间。")
            return
        self.runner.run("备份并校验记忆正文", [PY, script("share_workspace.py"), "--backup-memory"] + args)

    def share_recover(self):
        if self.runner.busy:
            return
        args = self._ws_args()
        if not args:
            messagebox.showinfo(APP_TITLE, "先选一个工作空间。")
            return
        initial = os.path.join(C.WORKSPACE_ROOT, self.ws_var.get(), ".shared", "memory-backups")
        path = filedialog.askopenfilename(title="选择记忆备份", initialdir=initial,
                                          filetypes=[("记忆备份 ZIP", "*.zip")], parent=self.root)
        if not path:
            return
        command = [PY, script("share_workspace.py"), "--restore-memory", path] + args

        def choose():
            mode = messagebox.askyesnocancel(
                "恢复记忆",
                "差异已列在日志中。\n\n是：合并到共享记忆，冲突保留两份。\n"
                "否：恢复到独立目录。\n取消：退出恢复。",
                default="no", parent=self.root,
            )
            if mode is not None:
                self.runner.run("校验并恢复记忆", command + (["--apply-memory"] if mode else []))

        self.runner.run("核对备份及当前记忆差异", command + ["--restore-preview"], after=choose)

    def _build_rules_tab(self):
        bar = ttk.Frame(self.tab_rules, padding=(10, 10))
        bar.pack(fill="x")
        acts = [
            ("状态", "-Status", "打印一致性摘要：目标数、块数、检查点数。"),
            ("Lint", "-Lint", "检查块长度、密钥、重复行；有问题直接报出来。"),
            ("预演更新", "-DryRun -Update", "预览将要写入的 16 个规则文件，不落盘。"),
            ("更新（分发）", "-Update", "把 snippet 里的块分发到 16 个客户端规则文件，改动前自动 .bak 备份。"),
            ("检查", "-Check", "只读检查。规则有差异时退出码 1，解析或读取失败时退出码 2。"),
        ]
        for text, flag, tip in acts:
            b = ttk.Button(bar, text=text, width=13,
                           command=lambda f=flag, t=text: self.rules(t, f))
            b.pack(side="left", padx=5)
            Tooltip(b, tip + "\n脚本：sync-agent-memory.ps1")

        f = ttk.LabelFrame(self.tab_rules, text=" 规则块源 ", padding=(10, 8))
        f.pack(fill="x", padx=10, pady=10)
        ttk.Label(f, text=C.SNIPPET, font=("Consolas", 9), wraplength=880).pack(anchor="w")
        b = ttk.Button(f, text="打开 snippet", command=lambda: shell_open(C.SNIPPET), width=16)
        b.pack(anchor="w", pady=(8, 0))
        Tooltip(b, "snippet 里的 <!-- block -->…<!-- /block --> 每块会同步到各客户端规则文件。")

        ttk.Label(self.tab_rules, justify="left", foreground="#555", padding=(12, 4),
                  text="改规则内容只改这一份 snippet，再点「更新（分发）」。\n"
                       "每次 -Update 会给被改动的文件留 .bak-<时间戳>，可随时比对回滚。\n"
                       "-Check 的退出码 1 可直接挂 CI 或 pre-commit。").pack(fill="x")

    def rules(self, label, flags):
        argv = [PS, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                script("sync-agent-memory.ps1"), "-Snippet", C.SNIPPET] + flags.split()
        notice = {"-Check": "检测到规则差异，请查看日志。",
                  "-Lint": "源规则检查有提示，请查看日志。"}.get(flags)
        self.runner.run("规则同步 · " + label, argv, enc="mbcs", notice=notice)

    def _render_status(self):
        text = getattr(self, '_status_full', self.operation_status)
        width = max(100, self.status_lbl.winfo_width() - 12)
        font = tkfont.Font(font=self.status_lbl.cget('font') or 'TkDefaultFont')
        display = text
        while len(display) > 10 and font.measure(display) > width:
            display = display[:-2]
        self.status_var.set(display + ('…' if display != text else ''))

    def set_busy(self, busy):
        if busy:
            self._disabled_widgets = []
            def walk(parent):
                for widget in parent.winfo_children():
                    if widget is self.session_panel:
                        continue
                    if isinstance(widget, (ttk.Button, ttk.Entry, ttk.Combobox, tk.Button, tk.Checkbutton)):
                        self._disabled_widgets.append((widget, widget.cget('state')))
                        widget.configure(state='disabled')
                    walk(widget)
            walk(self.nb)
            self.progress.start(15)
        else:
            self.progress.stop()
            for widget, state in self._disabled_widgets:
                if widget.winfo_exists():
                    widget.configure(state=state)
            self._disabled_widgets = []

    def log(self, text):
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        line = "[%s] %s\n" % (ts, text)
        try:
            self.log_text.configure(state="normal")
            self.log_text.insert("end", line)
            self.log_text.see("end")
            self.log_text.configure(state="disabled")
        except (tk.TclError, AttributeError):
            pass
        try:
            logging.info(text)
        except Exception:
            pass

    def clear_log(self):
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    def set_status(self, text):
        self.operation_status = text
        self.refresh_status()


def run_selftest():
    lines, ok = [], True

    def chk(label, cond, detail=""):
        nonlocal ok
        lines.append("%s %s%s" % ("OK  " if cond else "FAIL", label, (" — " + detail) if detail else ""))
        if not cond:
            ok = False

    chk("Python 版本 >= 3.10", sys.version_info >= (3, 10), sys.version.split()[0])
    chk("tkinter 可用", "tkinter" in sys.modules)
    chk("控制台 Python 解释器存在", os.path.isfile(PY), PY)
    chk("工作空间根存在", os.path.isdir(C.WORKSPACE_ROOT), C.WORKSPACE_ROOT)
    chk("规则块源存在", os.path.isfile(C.SNIPPET), C.SNIPPET)
    for f in ("share_workspace.py", "awb_memory_backup.py", "sync-agent-memory.ps1", "awb_sessions.py", "awb_session_panel.py", NATIVE_SYNC,
              "experiments/native_session_sync/session_lifecycle.py", "experiments/native_session_sync/probe_codex.py",
              "experiments/native_session_sync/storage_schema.py", "experiments/native_session_sync/sync_journal.py",
              "experiments/native_session_sync/sync_recovery.py"):
        chk("脚本存在 " + f, os.path.isfile(script(f)))
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as h:
            h.write("")
        chk("日志文件可写", True, LOG_FILE)
    except Exception as e:
        chk("日志文件可写", False, str(e))
    try:
        r = subprocess.run([PY, "-c", "print('awb-ok')"], capture_output=True,
                           text=True, encoding="utf-8", errors="replace",
                           timeout=20, creationflags=CREATE_NO_WINDOW)
        chk("子进程可执行", r.returncode == 0 and "awb-ok" in r.stdout)
    except Exception as e:
        chk("子进程可执行", False, str(e))
    try:
        if os.name == "nt":
            k = ctypes.WinDLL("kernel32", use_last_error=True)
            k.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
            k.CreateMutexW.restype = ctypes.c_void_p
            k.CloseHandle.argtypes = [ctypes.c_void_p]
            h = k.CreateMutexW(None, False, "Global\\AWB_GUI_SELFTEST")
            chk("单实例互斥量可创建", bool(h))
            if h:
                k.CloseHandle(h)
        else:
            chk("单实例互斥量（非 Windows 跳过）", True)
    except Exception as e:
        chk("单实例互斥量可创建", False, str(e))
    lines.append("SELFTEST %s" % ("OK" if ok else "FAILED"))
    return ok, lines


def run_once():
    cmds = [
        ("规则一致性", [PS, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                        script("sync-agent-memory.ps1"), "-Snippet", C.SNIPPET, "-Check"], "mbcs"),
    ]
    rc = 0
    for label, argv, enc in cmds:
        print("=== %s ===" % label)
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        p = subprocess.run(argv, cwd=REPO, capture_output=True, text=True,
                           encoding=enc, errors="replace", env=env,
                           creationflags=CREATE_NO_WINDOW)
        print((p.stdout or "").strip())
        if p.stderr:
            print((p.stderr or "").strip())
        rc += p.returncode
    print("ONCE DONE rc=%d" % rc)
    return rc


def _crash_log(text):
    """noconsole 打包后没有 stdout，异常落盘便于排查。"""
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(os.path.join(LOG_DIR, "awb_gui_error.log"), "a", encoding="utf-8") as f:
            f.write("[%s]\n%s\n" % (datetime.datetime.now().isoformat(), text))
    except Exception:
        pass


def _acquire_mutex():
    if os.name != "nt":
        return None
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
    k.CreateMutexW.restype = ctypes.c_void_p
    ctypes.set_last_error(0)          # 成功时 Win32 不清 last error，必须先归零
    h = k.CreateMutexW(None, False, "Global\\AWB_GUI_SINGLETON")
    if not h or ctypes.get_last_error() == 183:
        return None
    return h


def main():
    args = sys.argv[1:]
    if "--selftest-output" in args and ("--selftest" not in args or
                                        args.index("--selftest-output") + 1 >= len(args) or
                                        args[args.index("--selftest-output") + 1].startswith("--")):
        print("SELFTEST_ERROR --selftest-output requires --selftest and a file path", file=sys.stderr)
        return 2
    if "--selftest" in args:
        output = Path(args[args.index("--selftest-output") + 1]) if "--selftest-output" in args else SELFTEST_FILE
        ok, lines = (False, ["CONFIG_ERROR " + str(C.ERROR)]) if C.ERROR else run_selftest()
        try:
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(json.dumps({"version": VERSION, "ok": ok, "checks": lines},
                                         ensure_ascii=False, indent=2), encoding="utf-8")
        except (OSError, ValueError) as error:
            detail = f"SELFTEST_ERROR {output}: {error}"
            print(detail, file=sys.stderr)
            _crash_log(detail)
            return 2
        for l in lines:
            print(l)
        return 0 if ok else 1
    if C.ERROR:
        detail = "CONFIG_ERROR " + str(C.ERROR)
        _crash_log(detail)
        if "--once" in args:
            print(detail, file=sys.stderr)
        else:
            messagebox.showerror(APP_TITLE, "无法读取 AWB 配置。\n\n" + str(C.ERROR))
        return 2
    if "--once" in args:
        return run_once()

    mutex = _acquire_mutex()
    if mutex is None and os.name == "nt":
        _crash_log("mutex: another instance is already running")
        return 0

    try:
        root = tk.Tk()
        App(root)
        root.mainloop()
    except Exception as error:
        _crash_log(traceback.format_exc())
        messagebox.showerror(APP_TITLE, "无法启动 AWB。\n\n" + str(error))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
