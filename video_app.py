#!/usr/bin/env python3
"""
视频 & 音频处理工具 · PyQt6 原生版
功能：视频压缩 / 格式转换 / 提取音频 / 分辨率缩放 / 转文字文稿
依赖：pip install PyQt6   FFmpeg（brew install ffmpeg）
转文稿额外依赖：pip install openai-whisper
"""

import json, os, re, subprocess, sys, threading, time, tempfile
from pathlib import Path


def _get_python():
    """返回真实 Python 可执行路径。
    PyInstaller 打包后 sys.executable 指向 .app 本身，
    必须找系统 Python，否则子进程会重复启动 app（fork bomb）。
    """
    if getattr(sys, 'frozen', False):
        import shutil
        py = shutil.which('python3') or shutil.which('python')
        if py is None:
            raise RuntimeError("找不到 Python 解释器，请确认系统已安装 Python 3")
        return py
    return sys.executable


# ── Windows PATH 修复 ─────────────────────────────────────────────────────────
if sys.platform == "win32":
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Environment") as k:
            user_path, _ = winreg.QueryValueEx(k, "Path")
        os.environ["PATH"] = os.environ.get("PATH", "") + os.pathsep + user_path
    except Exception:
        pass

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QTabWidget, QListWidget, QListWidgetItem, QPushButton, QLabel,
    QProgressBar, QTextEdit, QComboBox, QSlider, QCheckBox,
    QFileDialog, QSplitter, QGroupBox, QLineEdit, QFrame,
    QSizePolicy, QSpacerItem, QScrollArea,
)
from PyQt6.QtCore import Qt, QTimer, pyqtSignal, QObject
from PyQt6.QtGui import QFont, QColor, QPalette, QDragEnterEvent, QDropEvent


# ══════════════════════════════════════════════════════════════════════════════
# 后端逻辑（与 video_web.py 完全相同）
# ══════════════════════════════════════════════════════════════════════════════

def human_size(n):
    n = int(n)
    for u in ["B", "KB", "MB", "GB"]:
        if n < 1024: return f"{n:.1f} {u}"
        n /= 1024
    return f"{n:.1f} TB"


def get_video_info(path):
    cmd = ["ffprobe", "-v", "quiet", "-print_format", "json",
           "-show_format", "-show_streams", path]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        d = json.loads(r.stdout)
        info = {}
        fmt = d.get("format", {})
        info["duration"] = float(fmt.get("duration", 0))
        info["size"]     = int(fmt.get("size", 0))
        info["bitrate"]  = int(fmt.get("bit_rate", 0))
        for s in d.get("streams", []):
            if s.get("codec_type") == "video":
                info["width"]  = s.get("width", 0)
                info["height"] = s.get("height", 0)
                info["vcodec"] = s.get("codec_name", "?")
                fr = s.get("r_frame_rate", "0/1").split("/")
                info["fps"] = round(int(fr[0]) / max(int(fr[1]), 1), 2)
            if s.get("codec_type") == "audio":
                info["acodec"]      = s.get("codec_name", "?")
                info["sample_rate"] = s.get("sample_rate", "?")
                info["channels"]    = s.get("channels", 0)
        return info
    except Exception as e:
        return {"error": str(e)}


def check_whisper():
    try:
        r = subprocess.run(
            [_get_python(), "-c", "import whisper; print(whisper.__version__)"],
            capture_output=True, text=True, timeout=10
        )
        if r.returncode == 0:
            return True, r.stdout.strip()
        return False, ""
    except Exception:
        return False, ""


def build_ffmpeg_cmd(params, input_path, output_path):
    mode = params.get("mode", "compress")
    cmd  = ["ffmpeg", "-y", "-i", input_path]

    if mode == "compress":
        vcodec = params.get("vcodec", "libx264").split(" ")[0]
        if params.get("hw") and "x264" in vcodec: vcodec = "h264_videotoolbox"
        if params.get("hw") and "x265" in vcodec: vcodec = "hevc_videotoolbox"
        if vcodec == "copy":
            cmd += ["-c:v", "copy"]
        else:
            cmd += ["-c:v", vcodec]
            crf = params.get("crf", 23)
            if "videotoolbox" not in vcodec:
                if "265" in vcodec or "hevc" in vcodec:
                    cmd += ["-crf", str(crf), "-preset", "medium"]
                elif "vp9" in vcodec:
                    cmd += ["-cq", str(crf), "-b:v", "0"]
                elif "av1" in vcodec or "libaom" in vcodec:
                    cmd += ["-crf", str(crf), "-b:v", "0"]
                else:
                    cmd += ["-crf", str(crf), "-preset", "medium"]
            else:
                cmd += ["-q:v", "65"]
        res = params.get("res", "")
        if res and res != "原始": cmd += ["-vf", f"scale={res.split()[0]}"]
        fps = params.get("fps", "")
        if fps and fps != "原始": cmd += ["-r", fps]
        abr = params.get("abr", "128k")
        cmd += ["-c:a", "copy"] if abr == "copy" else ["-c:a", "aac", "-b:a", abr]
        fmt = params.get("fmt", "mp4")
        output_path = str(Path(output_path).with_suffix("." + fmt))

    elif mode == "audio":
        afmt = params.get("afmt", "mp3")
        abr  = params.get("abr2", "192k")
        cmd += ["-vn"]
        codec_map = {"mp3": "libmp3lame", "aac": "aac", "flac": "flac",
                     "wav": "pcm_s16le", "ogg": "libvorbis", "opus": "libopus", "m4a": "aac"}
        cmd += ["-c:a", codec_map.get(afmt, "libmp3lame")]
        if afmt not in ("flac", "wav"): cmd += ["-b:a", abr]
        sr = params.get("sr", "")
        if sr and sr != "原始": cmd += ["-ar", sr]
        if params.get("mono"): cmd += ["-ac", "1"]
        output_path = str(Path(output_path).with_suffix("." + afmt))

    elif mode == "convert":
        cfmt = params.get("cfmt", "mp4")
        if params.get("stream_copy"): cmd += ["-c", "copy"]
        output_path = str(Path(output_path).with_suffix("." + cfmt))

    elif mode == "scale":
        res = params.get("scale_res", "1280x720").split()[0]
        cmd += ["-vf", f"scale={res}", "-c:a", "copy"]

    cmd.append(output_path)
    return cmd, output_path


