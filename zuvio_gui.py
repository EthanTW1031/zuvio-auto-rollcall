#!/usr/bin/env python3
"""Zuvio 自動點名助手：視窗介面版。

登入、讀取課程、偵測與送出點名都沿用 zuvio_rollcall.py；
這個檔案只負責畫面，並把網路工作放到背景執行緒，避免視窗卡住。
帳號密碼只留在記憶體中，不會寫進任何檔案。
"""

from __future__ import annotations

import datetime as dt
import os
import queue
import sys
import threading
import tkinter as tk
import tkinter.font as tkfont
import traceback
from tkinter import messagebox, ttk
from tkinter.scrolledtext import ScrolledText
from typing import Any

import zuvio_rollcall as zr

APP_TITLE = "Zuvio 自動點名助手"
EVENT_POLL_MS = 100
MAX_LOG_LINES = 2000

STATUS_COLORS = {
    "idle": "#6b7280",
    "busy": "#7c3aed",
    "watching": "#2563eb",
    "success": "#16a34a",
    "warning": "#d97706",
    "error": "#dc2626",
}
LOG_COLORS = {"success": "#15803d", "warning": "#b45309", "error": "#b91c1c"}


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------


def enable_high_dpi() -> None:
    """讓 Windows 高解析度螢幕上的字不會糊掉。"""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass


def pick_font_family(root: tk.Misc) -> str:
    available = set(tkfont.families(root))
    for family in (
        "Microsoft JhengHei UI",
        "Microsoft JhengHei",
        "PingFang TC",
        "Noto Sans CJK TC",
        "Noto Sans TC",
    ):
        if family in available:
            return family
    return str(tkfont.nametofont("TkDefaultFont").actual("family"))


def set_enabled(widget: tk.Misc, enabled: bool) -> None:
    try:
        widget.state(["!disabled"] if enabled else ["disabled"])  # type: ignore[attr-defined]
    except (AttributeError, tk.TclError):
        widget.configure(state="normal" if enabled else "disabled")  # type: ignore[call-arg]


def course_fields(course: dict[str, Any]) -> tuple[str, str, str]:
    name = str(course.get("course_name") or "(未命名課程)")
    teacher = str(course.get("teacher_name") or "-")
    semester = str(course.get("semester_name") or "").strip()
    return name, teacher, semester


def course_matches(course: dict[str, Any], keyword: str) -> bool:
    needle = zr.normalize_text(keyword)
    if not needle:
        return True
    haystack = " ".join(
        zr.normalize_text(course.get(key))
        for key in ("course_name", "teacher_name", "semester_name")
    )
    return needle in haystack


# ---------------------------------------------------------------------------
# 背景執行緒：持續偵測點名（邏輯對應 zuvio_rollcall.main 的
# --auto-submit --keep-watching 模式）
# ---------------------------------------------------------------------------


