# ⚡ 视频压缩 & 转文稿工具

本地视频处理工具，无需上传，全程在本机运行。

## 功能

| 模式 | 说明 |
|------|------|
| 🗜 压缩 | 视频压缩，支持 H.264 / H.265 / VP9 / AV1，可调质量 CRF |
| 🎵 提取音频 | 导出 MP3 / AAC / FLAC / WAV / OGG / OPUS / M4A |
| 🔄 转格式 | 视频格式互转（MP4 / MKV / MOV / WebM / AVI） |
| 📐 缩放 | 批量修改分辨率 |
| 📝 转文稿 | 使用 OpenAI Whisper 本地语音识别，支持多语言，导出 TXT / SRT |

支持**批量导入**，统一文件队列，一次导入对所有模式生效。

## 版本

- **`video_web.py`** — Web 版（推荐）：浏览器界面，本地 HTTP 服务器
- **`video_compressor.py`** — 桌面版：原生 Tkinter GUI

## 安装

```bash
# 必须：FFmpeg
brew install ffmpeg     # macOS
# 或 sudo apt install ffmpeg  # Linux

# 可选：转文稿功能需要 Whisper
pip install openai-whisper
```

## 使用

```bash
python3 video_web.py
```

自动在浏览器打开 `http://127.0.0.1:9527`，按 `Ctrl+C` 退出。

## 依赖

- Python 3.8+
- FFmpeg（系统命令行工具）
- `openai-whisper`（可选，仅转文稿功能需要）
- 无其他 Python 第三方库，使用标准库

## 转文稿说明

- 使用 Whisper 本地模型，**不联网**，隐私安全
- 批量文件**顺序处理**（模型只加载一次，节省内存）
- 支持视频和音频文件（MP4 / MKV / MOV / MP3 / WAV / AAC 等）
- 支持 SRT 字幕同步导出
