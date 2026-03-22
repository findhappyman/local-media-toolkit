#!/usr/bin/env python3
"""
超强视频压缩工具 - Video Power Compressor
支持视频压缩、格式转换、音频提取、批量处理
依赖：Python 3.8+ 及系统已安装 FFmpeg (brew install ffmpeg)
"""

import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import subprocess
import threading
import os
import re
import time
import json
from pathlib import Path

# ─────────────────────────────────────────────
# 颜色 & 字体
# ─────────────────────────────────────────────
BG       = "#0f0f1a"
CARD     = "#1a1a2e"
CARD2    = "#16213e"
ACCENT   = "#0f3460"
BLUE     = "#4fc3f7"
GREEN    = "#69f0ae"
ORANGE   = "#ff9100"
RED      = "#ff5252"
TEXT     = "#e0e0e0"
TEXT_DIM = "#888888"
FONT     = ("SF Pro Display", 12) if os.path.exists("/System/Library/Fonts/SFNS.ttf") else ("Helvetica", 12)


def ffmpeg_check():
    try:
        result = subprocess.run(["ffmpeg", "-version"], capture_output=True, text=True)
        return result.returncode == 0
    except FileNotFoundError:
        return False


def get_video_info(path):
    """使用 ffprobe 获取视频信息"""
    cmd = [
        "ffprobe", "-v", "quiet", "-print_format", "json",
        "-show_format", "-show_streams", path
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        data = json.loads(result.stdout)
        info = {}
        fmt = data.get("format", {})
        info["duration"] = float(fmt.get("duration", 0))
        info["size"] = int(fmt.get("size", 0))
        info["bitrate"] = int(fmt.get("bit_rate", 0))
        for s in data.get("streams", []):
            if s.get("codec_type") == "video":
                info["width"]  = s.get("width", 0)
                info["height"] = s.get("height", 0)
                info["vcodec"] = s.get("codec_name", "?")
                fr = s.get("r_frame_rate", "0/1").split("/")
                info["fps"] = round(int(fr[0]) / max(int(fr[1]), 1), 2)
            if s.get("codec_type") == "audio":
                info["acodec"]    = s.get("codec_name", "?")
                info["sample_rate"] = s.get("sample_rate", "?")
                info["channels"]  = s.get("channels", 0)
        return info
    except Exception:
        return {}


def human_size(n):
    for unit in ["B", "KB", "MB", "GB"]:
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


# ─────────────────────────────────────────────
# 主窗口
# ─────────────────────────────────────────────
class VideoCompressor(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("⚡ 超强视频压缩工具 Video Power Compressor")
        self.geometry("950x780")
        self.configure(bg=BG)
        self.resizable(True, True)
        self.minsize(800, 680)

        self.files        = []          # 批量文件列表
        self.proc         = None        # 当前 ffmpeg 进程
        self.running      = False
        self.duration_sec = 0.0

        self._build_ui()

        if not ffmpeg_check():
            messagebox.showerror(
                "缺少 FFmpeg",
                "未检测到 FFmpeg！\n请先安装：\n  brew install ffmpeg\n\n安装后重启本程序。"
            )

    # ── UI ──────────────────────────────────────
    def _build_ui(self):
        # 标题栏
        header = tk.Frame(self, bg=CARD, height=56)
        header.pack(fill="x")
        tk.Label(header, text="⚡  超强视频压缩工具", font=(FONT[0], 16, "bold"),
                 bg=CARD, fg=BLUE).pack(side="left", padx=20, pady=14)
        tk.Label(header, text="Video Power Compressor", font=(FONT[0], 11),
                 bg=CARD, fg=TEXT_DIM).pack(side="left", pady=14)

        # 主体
        main = tk.Frame(self, bg=BG)
        main.pack(fill="both", expand=True, padx=16, pady=12)

        left  = tk.Frame(main, bg=BG, width=440)
        right = tk.Frame(main, bg=BG)
        left.pack(side="left", fill="y", padx=(0, 8))
        right.pack(side="left", fill="both", expand=True)
        left.pack_propagate(False)

        self._build_left(left)
        self._build_right(right)
        self._mode_changed()  # 两栏都建完后再初始化模式

    # ── 左栏：输入 + 参数 ──────────────────────
    def _build_left(self, parent):
        # 文件选择区
        card = self._card(parent, "📂  输入文件 / 批量队列")
        card.pack(fill="x", pady=(0, 8))

        btn_row = tk.Frame(card, bg=CARD)
        btn_row.pack(fill="x", pady=4)
        self._btn(btn_row, "选择文件", self._pick_file, BLUE).pack(side="left", padx=4)
        self._btn(btn_row, "批量添加", self._pick_files, ACCENT).pack(side="left", padx=4)
        self._btn(btn_row, "清除列表", self._clear_files, "#444").pack(side="left", padx=4)

        list_frame = tk.Frame(card, bg="#111122")
        list_frame.pack(fill="x", pady=4)
        self.file_list = tk.Listbox(list_frame, bg="#111122", fg=TEXT, selectbackground=ACCENT,
                                    height=5, font=(FONT[0], 10), bd=0, highlightthickness=0)
        self.file_list.pack(side="left", fill="x", expand=True)
        sb = ttk.Scrollbar(list_frame, orient="vertical", command=self.file_list.yview)
        sb.pack(side="right", fill="y")
        self.file_list.config(yscrollcommand=sb.set)
        self.file_list.bind("<<ListboxSelect>>", self._on_select)

        # 视频信息
        self.info_lbl = tk.Label(card, text="（未选择文件）", font=(FONT[0], 9),
                                 bg=CARD, fg=TEXT_DIM, justify="left", wraplength=380)
        self.info_lbl.pack(anchor="w", padx=4, pady=2)

        # 输出目录
        od = self._card(parent, "📁  输出目录")
        od.pack(fill="x", pady=(0, 8))
        out_row = tk.Frame(od, bg=CARD)
        out_row.pack(fill="x")
        self.out_var = tk.StringVar(value="（与原文件相同目录）")
        tk.Entry(out_row, textvariable=self.out_var, bg=CARD2, fg=TEXT_DIM,
                 insertbackground=TEXT, bd=0, font=(FONT[0], 10), width=28).pack(side="left", fill="x", expand=True, padx=4)
        self._btn(out_row, "浏览", self._pick_outdir, ACCENT, width=6).pack(side="right", padx=4)

        # ── 模式选择 ─────────────────────────────
        mode_c = self._card(parent, "🎬  操作模式")
        mode_c.pack(fill="x", pady=(0, 8))
        self.mode = tk.StringVar(value="compress")
        modes = [("视频压缩", "compress"), ("格式转换", "convert"), ("提取音频", "audio"), ("仅缩放分辨率", "scale")]
        r = tk.Frame(mode_c, bg=CARD)
        r.pack(fill="x")
        for txt, val in modes:
            tk.Radiobutton(r, text=txt, variable=self.mode, value=val,
                           bg=CARD, fg=TEXT, selectcolor=ACCENT, activebackground=CARD,
                           font=(FONT[0], 10), command=self._mode_changed).pack(side="left", padx=8, pady=2)

        # ── 压缩参数（动态显示/隐藏） ─────────────
        self.comp_card = self._card(parent, "⚙️  压缩参数")
        self.comp_card.pack(fill="x", pady=(0, 8))
        self._build_compress_params(self.comp_card)

        self.audio_card = self._card(parent, "🎵  音频提取参数")
        self._build_audio_params(self.audio_card)

        self.conv_card = self._card(parent, "🔄  格式转换参数")
        self._build_conv_params(self.conv_card)

    def _build_compress_params(self, parent):
        # 预设
        row = tk.Frame(parent, bg=CARD)
        row.pack(fill="x", pady=2)
        tk.Label(row, text="快速预设", bg=CARD, fg=TEXT_DIM, font=(FONT[0], 10), width=10).pack(side="left")
        self.preset_var = tk.StringVar(value="平衡")
        presets = ["极小体积", "低质量", "平衡", "高质量", "无损"]
        preset_cb = ttk.Combobox(row, textvariable=self.preset_var, values=presets,
                                  state="readonly", width=12, font=(FONT[0], 10))
        preset_cb.pack(side="left", padx=4)
        preset_cb.bind("<<ComboboxSelected>>", self._apply_preset)

        # 视频编码器
        row2 = tk.Frame(parent, bg=CARD)
        row2.pack(fill="x", pady=2)
        tk.Label(row2, text="视频编码", bg=CARD, fg=TEXT_DIM, font=(FONT[0], 10), width=10).pack(side="left")
        self.vcodec_var = tk.StringVar(value="libx264")
        vcodecs = ["libx264 (H.264)", "libx265 (H.265/HEVC)", "libvpx-vp9 (VP9)",
                   "libaom-av1 (AV1)", "copy (不重编码)"]
        self.vcodec_cb = ttk.Combobox(row2, textvariable=self.vcodec_var, values=vcodecs,
                                       state="readonly", width=22, font=(FONT[0], 10))
        self.vcodec_cb.pack(side="left", padx=4)

        # CRF
        row3 = tk.Frame(parent, bg=CARD)
        row3.pack(fill="x", pady=2)
        tk.Label(row3, text="质量 CRF", bg=CARD, fg=TEXT_DIM, font=(FONT[0], 10), width=10).pack(side="left")
        self.crf_var = tk.IntVar(value=23)
        self.crf_scale = tk.Scale(row3, from_=0, to=51, orient="horizontal", variable=self.crf_var,
                                   bg=CARD, fg=TEXT, highlightthickness=0, length=160,
                                   troughcolor=CARD2, command=self._crf_label_update)
        self.crf_scale.pack(side="left", padx=4)
        self.crf_lbl = tk.Label(row3, text="23 (平衡)", bg=CARD, fg=BLUE, font=(FONT[0], 9), width=12)
        self.crf_lbl.pack(side="left")

        # 分辨率
        row4 = tk.Frame(parent, bg=CARD)
        row4.pack(fill="x", pady=2)
        tk.Label(row4, text="分辨率", bg=CARD, fg=TEXT_DIM, font=(FONT[0], 10), width=10).pack(side="left")
        self.res_var = tk.StringVar(value="保持原始")
        resolutions = ["保持原始", "3840x2160 (4K)", "2560x1440 (2K)", "1920x1080 (1080p)",
                       "1280x720 (720p)", "854x480 (480p)", "640x360 (360p)"]
        ttk.Combobox(row4, textvariable=self.res_var, values=resolutions,
                     state="readonly", width=22, font=(FONT[0], 10)).pack(side="left", padx=4)

        # 帧率
        row5 = tk.Frame(parent, bg=CARD)
        row5.pack(fill="x", pady=2)
        tk.Label(row5, text="帧率 FPS", bg=CARD, fg=TEXT_DIM, font=(FONT[0], 10), width=10).pack(side="left")
        self.fps_var = tk.StringVar(value="保持原始")
        fpss = ["保持原始", "60", "30", "24", "15"]
        ttk.Combobox(row5, textvariable=self.fps_var, values=fpss,
                     state="readonly", width=10, font=(FONT[0], 10)).pack(side="left", padx=4)

        # 音频码率
        row6 = tk.Frame(parent, bg=CARD)
        row6.pack(fill="x", pady=2)
        tk.Label(row6, text="音频码率", bg=CARD, fg=TEXT_DIM, font=(FONT[0], 10), width=10).pack(side="left")
        self.abr_var = tk.StringVar(value="128k")
        ttk.Combobox(row6, textvariable=self.abr_var,
                     values=["32k", "64k", "96k", "128k", "192k", "256k", "320k", "copy"],
                     state="readonly", width=10, font=(FONT[0], 10)).pack(side="left", padx=4)

        # 输出格式
        row7 = tk.Frame(parent, bg=CARD)
        row7.pack(fill="x", pady=2)
        tk.Label(row7, text="输出格式", bg=CARD, fg=TEXT_DIM, font=(FONT[0], 10), width=10).pack(side="left")
        self.comp_fmt_var = tk.StringVar(value="mp4")
        ttk.Combobox(row7, textvariable=self.comp_fmt_var,
                     values=["mp4", "mkv", "mov", "avi", "webm"],
                     state="readonly", width=10, font=(FONT[0], 10)).pack(side="left", padx=4)

        # 加速编码（GPU）
        row8 = tk.Frame(parent, bg=CARD)
        row8.pack(fill="x", pady=2)
        self.hw_var = tk.BooleanVar(value=False)
        tk.Checkbutton(row8, text="尝试硬件加速 (macOS VideoToolbox)", variable=self.hw_var,
                       bg=CARD, fg=TEXT_DIM, selectcolor=CARD2, activebackground=CARD,
                       font=(FONT[0], 10)).pack(side="left", padx=4)

    def _build_audio_params(self, parent):
        row = tk.Frame(parent, bg=CARD)
        row.pack(fill="x", pady=2)
        tk.Label(row, text="音频格式", bg=CARD, fg=TEXT_DIM, font=(FONT[0], 10), width=10).pack(side="left")
        self.afmt_var = tk.StringVar(value="mp3")
        ttk.Combobox(row, textvariable=self.afmt_var,
                     values=["mp3", "aac", "flac", "wav", "ogg", "m4a", "opus"],
                     state="readonly", width=10, font=(FONT[0], 10)).pack(side="left", padx=4)

        row2 = tk.Frame(parent, bg=CARD)
        row2.pack(fill="x", pady=2)
        tk.Label(row2, text="音频码率", bg=CARD, fg=TEXT_DIM, font=(FONT[0], 10), width=10).pack(side="left")
        self.abr2_var = tk.StringVar(value="192k")
        ttk.Combobox(row2, textvariable=self.abr2_var,
                     values=["64k", "96k", "128k", "160k", "192k", "256k", "320k"],
                     state="readonly", width=10, font=(FONT[0], 10)).pack(side="left", padx=4)

        row3 = tk.Frame(parent, bg=CARD)
        row3.pack(fill="x", pady=2)
        tk.Label(row3, text="采样率", bg=CARD, fg=TEXT_DIM, font=(FONT[0], 10), width=10).pack(side="left")
        self.sr_var = tk.StringVar(value="保持原始")
        ttk.Combobox(row3, textvariable=self.sr_var,
                     values=["保持原始", "44100", "48000", "22050"],
                     state="readonly", width=10, font=(FONT[0], 10)).pack(side="left", padx=4)

        row4 = tk.Frame(parent, bg=CARD)
        row4.pack(fill="x", pady=2)
        self.stereo_var = tk.BooleanVar(value=False)
        tk.Checkbutton(row4, text="转为单声道 (Mono)", variable=self.stereo_var,
                       bg=CARD, fg=TEXT_DIM, selectcolor=CARD2, activebackground=CARD,
                       font=(FONT[0], 10)).pack(side="left", padx=4)

    def _build_conv_params(self, parent):
        row = tk.Frame(parent, bg=CARD)
        row.pack(fill="x", pady=2)
        tk.Label(row, text="目标格式", bg=CARD, fg=TEXT_DIM, font=(FONT[0], 10), width=10).pack(side="left")
        self.conv_fmt_var = tk.StringVar(value="mp4")
        ttk.Combobox(row, textvariable=self.conv_fmt_var,
                     values=["mp4", "mkv", "mov", "avi", "webm", "gif"],
                     state="readonly", width=10, font=(FONT[0], 10)).pack(side="left", padx=4)

        row2 = tk.Frame(parent, bg=CARD)
        row2.pack(fill="x", pady=2)
        self.conv_copy_var = tk.BooleanVar(value=True)
        tk.Checkbutton(row2, text="流复制 (不重编码，速度最快)", variable=self.conv_copy_var,
                       bg=CARD, fg=TEXT_DIM, selectcolor=CARD2, activebackground=CARD,
                       font=(FONT[0], 10)).pack(side="left", padx=4)

    # ── 右栏：输出/进度 ─────────────────────────
    def _build_right(self, parent):
        # 命令预览
        cmd_card = self._card(parent, "🔧  FFmpeg 命令预览")
        cmd_card.pack(fill="x", pady=(0, 8))
        self.cmd_text = tk.Text(cmd_card, bg="#0a0a14", fg=GREEN, height=3,
                                font=("Menlo", 9), bd=0, wrap="word",
                                state="disabled", insertbackground=TEXT)
        self.cmd_text.pack(fill="x", padx=2, pady=2)
        self._btn(cmd_card, "刷新预览", self._preview_cmd, ACCENT, width=10).pack(anchor="e", padx=4, pady=2)

        # 进度
        prog_card = self._card(parent, "📊  转换进度")
        prog_card.pack(fill="x", pady=(0, 8))
        self.status_lbl = tk.Label(prog_card, text="就绪", font=(FONT[0], 10),
                                   bg=CARD, fg=TEXT_DIM)
        self.status_lbl.pack(anchor="w", padx=4)
        self.progress = ttk.Progressbar(prog_card, mode="determinate", length=400)
        self.progress.pack(fill="x", padx=4, pady=4)
        row = tk.Frame(prog_card, bg=CARD)
        row.pack(fill="x", padx=4)
        self.time_lbl  = tk.Label(row, text="", font=(FONT[0], 9), bg=CARD, fg=TEXT_DIM)
        self.time_lbl.pack(side="left")
        self.speed_lbl = tk.Label(row, text="", font=(FONT[0], 9), bg=CARD, fg=BLUE)
        self.speed_lbl.pack(side="right")

        # 操作按钮
        btn_card = tk.Frame(parent, bg=BG)
        btn_card.pack(fill="x", pady=4)
        self.start_btn = self._btn(btn_card, "▶  开始转换", self._start, GREEN, width=16, big=True)
        self.start_btn.pack(side="left", padx=4)
        self.stop_btn  = self._btn(btn_card, "⏹  停止", self._stop, RED, width=8, big=True)
        self.stop_btn.pack(side="left", padx=4)
        self.stop_btn.config(state="disabled")

        # 日志
        log_card = self._card(parent, "📋  转换日志")
        log_card.pack(fill="both", expand=True, pady=(0, 8))
        self.log_text = tk.Text(log_card, bg="#060610", fg=TEXT_DIM, height=14,
                                font=("Menlo", 9), bd=0, state="disabled",
                                wrap="word", insertbackground=TEXT)
        self.log_text.pack(fill="both", expand=True, padx=2, pady=2)
        sb2 = ttk.Scrollbar(log_card, orient="vertical", command=self.log_text.yview)
        sb2.place(relx=1.0, rely=0, relheight=1.0, anchor="ne")
        self.log_text.config(yscrollcommand=sb2.set)

        # 结果汇总
        self.result_lbl = tk.Label(parent, text="", font=(FONT[0], 10, "bold"),
                                   bg=BG, fg=GREEN, wraplength=460, justify="left")
        self.result_lbl.pack(anchor="w", padx=4)

    # ── 辅助 UI 组件 ────────────────────────────
    def _card(self, parent, title):
        frame = tk.Frame(parent, bg=CARD, bd=0, relief="flat")
        tk.Label(frame, text=title, font=(FONT[0], 10, "bold"),
                 bg=CARD, fg=BLUE).pack(anchor="w", padx=8, pady=(6, 2))
        sep = tk.Frame(frame, bg=ACCENT, height=1)
        sep.pack(fill="x", padx=8, pady=(0, 4))
        return frame

    def _btn(self, parent, text, cmd, color, width=None, big=False):
        kw = dict(text=text, command=cmd, bg=color, fg="white" if color != "#444" else TEXT_DIM,
                  relief="flat", cursor="hand2", activebackground=color,
                  font=(FONT[0], 11 if big else 10, "bold" if big else "normal"),
                  padx=10, pady=6 if big else 4)
        if width:
            kw["width"] = width
        return tk.Button(parent, **kw)

    # ── 事件处理 ────────────────────────────────
    def _pick_file(self):
        path = filedialog.askopenfilename(
            title="选择视频文件",
            filetypes=[("视频文件", "*.mp4 *.mkv *.mov *.avi *.webm *.m4v *.flv *.wmv *.ts *.m2ts"),
                       ("所有文件", "*.*")]
        )
        if path:
            self.files = [path]
            self._refresh_list()
            self._load_info(path)

    def _pick_files(self):
        paths = filedialog.askopenfilenames(
            title="批量选择视频文件",
            filetypes=[("视频文件", "*.mp4 *.mkv *.mov *.avi *.webm *.m4v *.flv *.wmv *.ts *.m2ts"),
                       ("所有文件", "*.*")]
        )
        if paths:
            self.files.extend(list(paths))
            self._refresh_list()
            if self.files:
                self._load_info(self.files[0])

    def _clear_files(self):
        self.files = []
        self._refresh_list()
        self.info_lbl.config(text="（未选择文件）")

    def _refresh_list(self):
        self.file_list.delete(0, "end")
        for f in self.files:
            self.file_list.insert("end", Path(f).name)

    def _on_select(self, event):
        sel = self.file_list.curselection()
        if sel and sel[0] < len(self.files):
            self._load_info(self.files[sel[0]])

    def _load_info(self, path):
        info = get_video_info(path)
        if not info:
            self.info_lbl.config(text=f"文件: {Path(path).name}")
            return
        self.duration_sec = info.get("duration", 0)
        lines = [
            f"📄 {Path(path).name}",
            f"📦 大小: {human_size(info.get('size',0))}  ⏱ 时长: {int(self.duration_sec//60)}:{int(self.duration_sec%60):02d}",
            f"🎞 {info.get('width','?')}x{info.get('height','?')} @ {info.get('fps','?')}fps  编码: {info.get('vcodec','?')}",
            f"🔊 音频: {info.get('acodec','?')} {info.get('sample_rate','?')}Hz {info.get('channels','?')}ch  总码率: {info.get('bitrate',0)//1000}kbps",
        ]
        self.info_lbl.config(text="\n".join(lines))
        self._preview_cmd()

    def _pick_outdir(self):
        d = filedialog.askdirectory(title="选择输出目录")
        if d:
            self.out_var.set(d)

    def _mode_changed(self):
        m = self.mode.get()
        self.comp_card.pack_forget()
        self.audio_card.pack_forget()
        self.conv_card.pack_forget()
        if m == "compress":
            self.comp_card.pack(fill="x", pady=(0, 8))
        elif m == "audio":
            self.audio_card.pack(fill="x", pady=(0, 8))
        elif m == "convert":
            self.conv_card.pack(fill="x", pady=(0, 8))
        self._preview_cmd()

    def _crf_label_update(self, val):
        v = int(float(val))
        if v <= 18:   desc = "接近无损"
        elif v <= 23: desc = "高质量"
        elif v <= 28: desc = "平衡"
        elif v <= 35: desc = "低质量"
        else:         desc = "极小体积"
        self.crf_lbl.config(text=f"{v}  ({desc})")
        self._preview_cmd()

    def _apply_preset(self, event=None):
        p = self.preset_var.get()
        mapping = {
            "极小体积": (51, "libx264 (H.264)", "64k"),
            "低质量":   (35, "libx264 (H.264)", "96k"),
            "平衡":     (23, "libx264 (H.264)", "128k"),
            "高质量":   (18, "libx264 (H.264)", "192k"),
            "无损":     (0,  "libx265 (H.265/HEVC)", "320k"),
        }
        crf, codec, abr = mapping.get(p, (23, "libx264 (H.264)", "128k"))
        self.crf_var.set(crf)
        self.vcodec_var.set(codec)
        self.abr_var.set(abr)
        self._crf_label_update(crf)

    # ── 构建 FFmpeg 命令 ─────────────────────────
    def _build_cmd(self, input_path, output_path):
        m = self.mode.get()
        cmd = ["ffmpeg", "-y", "-i", input_path]

        if m == "compress":
            vcodec_str = self.vcodec_var.get().split(" ")[0]
            if self.hw_var.get() and "x264" in vcodec_str:
                vcodec_str = "h264_videotoolbox"
            elif self.hw_var.get() and "x265" in vcodec_str:
                vcodec_str = "hevc_videotoolbox"

            if vcodec_str == "copy":
                cmd += ["-c:v", "copy"]
            else:
                cmd += ["-c:v", vcodec_str]
                if "videotoolbox" not in vcodec_str:
                    crf = self.crf_var.get()
                    if "265" in vcodec_str or "hevc" in vcodec_str:
                        cmd += ["-crf", str(crf), "-preset", "medium"]
                    elif "vp9" in vcodec_str:
                        cmd += ["-cq", str(crf), "-b:v", "0"]
                    elif "av1" in vcodec_str or "libaom" in vcodec_str:
                        cmd += ["-crf", str(crf), "-b:v", "0"]
                    else:
                        cmd += ["-crf", str(crf), "-preset", "medium"]
                else:
                    cmd += ["-q:v", "65"]

            res = self.res_var.get()
            if res != "保持原始":
                wh = res.split(" ")[0]
                cmd += ["-vf", f"scale={wh}"]

            fps = self.fps_var.get()
            if fps != "保持原始":
                cmd += ["-r", fps]

            abr = self.abr_var.get()
            if abr == "copy":
                cmd += ["-c:a", "copy"]
            else:
                cmd += ["-c:a", "aac", "-b:a", abr]

        elif m == "audio":
            fmt = self.afmt_var.get()
            abr = self.abr2_var.get()
            cmd += ["-vn"]
            if fmt == "flac":
                cmd += ["-c:a", "flac"]
            elif fmt == "wav":
                cmd += ["-c:a", "pcm_s16le"]
            elif fmt == "opus":
                cmd += ["-c:a", "libopus", "-b:a", abr]
            elif fmt == "ogg":
                cmd += ["-c:a", "libvorbis", "-b:a", abr]
            else:
                cmd += ["-c:a", fmt if fmt != "mp3" else "libmp3lame", "-b:a", abr]
            sr = self.sr_var.get()
            if sr != "保持原始":
                cmd += ["-ar", sr]
            if self.stereo_var.get():
                cmd += ["-ac", "1"]

        elif m == "convert":
            if self.conv_copy_var.get():
                cmd += ["-c", "copy"]

        elif m == "scale":
            res = self.res_var.get() if self.res_var.get() != "保持原始" else "1280x720 (720p)"
            wh = res.split(" ")[0]
            cmd += ["-vf", f"scale={wh}", "-c:a", "copy"]

        cmd.append(output_path)
        return cmd

    def _get_output_path(self, input_path):
        m = self.mode.get()
        stem = Path(input_path).stem
        out_dir_str = self.out_var.get()
        if out_dir_str == "（与原文件相同目录）":
            out_dir = Path(input_path).parent
        else:
            out_dir = Path(out_dir_str)

        suffix_map = {
            "compress": f"_compressed.{self.comp_fmt_var.get()}",
            "audio":    f"_audio.{self.afmt_var.get()}",
            "convert":  f"_converted.{self.conv_fmt_var.get()}",
            "scale":    "_scaled.mp4",
        }
        suffix = suffix_map.get(m, "_out.mp4")
        return str(out_dir / (stem + suffix))

    def _preview_cmd(self):
        if not self.files:
            self._set_cmd_text("（请先选择文件）")
            return
        try:
            inp = self.files[0]
            out = self._get_output_path(inp)
            cmd = self._build_cmd(inp, out)
            self._set_cmd_text(" ".join(cmd))
        except Exception as e:
            self._set_cmd_text(f"预览错误: {e}")

    def _set_cmd_text(self, text):
        if not hasattr(self, "cmd_text"):
            return
        self.cmd_text.config(state="normal")
        self.cmd_text.delete("1.0", "end")
        self.cmd_text.insert("end", text)
        self.cmd_text.config(state="disabled")

    # ── 运行转换 ────────────────────────────────
    def _start(self):
        if not self.files:
            messagebox.showwarning("提示", "请先选择视频文件！")
            return
        if self.running:
            return
        self.running = True
        self.start_btn.config(state="disabled")
        self.stop_btn.config(state="normal")
        self.result_lbl.config(text="")
        self._log_clear()
        threading.Thread(target=self._run_batch, daemon=True).start()

    def _stop(self):
        self.running = False
        if self.proc:
            self.proc.terminate()
        self._set_status("⏹ 已停止", RED)
        self._reset_btns()

    def _run_batch(self):
        results = []
        for i, inp in enumerate(self.files):
            if not self.running:
                break
            self._set_status(f"⏳ 处理 {i+1}/{len(self.files)}: {Path(inp).name}", ORANGE)
            out = self._get_output_path(inp)
            cmd = self._build_cmd(inp, out)
            self._log(f"\n{'='*50}\n[{i+1}/{len(self.files)}] {Path(inp).name}\n命令: {' '.join(cmd)}\n")
            ok, msg = self._run_ffmpeg(cmd, inp)
            if ok:
                orig  = os.path.getsize(inp)
                final = os.path.getsize(out) if os.path.exists(out) else 0
                ratio = (1 - final / orig) * 100 if orig > 0 else 0
                results.append(f"✅ {Path(inp).name} → {human_size(final)} (压缩 {ratio:.1f}%)")
                self._log(f"完成！输出: {out}\n原始: {human_size(orig)}  输出: {human_size(final)}  压缩率: {ratio:.1f}%\n")
            else:
                results.append(f"❌ {Path(inp).name}: {msg}")
                self._log(f"失败: {msg}\n")

        self.after(0, self._finish, results)

    def _run_ffmpeg(self, cmd, input_path):
        try:
            self.proc = subprocess.Popen(
                cmd, stderr=subprocess.PIPE, universal_newlines=True,
                bufsize=1
            )
            duration = self.duration_sec or 1.0
            for line in self.proc.stderr:
                if not self.running:
                    break
                self._parse_progress(line, duration)
            self.proc.wait()
            if self.proc.returncode == 0:
                return True, ""
            return False, f"FFmpeg 返回码 {self.proc.returncode}"
        except Exception as e:
            return False, str(e)

    def _parse_progress(self, line, duration):
        # 提取时间进度
        m = re.search(r"time=(\d+):(\d+):(\d+\.?\d*)", line)
        if m:
            h, mi, s = int(m.group(1)), int(m.group(2)), float(m.group(3))
            cur = h * 3600 + mi * 60 + s
            pct = min(cur / max(duration, 1) * 100, 100)
            # 提取速度
            speed_m = re.search(r"speed=\s*(\S+)", line)
            speed = speed_m.group(1) if speed_m else ""
            bitrate_m = re.search(r"bitrate=\s*(\S+)", line)
            br = bitrate_m.group(1) if bitrate_m else ""
            self.after(0, self._update_progress, pct, cur, duration, speed, br)

    def _update_progress(self, pct, cur, total, speed, bitrate):
        self.progress["value"] = pct
        elapsed = int(cur)
        remain  = int(total - cur)
        self.time_lbl.config(
            text=f"{elapsed//60}:{elapsed%60:02d} / {int(total//60)}:{int(total%60):02d}"
        )
        self.speed_lbl.config(text=f"速度: {speed}  码率: {bitrate}  {pct:.1f}%")

    def _finish(self, results):
        self.running = False
        self.progress["value"] = 100
        self._set_status("✅ 全部完成！", GREEN)
        self.result_lbl.config(text="\n".join(results))
        self._reset_btns()
        if len(results) == 1:
            messagebox.showinfo("完成", results[0])

    def _reset_btns(self):
        self.start_btn.config(state="normal")
        self.stop_btn.config(state="disabled")

    def _set_status(self, text, color=TEXT_DIM):
        self.status_lbl.config(text=text, fg=color)

    def _log(self, text):
        def _do():
            self.log_text.config(state="normal")
            self.log_text.insert("end", text)
            self.log_text.see("end")
            self.log_text.config(state="disabled")
        self.after(0, _do)

    def _log_clear(self):
        self.log_text.config(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.config(state="disabled")


# ─────────────────────────────────────────────
if __name__ == "__main__":
    app = VideoCompressor()

    # 样式
    style = ttk.Style()
    style.theme_use("default")
    style.configure("TCombobox", fieldbackground=CARD2, background=CARD2,
                    foreground=TEXT, borderwidth=0, arrowcolor=BLUE)
    style.configure("Horizontal.TProgressbar",
                    troughcolor=CARD2, background=BLUE, thickness=14)
    style.configure("TScrollbar", background=CARD2, troughcolor=CARD2,
                    bordercolor=CARD, arrowcolor=TEXT_DIM)

    app.mainloop()
