# -*- coding: utf-8 -*-
"""Desktop view for searching local sessions and preparing a handoff."""
from datetime import datetime
from pathlib import Path
import queue
import threading
import tkinter as tk
from tkinter import ttk

import awb_sessions as sessions


class SessionPanel(ttk.Frame):
    def __init__(self, parent, workspace_root, workspaces):
        super().__init__(parent, padding=8)
        self.workspace_root = Path(workspace_root)
        self.workspace = None
        self.items = []
        self.rows = []
        self.query = ""
        self.busy = False
        self.loaded = False
        self.pack_ready = False
        self.status = tk.StringVar(value="选择一个工作空间，读取本机会话。")
        self.scope = tk.StringVar()
        self.agent = tk.StringVar(value="全部客户端")
        self.keyword = tk.StringVar()

        bar = ttk.Frame(self)
        bar.pack(fill="x", pady=(0, 6))
        ttk.Label(bar, text="工作空间").pack(side="left")
        self.combo = ttk.Combobox(bar, textvariable=self.scope, values=workspaces, width=26, state="readonly")
        self.combo.pack(side="left", padx=6)
        self.combo.bind("<<ComboboxSelected>>", lambda _event: self.load())
        self.refresh = ttk.Button(bar, text="重新读取", command=self.load)
        self.refresh.pack(side="left")
        self.agents = ttk.Combobox(bar, textvariable=self.agent, values=("全部客户端", sessions.CODEX, sessions.WORKBUDDY),
                                  width=12, state="readonly")
        self.agents.pack(side="right")
        self.agents.bind("<<ComboboxSelected>>", lambda _event: self.find())
        filters = ttk.Frame(self)
        filters.pack(fill="x", pady=(0, 6))
        ttk.Label(filters, text="标题或正文").pack(side="left")
        self.entry = ttk.Entry(filters, textvariable=self.keyword)
        self.entry.pack(side="left", fill="x", expand=True, padx=6)
        self.entry.bind("<Return>", lambda _event: self.find())
        self.find_button = ttk.Button(filters, text="搜索", command=self.find)
        self.find_button.pack(side="left")
        ttk.Label(self, textvariable=self.status, wraplength=800).pack(fill="x", pady=(0, 5))

        panes = ttk.Panedwindow(self, orient="horizontal")
        panes.pack(fill="both", expand=True)
        listing = ttk.Frame(panes)
        preview = ttk.Frame(panes)
        panes.add(listing, weight=2)
        panes.add(preview, weight=3)
        listing.columnconfigure(0, weight=1)
        listing.rowconfigure(0, weight=1)
        self.tree = ttk.Treeview(listing, columns=("agent", "title", "updated"), show="headings", selectmode="browse")
        for column, label, width in (("agent", "来源", 82), ("title", "会话", 210), ("updated", "文件更新", 130)):
            self.tree.heading(column, text=label)
            self.tree.column(column, width=width, minwidth=45, stretch=column == "title")
        self.tree.grid(row=0, column=0, sticky="nsew")
        list_scroll = ttk.Scrollbar(listing, orient="vertical", command=self.tree.yview)
        list_scroll.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=list_scroll.set)
        self.tree.bind("<<TreeviewSelect>>", self.select)

        footer = ttk.Frame(preview)
        footer.pack(side="bottom", fill="x", pady=(6, 0))
        self.make_button = ttk.Button(footer, text="生成接力包", command=self.prepare)
        self.make_button.pack(side="left")
        self.copy_button = ttk.Button(footer, text="复制接力包", command=self.copy_pack)
        self.copy_button.pack(side="left", padx=6)
        self.text = tk.Text(preview, width=39, height=8, wrap="word", state="disabled")
        detail_scroll = ttk.Scrollbar(preview, orient="vertical", command=self.text.yview)
        detail_scroll.pack(side="right", fill="y")
        self.text.configure(yscrollcommand=detail_scroll.set)
        self.text.pack(fill="both", expand=True)
        self.clear_preview()
        self.update_controls()

    def set_workspaces(self, values):
        self.combo.configure(values=values)
        if self.scope.get() and self.scope.get() not in values:
            self.scope.set("")
            self.workspace = None
            self.loaded = False
            self.items = []
            self.display([], "原工作空间已不在列表中，请重新选择。")

    def update_controls(self):
        self.combo.configure(state="disabled" if self.busy else "readonly")
        self.agents.configure(state="readonly" if self.loaded and not self.busy else "disabled")
        self.refresh.configure(state="disabled" if self.busy else "normal")
        self.entry.configure(state="normal" if self.loaded and not self.busy else "disabled")
        self.find_button.configure(state="normal" if self.loaded and not self.busy else "disabled")
        self.make_button.configure(state="normal" if getattr(self, "selection", None) and self.preview and not self.busy else "disabled")
        self.copy_button.configure(state="normal" if self.pack_ready and not self.busy else "disabled")

    def run(self, label, work, done, failed=None):
        if self.busy:
            return
        self.busy = True
        self.status.set(label)
        self.update_controls()
        results = queue.Queue(maxsize=1)

        def worker():
            try:
                results.put((True, work()))
            except Exception as error:
                # Conversation text stays out of the general AWB log.
                results.put((False, type(error).__name__))

        def poll():
            try:
                ok, value = results.get_nowait()
            except queue.Empty:
                self.after(50, poll)
                return
            self.busy = False
            if ok:
                done(value)
            else:
                if failed:
                    failed()
                self.status.set("读取失败（%s），请尝试重新读取。" % value)
            self.update_controls()

        threading.Thread(target=worker, daemon=True).start()
        self.after(50, poll)

    def load(self):
        name = self.scope.get()
        if self.busy or name not in self.combo.cget("values"):
            return
        path = self.workspace_root / name
        self.workspace = path
        self.loaded = False
        self.items = []
        self.query = ""
        self.keyword.set("")
        self.display([], "")

        def loaded(result):
            if self.workspace != path:
                return
            self.items, errors = result
            self.loaded = True
            visible = self.filtered()
            self.display([(item, "") for item in visible],
                         "%d 条本机会话%s。点选一条查看摘录。" %
                         (len(visible), self.error_suffix(errors)))
        self.run("正在读取本机会话列表…", lambda: sessions.scan(path), loaded)

    @staticmethod
    def error_suffix(errors):
        return "，%d 个来源读取失败" % errors if errors else ""

    def filtered(self):
        agent = self.agent.get()
        return [item for item in self.items if agent == "全部客户端" or item.agent == agent]

    def find(self):
        if self.busy or not self.loaded:
            return
        self.query = self.keyword.get().strip()
        query, candidates, workspace = self.query, self.filtered(), self.workspace

        def found(result):
            if workspace != self.workspace:
                return
            hits, errors = result
            self.display(hits, "%d 条%s%s。" %
                         (len(hits), "匹配会话" if query else "会话", self.error_suffix(errors)))
        self.clear_preview()
        self.run("正在搜索标题和正文…", lambda: sessions.search(candidates, query), found)

    def display(self, rows, status):
        self.rows = rows
        self.tree.delete(*self.tree.get_children())
        for i, (item, hit) in enumerate(rows):
            title = ("%s · %s" % (hit, item.title)) if hit else item.title
            date = datetime.fromtimestamp(item.updated).strftime("%m-%d %H:%M")
            self.tree.insert("", "end", iid=str(i), values=(item.agent, title, date))
        self.clear_preview()
        self.status.set(status)
        self.update_controls()

    def set_text(self, content, editable=False):
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.insert("1.0", content.encode("utf-8", errors="replace").decode("utf-8"))
        self.text.configure(state="normal" if editable else "disabled")
        self.text.yview_moveto(0)

    def clear_preview(self):
        self.selection = None
        self.preview = []
        self.pack_ready = False
        self.set_text("从左侧选一条会话。这里显示近期消息，搜索后会显示正文命中附近的摘录。")

    def select(self, _event=None):
        selected = self.tree.selection()
        if self.busy or not selected:
            return
        index = selected[0]
        session, hit = self.rows[int(index)]
        query = self.query if hit.startswith("正文") else ""
        self.clear_preview()
        self.set_text("正在读取会话摘录…")

        def ready(result):
            if self.tree.selection() != (index,):
                self.select()
                return
            self.selection = session
            self.preview = result
            self.preview_query = query
            parts = [session.title, "来源 %s · %s" % (session.agent, session.cwd)]
            for item in result:
                role = "用户" if item.role == "user" else "助手"
                parts.append("%s · %s 第 %d 行\n%s" % (role, item.file.name, item.line,
                                                        sessions.clipped(item.text, query)))
            self.set_text("\n\n".join(parts) if result else "\n\n".join(parts + ["没有可预览的对话正文。"]))
            if query and any(query.casefold() in item.text.casefold() for item in result):
                self.status.set("摘录已定位到正文命中处。")
            elif query:
                self.status.set("原命中已变化。当前显示近期摘录，可重新搜索。")
            else:
                self.status.set("已显示最多 8 条对话摘录。")
        self.run("正在读取会话摘录…", lambda: sessions.excerpt(session, query, count=8), ready, self.clear_preview)

    def prepare(self):
        if self.busy or not self.selection or not self.preview or not self.workspace:
            return
        draft = sessions.handoff(self.selection, self.preview, self.workspace, self.preview_query)
        self.pack_ready = True
        self.set_text(draft, editable=True)
        self.status.set("可在右侧编辑摘录，确认后复制到目标 AI 的新会话。")
        self.update_controls()

    def copy_pack(self):
        if self.busy or not self.pack_ready:
            return
        draft = self.text.get("1.0", "end-1c")
        if draft.strip():
            self.clipboard_clear()
            self.clipboard_append(draft)
            self.status.set("接力包已复制。请在目标 AI 打开对应工作空间并粘贴。")
