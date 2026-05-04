#!/usr/bin/env python3
"""
视频压缩工具 · Web 版（本地服务器，无需上传）
功能：视频压缩 / 格式转换 / 提取音频 / 分辨率缩放 / 转文字文稿
依赖：Python 3.8+  FFmpeg（brew install ffmpeg）
转文稿额外依赖：pip install openai-whisper
"""

import json, os, re, socket, subprocess, sys, threading, webbrowser, queue, time, tempfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

import tkinter as tk
from tkinter import filedialog

APP_HOST = "127.0.0.1"
APP_PORT = 9527
APP_URL = f"http://{APP_HOST}:{APP_PORT}"

def _get_python():
    """打包后返回系统 Python，避免子进程再次启动 .app。"""
    if getattr(sys, "frozen", False):
        import shutil
        py = shutil.which("python3") or shutil.which("python")
        if py:
            return py
        raise RuntimeError("找不到 Python 解释器，请确认系统已安装 Python 3")
    return sys.executable

def _is_server_running():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.2)
        return s.connect_ex((APP_HOST, APP_PORT)) == 0

def _open_browser_once():
    if sys.platform == "darwin":
        subprocess.Popen(["open", APP_URL])
    else:
        webbrowser.open(APP_URL)

# ── 修复 Windows 子进程 PATH（用户级环境变量不自动继承）────────────────────────
if sys.platform == "win32":
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Environment") as k:
            user_path, _ = winreg.QueryValueEx(k, "Path")
        os.environ["PATH"] = os.environ.get("PATH", "") + os.pathsep + user_path
    except Exception:
        pass

# ── 全局状态 ─────────────────────────────────────────────────────────────────
dialog_req = queue.Queue()
dialog_res = queue.Queue()

# 视频处理任务状态（批量版，与 tr_state 结构对齐）
job_state = {
    "running": False, "status": "就绪", "log": "",
    "queue": [], "total": 0, "done_count": 0, "overall_pct": 0,
    "cur_idx": -1, "cur_pct": 0, "speed": "",
    "time_cur": "", "time_total": "",
}
job_lock = threading.Lock()
current_proc = None

# 转文稿任务状态（批量版）
tr_state = {
    "running":   False,
    "status":    "就绪",
    "log":       "",
    "whisper_ok": None,
    # 批量队列
    "queue":        [],   # [{name,path,status,progress,text,segments,saved,error}]
    "total":        0,
    "done_count":   0,
    "overall_pct":  0,
    # 当前文件
    "cur_idx":   -1,
    "cur_pct":    0,
    "cur_text":  "",
    "time_cur":  "",
    "time_total":"",
}
tr_lock = threading.Lock()
tr_proc = None


# ── 工具函数 ──────────────────────────────────────────────────────────────────
def human_size(n):
    n = int(n)
    for u in ["B","KB","MB","GB"]:
        if n < 1024: return f"{n:.1f} {u}"
        n /= 1024
    return f"{n:.1f} TB"

def get_video_info(path):
    cmd = ["ffprobe","-v","quiet","-print_format","json","-show_format","-show_streams",path]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        d = json.loads(r.stdout)
        info = {}
        fmt = d.get("format",{})
        info["duration"] = float(fmt.get("duration",0))
        info["size"]     = int(fmt.get("size",0))
        info["bitrate"]  = int(fmt.get("bit_rate",0))
        for s in d.get("streams",[]):
            if s.get("codec_type") == "video":
                info["width"]  = s.get("width",0)
                info["height"] = s.get("height",0)
                info["vcodec"] = s.get("codec_name","?")
                fr = s.get("r_frame_rate","0/1").split("/")
                info["fps"] = round(int(fr[0])/max(int(fr[1]),1), 2)
            if s.get("codec_type") == "audio":
                info["acodec"]      = s.get("codec_name","?")
                info["sample_rate"] = s.get("sample_rate","?")
                info["channels"]    = s.get("channels",0)
        return info
    except Exception as e:
        return {"error": str(e)}

def check_whisper_detail():
    """检测 whisper 是否可用，返回错误原因用于界面提示。"""
    try:
        r = subprocess.run(
            [_get_python(), "-c", "import whisper; print(whisper.__version__)"],
            capture_output=True, text=True, timeout=10
        )
        if r.returncode == 0:
            return True, r.stdout.strip(), ""
        err = (r.stderr or r.stdout).strip()
        return False, "", err or f"Python 返回码 {r.returncode}"
    except Exception as e:
        return False, "", str(e)

def check_whisper():
    """兼容旧调用：只返回是否可用与版本。"""
    ok, ver, _ = check_whisper_detail()
    return ok, ver

def build_ffmpeg_cmd(params, input_path, output_path):
    mode = params.get("mode","compress")
    cmd  = ["ffmpeg","-y","-i",input_path]

    if mode == "compress":
        vcodec = params.get("vcodec","libx264").split(" ")[0]
        if params.get("hw") and "x264" in vcodec: vcodec = "h264_videotoolbox"
        if params.get("hw") and "x265" in vcodec: vcodec = "hevc_videotoolbox"
        if vcodec == "copy":
            cmd += ["-c:v","copy"]
        else:
            cmd += ["-c:v",vcodec]
            crf = params.get("crf",23)
            if "videotoolbox" not in vcodec:
                if "265" in vcodec or "hevc" in vcodec:
                    cmd += ["-crf",str(crf),"-preset","medium"]
                elif "vp9" in vcodec:
                    cmd += ["-cq",str(crf),"-b:v","0"]
                elif "av1" in vcodec or "libaom" in vcodec:
                    cmd += ["-crf",str(crf),"-b:v","0"]
                else:
                    cmd += ["-crf",str(crf),"-preset","medium"]
            else:
                cmd += ["-q:v","65"]
        res = params.get("res","")
        if res and res != "原始":
            cmd += ["-vf",f"scale={res.split()[0]}"]
        fps = params.get("fps","")
        if fps and fps != "原始": cmd += ["-r",fps]
        abr = params.get("abr","128k")
        cmd += ["-c:a","copy"] if abr=="copy" else ["-c:a","aac","-b:a",abr]
        fmt = params.get("fmt","mp4")
        output_path = str(Path(output_path).with_suffix("."+fmt))

    elif mode == "audio":
        afmt = params.get("afmt","mp3")
        abr  = params.get("abr2","192k")
        cmd += ["-vn"]
        codec_map = {"mp3":"libmp3lame","aac":"aac","flac":"flac",
                     "wav":"pcm_s16le","ogg":"libvorbis","opus":"libopus","m4a":"aac"}
        cmd += ["-c:a",codec_map.get(afmt,"libmp3lame")]
        if afmt not in ("flac","wav"): cmd += ["-b:a",abr]
        sr = params.get("sr","")
        if sr and sr != "原始": cmd += ["-ar",sr]
        if params.get("mono"): cmd += ["-ac","1"]
        output_path = str(Path(output_path).with_suffix("."+afmt))

    elif mode == "convert":
        cfmt = params.get("cfmt","mp4")
        if params.get("stream_copy"): cmd += ["-c","copy"]
        output_path = str(Path(output_path).with_suffix("."+cfmt))

    elif mode == "scale":
        res = params.get("scale_res","1280x720").split()[0]
        cmd += ["-vf",f"scale={res}","-c:a","copy"]
        output_path = str(Path(output_path).with_suffix(Path(input_path).suffix or ".mp4"))

    cmd.append(output_path)
    return cmd, output_path