# ── 全局任务状态 ──────────────────────────────────────────────────────────────
import threading as _threading

job_state = {
    "running": False, "status": "就绪", "log": "",
    "queue": [], "total": 0, "done_count": 0, "overall_pct": 0,
    "cur_idx": -1, "cur_pct": 0, "speed": "", "time_cur": "", "time_total": "",
}
job_lock     = _threading.Lock()
current_proc = None

tr_state = {
    "running": False, "status": "就绪", "log": "", "whisper_ok": None,
    "queue": [], "total": 0, "done_count": 0, "overall_pct": 0,
    "cur_idx": -1, "cur_pct": 0, "cur_text": "", "time_cur": "", "time_total": "",
}
tr_lock  = _threading.Lock()
tr_proc  = None


def _run_one_ffmpeg(input_path, params, out_dir, idx, total):
    global current_proc
    mode    = params.get("mode", "compress")
    suffix  = {"compress": "_compressed", "audio": "_audio",
               "convert": "_converted", "scale": "_scaled"}.get(mode, "_out")
    stem    = Path(input_path).stem
    out_base = str((Path(out_dir) if out_dir else Path(input_path).parent) / (stem + suffix + ".tmp"))
    cmd, out_path = build_ffmpeg_cmd(params, input_path, out_base)
    out_path = out_path.replace(".tmp", ""); cmd[-1] = out_path

    with job_lock:
        job_state["log"] += " ".join(cmd) + "\n\n"
        job_state["cur_idx"] = idx
        job_state["cur_pct"] = 0
        job_state["queue"][idx]["status"] = "processing"

    info     = get_video_info(input_path)
    duration = info.get("duration", 0) or 1.0
    try:
        current_proc = subprocess.Popen(
            cmd, stderr=subprocess.PIPE, encoding="utf-8", errors="replace", bufsize=1)
        for line in current_proc.stderr:
            with job_lock:
                if not job_state["running"]: current_proc.terminate(); break
                job_state["log"] += line
            m = re.search(r"time=(\d+):(\d+):(\d+\.?\d*)", line)
            if m:
                h, mi, s = int(m.group(1)), int(m.group(2)), float(m.group(3))
                cur  = h * 3600 + mi * 60 + s
                pct  = min(cur / duration * 100, 99)
                spd  = (re.search(r"speed=\s*(\S+)", line) or
                        type("", (), {"group": lambda *a: ""})()).group(1)
                done = 0
                with job_lock: done = job_state["done_count"]
                overall = int((done + pct / 100) / total * 100)
                with job_lock:
                    job_state["cur_pct"]     = pct
                    job_state["overall_pct"] = overall
                    job_state["speed"]       = spd
                    job_state["time_cur"]    = f"{int(cur // 60)}:{int(cur % 60):02d}"
                    job_state["time_total"]  = f"{int(duration // 60)}:{int(duration % 60):02d}"
                    job_state["queue"][idx]["progress"] = pct
        current_proc.wait()
        rc = current_proc.returncode
        if rc == 0:
            orig  = os.path.getsize(input_path)
            final = os.path.getsize(out_path) if os.path.exists(out_path) else 0
            ratio = (1 - final / orig) * 100 if orig > 0 else 0
            result = f"{human_size(orig)} → {human_size(final)}  压缩 {ratio:.1f}%\n保存: {out_path}"
            with job_lock:
                job_state["queue"][idx].update({"status": "done", "progress": 100, "result": result})
            return True, result
        else:
            with job_lock:
                job_state["queue"][idx].update({"status": "error", "error": f"返回码 {rc}"})
            return False, f"FFmpeg 返回码 {rc}"
    except Exception as e:
        with job_lock:
            job_state["queue"][idx].update({"status": "error", "error": str(e)})
        return False, str(e)


def run_job_batch(files, params, out_dir):
    q = [{"name": Path(p).name, "path": p, "status": "pending",
          "progress": 0, "result": "", "error": ""} for p in files]
    with job_lock:
        job_state.update({"running": True, "status": "⏳ 处理中…", "log": "",
                          "queue": q, "total": len(files), "done_count": 0,
                          "overall_pct": 0, "cur_idx": -1, "cur_pct": 0,
                          "speed": "", "time_cur": "", "time_total": ""})
    try:
        for i, path in enumerate(files):
            with job_lock:
                if not job_state["running"]:
                    for item in job_state["queue"]:
                        if item["status"] in ("pending", "processing"):
                            item["status"] = "error"; item["error"] = "已中止"
                    break
                job_state["status"] = f"⏳ 处理第 {i + 1}/{len(files)} 个…"
            _run_one_ffmpeg(path, params, out_dir, i, len(files))
            with job_lock:
                job_state["done_count"] += 1
                job_state["overall_pct"] = int(job_state["done_count"] / len(files) * 100)
        with job_lock:
            done = sum(1 for item in job_state["queue"] if item["status"] == "done")
            job_state["status"] = f"✅ 完成（{done}/{len(files)} 个文件）"
            job_state["overall_pct"] = 100
    except Exception as e:
        with job_lock: job_state["status"] = f"❌ 错误：{e}"
    finally:
        with job_lock: job_state["running"] = False