class Watcher(threading.Thread):
    def __init__(
        self,
        *,
        session: Any,
        auth: zr.AuthContext,
        course_id: str,
        course_name: str,
        interval: int,
        deadline: dt.datetime | None,
        debug: bool,
        lat: str,
        lng: str,
        events: queue.Queue,
    ) -> None:
        super().__init__(name="zuvio-watcher", daemon=True)
        self.session = session
        self.auth = auth
        self.course_id = course_id
        self.course_name = course_name
        self.interval = interval
        self.deadline = deadline
        self.debug = debug
        self.lat = lat
        self.lng = lng
        self.events = events
        self._stop_event = threading.Event()

    def stop(self) -> None:
        self._stop_event.set()

    def _emit(self, kind: str, **data: Any) -> None:
        self.events.put((kind, data))

    def _log(self, text: str, tag: str | None = None) -> None:
        self._emit("log", text=text, tag=tag, at=zr.now_label())

    def run(self) -> None:
        try:
            level, text = self._watch()
        except Exception as exc:  # 保底：任何沒預期到的錯誤都要回報到畫面上
            level, text = "error", f"發生未預期錯誤：{exc.__class__.__name__}: {exc}"
        self._emit("finished", level=level, text=text)

    def _save_debug(self, page: zr.RollcallPage) -> None:
        self._log(f"診斷：HTTP {page.status_code}｜{page.url}")
        try:
            path = zr.save_debug_snapshot(self.course_id, page)
        except OSError as exc:
            self._log(f"診斷：快照存檔失敗（{exc}）", "warning")
        else:
            self._log(f"診斷：已保存頁面快照 {path}")
        self._log(f"診斷：頁面文字摘要：{zr.page_text_summary(page.html)}")

    def _watch(self) -> tuple[str, str]:
        handled_ids: set[str] = set()
        last_logged: str | None = None
        last_fingerprint: str | None = None
        done_text = ""
        checks = 0

        if self.deadline:
            plan = f"{self.deadline.strftime('%H:%M')} 自動停止"
        else:
            plan = "直到按下「停止」"
        self._log(f"開始監看「{self.course_name}」，每 {self.interval} 秒檢查一次，{plan}。")
        if self.deadline and self.deadline < dt.datetime.now():
            self._log("提醒：截止時間已早於現在，會檢查一次後就結束。", "warning")

        while not self._stop_event.is_set():
            tag: str | None = None
            try:
                page = zr.fetch_rollcall_page(self.session, self.course_id)
            except zr.requests.RequestException as exc:
                status, level, tag = f"讀取失敗（{exc.__class__.__name__}），稍後重試", "warning", "warning"
            else:
                rollcall_id = page.rollcall_id
                if not rollcall_id:
                    status, level = "點名尚未開放，持續偵測中", "watching"
                    if self.debug:
                        fingerprint = zr.page_fingerprint(page.html)
                        if fingerprint != last_fingerprint:
                            self._save_debug(page)
                            last_fingerprint = fingerprint
                elif rollcall_id in handled_ids:
                    status, level = done_text or "這次點名已處理，等待下一次", "success" if done_text else "watching"
                else:
                    self._log("偵測到點名開放，正在送出簽到…")
                    try:
                        result = zr.submit_rollcall(
                            self.session, self.auth, rollcall_id, self.lat, self.lng
                        )
                    except zr.requests.RequestException as exc:
                        # 不記為已處理，下一輪會再試一次
                        status, level, tag = f"送出失敗（{exc.__class__.__name__}），下次檢查再試", "warning", "warning"
                    else:
                        handled_ids.add(rollcall_id)
                        if result.message:
                            self._log(f"Zuvio 回應：{result.message}")

                        if result.status == "gps_required":
                            self._emit("alert")
                            return "error", "需要 GPS 定位，請改用手機手動簽到"
                        if result.status == "success":
                            done_text = f"{zr.now_label()} 簽到成功！繼續偵測下一次點名"
                            status, level, tag = done_text, "success", "success"
                            self._emit("signed")
                            self._emit("alert")
                        elif result.status == "answered":
                            done_text = f"這次點名已簽到過（{zr.now_label()}），繼續偵測"
                            status, level, tag = done_text, "success", "success"
                            self._emit("signed")
                        elif result.status == "not_onair":
                            status, level = "這次點名目前不可送出，繼續偵測", "watching"
                        else:
                            status, level, tag = "收到無法辨識的回應（見紀錄），繼續偵測", "warning", "warning"

            checks += 1
            self._emit("check", count=checks, at=zr.now_label())
            if status != last_logged:  # 只在狀態改變時寫紀錄，避免洗版
                self._log(status, tag)
                last_logged = status
            self._emit("status", level=level, text=status)

            if self.deadline and dt.datetime.now() >= self.deadline:
                return "idle", f"已到截止時間 {self.deadline.strftime('%H:%M')}，停止偵測。"

            if self._stop_event.wait(self.interval):
                break

        return "idle", "已停止偵測。"


# ---------------------------------------------------------------------------
# 主視窗
# ---------------------------------------------------------------------------


