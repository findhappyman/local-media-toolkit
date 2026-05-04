# ⚡ 视频 & 音频处理工具

本地多媒体处理工具，支持视频和音频文件，全程在本机运行，无需上传，无需网络。

## 功能

| 模式 | 支持输入 | 说明 |
|------|---------|------|
| 🗜 压缩 | 视频 | 大幅缩减体积，支持 H.264 / H.265 / VP9 / AV1，自由调节质量 |
| 🎵 提取音频 | 视频 | 从视频中提取音轨，导出 MP3 / AAC / FLAC / WAV / OGG / OPUS / M4A |
| 🔄 转格式 | 视频 | 视频格式互转（MP4 / MKV / MOV / WebM / AVI） |
| 📐 缩放 | 视频 | 批量修改分辨率，支持 360p → 4K |
| 📝 转文稿 | **视频 + 音频** | 使用 Whisper 本地语音识别，支持多语言，导出 TXT / SRT 字幕 |

**转文稿模式**直接支持音频文件输入（MP3、WAV、AAC、M4A、FLAC、OGG、OPUS），无需先转换格式。

支持**批量导入**，统一文件队列，一次导入对所有模式生效。

## 版本

- **`video_web.py`** — Web 版：浏览器界面，本地 HTTP 服务器

## 安装

```bash
# 必须：FFmpeg
brew install ffmpeg      # macOS
sudo apt install ffmpeg  # Ubuntu / Debian

# 可选：转文稿功能需要 Whisper
pip install openai-whisper
```

## 使用

```bash
python3 video_web.py
```

自动在浏览器打开 `http://127.0.0.1:9527`，按 `Ctrl+C` 退出。

## 打包

```bash
pyinstaller video_web.spec
```

打包后的 `VideoToolkit.app` 仍然运行本地 Web 服务，并只打开一次浏览器页面。转文稿会调用系统 Python，避免 PyInstaller 打包后子进程重复启动 `.app`。

## 支持的文件格式

| 类型 | 格式 |
|------|------|
| 视频 | MP4、MKV、MOV、AVI、WebM、M4V、FLV、WMV、TS |
| 音频 | MP3、WAV、AAC、M4A、FLAC、OGG、OPUS |

## 依赖

- Python 3.8+（无需安装额外 Python 库）
- FFmpeg（系统命令行工具）
- `openai-whisper`（可选，仅转文稿功能需要）

## 转文稿说明

- 使用 Whisper 本地模型，**完全离线**，隐私安全
- 直接接受视频或音频文件，无需手动预处理
- 批量文件**顺序处理**，模型只加载一次，节省内存和时间
- 支持 SRT 字幕同步导出
- 支持语言：中文、英文、日文、韩文、法文、德文及自动检测