def _fmt_srt(t):
    hh = int(t // 3600); mm = int((t % 3600) // 60)
    ss = int(t % 60);    ms = int((t - int(t)) * 1000)
    return f"{hh:02d}:{mm:02d}:{ss:02d},{ms:03d}"


def _save_results(input_path, text, segments, with_ts, out_dir):
    base_dir = Path(out_dir) if out_dir else Path(input_path).parent
    stem_    = Path(input_path).stem
    saved    = []
    txt_path = str(base_dir / (stem_ + "_transcript.txt"))
    with open(txt_path, "w", encoding="utf-8") as f: f.write(text)
    saved.append(txt_path)
    if with_ts and segments:
        srt_path = str(base_dir / (stem_ + "_transcript.srt"))
        with open(srt_path, "w", encoding="utf-8") as f:
            for i, seg in enumerate(segments, 1):
                f.write(f"{i}\n{_fmt_srt(seg['start'])} --> "
                        f"{_fmt_srt(seg['end'])}\n{seg['text'].strip()}\n\n")
        saved.append(srt_path)
    return saved


def run_transcribe_batch(files, model_name, language, with_ts, out_dir):
    global tr_proc

    batch_script = r"""
import whisper, json, sys, subprocess, os, tempfile
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")
cfg   = json.loads(sys.argv[1])
model = whisper.load_model(cfg["model"])
lang  = cfg["language"]
files = cfg["files"]
print("__MODEL_LOADED__", flush=True)
for item in files:
    idx  = item["idx"]
    path = item["path"]
    print(f"__FILE_START__{idx}", flush=True)
    tmp = tempfile.mktemp(suffix=".wav")
    try:
        subprocess.run(
            ["ffmpeg","-y","-i",path,"-vn","-ar","16000","-ac","1","-c:a","pcm_s16le",tmp],
            capture_output=True, encoding="utf-8", errors="replace"
        )
        result = model.transcribe(tmp, language=lang, verbose=True, fp16=False)
        out = {"text": result["text"].strip(),
               "segments": [{"start":s["start"],"end":s["end"],"text":s["text"]}
                             for s in result["segments"]]}
        print(f"__FILE_DONE__{idx}__" + json.dumps(out, ensure_ascii=False), flush=True)
    except Exception as e:
        print(f"__FILE_ERROR__{idx}__" + str(e), flush=True)
    finally:
        try: os.unlink(tmp)
        except: pass
print("__ALL_DONE__", flush=True)
"""

    q = [{"name": Path(p).name, "path": p, "status": "pending",
          "progress": 0, "text": "", "segments": [], "saved": [], "error": ""}
         for p in files]
    with tr_lock:
        tr_state.update({"running": True, "status": "⏳ 加载模型…", "log": "",
                         "queue": q, "total": len(files), "done_count": 0,
                         "overall_pct": 0, "cur_idx": -1, "cur_pct": 0,
                         "cur_text": "", "time_cur": "", "time_total": ""})

    durations = {p: (get_video_info(p).get("duration", 60) or 60) for p in files}
    cfg_data  = json.dumps({"model": model_name,
                             "language": None if language == "auto" else language,
                             "files": [{"idx": i, "path": p} for i, p in enumerate(files)]})
    try:
        tr_proc = subprocess.Popen(
            [_get_python(), "-c", batch_script, cfg_data],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            encoding="utf-8", errors="replace", bufsize=1)

        cur_idx = -1

        def read_stdout():
            nonlocal cur_idx
            for line in tr_proc.stdout:
                line = line.rstrip()
                with tr_lock: tr_state["log"] += line + "\n"
                if line == "__MODEL_LOADED__":
                    with tr_lock: tr_state["status"] = "✅ 模型已加载，开始转写…"
                elif line.startswith("__FILE_START__"):
                    idx = int(line.split("__FILE_START__")[1])
                    cur_idx = idx
                    with tr_lock:
                        tr_state["cur_idx"] = idx
                        tr_state["cur_pct"] = 0
                        tr_state["cur_text"] = ""
                        tr_state["status"]  = f"⏳ 转写第 {idx+1}/{len(files)} 个…"
                        tr_state["queue"][idx]["status"]   = "processing"
                        tr_state["queue"][idx]["progress"] = 0
                elif "__FILE_DONE__" in line:
                    parts = line.split("__FILE_DONE__", 1)[1].split("__", 1)
                    idx = int(parts[0])
                    try:
                        result = json.loads(parts[1])
                        text   = result["text"]
                        segs   = result["segments"]
                        saved  = _save_results(files[idx], text, segs, with_ts, out_dir)
                        with tr_lock:
                            tr_state["queue"][idx].update(
                                {"status": "done", "progress": 100,
                                 "text": text, "segments": segs, "saved": saved})
                            tr_state["cur_text"]    = text
                            tr_state["done_count"] += 1
                            tr_state["overall_pct"] = int(tr_state["done_count"] / len(files) * 100)
                    except Exception as e:
                        with tr_lock:
                            tr_state["queue"][idx].update({"status": "error", "error": str(e)})
                            tr_state["done_count"] += 1
                elif "__FILE_ERROR__" in line:
                    parts = line.split("__FILE_ERROR__", 1)[1].split("__", 1)
                    idx = int(parts[0])
                    msg = parts[1] if len(parts) > 1 else "未知错误"
                    with tr_lock:
                        tr_state["queue"][idx].update({"status": "error", "error": msg})
                        tr_state["done_count"] += 1
                        tr_state["overall_pct"] = int(tr_state["done_count"] / len(files) * 100)
                elif line == "__ALL_DONE__":
                    with tr_lock:
                        tr_state["status"]      = f"✅ 全部完成（{len(files)} 个文件）"
                        tr_state["overall_pct"] = 100
                        tr_state["running"]     = False

        t_out = _threading.Thread(target=read_stdout, daemon=True)
        t_out.start()

        for line in tr_proc.stderr:
            with tr_lock:
                if not tr_state["running"]: tr_proc.terminate(); break
            m = re.search(r"\[(\d+):(\d+\.\d+) -->", line)
            if m and cur_idx >= 0:
                mi, s = int(m.group(1)), float(m.group(2))
                cur  = mi * 60 + s
                dur  = durations.get(files[cur_idx], 1.0)
                pct  = min(cur / dur * 100, 99)
                text_m   = re.search(r"\]\s+(.+)", line)
                new_text = text_m.group(1).strip() if text_m else ""
                done = 0
                with tr_lock: done = tr_state["done_count"]
                overall = int((done + pct / 100) / len(files) * 100)
                with tr_lock:
                    tr_state["cur_pct"]      = pct
                    tr_state["overall_pct"]  = overall
                    tr_state["time_cur"]     = f"{int(cur // 60)}:{int(cur % 60):02d}"
                    tr_state["time_total"]   = f"{int(dur // 60)}:{int(dur % 60):02d}"
                    if cur_idx < len(tr_state["queue"]):
                        tr_state["queue"][cur_idx]["progress"] = pct
                        if new_text:
                            tr_state["cur_text"] += new_text + "\n"
                            tr_state["queue"][cur_idx]["text"] = tr_state["cur_text"]

        tr_proc.wait()
        t_out.join(timeout=5)

    except Exception as e:
        with tr_lock:
            tr_state["status"]  = f"❌ 错误：{e}"
            tr_state["running"] = False
    finally:
        with tr_lock:
            if tr_state["running"]:
                for item in tr_state["queue"]:
                    if item["status"] == "processing": item["status"] = "error"; item["error"] = "已中止"
                    elif item["status"] == "pending":  item["status"] = "error"; item["error"] = "未执行"
                tr_state["running"] = False


# ══════════════════════════════════════════════════════════════════════════════
# PyQt6 界面
# ══════════════════════════════════════════════════════════════════════════════

STYLE = """
QMainWindow, QWidget {
    background-color: #0a0a14;
    color: #e0e0e0;
    font-family: -apple-system, "SF Pro Text", "Segoe UI", sans-serif;
    font-size: 13px;
}
QTabWidget::pane {
    border: 1px solid #1e1e3a;
    border-radius: 8px;
    background: #12121f;
}
QTabBar::tab {
    background: #1e1e3a;
    color: #666;
    padding: 7px 14px;
    margin-right: 3px;
    border-radius: 6px 6px 0 0;
    font-size: 12px;
    font-weight: 600;
}
QTabBar::tab:selected {
    background: #0f3460;
    color: #4fc3f7;
}
QTabBar::tab:hover:!selected {
    background: #252540;
    color: #aaa;
}
QPushButton {
    background: #252540;
    color: #e0e0e0;
    border: none;
    border-radius: 7px;
    padding: 7px 16px;
    font-size: 12px;
    font-weight: 600;
}
QPushButton:hover { background: #2e2e50; }
QPushButton:pressed { background: #1a1a30; }
QPushButton:disabled { color: #444; background: #16161e; }
QPushButton#btn_start {
    background: #4fc3f7;
    color: #000;
    padding: 8px 24px;
    font-size: 13px;
}
QPushButton#btn_start:hover { background: #81d4fa; }
QPushButton#btn_start:disabled { background: #1a3040; color: #444; }
QPushButton#btn_stop {
    background: #ff5252;
    color: #fff;
    padding: 8px 20px;
}
QPushButton#btn_stop:hover { background: #ff6e6e; }
QPushButton#btn_stop:disabled { background: #2d1010; color: #444; }
QPushButton#btn_add {
    background: #1a4a2a;
    color: #69f0ae;
    font-size: 12px;
}
QPushButton#btn_add:hover { background: #1e5530; }
QPushButton#btn_clear {
    background: #2d1010;
    color: #ff5252;
    font-size: 12px;
}
QListWidget {
    background: #06060f;
    border: 1px solid #1e1e3a;
    border-radius: 8px;
    color: #c0c0c0;
    font-size: 12px;
    outline: none;
}
QListWidget::item {
    padding: 5px 8px;
    border-bottom: 1px solid #12121f;
}
QListWidget::item:selected {
    background: #0f3460;
    color: #4fc3f7;
}
QListWidget::item:hover:!selected {
    background: #1a1a30;
}
QComboBox {
    background: #0d0d1f;
    color: #e0e0e0;
    border: 1px solid #2a2a40;
    border-radius: 6px;
    padding: 5px 10px;
    font-size: 12px;
    min-width: 100px;
}
QComboBox::drop-down { border: none; }
QComboBox:focus { border-color: #4fc3f7; }
QComboBox QAbstractItemView {
    background: #12121f;
    border: 1px solid #2a2a40;
    selection-background-color: #0f3460;
    color: #e0e0e0;
}
QSlider::groove:horizontal {
    background: #1e1e3a;
    height: 6px;
    border-radius: 3px;
}
QSlider::handle:horizontal {
    background: #4fc3f7;
    width: 14px;
    height: 14px;
    margin: -4px 0;
    border-radius: 7px;
}
QSlider::sub-page:horizontal {
    background: #4fc3f7;
    border-radius: 3px;
}
QProgressBar {
    background: #0d0d1f;
    border: none;
    border-radius: 4px;
    height: 8px;
    text-align: center;
    font-size: 10px;
    color: transparent;
}
QProgressBar::chunk {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                                stop:0 #4fc3f7, stop:1 #69f0ae);
    border-radius: 4px;
}
QTextEdit {
    background: #06060f;
    border: 1px solid #1e1e3a;
    border-radius: 8px;
    color: #888;
    font-family: "SF Mono", "Consolas", monospace;
    font-size: 11px;
    padding: 8px;
}
QLineEdit {
    background: #0d0d1f;
    border: 1px solid #2a2a40;
    border-radius: 6px;
    color: #888;
    padding: 5px 10px;
    font-family: "SF Mono", monospace;
    font-size: 12px;
}
QLineEdit:focus { border-color: #4fc3f7; color: #ccc; }
QCheckBox { color: #888; font-size: 12px; spacing: 6px; }
QCheckBox::indicator {
    width: 15px; height: 15px;
    background: #0d0d1f;
    border: 1px solid #2a2a40;
    border-radius: 4px;
}
QCheckBox::indicator:checked {
    background: #4fc3f7;
    border-color: #4fc3f7;
    image: none;
}
QLabel { color: #888; font-size: 12px; }
QLabel#title_label { color: #4fc3f7; font-size: 15px; font-weight: 700; }
QLabel#status_label { color: #69f0ae; font-size: 12px; }
QSplitter::handle { background: #1e1e3a; width: 1px; }
QScrollBar:vertical {
    background: #06060f;
    width: 6px;
    border-radius: 3px;
}
QScrollBar::handle:vertical {
    background: #2a2a50;
    border-radius: 3px;
    min-height: 20px;
}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
"""


def make_label(text, dim=True):
    l = QLabel(text)
    if dim:
        l.setStyleSheet("color: #666; font-size: 11px;")
    return l


def make_row(*widgets, spacing=8):
    w = QWidget()
    h = QHBoxLayout(w)
    h.setContentsMargins(0, 0, 0, 0)
    h.setSpacing(spacing)
    for item in widgets:
        if isinstance(item, int):
            h.addSpacing(item)
        elif item == "stretch":
            h.addStretch()
        else:
            h.addWidget(item)
    return w


class FileListWidget(QListWidget):
    """支持拖放文件的队列列表"""
    files_dropped = pyqtSignal(list)

    def __init__(self):
        super().__init__()
        self.setAcceptDrops(True)
        self.setDragDropMode(QListWidget.DragDropMode.DropOnly)

    def dragEnterEvent(self, e: QDragEnterEvent):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dragMoveEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e: QDropEvent):
        paths = [u.toLocalFile() for u in e.mimeData().urls()]
        self.files_dropped.emit(paths)
        e.acceptProposedAction()