class App:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.events: queue.Queue = queue.Queue()
        self.session: Any = None
        self.auth: zr.AuthContext | None = None
        self.course_by_iid: dict[str, dict[str, Any]] = {}
        self.watcher: Watcher | None = None
        self.logging_in = False
        self.stopping = False
        self.signed_count = 0
        self.check_count = 0
        self.last_check = "—"

        self._build_ui()
        self._set_status("idle", "請先登入 Zuvio")
        self._update_controls()

        root.report_callback_exception = self._report_callback_error  # type: ignore[assignment]
        root.protocol("WM_DELETE_WINDOW", self._on_close)
        root.after(EVENT_POLL_MS, self._drain_events)
        self.email_entry.focus_set()

    # ----- 版面 -------------------------------------------------------------

    def _build_ui(self) -> None:
        root = self.root
        root.title(APP_TITLE)

        family = pick_font_family(root)
        for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont", "TkCaptionFont"):
            try:
                tkfont.nametofont(name).configure(family=family, size=10)
            except tk.TclError:
                pass
        base_font = tkfont.nametofont("TkDefaultFont")
        char_w = base_font.measure("字")
        line_h = base_font.metrics("linespace")

        style = ttk.Style(root)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        style.configure("Treeview", rowheight=int(line_h * 1.5))
        style.configure("Title.TLabel", font=(family, 16, "bold"))
        style.configure("Hint.TLabel", foreground="#6b7280")
        style.configure("Big.TButton", font=(family, 11, "bold"), padding=(14, 6))
        style.configure("TLabelframe.Label", font=(family, 10, "bold"))

        outer = ttk.Frame(root, padding=16)
        outer.grid(row=0, column=0, sticky="nsew")
        root.columnconfigure(0, weight=1)
        root.rowconfigure(0, weight=1)
        outer.columnconfigure(1, weight=1)
        outer.rowconfigure(1, weight=1)

        header = ttk.Frame(outer)
        header.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 12))
        ttk.Label(header, text=APP_TITLE, style="Title.TLabel").pack(anchor="w")
        ttk.Label(
            header,
            text="帳號密碼只在這次開啟期間使用，不會存成檔案。偵測期間請保持視窗開著。",
            style="Hint.TLabel",
        ).pack(anchor="w")

        left = ttk.Frame(outer)
        left.grid(row=1, column=0, sticky="nsw", padx=(0, 16))
        left.columnconfigure(0, weight=1)
        right = ttk.Frame(outer)
        right.grid(row=1, column=1, sticky="nsew")

        self._build_login(left)
        self._build_courses(left, char_w)
        self._build_settings(left)
        self._build_actions(left)
        self._build_status(right, family)

    def _build_login(self, parent: ttk.Frame) -> None:
        box = ttk.LabelFrame(parent, text=" 1. 登入 Zuvio ", padding=12)
        box.grid(row=0, column=0, sticky="ew")
        box.columnconfigure(1, weight=1)

        ttk.Label(box, text="帳號 / Email").grid(row=0, column=0, sticky="w", pady=3)
        self.email_var = tk.StringVar()
        self.email_entry = ttk.Entry(box, textvariable=self.email_var, width=30)
        self.email_entry.grid(row=0, column=1, sticky="ew", pady=3, padx=(10, 0))
        self.email_entry.bind("<Return>", lambda _e: self.password_entry.focus_set())

        ttk.Label(box, text="密碼").grid(row=1, column=0, sticky="w", pady=3)
        self.password_var = tk.StringVar()
        self.password_entry = ttk.Entry(box, textvariable=self.password_var, show="●", width=30)
        self.password_entry.grid(row=1, column=1, sticky="ew", pady=3, padx=(10, 0))
        self.password_entry.bind("<Return>", lambda _e: self._login())

        self.show_pw_var = tk.BooleanVar(value=False)
        self.show_pw_check = ttk.Checkbutton(
            box, text="顯示密碼", variable=self.show_pw_var, command=self._toggle_password
        )
        self.show_pw_check.grid(row=2, column=1, sticky="w", padx=(10, 0))

        row = ttk.Frame(box)
        row.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        self.login_btn = ttk.Button(row, text="登入並讀取課程", command=self._login)
        self.login_btn.pack(side="left")
        self.login_status = ttk.Label(row, text="", style="Hint.TLabel")
        self.login_status.pack(side="left", padx=(10, 0))

    def _build_courses(self, parent: ttk.Frame, char_w: int) -> None:
        box = ttk.LabelFrame(parent, text=" 2. 選擇課程 ", padding=12)
        box.grid(row=1, column=0, sticky="ew", pady=(12, 0))
        box.columnconfigure(1, weight=1)

        ttk.Label(box, text="搜尋").grid(row=0, column=0, sticky="w")
        self.search_var = tk.StringVar()
        self.search_var.trace_add("write", lambda *_: self._refresh_courses())
        self.search_entry = ttk.Entry(box, textvariable=self.search_var)
        self.search_entry.grid(row=0, column=1, sticky="ew", padx=(10, 8))
        self.course_count = ttk.Label(box, text="", style="Hint.TLabel")
        self.course_count.grid(row=0, column=2, sticky="e")

        tree_frame = ttk.Frame(box)
        tree_frame.grid(row=1, column=0, columnspan=3, sticky="nsew", pady=(8, 0))
        tree_frame.columnconfigure(0, weight=1)
        self.course_tree = ttk.Treeview(
            tree_frame,
            columns=("name", "teacher", "semester"),
            show="headings",
            height=7,
            selectmode="browse",
        )
        for col, title, width, stretch in (
            ("name", "課程", char_w * 14, True),
            ("teacher", "老師", char_w * 5, False),
            ("semester", "學期", char_w * 6, False),
        ):
            self.course_tree.heading(col, text=title, anchor="w")
            self.course_tree.column(col, width=width, minwidth=char_w * 3, stretch=stretch, anchor="w")
        self.course_tree.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(tree_frame, orient="vertical", command=self.course_tree.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.course_tree.configure(yscrollcommand=scroll.set)
        self.course_tree.bind("<<TreeviewSelect>>", lambda _e: self._on_course_selected())

        self.course_hint = ttk.Label(box, text="請先登入，課程會出現在這裡。", style="Hint.TLabel")
        self.course_hint.grid(row=2, column=0, columnspan=3, sticky="w", pady=(6, 0))

    def _build_settings(self, parent: ttk.Frame) -> None:
        box = ttk.LabelFrame(parent, text=" 3. 偵測設定 ", padding=12)
        box.grid(row=2, column=0, sticky="ew", pady=(12, 0))

        row1 = ttk.Frame(box)
        row1.grid(row=0, column=0, sticky="w", pady=3)
        ttk.Label(row1, text="每").pack(side="left")
        self.interval_var = tk.StringVar(value="10")
        self.interval_spin = ttk.Spinbox(
            row1, from_=1, to=600, increment=1, textvariable=self.interval_var, width=5
        )
        self.interval_spin.pack(side="left", padx=6)
        ttk.Label(row1, text="秒檢查一次").pack(side="left")

        row2 = ttk.Frame(box)
        row2.grid(row=1, column=0, sticky="w", pady=3)
        ttk.Label(row2, text="自動停止時間").pack(side="left")
        self.until_var = tk.StringVar()
        self.until_entry = ttk.Entry(row2, textvariable=self.until_var, width=7)
        self.until_entry.pack(side="left", padx=6)
        ttk.Label(row2, text="例如 19:55；留空＝一直偵測", style="Hint.TLabel").pack(side="left")

        row3 = ttk.Frame(box)
        row3.grid(row=2, column=0, sticky="w", pady=3)
        self.gps_var = tk.BooleanVar(value=False)
        self.gps_check = ttk.Checkbutton(
            row3, text="老師要求 GPS", variable=self.gps_var, command=self._update_controls
        )
        self.gps_check.pack(side="left")
        ttk.Label(row3, text="緯度").pack(side="left", padx=(10, 4))
        self.lat_var = tk.StringVar()
        self.lat_entry = ttk.Entry(row3, textvariable=self.lat_var, width=10)
        self.lat_entry.pack(side="left")
        ttk.Label(row3, text="經度").pack(side="left", padx=(8, 4))
        self.lng_var = tk.StringVar()
        self.lng_entry = ttk.Entry(row3, textvariable=self.lng_var, width=10)
        self.lng_entry.pack(side="left")

        self.debug_var = tk.BooleanVar(value=False)
        self.debug_check = ttk.Checkbutton(
            box, text="診斷模式（點名沒開時把頁面快照存到 debug_logs）", variable=self.debug_var
        )
        self.debug_check.grid(row=3, column=0, sticky="w", pady=(3, 0))

    def _build_actions(self, parent: ttk.Frame) -> None:
        row = ttk.Frame(parent)
        row.grid(row=3, column=0, sticky="ew", pady=(14, 0))
        row.columnconfigure(0, weight=3)
        row.columnconfigure(1, weight=2)
        self.start_btn = ttk.Button(row, text="▶  開始自動點名", style="Big.TButton", command=self._start)
        self.start_btn.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        self.stop_btn = ttk.Button(row, text="■  停止", style="Big.TButton", command=self._stop)
        self.stop_btn.grid(row=0, column=1, sticky="ew")

    def _build_status(self, parent: ttk.Frame, family: str) -> None:
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(2, weight=1)

        self.status_label = tk.Label(
            parent,
            text="",
            fg="white",
            font=(family, 13, "bold"),
            anchor="w",
            justify="left",
            padx=14,
            pady=10,
        )
        self.status_label.grid(row=0, column=0, sticky="ew")
        self.status_label.bind(
            "<Configure>", lambda e: self.status_label.configure(wraplength=max(e.width - 28, 100))
        )

        info = ttk.Frame(parent)
        info.grid(row=1, column=0, sticky="ew", pady=(8, 10))
        self.course_label = ttk.Label(info, text="監看課程：—")
        self.course_label.pack(anchor="w")
        self.detail_label = ttk.Label(info, text="", style="Hint.TLabel")
        self.detail_label.pack(anchor="w")
        self._update_detail()

        log_box = ttk.LabelFrame(parent, text=" 執行紀錄 ", padding=8)
        log_box.grid(row=2, column=0, sticky="nsew")
        log_box.columnconfigure(0, weight=1)
        log_box.rowconfigure(0, weight=1)
        self.log_text = ScrolledText(
            log_box,
            width=52,
            height=18,
            wrap="char",
            font=(family, 10),
            state="disabled",
            relief="flat",
            borderwidth=0,
            padx=6,
            pady=4,
        )
        self.log_text.grid(row=0, column=0, sticky="nsew")
        for tag, color in LOG_COLORS.items():
            self.log_text.tag_configure(tag, foreground=color)
        ttk.Button(log_box, text="清除紀錄", command=self._clear_log).grid(
            row=1, column=0, sticky="e", pady=(6, 0)
        )

    # ----- 狀態與畫面更新 ----------------------------------------------------

    def _set_status(self, level: str, text: str) -> None:
        self.status_label.configure(text=text, bg=STATUS_COLORS.get(level, STATUS_COLORS["idle"]))

    def _update_detail(self) -> None:
        self.detail_label.configure(
            text=(
                f"已檢查 {self.check_count} 次｜最後檢查 {self.last_check}｜"
                f"完成簽到 {self.signed_count} 次"
            )
        )

    def _update_controls(self) -> None:
        running = self.watcher is not None
        logged_in = self.session is not None
        login_ok = not running and not self.logging_in
        course_ok = logged_in and login_ok

        for widget in (self.email_entry, self.password_entry, self.show_pw_check, self.login_btn):
            set_enabled(widget, login_ok)
        self.login_btn.configure(text="重新登入" if logged_in else "登入並讀取課程")

        set_enabled(self.search_entry, course_ok)
        set_enabled(self.course_tree, course_ok)
        self.course_tree.configure(selectmode="browse" if course_ok else "none")

        for widget in (self.interval_spin, self.until_entry, self.gps_check, self.debug_check):
            set_enabled(widget, not running)
        for widget in (self.lat_entry, self.lng_entry):
            set_enabled(widget, not running and self.gps_var.get())

        set_enabled(self.start_btn, course_ok)
        set_enabled(self.stop_btn, running and not self.stopping)

    def _log(self, text: str, tag: str | None = None, at: str | None = None) -> None:
        line = f"[{at or zr.now_label()}] {text}\n"
        self.log_text.configure(state="normal")
        self.log_text.insert("end", line, (tag,) if tag else ())
        excess = int(self.log_text.index("end-1c").split(".")[0]) - MAX_LOG_LINES
        if excess > 0:
            self.log_text.delete("1.0", f"{excess + 1}.0")
        self.log_text.configure(state="disabled")
        self.log_text.see("end")

    def _clear_log(self) -> None:
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    def _toggle_password(self) -> None:
        self.password_entry.configure(show="" if self.show_pw_var.get() else "●")

    def _bring_to_front(self) -> None:
        root = self.root
        try:
            root.deiconify()
            root.lift()
            root.attributes("-topmost", True)
            root.after(1500, lambda: root.attributes("-topmost", False))
        except tk.TclError:
            pass
        root.bell()

    # ----- 課程列表 ----------------------------------------------------------

    def _selected_iid(self) -> str | None:
        selection = self.course_tree.selection()
        return selection[0] if selection else None

    def _selected_course(self) -> dict[str, Any] | None:
        iid = self._selected_iid()
        return self.course_by_iid.get(iid) if iid else None

    def _refresh_courses(self) -> None:
        selected = self._selected_iid()
        tree = self.course_tree
        tree.delete(*tree.get_children())

        keyword = self.search_var.get()
        shown = 0
        for iid, course in self.course_by_iid.items():
            if course_matches(course, keyword):
                tree.insert("", "end", iid=iid, values=course_fields(course))
                shown += 1
        if selected and tree.exists(selected):
            tree.selection_set(selected)
            tree.see(selected)

        total = len(self.course_by_iid)
        if self.session is None:
            self.course_count.configure(text="")
            self.course_hint.configure(text="請先登入，課程會出現在這裡。")
        elif total == 0:
            self.course_count.configure(text="")
            self.course_hint.configure(text="目前課程列表是空的。")
        else:
            self.course_count.configure(text=f"{shown} / {total} 門")
            self._on_course_selected()
            if shown == 0:
                self.course_hint.configure(text="找不到符合的課程，換個關鍵字試試。")

    def _on_course_selected(self) -> None:
        course = self._selected_course()
        if course:
            self.course_hint.configure(text=f"已選擇：{course_fields(course)[0]}")
        elif self.course_by_iid:
            self.course_hint.configure(text="點一下要自動點名的課程。")

    # ----- 登入 --------------------------------------------------------------

    def _login(self) -> None:
        if self.logging_in or self.watcher is not None:
            return
        email = self.email_var.get().strip()
        password = self.password_var.get().strip()
        if not email:
            messagebox.showwarning(APP_TITLE, "請輸入 Zuvio 帳號 / Email。", parent=self.root)
            self.email_entry.focus_set()
            return
        if not password:
            messagebox.showwarning(APP_TITLE, "請輸入密碼。", parent=self.root)
            self.password_entry.focus_set()
            return

        self.logging_in = True
        self.login_status.configure(text="登入中…")
        self._set_status("busy", "正在登入 Zuvio…")
        self._log("正在登入並讀取課程列表…")
        self._update_controls()
        threading.Thread(
            target=self._login_worker, args=(email, password), name="zuvio-login", daemon=True
        ).start()

    def _login_worker(self, email: str, password: str) -> None:
        """在背景執行緒登入；帳密只在呼叫 zr.login 的那一刻放進環境變數。"""
        os.environ["ZUVIO_EMAIL"] = email
        os.environ["ZUVIO_PASSWORD"] = password
        try:
            try:
                session = zr.make_session()
                auth = zr.login(session)
            finally:
                os.environ.pop("ZUVIO_EMAIL", None)
                os.environ.pop("ZUVIO_PASSWORD", None)
            courses = zr.fetch_courses(session, auth)
        except zr.LoginFailed as exc:
            self.events.put(("login_failed", {"text": "登入失敗：請確認帳號密碼是否正確。", "detail": str(exc)}))
        except zr.requests.RequestException as exc:
            self.events.put(
                ("login_failed", {"text": f"連不上 Zuvio（{exc.__class__.__name__}），請確認網路後再試一次。", "detail": ""})
            )
        except zr.ZuvioError as exc:
            self.events.put(("login_failed", {"text": f"讀取課程失敗：{exc}", "detail": ""}))
        except Exception as exc:
            self.events.put(
                ("login_failed", {"text": f"發生未預期錯誤：{exc.__class__.__name__}", "detail": str(exc)})
            )
        else:
            self.events.put(("login_ok", {"session": session, "auth": auth, "courses": courses}))

    def _on_login_ok(self, session: Any, auth: zr.AuthContext, courses: list[dict[str, Any]]) -> None:
        self.logging_in = False
        self.session, self.auth = session, auth
        # 先清空舊列表，避免換帳號後沿用到同編號的另一門課
        self.course_tree.delete(*self.course_tree.get_children())
        self.course_by_iid = {str(i): course for i, course in enumerate(courses)}
        self.search_var.set("")
        self._refresh_courses()
        if len(courses) == 1:
            self.course_tree.selection_set("0")

        self.login_status.configure(text=f"登入成功，共 {len(courses)} 門課")
        self._log(f"登入成功，讀到 {len(courses)} 門課。", "success")
        self._set_status("idle", "已登入。選好課程後按「開始自動點名」")
        self._update_controls()
        self.search_entry.focus_set()

    def _on_login_failed(self, text: str, detail: str) -> None:
        self.logging_in = False
        self.session, self.auth = None, None
        self.course_by_iid = {}
        self._refresh_courses()
        self.login_status.configure(text="登入失敗")
        self._log(text + (f"（{detail}）" if detail else ""), "error")
        self._set_status("error", text)
        self._update_controls()
        messagebox.showerror(APP_TITLE, text, parent=self.root)
        self.password_entry.focus_set()

    # ----- 開始 / 停止 -------------------------------------------------------

    def _start(self) -> None:
        if self.watcher is not None or self.session is None or self.auth is None:
            return
        course = self._selected_course()
        if course is None:
            messagebox.showwarning(APP_TITLE, "請先在課程列表中點選一門課。", parent=self.root)
            return
        course_id = str(course.get("course_id") or "").strip()
        if not course_id:
            messagebox.showwarning(APP_TITLE, "這門課缺少課程代碼，請選其他課程。", parent=self.root)
            return

        try:
            interval = int(self.interval_var.get().strip())
        except ValueError:
            interval = 0
        if interval < 1:
            messagebox.showwarning(APP_TITLE, "檢查間隔請輸入 1 以上的整數秒數。", parent=self.root)
            return

        deadline = None
        until = self.until_var.get().strip()
        if until:
            try:
                deadline = zr.parse_until_today(until)
            except ValueError:
                messagebox.showwarning(
                    APP_TITLE, "自動停止時間請用 24 小時制 HH:MM，例如 19:55。", parent=self.root
                )
                return

        lat = lng = ""
        if self.gps_var.get():
            raw_lat, raw_lng = self.lat_var.get().strip(), self.lng_var.get().strip()
            if not raw_lat or not raw_lng:
                messagebox.showwarning(APP_TITLE, "勾選 GPS 時，緯度和經度都要填。", parent=self.root)
                return
            try:
                lat, lng = zr.validate_gps(raw_lat, raw_lng)
            except ValueError:
                messagebox.showwarning(
                    APP_TITLE,
                    "GPS 格式不正確：緯度要在 -90～90、經度要在 -180～180，例如 25.0330 / 121.5654。",
                    parent=self.root,
                )
                return

        name = course_fields(course)[0]
        self.check_count = 0
        self.last_check = "—"
        self.stopping = False
        self._update_detail()
        self.course_label.configure(text=f"監看課程：{name}")
        self.watcher = Watcher(
            session=self.session,
            auth=self.auth,
            course_id=course_id,
            course_name=name,
            interval=interval,
            deadline=deadline,
            debug=self.debug_var.get(),
            lat=lat,
            lng=lng,
            events=self.events,
        )
        self._set_status("watching", "開始偵測…")
        self._update_controls()
        self.watcher.start()

    def _stop(self) -> None:
        if self.watcher is None or self.stopping:
            return
        self.stopping = True
        self.watcher.stop()
        self._set_status("busy", "正在停止…（等目前這次檢查結束）")
        self._update_controls()

    # ----- 背景事件 ----------------------------------------------------------

    def _drain_events(self) -> None:
        try:
            while True:
                try:
                    kind, data = self.events.get_nowait()
                except queue.Empty:
                    break
                handler = getattr(self, f"_on_{kind}", None)
                if handler is None:
                    continue
                try:
                    handler(**data)
                except Exception:
                    self._log("畫面更新時發生錯誤：" + traceback.format_exc(limit=2), "error")
        finally:
            self.root.after(EVENT_POLL_MS, self._drain_events)

    def _on_log(self, text: str, tag: str | None = None, at: str | None = None) -> None:
        self._log(text, tag, at)

    def _on_status(self, level: str, text: str) -> None:
        if not self.stopping:
            self._set_status(level, text)

    def _on_check(self, count: int, at: str) -> None:
        self.check_count = count
        self.last_check = at
        self._update_detail()

    def _on_signed(self) -> None:
        self.signed_count += 1
        self._update_detail()

    def _on_alert(self) -> None:
        self._bring_to_front()

    def _on_finished(self, level: str, text: str) -> None:
        self.watcher = None
        self.stopping = False
        self._set_status(level, text)
        self._log(text, level if level in LOG_COLORS else None)
        self._update_controls()
        if level == "error":
            messagebox.showwarning(APP_TITLE, f"{text}。\n\n自動偵測已停止。", parent=self.root)

    # ----- 其他 --------------------------------------------------------------

    def _report_callback_error(self, exc_type: type, exc: BaseException, tb: Any) -> None:
        self._log("程式錯誤：" + "".join(traceback.format_exception(exc_type, exc, tb, limit=3)), "error")
        messagebox.showerror(APP_TITLE, f"程式發生錯誤：\n{exc}", parent=self.root)

    def _on_close(self) -> None:
        if self.watcher is not None:
            if not messagebox.askyesno(
                APP_TITLE, "目前正在偵測點名，關閉視窗就會停止偵測。\n確定要關閉嗎？", parent=self.root
            ):
                return
            self.watcher.stop()
        self.password_var.set("")
        self.root.destroy()


def center_window(root: tk.Tk) -> None:
    root.update_idletasks()
    width, height = root.winfo_reqwidth(), root.winfo_reqheight()
    root.minsize(width, height)
    x = max((root.winfo_screenwidth() - width) // 2, 0)
    y = max((root.winfo_screenheight() - height) // 3, 0)
    root.geometry(f"+{x}+{y}")


def main() -> int:
    enable_high_dpi()
    root = tk.Tk()
    if zr.requests is None:
        root.withdraw()
        messagebox.showerror(
            APP_TITLE,
            "缺少 Python 套件 requests。\n請先雙擊「第一次使用-安裝套件.bat」，完成後再開啟本程式。",
        )
        root.destroy()
        return 70
    App(root)
    center_window(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