# ── 视频处理任务（批量） ───────────────────────────────────────────────────────
def _run_one_ffmpeg(input_path, params, out_dir, idx, total):
    """处理单个文件，更新 job_state.queue[idx]；返回 (ok, msg)"""
    global current_proc
    mode   = params.get("mode","compress")
    suffix = {"compress":"_compressed","audio":"_audio","convert":"_converted","scale":"_scaled"}.get(mode,"_out")
    stem   = Path(input_path).stem
    out_base = str((Path(out_dir) if out_dir else Path(input_path).parent) / (stem+suffix+".tmp"))
    cmd, out_path = build_ffmpeg_cmd(params, input_path, out_base)
    out_path = out_path.replace(".tmp",""); cmd[-1] = out_path

    with job_lock:
        job_state["log"] += " ".join(cmd) + "\n\n"
        job_state["cur_idx"] = idx
        job_state["cur_pct"] = 0
        job_state["queue"][idx]["status"] = "processing"

    info     = get_video_info(input_path)
    duration = info.get("duration",0) or 1.0
    try:
        current_proc = subprocess.Popen(cmd, stderr=subprocess.PIPE, encoding="utf-8", errors="replace", bufsize=1)
        for line in current_proc.stderr:
            with job_lock:
                if not job_state["running"]: current_proc.terminate(); break
                job_state["log"] += line
            m = re.search(r"time=(\d+):(\d+):(\d+\.?\d*)", line)
            if m:
                h,mi,s = int(m.group(1)),int(m.group(2)),float(m.group(3))
                cur = h*3600+mi*60+s
                pct = min(cur/duration*100, 99)
                spd = (re.search(r"speed=\s*(\S+)",line) or type("",(),{"group":lambda *a:""})()).group(1)
                done = 0
                with job_lock: done = job_state["done_count"]
                overall = int((done + pct/100) / total * 100)
                with job_lock:
                    job_state["cur_pct"]     = pct
                    job_state["overall_pct"] = overall
                    job_state["speed"]       = spd
                    job_state["time_cur"]    = f"{int(cur//60)}:{int(cur%60):02d}"
                    job_state["time_total"]  = f"{int(duration//60)}:{int(duration%60):02d}"
                    job_state["queue"][idx]["progress"] = pct
        current_proc.wait()
        rc = current_proc.returncode
        if rc == 0:
            orig  = os.path.getsize(input_path)
            final = os.path.getsize(out_path) if os.path.exists(out_path) else 0
            ratio = (1-final/orig)*100 if orig > 0 else 0
            label = "压缩" if mode == "compress" else "体积变化"
            result = f"{human_size(orig)} → {human_size(final)}  {label} {ratio:.1f}%\n保存: {out_path}"
            with job_lock:
                job_state["queue"][idx].update({"status":"done","progress":100,"result":result})
            return True, result
        else:
            with job_lock: job_state["queue"][idx].update({"status":"error","error":f"返回码 {rc}"})
            return False, f"FFmpeg 返回码 {rc}"
    except Exception as e:
        with job_lock: job_state["queue"][idx].update({"status":"error","error":str(e)})
        return False, str(e)

def run_job_batch(files, params, out_dir):
    queue_list = [{"name":Path(p).name,"path":p,"status":"pending","progress":0,"result":"","error":""}
                  for p in files]
    with job_lock:
        job_state.update({"running":True,"status":"⏳ 处理中…","log":"",
                          "queue":queue_list,"total":len(files),
                          "done_count":0,"overall_pct":0,
                          "cur_idx":-1,"cur_pct":0,"speed":"",
                          "time_cur":"","time_total":""})
    try:
        for i, path in enumerate(files):
            with job_lock:
                if not job_state["running"]:
                    for q in job_state["queue"]:
                        if q["status"] in ("pending","processing"):
                            q["status"]="error"; q["error"]="已中止"
                    break
                job_state["status"] = f"⏳ 处理第 {i+1}/{len(files)} 个…"
            _run_one_ffmpeg(path, params, out_dir, i, len(files))
            with job_lock:
                job_state["done_count"] += 1
                job_state["overall_pct"] = int(job_state["done_count"]/len(files)*100)
        with job_lock:
            done = sum(1 for q in job_state["queue"] if q["status"]=="done")
            if done == len(files):
                job_state["status"] = f"✅ 全部完成（{done}/{len(files)} 个文件）"
            elif done:
                job_state["status"] = f"⚠️ 部分完成（{done}/{len(files)} 个文件）"
            else:
                job_state["status"] = f"❌ 全部失败（0/{len(files)} 个文件）"
            job_state["overall_pct"] = 100
    except Exception as e:
        with job_lock: job_state["status"] = f"❌ 错误：{e}"
    finally:
        with job_lock: job_state["running"] = False