class CompressTab(QWidget):
    def __init__(self):
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setSpacing(10)
        layout.setContentsMargins(12, 12, 12, 12)

        # 编码器
        row1 = QWidget(); h1 = QHBoxLayout(row1); h1.setContentsMargins(0,0,0,0)
        h1.addWidget(make_label("编码器")); self.vcodec = QComboBox()
        self.vcodec.addItems(["libx264 (H.264)", "libx265 (H.265)", "libvp9 (VP9)",
                               "libaom-av1 (AV1)", "copy (不重编)"])
        h1.addWidget(self.vcodec); layout.addWidget(row1)

        # 质量
        row2 = QWidget(); h2 = QHBoxLayout(row2); h2.setContentsMargins(0,0,0,0)
        h2.addWidget(make_label("质量 CRF"))
        self.crf_slider = QSlider(Qt.Orientation.Horizontal)
        self.crf_slider.setRange(10, 51); self.crf_slider.setValue(23)
        self.crf_val = QLabel("23"); self.crf_val.setStyleSheet("color:#4fc3f7; min-width:24px;")
        self.crf_slider.valueChanged.connect(lambda v: self.crf_val.setText(str(v)))
        h2.addWidget(self.crf_slider); h2.addWidget(self.crf_val); layout.addWidget(row2)

        # 分辨率 / 帧率
        row3 = QWidget(); h3 = QHBoxLayout(row3); h3.setContentsMargins(0,0,0,0)
        h3.addWidget(make_label("分辨率")); self.res = QComboBox()
        self.res.addItems(["原始", "3840x2160 (4K)", "2560x1440 (2K)",
                           "1920x1080 (1080p)", "1280x720 (720p)",
                           "854x480 (480p)", "640x360 (360p)"])
        h3.addWidget(self.res)
        h3.addSpacing(12)
        h3.addWidget(make_label("帧率")); self.fps = QComboBox()
        self.fps.addItems(["原始", "60", "30", "25", "24"])
        h3.addWidget(self.fps); layout.addWidget(row3)

        # 音频码率
        row4 = QWidget(); h4 = QHBoxLayout(row4); h4.setContentsMargins(0,0,0,0)
        h4.addWidget(make_label("音频码率")); self.abr = QComboBox()
        self.abr.addItems(["copy", "320k", "256k", "192k", "128k", "96k", "64k"])
        self.abr.setCurrentIndex(4)
        h4.addWidget(self.abr)
        h4.addSpacing(12)
        h4.addWidget(make_label("格式")); self.fmt = QComboBox()
        self.fmt.addItems(["mp4", "mkv", "mov", "webm", "avi"])
        h4.addWidget(self.fmt); layout.addWidget(row4)

        self.hw = QCheckBox("使用硬件加速 (VideoToolbox, macOS)")
        layout.addWidget(self.hw)
        layout.addStretch()

    def get_params(self):
        return {
            "mode": "compress",
            "vcodec": self.vcodec.currentText().split(" ")[0],
            "crf": self.crf_slider.value(),
            "res": self.res.currentText(),
            "fps": self.fps.currentText(),
            "abr": self.abr.currentText(),
            "fmt": self.fmt.currentText(),
            "hw":  self.hw.isChecked(),
        }


class AudioTab(QWidget):
    def __init__(self):
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setSpacing(10)
        layout.setContentsMargins(12, 12, 12, 12)

        row1 = QWidget(); h1 = QHBoxLayout(row1); h1.setContentsMargins(0,0,0,0)
        h1.addWidget(make_label("输出格式")); self.afmt = QComboBox()
        self.afmt.addItems(["mp3", "aac", "flac", "wav", "ogg", "opus", "m4a"])
        h1.addWidget(self.afmt); layout.addWidget(row1)

        row2 = QWidget(); h2 = QHBoxLayout(row2); h2.setContentsMargins(0,0,0,0)
        h2.addWidget(make_label("码率")); self.abr2 = QComboBox()
        self.abr2.addItems(["320k", "256k", "192k", "128k", "96k", "64k"])
        self.abr2.setCurrentIndex(2)
        h2.addWidget(self.abr2); layout.addWidget(row2)

        row3 = QWidget(); h3 = QHBoxLayout(row3); h3.setContentsMargins(0,0,0,0)
        h3.addWidget(make_label("采样率")); self.sr = QComboBox()
        self.sr.addItems(["原始", "48000", "44100", "22050", "16000"])
        h3.addWidget(self.sr); layout.addWidget(row3)

        self.mono = QCheckBox("转为单声道")
        layout.addWidget(self.mono)
        layout.addStretch()

    def get_params(self):
        return {
            "mode": "audio",
            "afmt": self.afmt.currentText(),
            "abr2": self.abr2.currentText(),
            "sr":   self.sr.currentText(),
            "mono": self.mono.isChecked(),
        }