# ── 转文稿任务（批量，模型只加载一次）────────────────────────────────────────
def _fmt_srt(t):
    hh=int(t//3600); mm=int((t%3600)//60); ss=int(t%60); ms=int((t-int(t))*1000)
    return f"{hh:02d}:{mm:02d}:{ss:02d},{ms:03d}"

def _save_results(input_path, text, segments, with_ts, out_dir):
    base_dir = Path(out_dir) if out_dir else Path(input_path).parent
    stem_    = Path(input_path).stem
    saved    = []
    txt_path = str(base_dir / (stem_+"_transcript.txt"))
    with open(txt_path,"w",encoding="utf-8") as f: f.write(text)
    saved.append(txt_path)
    if with_ts and segments:
        srt_path = str(base_dir / (stem_+"_transcript.srt"))
        with open(srt_path,"w",encoding="utf-8") as f:
            for i,seg in enumerate(segments,1):
                f.write(f"{i}\n{_fmt_srt(seg['start'])} --> {_fmt_srt(seg['end'])}\n{seg['text'].strip()}\n\n")
        saved.append(srt_path)
    return saved

def run_transcribe_batch(files, model_name, language, with_ts, out_dir):
    """
    顺序批量转写：单个 Whisper 子进程加载模型一次，逐文件处理。
    进度标记通过 stdout 传递，转写进度（时间戳）通过 stderr 传递。
    """
    global tr_proc

    lang_val = language if language != "auto" else "None"

    # 构建批处理 Python 脚本（模型只 load_model 一次）
    batch_script = r"""
import whisper, json, sys, subprocess, os, tempfile

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

cfg      = json.loads(sys.argv[1])
model    = whisper.load_model(cfg["model"])
lang     = cfg["language"]  # None 或具体语言代码
files    = cfg["files"]     # [{idx, path}]

print("__MODEL_LOADED__", flush=True)

for item in files:
    idx  = item["idx"]
    path = item["path"]
    print(f"__FILE_START__{idx}", flush=True)

    tmp = tempfile.mktemp(suffix=".wav")
    try:
        # 提取 16kHz mono WAV（whisper 最优输入格式）
        subprocess.run(
            ["ffmpeg","-y","-i",path,"-vn","-ar","16000","-ac","1","-c:a","pcm_s16le",tmp],
            capture_output=True, encoding="utf-8", errors="replace"
        )
        result = model.transcribe(tmp, language=lang, verbose=True, fp16=False)
        out = {
            "text":     result["text"].strip(),
            "segments": [{"start":s["start"],"end":s["end"],"text":s["text"]}
                         for s in result["segments"]]
        }
        print(f"__FILE_DONE__{idx}__" + json.dumps(out, ensure_ascii=False), flush=True)
    except Exception as e:
        print(f"__FILE_ERROR__{idx}__" + str(e), flush=True)
    finally:
        try: os.unlink(tmp)
        except: pass

print("__ALL_DONE__", flush=True)
"""

    # 初始化队列状态
    queue = [{"name": Path(p).name, "path": p, "status": "pending",
               "progress": 0, "text": "", "segments": [], "saved": [], "error": ""}
             for p in files]
    with tr_lock:
        tr_state.update({
            "running": True, "status": "⏳ 加载模型…", "log": "",
            "queue": queue, "total": len(files),
            "done_count": 0, "overall_pct": 0,
            "cur_idx": -1, "cur_pct": 0, "cur_text": "",
            "time_cur": "", "time_total": "",
        })

    # 获取各文件时长（用于进度估算）
    durations = {}
    for p in files:
        info = get_video_info(p)
        durations[p] = info.get("duration", 60) or 60

    cfg_data = json.dumps({
        "model":    model_name,
        "language": None if language == "auto" else language,
        "files":    [{"idx": i, "path": p} for i, p in enumerate(files)],
    })

    try:
        tr_proc = subprocess.Popen(
            [_get_python(), "-c", batch_script, cfg_data],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            encoding="utf-8", errors="replace", bufsize=1
        )

        cur_idx      = -1
        cur_duration = 1.0
        stdout_lines = {}  # idx -> full result line

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
                        tr_state["cur_idx"]  = idx
                        tr_state["cur_pct"]  = 0
                        tr_state["cur_text"] = ""
                        tr_state["status"]   = f"⏳ 转写第 {idx+1}/{len(files)} 个文件…"
                        tr_state["queue"][idx]["status"]   = "processing"
                        tr_state["queue"][idx]["progress"] = 0

                elif "__FILE_DONE__" in line:
                    parts = line.split("__FILE_DONE__", 1)[1].split("__", 1)
                    idx   = int(parts[0])
                    try:
                        result = json.loads(parts[1])
                        text   = result["text"]
                        segs   = result["segments"]
                        saved  = _save_results(files[idx], text, segs, with_ts, out_dir)
                        with tr_lock:
                            tr_state["queue"][idx].update({
                                "status": "done", "progress": 100,
                                "text": text, "segments": segs, "saved": saved,
                            })
                            tr_state["cur_text"] = text  # 实时视图同步最终完整文本
                            tr_state["done_count"] += 1
                            tr_state["overall_pct"] = int(tr_state["done_count"] / len(files) * 100)
                    except Exception as e:
                        with tr_lock:
                            tr_state["queue"][idx].update({"status":"error","error":str(e)})
                            tr_state["done_count"] += 1

                elif "__FILE_ERROR__" in line:
                    parts = line.split("__FILE_ERROR__", 1)[1].split("__", 1)
                    idx   = int(parts[0])
                    msg   = parts[1] if len(parts) > 1 else "未知错误"
                    with tr_lock:
                        tr_state["queue"][idx].update({"status":"error","error":msg})
                        tr_state["done_count"] += 1
                        tr_state["overall_pct"] = int(tr_state["done_count"] / len(files) * 100)

                elif line == "__ALL_DONE__":
                    with tr_lock:
                        tr_state["status"]      = f"✅ 全部完成（{len(files)} 个文件）"
                        tr_state["overall_pct"] = 100
                        tr_state["running"]     = False

        t_out = threading.Thread(target=read_stdout, daemon=True)
        t_out.start()

        # stderr：解析 Whisper verbose 时间戳（单文件进度）
        for line in tr_proc.stderr:
            with tr_lock:
                if not tr_state["running"]: tr_proc.terminate(); break
            m = re.search(r"\[(\d+):(\d+\.\d+) -->", line)
            if m and cur_idx >= 0:
                mi, s = int(m.group(1)), float(m.group(2))
                cur  = mi * 60 + s
                dur  = durations.get(files[cur_idx], 1.0)
                pct  = min(cur / dur * 100, 99)
                text_m = re.search(r"\]\s+(.+)", line)
                new_text = text_m.group(1).strip() if text_m else ""
                # 整体进度 = 已完成文件 + 当前文件进度
                done = 0
                with tr_lock: done = tr_state["done_count"]
                overall = int((done + pct / 100) / len(files) * 100)
                with tr_lock:
                    tr_state["cur_pct"]  = pct
                    tr_state["overall_pct"] = overall
                    tr_state["time_cur"] = f"{int(cur//60)}:{int(cur%60):02d}"
                    tr_state["time_total"] = f"{int(dur//60)}:{int(dur%60):02d}"
                    if cur_idx < len(tr_state["queue"]):
                        tr_state["queue"][cur_idx]["progress"] = pct
                        if new_text:
                            tr_state["cur_text"] += new_text + "\n"
                            tr_state["queue"][cur_idx]["text"] = tr_state["cur_text"]

        tr_proc.wait()
        t_out.join(timeout=5)
        if tr_proc.returncode != 0:
            err = (tr_proc.stderr.read() if tr_proc.stderr else "").strip()
            with tr_lock:
                tr_state["status"] = "❌ Whisper 启动失败"
                tr_state["log"] += (("\n" + err) if err else f"\nWhisper 子进程返回码 {tr_proc.returncode}")
                for q in tr_state["queue"]:
                    if q["status"] in ("pending", "processing"):
                        q["status"] = "error"
                        q["error"] = err or f"Whisper 子进程返回码 {tr_proc.returncode}"
                tr_state["done_count"] = sum(1 for q in tr_state["queue"] if q["status"] in ("done", "error"))
                tr_state["overall_pct"] = 100 if tr_state["queue"] else 0

    except Exception as e:
        with tr_lock:
            tr_state["status"]  = f"❌ 错误：{e}"
            tr_state["running"] = False
    finally:
        with tr_lock:
            if tr_state["running"]:
                # 标记未完成文件为中止
                for q in tr_state["queue"]:
                    if q["status"] == "processing": q["status"] = "error"; q["error"] = "已中止"
                    elif q["status"] == "pending":   q["status"] = "error"; q["error"] = "未执行"
                tr_state["running"] = False


# ── HTTP 处理器 ───────────────────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def _json(self, data, code=200):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type","application/json; charset=utf-8")
        self.send_header("Content-Length", len(body))
        self.send_header("Access-Control-Allow-Origin","*")
        self.end_headers()
        self.wfile.write(body)

    def _html(self, html):
        body = html.encode()
        self.send_response(200)
        self.send_header("Content-Type","text/html; charset=utf-8")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin","*")
        self.send_header("Access-Control-Allow-Methods","GET,POST,OPTIONS")
        self.send_header("Access-Control-Allow-Headers","Content-Type")
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        path   = parsed.path

        if path == "/":
            self._html(HTML)

        elif path == "/browse":
            qs   = parse_qs(parsed.query)
            mode = qs.get("mode",["file"])[0]   # file | files | dir
            dialog_req.put(mode)
            try:    result = dialog_res.get(timeout=60)
            except: result = ""
            if isinstance(result,(list,tuple)): self._json({"paths":list(result)})
            else: self._json({"path":result or ""})

        elif path == "/info":
            qs = parse_qs(parsed.query)
            p  = qs.get("path",[""])[0]
            self._json(get_video_info(p) if p and os.path.exists(p) else {"error":"文件不存在"})

        elif path == "/status":
            with job_lock: self._json(dict(job_state))

        elif path == "/stop":
            global current_proc
            with job_lock:
                job_state["running"] = False
                job_state["status"]  = "⏹ 已停止"
            if current_proc: current_proc.terminate()
            self._json({"ok":True})

        elif path == "/check_whisper":
            ok, ver, err = check_whisper_detail()
            with tr_lock: tr_state["whisper_ok"] = ok
            self._json({"ok":ok,"version":ver,"error":err})

        elif path == "/tr_status":
            with tr_lock: self._json(dict(tr_state))

        elif path == "/stop_tr":
            global tr_proc
            with tr_lock:
                tr_state["running"] = False
                tr_state["status"]  = "⏹ 已停止"
            if tr_proc: tr_proc.terminate()
            self._json({"ok":True})

        else:
            self.send_error(404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length",0))
        body   = self.rfile.read(length)
        try:    data = json.loads(body)
        except: self._json({"error":"无效 JSON"},400); return

        if self.path == "/convert":
            files   = data.get("files", [])
            params  = data.get("params",{})
            out_dir = data.get("out_dir","")
            # 兼容旧格式 {"input": "..."}
            if not files and data.get("input"):
                files = [data["input"]]
            bad = [f for f in files if not os.path.exists(f)]
            if bad:
                self._json({"error": f"文件不存在：{bad[0]}"},400); return
            if not files:
                self._json({"error":"请提供至少一个文件"},400); return
            with job_lock:
                if job_state["running"]:
                    self._json({"error":"已有任务在运行"},409); return
            threading.Thread(target=run_job_batch, args=(files,params,out_dir), daemon=True).start()
            self._json({"ok":True})

        elif self.path == "/transcribe":
            files   = data.get("files", [])           # 批量文件路径列表
            model   = data.get("model","medium")
            lang    = data.get("language","auto")
            with_ts = data.get("with_timestamps", True)
            out_dir = data.get("out_dir","")
            # 兼容单文件 {"input": "..."} 旧格式
            if not files and data.get("input"):
                files = [data["input"]]
            bad = [f for f in files if not os.path.exists(f)]
            if bad:
                self._json({"error": f"文件不存在：{bad[0]}"},400); return
            if not files:
                self._json({"error":"请提供至少一个文件"},400); return
            ok, ver, err = check_whisper_detail()
            if not ok:
                with tr_lock:
                    tr_state["whisper_ok"] = False
                    tr_state["running"] = False
                    tr_state["status"] = "❌ Whisper 不可用"
                    tr_state["log"] = err
                    tr_state["queue"] = []
                    tr_state["total"] = 0
                    tr_state["done_count"] = 0
                    tr_state["overall_pct"] = 0
                    tr_state["cur_idx"] = -1
                    tr_state["cur_pct"] = 0
                    tr_state["cur_text"] = ""
                self._json({"error": "Whisper 不可用", "detail": err}, 503); return
            with tr_lock:
                tr_state["whisper_ok"] = True
            with tr_lock:
                if tr_state["running"]:
                    self._json({"error":"转写任务正在运行"},409); return
            threading.Thread(target=run_transcribe_batch,
                             args=(files,model,lang,with_ts,out_dir), daemon=True).start()
            self._json({"ok":True})

        else:
            self.send_error(404)


# ── 前端 HTML ─────────────────────────────────────────────────────────────────
HTML = r"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>⚡ 视频工具</title>
<style>
:root{
  --bg:#0a0a14;--card:#12121f;--card2:#1a1a2e;
  --accent:#0f3460;--blue:#4fc3f7;--green:#69f0ae;
  --purple:#ce93d8;--orange:#ff9800;--red:#ff5252;
  --text:#e0e0e0;--dim:#666;--radius:12px;--gap:12px;
}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--text);font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;min-height:100vh}
header{background:var(--card);padding:12px 20px;display:flex;align-items:center;gap:12px;border-bottom:1px solid #1e1e3a}
header h1{font-size:1.1rem;color:var(--blue);font-weight:700}
.dot{width:9px;height:9px;border-radius:50%;background:var(--green);box-shadow:0 0 8px var(--green);animation:pulse 2s infinite;flex-shrink:0}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.4}}
.layout{display:grid;grid-template-columns:420px 1fr;gap:var(--gap);padding:var(--gap);max-width:1440px;margin:0 auto;align-items:start}
@media(max-width:920px){.layout{grid-template-columns:1fr}}
.card{background:var(--card);border-radius:var(--radius);padding:14px;border:1px solid #1a1a30;margin-bottom:var(--gap)}
.card:last-child{margin-bottom:0}
.card-title{font-size:.72rem;font-weight:700;color:var(--blue);text-transform:uppercase;letter-spacing:.08em;margin-bottom:10px;padding-bottom:6px;border-bottom:1px solid #1e1e3a;display:flex;align-items:center;gap:6px}
.card-title.purple{color:var(--purple)}
.btn{display:inline-flex;align-items:center;gap:5px;padding:6px 14px;border-radius:8px;border:none;cursor:pointer;font-size:.82rem;font-weight:600;transition:filter .15s,transform .1s;white-space:nowrap}
.btn:active{transform:scale(.97)}
.btn:hover:not(:disabled){filter:brightness(1.2)}
.btn-blue{background:var(--blue);color:#000}
.btn-green{background:var(--green);color:#000}
.btn-red{background:var(--red);color:#fff}
.btn-dim{background:#252540;color:var(--text)}
.btn-sm{padding:4px 10px;font-size:.76rem}
.btn:disabled{opacity:.38;cursor:not-allowed;filter:none;transform:none}
.btn-row{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
.row{display:flex;align-items:center;gap:10px;margin-bottom:8px}
.row label{font-size:.79rem;color:var(--dim);flex-shrink:0;width:76px}
select{background:#0d0d1f;color:var(--text);border:1px solid #2a2a40;border-radius:6px;padding:5px 8px;font-size:.82rem;flex:1;cursor:pointer}
select:focus{outline:none;border-color:var(--blue)}
input[type=range]{background:#0d0d1f;flex:1;accent-color:var(--blue)}
.rval{font-size:.79rem;color:var(--blue);width:90px;text-align:right;flex-shrink:0}
input[type=checkbox]{width:15px;height:15px;accent-color:var(--blue);cursor:pointer}
.chk{display:flex;align-items:center;gap:7px;font-size:.79rem;color:var(--dim);cursor:pointer;margin-bottom:5px}
.tabs{display:flex;gap:3px;margin-bottom:12px}
.tab{flex:1;padding:6px 4px;border-radius:7px;border:none;cursor:pointer;font-size:.77rem;font-weight:600;background:#1e1e3a;color:var(--dim);transition:all .15s;text-align:center}
.tab.active{background:var(--accent);color:var(--blue)}
.tab-panel{display:none}.tab-panel.active{display:block}
.q-list{max-height:180px;overflow-y:auto;margin-top:8px}
.q-item{display:flex;align-items:center;gap:7px;padding:5px 4px;border-bottom:1px solid #1a1a30;font-size:.78rem}
.q-item:last-child{border-bottom:none}
.q-num{color:var(--dim);font-size:.71rem;width:18px;text-align:right;flex-shrink:0}
.q-name{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-family:monospace}
.q-rm{background:none;border:none;color:var(--dim);cursor:pointer;font-size:.85rem;padding:0 3px;line-height:1}
.q-rm:hover{color:var(--red)}
.q-empty{color:var(--dim);font-size:.79rem;font-style:italic;padding:12px 0;text-align:center}
.prog-wrap{background:#0d0d1f;border-radius:100px;height:8px;overflow:hidden;margin:6px 0}
.prog-bar{height:100%;border-radius:100px;transition:width .4s;width:0}
.prog-bar.blue{background:linear-gradient(90deg,var(--blue),var(--green))}
.prog-bar.purple{background:linear-gradient(90deg,var(--purple),#f48fb1)}
.prog-meta{display:flex;justify-content:space-between;font-size:.71rem;color:var(--dim);margin-top:2px}
.out-row{display:flex;gap:6px;align-items:center}
.out-inp{flex:1;background:#0d0d1f;border:1px solid #2a2a40;border-radius:6px;padding:6px 10px;font-size:.78rem;color:var(--dim);font-family:monospace}
.out-inp:focus{outline:none;border-color:var(--blue)}
.log-box{background:#06060f;border-radius:8px;padding:9px;font-family:monospace;font-size:.71rem;color:#777;max-height:150px;overflow-y:auto;white-space:pre-wrap;word-break:break-all;line-height:1.5}
.cmd-box{background:#06060f;border-radius:8px;padding:9px;font-family:monospace;font-size:.7rem;color:#4fc3f7;white-space:pre-wrap;word-break:break-all;max-height:80px;overflow-y:auto}
.transcript-box{background:#06060f;border:1px solid #1e1e3a;border-radius:8px;padding:12px;font-size:.88rem;color:var(--text);line-height:1.8;min-height:180px;max-height:380px;overflow-y:auto;white-space:pre-wrap;word-break:break-word;resize:vertical}
.transcript-box:empty::before{content:"（转写结果将显示在这里…）";color:var(--dim);font-style:italic}
.badge{display:inline-block;padding:2px 9px;border-radius:100px;font-size:.71rem;font-weight:700}
.bg{background:#0d2d1a;color:var(--green)}
.br{background:#2d0d0d;color:var(--red)}
.bo{background:#2d1a00;color:var(--orange)}
.bp{background:#1e1030;color:var(--purple)}
.bd{background:#1e1e3a;color:var(--dim)}
.w-no{background:#1f0d0d;border:1px solid #401a1a;color:#ff8a80;border-radius:8px;padding:12px;font-size:.82rem;line-height:1.6;margin-top:10px}
.w-no code{background:#2a0d0d;padding:4px 8px;border-radius:5px;font-size:.78rem;display:block;margin-top:8px;color:#ffcc02;font-family:monospace;user-select:all}
.w-ok{background:#0d1f10;border:1px solid #1a4020;color:var(--green);border-radius:8px;padding:10px;font-size:.82rem;margin-top:10px}
.action-row{display:flex;gap:8px}
.action-row .btn{flex:1;justify-content:center;padding:9px}
::-webkit-scrollbar{width:4px;height:4px}
::-webkit-scrollbar-track{background:#0d0d1f}
::-webkit-scrollbar-thumb{background:#2a2a50;border-radius:3px}
</style>
</head>
<body>

<header>
  <div class="dot"></div>
  <h1>⚡ 视频工具</h1>
  <span style="font-size:.81rem;color:var(--dim)">本地处理 · 无需上传</span>
</header>

<div class="layout">

<!-- ══ 左栏 ══ -->
<div id="leftPanel">

  <!-- 文件队列 -->
  <div class="card">
    <div class="card-title">📂 文件队列</div>
    <div class="btn-row">
      <button class="btn btn-blue btn-sm" onclick="browseFiles()">📁 批量导入</button>
      <button class="btn btn-dim btn-sm" onclick="clearAll()">✕ 清空</button>
    </div>
    <div class="q-list" id="queueList">
      <div class="q-empty" id="queueEmpty">拖放文件或点击「批量导入」</div>
    </div>
    <div style="font-size:.71rem;color:var(--dim);margin-top:6px" id="queueStat"></div>
  </div>

  <!-- 输出目录 -->
  <div class="card">
    <div class="card-title">📁 输出目录</div>
    <div class="out-row">
      <input class="out-inp" id="outDir" placeholder="留空 = 与源文件同目录">
      <button class="btn btn-dim btn-sm" onclick="browseOutDir()">浏览</button>
    </div>
  </div>

  <!-- 模式 + 参数 -->
  <div class="card">
    <div class="card-title">⚙️ 处理模式 &amp; 参数</div>
    <div class="tabs">
      <button class="tab active" id="tab-compress"   onclick="setMode('compress')">🗜 压缩</button>
      <button class="tab"        id="tab-audio"       onclick="setMode('audio')">🎵 音频</button>
      <button class="tab"        id="tab-convert"     onclick="setMode('convert')">🔄 转格式</button>
      <button class="tab"        id="tab-scale"       onclick="setMode('scale')">📐 缩放</button>
      <button class="tab"        id="tab-transcribe"  onclick="setMode('transcribe')">📝 转文稿</button>
    </div>

    <!-- 压缩 -->
    <div class="tab-panel active" id="panel-compress">
      <div class="row"><label>视频编码</label>
        <select id="vcodec" onchange="buildPreview()">
          <option>libx264 (H.264)</option>
          <option>libx265 (H.265)</option>
          <option>libvpx-vp9 (VP9)</option>
          <option>libaom-av1 (AV1)</option>
          <option>copy (不转码)</option>
        </select>
      </div>
      <div class="row"><label>质量 CRF</label>
        <input type="range" id="crf" min="1" max="51" value="28" oninput="crfUpdate();buildPreview()">
        <span class="rval"><span id="crfVal">28</span> <small id="crfHint" style="color:var(--dim)">均衡</small></span>
      </div>
      <div class="row"><label>分辨率</label>
        <select id="res" onchange="buildPreview()">
          <option>原始</option><option>1920x1080 (1080p)</option>
          <option>1280x720 (720p)</option><option>854x480 (480p)</option><option>640x360 (360p)</option>
        </select>
      </div>
      <div class="row"><label>帧率</label>
        <select id="fps" onchange="buildPreview()">
          <option>原始</option><option>60</option><option>30</option><option>25</option><option>24</option>
        </select>
      </div>
      <div class="row"><label>音频码率</label>
        <select id="abr" onchange="buildPreview()">
          <option>320k</option><option>192k</option><option selected>128k</option><option>96k</option><option>copy</option>
        </select>
      </div>
      <div class="row"><label>输出格式</label>
        <select id="fmt" onchange="buildPreview()">
          <option selected>mp4</option><option>mkv</option><option>mov</option><option>webm</option>
        </select>
      </div>
      <label class="chk"><input type="checkbox" id="hw" onchange="buildPreview()"> 使用 VideoToolbox 硬件加速 (macOS)</label>
      <div style="margin-top:10px">
        <div style="font-size:.71rem;color:var(--dim);margin-bottom:5px">快速预设</div>
        <div class="btn-row">
          <button class="btn btn-dim btn-sm" onclick="applyPreset(18,'libx264 (H.264)','128k')">高质量</button>
          <button class="btn btn-dim btn-sm" onclick="applyPreset(28,'libx264 (H.264)','128k')">均衡</button>
          <button class="btn btn-dim btn-sm" onclick="applyPreset(38,'libx264 (H.264)','96k')">小体积</button>
        </div>
      </div>
    </div>

    <!-- 提取音频 -->
    <div class="tab-panel" id="panel-audio">
      <div class="row"><label>音频格式</label>
        <select id="afmt" onchange="buildPreview()">
          <option>mp3</option><option>aac</option><option>flac</option>
          <option>wav</option><option>ogg</option><option>opus</option><option>m4a</option>
        </select>
      </div>
      <div class="row"><label>码率</label>
        <select id="abr2" onchange="buildPreview()">
          <option>320k</option><option selected>192k</option><option>128k</option><option>96k</option>
        </select>
      </div>
      <div class="row"><label>采样率</label>
        <select id="sr" onchange="buildPreview()">
          <option>原始</option><option>48000</option><option>44100</option><option>22050</option>
        </select>
      </div>
      <label class="chk"><input type="checkbox" id="mono" onchange="buildPreview()"> 单声道</label>
    </div>

    <!-- 转格式 -->
    <div class="tab-panel" id="panel-convert">
      <div class="row"><label>目标格式</label>
        <select id="cfmt" onchange="buildPreview()">
          <option>mp4</option><option>mkv</option><option>mov</option><option>webm</option><option>avi</option>
        </select>
      </div>
      <label class="chk"><input type="checkbox" id="streamCopy" onchange="buildPreview()"> 流拷贝（不重新编码，极快）</label>
    </div>

    <!-- 缩放 -->
    <div class="tab-panel" id="panel-scale">
      <div class="row"><label>目标分辨率</label>
        <select id="scaleRes" onchange="buildPreview()">
          <option>1920x1080 (1080p)</option><option>1280x720 (720p)</option>
          <option>854x480 (480p)</option><option>640x360 (360p)</option><option>3840x2160 (4K)</option>
        </select>
      </div>
    </div>

    <!-- 转文稿 -->
    <div class="tab-panel" id="panel-transcribe">
      <div class="row"><label>Whisper 模型</label>
        <select id="trModel">
          <option value="tiny">tiny（最快，精度低）</option>
          <option value="base">base</option>
          <option value="small" selected>small（推荐）</option>
          <option value="medium">medium</option>
          <option value="large">large（最准，最慢）</option>
        </select>
      </div>
      <div class="row"><label>语言</label>
        <select id="trLang">
          <option value="auto">自动检测</option><option value="zh">中文</option>
          <option value="en">英文</option><option value="ja">日文</option>
          <option value="ko">韩文</option><option value="fr">法文</option><option value="de">德文</option>
        </select>
      </div>
      <label class="chk"><input type="checkbox" id="trWithTs" checked> 同时导出 SRT 字幕</label>
      <div id="whisperStatus" style="font-size:.79rem;color:var(--dim);margin-top:8px">正在检测 Whisper…</div>
    </div>
  </div>

  <!-- 开始/停止 -->
  <div class="card">
    <div class="action-row">
      <button class="btn btn-green" id="startBtn" onclick="startJob()">▶ 开始处理</button>
      <button class="btn btn-red"   id="stopBtn"  onclick="stopJob()" disabled>⏹ 停止</button>
    </div>
  </div>

</div><!-- /leftPanel -->

<!-- ══ 右栏 ══ -->
<div id="rightPanel">

  <!-- 视频模式右栏 -->
  <div id="videoRight">

    <div class="card">
      <div class="card-title">📋 FFmpeg 命令预览</div>
      <div class="cmd-box" id="cmdPreview">（请在左侧导入文件并配置参数）</div>
    </div>

    <div class="card">
      <div class="card-title">📊 处理进度</div>
      <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:6px">
        <span id="jobStatus" class="badge bd">就绪</span>
        <span id="jobSpeed" style="font-size:.73rem;color:var(--dim)"></span>
      </div>
      <div style="font-size:.72rem;color:var(--dim);margin-bottom:2px">总体进度</div>
      <div class="prog-wrap"><div class="prog-bar blue" id="overallBar"></div></div>
      <div class="prog-meta"><span id="overallPct">0%</span><span id="overallCount"></span></div>
      <div style="font-size:.72rem;color:var(--dim);margin:8px 0 2px">当前文件</div>
      <div class="prog-wrap"><div class="prog-bar blue" id="curBar"></div></div>
      <div class="prog-meta"><span id="curPct">0%</span><span id="curTime"></span></div>
    </div>

    <div class="card">
      <div class="card-title">📄 文件处理状态</div>
      <div id="jobQueueList" style="max-height:220px;overflow-y:auto">
        <div style="color:var(--dim);font-size:.79rem;padding:10px 0">（尚未开始）</div>
      </div>
    </div>

    <div class="card">
      <div class="card-title" style="display:flex;justify-content:space-between;align-items:center">
        <span>🖥 FFmpeg 日志</span>
        <button class="btn btn-dim btn-sm" onclick="clearLog()">清空</button>
      </div>
      <div class="log-box" id="logBox"></div>
    </div>

  </div><!-- /videoRight -->

  <!-- 转文稿右栏 -->
  <div id="trRight" style="display:none">

    <div class="card">
      <div class="card-title">📊 转写进度</div>
      <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:6px">
        <span id="trStatusBadge" class="badge bd">就绪</span>
        <span id="trTimeInfo" style="font-size:.73rem;color:var(--dim)"></span>
      </div>
      <div style="font-size:.72rem;color:var(--dim);margin-bottom:2px">总体进度</div>
      <div class="prog-wrap"><div class="prog-bar purple" id="trOverallBar"></div></div>
      <div class="prog-meta"><span id="trOverallPct">0%</span><span id="trDoneCount"></span></div>
      <div style="font-size:.72rem;color:var(--dim);margin:8px 0 2px">当前文件</div>
      <div class="prog-wrap"><div class="prog-bar purple" id="trCurBar"></div></div>
      <div class="prog-meta"><span id="trCurPct">0%</span><span id="trCurTimeInfo"></span></div>
    </div>

    <div class="card">
      <div class="card-title">📄 文件处理状态</div>
      <div id="trQueueList" style="max-height:220px;overflow-y:auto">
        <div style="color:var(--dim);font-size:.79rem;padding:10px 0">（尚未开始）</div>
      </div>
    </div>

    <div class="card">
      <div class="card-title" style="display:flex;justify-content:space-between;align-items:center">
        <span>📝 转写结果</span>
        <div class="btn-row" style="gap:6px">
          <select id="trViewSelect" style="font-size:.75rem;padding:3px 6px;width:auto;min-width:160px;flex:none" onchange="switchTrView()">
            <option value="-1">实时（当前文件）</option>
          </select>
          <button class="btn btn-dim btn-sm" onclick="copyTranscript()">📋 复制</button>
          <button class="btn btn-dim btn-sm" onclick="downloadTranscript()">⬇ 下载</button>
        </div>
      </div>
      <div class="transcript-box" id="transcriptBox"></div>
    </div>

  </div><!-- /trRight -->

</div><!-- /rightPanel -->
</div><!-- /layout -->

<script>
const API = 'http://127.0.0.1:9527'

// ── 状态 ──────────────────────────────────────────────────────
let fileQueue   = []       // [{name, path}]
let currentMode = 'compress'
let pollTimer   = null
let trViewIdx   = -1
let lastTrQueue = null
let convLastLog = ''

// ── 初始化 ────────────────────────────────────────────────────
window.addEventListener('DOMContentLoaded', () => {
  crfUpdate()
  buildPreview()
  checkWhisper()
})

// ── 文件队列 ──────────────────────────────────────────────────
async function browseFiles() {
  let r, d
  try {
    r = await fetch(`${API}/browse?mode=files`)
    d = await r.json()
  } catch(e) {
    alert('无法连接本地服务器，请确认 video_web.py 正在运行')
    return
  }
  const paths = Array.isArray(d.paths) ? d.paths : (d.path ? [d.path] : [])
  if (!paths.length) return
  let added = 0
  paths.forEach(p => {
    if (!fileQueue.find(f => f.path === p)) {
      fileQueue.push({ name: p.replace(/.*[/\\]/,''), path: p })
      added++
    }
  })
  renderQueue()
  if (currentMode !== 'transcribe') buildPreview()
}

async function browseOutDir() {
  let r, d
  try {
    r = await fetch(`${API}/browse?mode=dir`)
    d = await r.json()
  } catch(e) { return }
  if (d.path) document.getElementById('outDir').value = d.path
}

function removeFile(idx) {
  fileQueue.splice(idx, 1)
  renderQueue()
  if (currentMode !== 'transcribe') buildPreview()
}

function clearAll() {
  fileQueue = []
  renderQueue()
  if (currentMode !== 'transcribe') buildPreview()
}

function renderQueue() {
  const list  = document.getElementById('queueList')
  const stat  = document.getElementById('queueStat')
  const empty = document.getElementById('queueEmpty')
  if (!fileQueue.length) {
    list.innerHTML = ''
    list.appendChild(empty)
    empty.style.display = 'block'
    stat.textContent = ''
    return
  }
  empty.style.display = 'none'
  list.innerHTML = fileQueue.map((f,i) => `
    <div class="q-item" id="qrow${i}">
      <span class="q-num">${i+1}</span>
      <span class="q-name" title="${esc(f.path)}">${esc(f.name)}</span>
      <span class="badge bd" id="qbadge${i}">待处理</span>
      <button class="q-rm" onclick="removeFile(${i})">✕</button>
    </div>`).join('')
  stat.textContent = `共 ${fileQueue.length} 个文件`
}

// ── 模式切换 ──────────────────────────────────────────────────
function setMode(mode) {
  currentMode = mode
  document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'))
  document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'))
  document.getElementById('tab-'+mode).classList.add('active')
  document.getElementById('panel-'+mode).classList.add('active')
  const isTr = mode === 'transcribe'
  document.getElementById('videoRight').style.display = isTr ? 'none' : 'block'
  document.getElementById('trRight').style.display    = isTr ? 'block' : 'none'
  if (!isTr) buildPreview()
}

// ── 参数 & 预览 ───────────────────────────────────────────────
function crfUpdate() {
  const v = +document.getElementById('crf').value
  document.getElementById('crfVal').textContent = v
  const labels = [[10,'极高质量'],[18,'高质量'],[23,'默认'],[28,'均衡'],[35,'低质量'],[51,'最差']]
  const h = labels.find(l => v <= l[0]) || labels[labels.length-1]
  document.getElementById('crfHint').textContent = h[1]
}

function applyPreset(crf, codec, abr) {
  document.getElementById('crf').value = crf
  const sel = document.getElementById('vcodec')
  for (let o of sel.options) if (o.text === codec || o.value === codec) { sel.value = o.value; break }
  document.getElementById('abr').value = abr
  crfUpdate(); buildPreview()
}

function collectParams() {
  return {
    mode:        currentMode,
    vcodec:      document.getElementById('vcodec').value,
    crf:         +document.getElementById('crf').value,
    res:         document.getElementById('res').value,
    fps:         document.getElementById('fps').value,
    abr:         document.getElementById('abr').value,
    fmt:         document.getElementById('fmt').value,
    hw:          document.getElementById('hw').checked,
    afmt:        document.getElementById('afmt').value,
    abr2:        document.getElementById('abr2').value,
    sr:          document.getElementById('sr').value,
    mono:        document.getElementById('mono').checked,
    cfmt:        document.getElementById('cfmt').value,
    stream_copy: document.getElementById('streamCopy').checked,
    scale_res:   document.getElementById('scaleRes').value,
  }
}

function buildPreview() {
  const el = document.getElementById('cmdPreview')
  if (!el) return
  if (!fileQueue.length) { el.textContent='（请先导入文件）'; return }
  const inp  = fileQueue[0].path
  const p    = collectParams()
  const odir = document.getElementById('outDir').value.trim() || '<原文件目录>'
  const stem = inp.replace(/.*[/\\]/,'').replace(/\.[^.]+$/,'')
  const extM = {compress:p.fmt||'mp4',audio:p.afmt||'mp3',convert:p.cfmt||'mp4',scale:'mp4'}
  const sfxM = {compress:'_compressed',audio:'_audio',convert:'_converted',scale:'_scaled'}
  const out  = `${odir}/${stem}${sfxM[p.mode]||'_out'}.${extM[p.mode]||'mp4'}`
  let cmd = `ffmpeg -y -i "${inp}"`
  if (p.mode==='compress') {
    const vc = p.vcodec.split(' ')[0]
    if (vc==='copy') cmd+=' -c:v copy'
    else cmd+=` -c:v ${vc} -crf ${p.crf} -preset medium`
    if (p.res&&p.res!=='原始') cmd+=` -vf scale=${p.res.split(' ')[0]}`
    if (p.fps&&p.fps!=='原始') cmd+=` -r ${p.fps}`
    cmd += p.abr==='copy' ? ' -c:a copy' : ` -c:a aac -b:a ${p.abr}`
  } else if (p.mode==='audio') {
    cmd+=` -vn -c:a libmp3lame -b:a ${p.abr2}`
    if (p.sr&&p.sr!=='原始') cmd+=` -ar ${p.sr}`
    if (p.mono) cmd+=` -ac 1`
  } else if (p.mode==='convert') {
    if (p.stream_copy) cmd+=` -c copy`
  } else if (p.mode==='scale') {
    cmd+=` -vf scale=${p.scale_res.split(' ')[0]} -c:a copy`
  }
  cmd += ` "${out}"`
  if (fileQueue.length > 1) cmd += `\n…（批量：共 ${fileQueue.length} 个文件，以第 1 个为示例）`
  el.textContent = cmd
}

// ── Whisper 检测 ──────────────────────────────────────────────
async function checkWhisper() {
  const el = document.getElementById('whisperStatus')
  try {
    const r = await fetch(`${API}/check_whisper`)
    const d = await r.json()
    if (d.ok) {
      el.className = 'w-ok'
      el.textContent = `✅ Whisper ${d.version} 已就绪`
    } else {
      el.className = 'w-no'
      el.innerHTML = `⚠️ 未安装 Whisper<code>pip install openai-whisper</code>`
    }
  } catch(e) { el.textContent = '⚠️ 无法连接服务器' }
}

// ── 开始 / 停止 ───────────────────────────────────────────────
function startJob() {
  if (currentMode === 'transcribe') startTranscribe()
  else startConvert()
}
function stopJob() {
  if (currentMode === 'transcribe') stopTranscribe()
  else stopConvert()
}

async function startConvert() {
  if (!fileQueue.length) { alert('请先导入文件！'); return }
  convLastLog = ''
  document.getElementById('logBox').textContent = ''
  document.getElementById('jobQueueList').innerHTML = '<div style="color:var(--dim);font-size:.79rem;padding:6px 0">准备中…</div>'
  const r = await fetch(`${API}/convert`, {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({
      files:   fileQueue.map(f=>f.path),
      params:  collectParams(),
      out_dir: document.getElementById('outDir').value.trim()
    })
  }).catch(()=>null)
  if (!r) return
  const d = await r.json()
  if (d.error) { alert('错误：'+d.error); return }
  document.getElementById('startBtn').disabled = true
  document.getElementById('stopBtn').disabled  = false
  startPoll()
}
async function stopConvert() {
  await fetch(`${API}/stop`).catch(()=>{})
}

async function startTranscribe() {
  if (!fileQueue.length) { alert('请先导入文件！'); return }
  trViewIdx = -1; lastTrQueue = null
  document.getElementById('transcriptBox').textContent = ''
  document.getElementById('trQueueList').innerHTML = '<div style="color:var(--dim);font-size:.79rem;padding:6px 0">准备中…</div>'
  rebuildViewSelect([])
  const r = await fetch(`${API}/transcribe`, {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({
      files:           fileQueue.map(f=>f.path),
      model:           document.getElementById('trModel').value,
      language:        document.getElementById('trLang').value,
      with_timestamps: document.getElementById('trWithTs').checked,
      out_dir:         document.getElementById('outDir').value.trim()
    })
  }).catch(()=>null)
  if (!r) return
  const d = await r.json()
  if (d.error) { alert('错误：'+d.error); return }
  document.getElementById('startBtn').disabled = true
  document.getElementById('stopBtn').disabled  = false
  startPoll()
}
async function stopTranscribe() {
  await fetch(`${API}/stop_tr`).catch(()=>{})
}

// ── 轮询 ─────────────────────────────────────────────────────
function startPoll() {
  if (pollTimer) return
  pollTimer = setInterval(poll, 500)
}
function stopPoll() {
  if (pollTimer) { clearInterval(pollTimer); pollTimer = null }
}
async function poll() {
  if (currentMode === 'transcribe') await pollTr()
  else await pollVideo()
}

async function pollVideo() {
  let d
  try { const r = await fetch(`${API}/status`); d = await r.json() } catch(e) { return }
  renderVideoProgress(d)
  if (d.queue) d.queue.forEach((q,i) => {
    const el = document.getElementById('qbadge'+i)
    if (el) { const [cls,txt] = statusBadge(q.status); el.className='badge '+cls; el.textContent=txt }
  })
  if (!d.running) {
    document.getElementById('startBtn').disabled = false
    document.getElementById('stopBtn').disabled  = true
    stopPoll()
  }
}
async function pollTr() {
  let d
  try { const r = await fetch(`${API}/tr_status`); d = await r.json() } catch(e) { return }
  renderTrProgress(d)
  if (d.queue) {
    d.queue.forEach((q,i) => {
      const el = document.getElementById('qbadge'+i)
      if (el) { const [cls,txt] = statusBadge(q.status); el.className='badge '+cls; el.textContent=txt }
    })
    rebuildViewSelect(d.queue)
    lastTrQueue = d.queue
    const box = document.getElementById('transcriptBox')
    if (trViewIdx === -1) {
      // 实时模式：优先 cur_text，回退到当前处理文件的 text
      let txt = d.cur_text || ''
      if (!txt && d.cur_idx >= 0 && d.queue[d.cur_idx]) {
        txt = d.queue[d.cur_idx].text || ''
      }
      if (txt && box.textContent !== txt) {
        box.textContent = txt
        box.scrollTop = box.scrollHeight
      }
    } else if (trViewIdx >= 0 && d.queue[trViewIdx]) {
      box.textContent = d.queue[trViewIdx].text || ''
    }
  }
  if (!d.running) {
    document.getElementById('startBtn').disabled = false
    document.getElementById('stopBtn').disabled  = true
    stopPoll()
  }
}

// ── 渲染 ─────────────────────────────────────────────────────
function renderVideoProgress(d) {
  document.getElementById('jobStatus').textContent = d.status||'就绪'
  document.getElementById('jobStatus').className   = 'badge '+(d.running?'bo':'bd')
  document.getElementById('jobSpeed').textContent  = d.speed||''
  document.getElementById('overallBar').style.width = (d.overall_pct||0)+'%'
  document.getElementById('overallPct').textContent = (d.overall_pct||0)+'%'
  document.getElementById('overallCount').textContent = d.total ? `${d.done_count}/${d.total}` : ''
  document.getElementById('curBar').style.width = (d.cur_pct||0)+'%'
  document.getElementById('curPct').textContent = Math.round(d.cur_pct||0)+'%'
  document.getElementById('curTime').textContent = (d.time_cur&&d.time_total) ? `${d.time_cur} / ${d.time_total}` : ''
  if (d.log && d.log !== convLastLog) {
    convLastLog = d.log
    const lb = document.getElementById('logBox')
    lb.textContent = d.log
    lb.scrollTop = lb.scrollHeight
  }
  if (d.queue && d.queue.length) {
    document.getElementById('jobQueueList').innerHTML = d.queue.map((q,i) => {
      const [cls,txt] = statusBadge(q.status)
      const prog = q.status==='processing'
        ? ` <span style="color:var(--blue);font-size:.71rem">${Math.round(q.progress||0)}%</span>` : ''
      const res = q.result ? `<div style="font-size:.7rem;color:var(--green);margin-top:2px">${esc(q.result)}</div>` : ''
      const err = q.error  ? `<div style="font-size:.7rem;color:var(--red);margin-top:2px">${esc(q.error)}</div>` : ''
      return `<div style="padding:6px 8px;border-bottom:1px solid #1a1a30">
        <div style="display:flex;align-items:center;gap:7px;font-size:.79rem">
          <span style="color:var(--dim);width:18px;text-align:right;flex-shrink:0">${i+1}</span>
          <span style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-family:monospace">${esc(q.name)}</span>
          <span class="badge ${cls}">${txt}</span>${prog}
        </div>${res}${err}</div>`
    }).join('')
  }
}

function renderTrProgress(d) {
  document.getElementById('trStatusBadge').textContent = d.status||'就绪'
  document.getElementById('trStatusBadge').className   = 'badge '+(d.running?'bo':'bd')
  document.getElementById('trOverallBar').style.width  = (d.overall_pct||0)+'%'
  document.getElementById('trOverallPct').textContent  = (d.overall_pct||0)+'%'
  document.getElementById('trDoneCount').textContent   = d.total ? `${d.done_count}/${d.total}` : ''
  document.getElementById('trCurBar').style.width = (d.cur_pct||0)+'%'
  document.getElementById('trCurPct').textContent = Math.round(d.cur_pct||0)+'%'
  document.getElementById('trCurTimeInfo').textContent = (d.time_cur&&d.time_total) ? `${d.time_cur} / ${d.time_total}` : ''
  if (d.queue && d.queue.length) {
    document.getElementById('trQueueList').innerHTML = d.queue.map((q,i) => {
      const [cls,txt] = statusBadge(q.status)
      const prog = q.status==='processing'
        ? ` <span style="color:var(--purple);font-size:.71rem">${Math.round(q.progress||0)}%</span>` : ''
      const saved = q.saved&&q.saved.length
        ? `<div style="font-size:.7rem;color:var(--green);margin-top:2px">已保存: ${q.saved.map(s=>esc(s)).join(', ')}</div>` : ''
      const err = q.error ? `<div style="font-size:.7rem;color:var(--red);margin-top:2px">${esc(q.error)}</div>` : ''
      return `<div style="padding:6px 8px;border-bottom:1px solid #1a1a30">
        <div style="display:flex;align-items:center;gap:7px;font-size:.79rem">
          <span style="color:var(--dim);width:18px;text-align:right;flex-shrink:0">${i+1}</span>
          <span style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-family:monospace">${esc(q.name)}</span>
          <span class="badge ${cls}">${txt}</span>${prog}
        </div>${saved}${err}</div>`
    }).join('')
  }
}

function statusBadge(status) {
  return {
    pending:    ['bd','待处理'],
    processing: ['bo','处理中'],
    done:       ['bg','✓ 完成'],
    error:      ['br','✗ 错误'],
  }[status] || ['bd', status||'—']
}

// ── 转文稿查看器 ──────────────────────────────────────────────
function rebuildViewSelect(queue) {
  const sel = document.getElementById('trViewSelect')
  const cur = sel.value
  sel.innerHTML = '<option value="-1">实时（当前文件）</option>' +
    queue.filter(q=>q.status==='done').map(q => {
      const i = queue.indexOf(q)
      return `<option value="${i}">${i+1}. ${esc(q.name)}</option>`
    }).join('')
  if (cur && sel.querySelector(`option[value="${cur}"]`)) sel.value = cur
}
function switchTrView() {
  trViewIdx = +document.getElementById('trViewSelect').value
  if (trViewIdx >= 0 && lastTrQueue && lastTrQueue[trViewIdx])
    document.getElementById('transcriptBox').textContent = lastTrQueue[trViewIdx].text || ''
}
function copyTranscript() {
  const text = document.getElementById('transcriptBox').textContent
  if (!text) { alert('文稿为空'); return }
  navigator.clipboard.writeText(text).then(()=>alert('✅ 已复制！')).catch(()=>{
    const ta = document.createElement('textarea')
    ta.value = text; document.body.appendChild(ta)
    ta.select(); document.execCommand('copy'); document.body.removeChild(ta)
    alert('✅ 已复制！')
  })
}
function downloadTranscript() {
  const text = document.getElementById('transcriptBox').textContent
  if (!text) { alert('文稿为空'); return }
  let stem = 'transcript'
  if (trViewIdx >= 0 && lastTrQueue && lastTrQueue[trViewIdx])
    stem = lastTrQueue[trViewIdx].name.replace(/\.[^.]+$/,'')
  const a = document.createElement('a')
  a.href = URL.createObjectURL(new Blob([text],{type:'text/plain;charset=utf-8'}))
  a.download = stem + '_transcript.txt'; a.click()
}
function clearLog() {
  document.getElementById('logBox').textContent = ''
  convLastLog = ''
}
function esc(s) {
  return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
}
</script>
</body>
</html>
"""

# ── 主线程：tkinter 对话框 ────────────────────────────────────────────────────
def start_server():
    server = ThreadingHTTPServer((APP_HOST, APP_PORT), Handler)
    server.daemon_threads = True
    print(f"✅  服务器已启动：{APP_URL}")
    server.serve_forever()

def main():
    if _is_server_running():
        _open_browser_once()
        print(f"🌐  已打开已有服务：{APP_URL}")
        return

    threading.Thread(target=start_server, daemon=True).start()
    time.sleep(0.8)
    _open_browser_once()
    print("🌐  已在浏览器中打开，按 Ctrl+C 退出\n")
    print("📝  转文稿功能需要 Whisper：")
    print("    pip install openai-whisper\n")

    root = tk.Tk()
    root.withdraw()                     # 隐藏根窗口
    root.wm_attributes('-alpha', 0.0)  # 完全透明，防止 macOS 上短暂显示

    # 文件类型定义
    VIDEO_TYPES = [
        ("视频文件", "*.mp4 *.mkv *.mov *.avi *.webm *.m4v *.flv *.wmv *.ts *.m2ts"),
        ("所有文件", "*.*"),
    ]
    MEDIA_TYPES = [                     # 转文稿：视频 + 音频都支持
        ("视频/音频文件",
         "*.mp4 *.mkv *.mov *.avi *.webm *.m4v *.flv *.wmv *.ts *.m2ts "
         "*.mp3 *.wav *.aac *.m4a *.flac *.ogg *.opus"),
        ("视频文件", "*.mp4 *.mkv *.mov *.avi *.webm *.m4v *.flv *.wmv *.ts *.m2ts"),
        ("音频文件", "*.mp3 *.wav *.aac *.m4a *.flac *.ogg *.opus"),
        ("所有文件", "*.*"),
    ]

    def tk_loop():
        try:
            req = dialog_req.get_nowait()

            if req == "file":
                result = filedialog.askopenfilename(
                    parent=root,
                    title="选择视频文件",
                    filetypes=VIDEO_TYPES,
                )
                dialog_res.put(result or "")

            elif req == "files":
                result = filedialog.askopenfilenames(
                    parent=root,
                    title="批量选择文件（Cmd+点击 或 Shift+点击 可多选）",
                    filetypes=MEDIA_TYPES,
                )
                dialog_res.put(list(result))   # 总是返回 list，空选也返回 []

            elif req == "dir":
                result = filedialog.askdirectory(
                    parent=root,
                    title="选择输出目录",
                )
                dialog_res.put(result or "")

            root.withdraw()  # 确保对话框关闭后根窗口保持隐藏

        except queue.Empty:
            pass

        root.after(100, tk_loop)       # 每 100ms 检查一次，保持循环

    root.after(100, tk_loop)
    try:
        root.mainloop()
    except KeyboardInterrupt:
        print("\n👋 已退出")
        sys.exit(0)

if __name__ == "__main__":
    main()