class ConvertTab(QWidget):
    def __init__(self):
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setSpacing(10)
        layout.setContentsMargins(12, 12, 12, 12)

        row1 = QWidget(); h1 = QHBoxLayout(row1); h1.setContentsMargins(0,0,0,0)
        h1.addWidget(make_label("目标格式")); self.cfmt = QComboBox()
        self.cfmt.addItems(["mp4", "mkv", "mov", "webm", "avi"])
        h1.addWidget(self.cfmt); layout.addWidget(row1)

        self.stream_copy = QCheckBox("直接复制流（不重编码，速度快）")
        self.stream_copy.setChecked(True)
        layout.addWidget(self.stream_copy)
        layout.addStretch()

    def get_params(self):
        return {
            "mode": "convert",
            "cfmt": self.cfmt.currentText(),
            "stream_copy": self.stream_copy.isChecked(),
        }


class ScaleTab(QWidget):
    def __init__(self):
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setSpacing(10)
        layout.setContentsMargins(12, 12, 12, 12)

        row1 = QWidget(); h1 = QHBoxLayout(row1); h1.setContentsMargins(0,0,0,0)
        h1.addWidget(make_label("目标分辨率")); self.scale_res = QComboBox()
        self.scale_res.addItems(["3840x2160 (4K)", "2560x1440 (2K)",
                                  "1920x1080 (1080p)", "1280x720 (720p)",
                                  "854x480 (480p)", "640x360 (360p)"])
        self.scale_res.setCurrentIndex(2)
        h1.addWidget(self.scale_res); layout.addWidget(row1)
        layout.addStretch()

    def get_params(self):
        return {"mode": "scale", "scale_res": self.scale_res.currentText()}


class TranscribeTab(QWidget):
    def __init__(self):
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setSpacing(10)
        layout.setContentsMargins(12, 12, 12, 12)

        # Whisper 状态
        self.whisper_label = QLabel("⏳ 检测 Whisper…")
        self.whisper_label.setStyleSheet("color:#666; font-size:12px;")
        layout.addWidget(self.whisper_label)

        row1 = QWidget(); h1 = QHBoxLayout(row1); h1.setContentsMargins(0,0,0,0)
        h1.addWidget(make_label("模型")); self.model = QComboBox()
        self.model.addItems(["tiny", "base", "small", "medium", "large"])
        self.model.setCurrentIndex(3)
        h1.addWidget(self.model); layout.addWidget(row1)

        row2 = QWidget(); h2 = QHBoxLayout(row2); h2.setContentsMargins(0,0,0,0)
        h2.addWidget(make_label("语言")); self.lang = QComboBox()
        self.lang.addItems(["auto", "zh", "en", "ja", "ko"])
        h2.addWidget(self.lang); layout.addWidget(row2)

        self.with_ts = QCheckBox("同时生成 SRT 字幕文件")
        self.with_ts.setChecked(True)
        layout.addWidget(self.with_ts)
        layout.addStretch()

        # 转写结果文本框
        self.transcript = QTextEdit()
        self.transcript.setPlaceholderText("转写结果将显示在这里…")
        self.transcript.setReadOnly(True)
        self.transcript.setMinimumHeight(160)
        self.transcript.setStyleSheet(
            "color: #c0c0c0; background: #06060f; font-size: 13px; line-height: 1.8;")
        layout.addWidget(self.transcript)

    def get_params(self):
        return {
            "model":    self.model.currentText(),
            "language": self.lang.currentText(),
            "with_ts":  self.with_ts.isChecked(),
        }

    def set_whisper_status(self, ok, ver=""):
        if ok:
            self.whisper_label.setText(f"✅ Whisper {ver} 可用")
            self.whisper_label.setStyleSheet("color:#69f0ae; font-size:12px;")
        else:
            self.whisper_label.setText("❌ Whisper 未安装  →  pip install openai-whisper")
            self.whisper_label.setStyleSheet("color:#ff5252; font-size:12px;")


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("⚡ 视频工具")
        self.setMinimumSize(960, 620)
        self.resize(1100, 680)

        self._files: list[str] = []
        self._is_transcribe = False

        self._build_ui()
        self._start_timer()
        self._check_whisper_async()

    # ── 构建界面 ───────────────────────────────────────────────────────────────
    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # 标题栏
        header = QWidget()
        header.setStyleSheet("background:#12121f; border-bottom:1px solid #1e1e3a;")
        header.setFixedHeight(46)
        hh = QHBoxLayout(header)
        hh.setContentsMargins(16, 0, 16, 0)
        dot = QLabel("●")
        dot.setStyleSheet("color:#69f0ae; font-size:10px;")
        title = QLabel("⚡ 视频工具")
        title.setObjectName("title_label")
        sub = QLabel("本地处理 · 无需上传")
        sub.setStyleSheet("color:#444; font-size:12px;")
        hh.addWidget(dot); hh.addWidget(title); hh.addSpacing(12)
        hh.addWidget(sub); hh.addStretch()
        root.addWidget(header)

        # 主体
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setStyleSheet("QSplitter::handle{background:#1e1e3a;width:1px;}")
        root.addWidget(splitter)

        # ── 左栏：文件队列 ──
        left = QWidget()
        left.setFixedWidth(280)
        left.setStyleSheet("background:#0d0d1a;")
        lv = QVBoxLayout(left)
        lv.setContentsMargins(12, 12, 12, 12)
        lv.setSpacing(8)

        ql = QLabel("📂  文件队列")
        ql.setStyleSheet("color:#4fc3f7; font-size:12px; font-weight:700;")
        lv.addWidget(ql)

        self.file_list = FileListWidget()
        self.file_list.files_dropped.connect(self._add_files)
        self.file_list.setMinimumHeight(200)
        lv.addWidget(self.file_list)

        btn_add_f = QPushButton("＋ 添加文件"); btn_add_f.setObjectName("btn_add")
        btn_add_d = QPushButton("＋ 添加文件夹"); btn_add_d.setObjectName("btn_add")
        btn_clear = QPushButton("清空列表"); btn_clear.setObjectName("btn_clear")
        btn_add_f.clicked.connect(self._browse_files)
        btn_add_d.clicked.connect(self._browse_folder)
        btn_clear.clicked.connect(self._clear_files)
        lv.addWidget(btn_add_f); lv.addWidget(btn_add_d); lv.addWidget(btn_clear)

        self.file_count_label = QLabel("0 个文件")
        self.file_count_label.setStyleSheet("color:#444; font-size:11px; text-align:center;")
        self.file_count_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lv.addWidget(self.file_count_label)

        splitter.addWidget(left)

        # ── 右栏：设置 + 进度 + 日志 ──
        right = QWidget()
        rv = QVBoxLayout(right)
        rv.setContentsMargins(16, 12, 16, 12)
        rv.setSpacing(10)

        # 选项卡
        self.tabs = QTabWidget()
        self.tab_compress  = CompressTab()
        self.tab_audio     = AudioTab()
        self.tab_convert   = ConvertTab()
        self.tab_scale     = ScaleTab()
        self.tab_transcribe = TranscribeTab()
        self.tabs.addTab(self.tab_compress,  "🗜 压缩")
        self.tabs.addTab(self.tab_audio,     "🎵 提取音频")
        self.tabs.addTab(self.tab_convert,   "🔄 转格式")
        self.tabs.addTab(self.tab_scale,     "📐 缩放")
        self.tabs.addTab(self.tab_transcribe,"📝 转文稿")
        self.tabs.setMinimumHeight(220)
        rv.addWidget(self.tabs)

        # 输出目录
        out_row = QWidget(); oh = QHBoxLayout(out_row)
        oh.setContentsMargins(0, 0, 0, 0); oh.setSpacing(6)
        oh.addWidget(make_label("输出目录"))
        self.out_dir = QLineEdit(); self.out_dir.setPlaceholderText("默认：与源文件相同目录")
        btn_out = QPushButton("选择"); btn_out.setFixedWidth(52)
        btn_out.clicked.connect(self._browse_outdir)
        oh.addWidget(self.out_dir); oh.addWidget(btn_out)
        rv.addWidget(out_row)

        # 操作按钮
        act_row = QWidget(); ah = QHBoxLayout(act_row)
        ah.setContentsMargins(0, 0, 0, 0); ah.setSpacing(8)
        self.btn_start = QPushButton("▶  开始"); self.btn_start.setObjectName("btn_start")
        self.btn_stop  = QPushButton("⏹  停止"); self.btn_stop.setObjectName("btn_stop")
        self.btn_stop.setEnabled(False)
        self.btn_start.clicked.connect(self._start)
        self.btn_stop.clicked.connect(self._stop)
        ah.addWidget(self.btn_start); ah.addWidget(self.btn_stop); ah.addStretch()
        rv.addWidget(act_row)

        # 状态 + 进度
        self.status_label = QLabel("就绪")
        self.status_label.setObjectName("status_label")
        rv.addWidget(self.status_label)

        prog_widget = QWidget(); pv = QVBoxLayout(prog_widget)
        pv.setContentsMargins(0, 0, 0, 0); pv.setSpacing(4)
        self.prog_overall = QProgressBar(); self.prog_overall.setMaximum(100)
        self.prog_current = QProgressBar(); self.prog_current.setMaximum(100)
        self.prog_current.setStyleSheet(
            "QProgressBar::chunk { background: qlineargradient(x1:0,y1:0,x2:1,y2:0,"
            "stop:0 #ce93d8, stop:1 #f48fb1); border-radius:4px; }")
        meta_row = QWidget(); mh = QHBoxLayout(meta_row)
        mh.setContentsMargins(0, 0, 0, 0)
        self.prog_meta_l = QLabel("整体")
        self.prog_meta_l.setStyleSheet("color:#444; font-size:11px;")
        self.prog_meta_r = QLabel("")
        self.prog_meta_r.setStyleSheet("color:#444; font-size:11px;")
        self.prog_meta_r.setAlignment(Qt.AlignmentFlag.AlignRight)
        mh.addWidget(self.prog_meta_l); mh.addWidget(self.prog_meta_r)
        pv.addWidget(meta_row)
        pv.addWidget(self.prog_overall)
        pv.addWidget(self.prog_current)
        rv.addWidget(prog_widget)

        # 日志
        log_label = QLabel("日志")
        log_label.setStyleSheet("color:#333; font-size:11px;")
        rv.addWidget(log_label)
        self.log_box = QTextEdit()
        self.log_box.setReadOnly(True)
        self.log_box.setMinimumHeight(100)
        self.log_box.setMaximumHeight(180)
        rv.addWidget(self.log_box)

        splitter.addWidget(right)
        splitter.setSizes([280, 820])

    # ── 文件操作 ───────────────────────────────────────────────────────────────
    def _browse_files(self):
        paths, _ = QFileDialog.getOpenFileNames(
            self, "选择文件", "",
            "视频/音频文件 (*.mp4 *.mkv *.mov *.avi *.webm *.m4v *.flv "
            "*.wmv *.ts *.mp3 *.wav *.aac *.m4a *.flac *.ogg *.opus);;"
            "所有文件 (*.*)"
        )
        self._add_files(paths)

    def _browse_folder(self):
        path = QFileDialog.getExistingDirectory(self, "选择文件夹")
        if not path: return
        exts = {".mp4",".mkv",".mov",".avi",".webm",".m4v",".flv",".wmv",".ts",
                ".mp3",".wav",".aac",".m4a",".flac",".ogg",".opus"}
        found = [str(p) for p in Path(path).rglob("*") if p.suffix.lower() in exts]
        self._add_files(found)

    def _add_files(self, paths):
        for p in paths:
            if p and os.path.isfile(p) and p not in self._files:
                self._files.append(p)
                item = QListWidgetItem(Path(p).name)
                item.setToolTip(p)
                self.file_list.addItem(item)
        self.file_count_label.setText(f"{len(self._files)} 个文件")

    def _clear_files(self):
        self._files.clear()
        self.file_list.clear()
        self.file_count_label.setText("0 个文件")

    def _browse_outdir(self):
        path = QFileDialog.getExistingDirectory(self, "选择输出目录")
        if path: self.out_dir.setText(path)

    # ── 任务控制 ───────────────────────────────────────────────────────────────
    def _start(self):
        if not self._files:
            self.status_label.setText("❌ 请先添加文件")
            self.status_label.setStyleSheet("color:#ff5252; font-size:12px;")
            return

        out_dir = self.out_dir.text().strip()
        tab_idx = self.tabs.currentIndex()
        self._is_transcribe = (tab_idx == 4)

        self.btn_start.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self.log_box.clear()
        if self._is_transcribe:
            self.tab_transcribe.transcript.clear()

        if self._is_transcribe:
            p = self.tab_transcribe.get_params()
            _threading.Thread(
                target=run_transcribe_batch,
                args=(list(self._files), p["model"], p["language"], p["with_ts"], out_dir),
                daemon=True
            ).start()
        else:
            tabs = [self.tab_compress, self.tab_audio, self.tab_convert, self.tab_scale]
            params = tabs[tab_idx].get_params()
            _threading.Thread(
                target=run_job_batch,
                args=(list(self._files), params, out_dir),
                daemon=True
            ).start()

    def _stop(self):
        global current_proc, tr_proc
        if self._is_transcribe:
            with tr_lock:
                tr_state["running"] = False
            if tr_proc: tr_proc.terminate()
        else:
            with job_lock:
                job_state["running"] = False
            if current_proc: current_proc.terminate()

    # ── Whisper 异步检测 ───────────────────────────────────────────────────────
    def _check_whisper_async(self):
        def _check():
            ok, ver = check_whisper()
            # 通过 QTimer 回到主线程更新 UI
            QTimer.singleShot(0, lambda: self.tab_transcribe.set_whisper_status(ok, ver))
        _threading.Thread(target=_check, daemon=True).start()

    # ── 定时器：轮询任务状态刷新 UI ──────────────────────────────────────────
    def _start_timer(self):
        self._timer = QTimer()
        self._timer.setInterval(200)
        self._timer.timeout.connect(self._refresh)
        self._timer.start()

    def _refresh(self):
        if self._is_transcribe:
            with tr_lock:
                state   = dict(tr_state)
            running     = state["running"]
            status      = state["status"]
            overall_pct = state["overall_pct"]
            cur_pct     = state["cur_pct"]
            time_cur    = state["time_cur"]
            time_total  = state["time_total"]
            log_text    = state["log"]
            cur_text    = state["cur_text"]

            self.status_label.setText(status)
            self.prog_overall.setValue(int(overall_pct))
            self.prog_current.setValue(int(cur_pct))
            if time_cur and time_total:
                self.prog_meta_r.setText(f"{time_cur} / {time_total}")
            # 更新转写文本框
            if cur_text:
                self.tab_transcribe.transcript.setPlainText(cur_text)
                sb = self.tab_transcribe.transcript.verticalScrollBar()
                sb.setValue(sb.maximum())
        else:
            with job_lock:
                state   = dict(job_state)
            running     = state["running"]
            status      = state["status"]
            overall_pct = state["overall_pct"]
            cur_pct     = state["cur_pct"]
            speed       = state["speed"]
            time_cur    = state["time_cur"]
            time_total  = state["time_total"]
            log_text    = state["log"]

            self.status_label.setText(status)
            self.prog_overall.setValue(int(overall_pct))
            self.prog_current.setValue(int(cur_pct))
            meta = ""
            if time_cur and time_total: meta += f"{time_cur} / {time_total}"
            if speed: meta += f"  {speed}"
            self.prog_meta_r.setText(meta)

        # 日志
        if log_text:
            cur = self.log_box.toPlainText()
            if log_text != cur:
                self.log_box.setPlainText(log_text)
                sb = self.log_box.verticalScrollBar()
                sb.setValue(sb.maximum())

        # 按钮状态
        if not running and (self.btn_start.isEnabled() is False):
            self.btn_start.setEnabled(True)
            self.btn_stop.setEnabled(False)
            color = "#ff5252" if "❌" in status else "#69f0ae"
            self.status_label.setStyleSheet(f"color:{color}; font-size:12px;")


# ══════════════════════════════════════════════════════════════════════════════
# 入口
# ══════════════════════════════════════════════════════════════════════════════

def main():
    app = QApplication(sys.argv)
    app.setStyleSheet(STYLE)

    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
