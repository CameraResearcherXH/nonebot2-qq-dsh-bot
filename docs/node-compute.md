# 算力机（node.py / node.env / ComfyUI / RVC / IndexTTS）

> 服务器（QQ 机器人）不直接跑重活：画图、翻唱、语音合成、视频理解都交给
> 「算力机」上的 `node.py`。两边通过 HTTP 任务队列通信。

---

## 1. 它是怎么工作的

```
QQ 机器人（服务器 /app）
   │  1. /画图 /翻唱 /测语音 … 生成 task
   ▼
任务队列（服务器 services/task_queue 或算力调度）
   │  2. node.py 定时 GET /api/tasks/pull?secret=...
   ▼
算力机 node.py
   │  3. 按 task["type"] 分发给 TASK_HANDLERS
   │     cover / split / audio_separation / rvc / rvc_mix /
   │     tts / reflection / probe / draw
   ▼
本地服务：ComfyUI(8188) / RVC / IndexTTS(7860) / ffmpeg / Demucs
   │  4. POST /api/tasks/complete 回传文件或 JSON
   ▼
服务器把结果发回 QQ
```

心跳：`node.py` 定时 `GET /api/heartbeat`，上报自己支持的能力（caps）。
ComfyUI 不在线时自动把 `draw` 从 caps 摘掉，服务器会直接回「画图离线」，
不会把任务塞进队列干等。

---

## 2. 文件清单

| 文件 | 作用 |
|---|---|
| `node.py` | 算力机主程序：心跳、拉任务、分发、回传 |
| `node.env.example` | 配置模板；复制成 `node.env` 后填写 |
| `node_config/prompt_presets.py` | 画图 prompt / 采样参数配方（可选，import 失败自动回退内置默认） |
| `comfy_workflows/txt2img.json` | 文生图 + HiRes |
| `comfy_workflows/img2img.json` | 图生图（重绘）+ HiRes |
| `comfy_workflows/lineart.json` | 线稿上色（ControlNet） |
| `my_kkI_rvc.py` | RVC 单文件推理入口（被 `rvc_pipeline.py` / node.py 调用） |
| `rvc_pipeline.py` | RVC 音域对齐、变调、混音流水线 |

`comfy_workflows/README.md` 里有每个 workflow 的占位符和节点链说明。

---

## 3. 算力机准备

### 3.1 Python 依赖
```bash
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
# 另外需要：ffmpeg（PATH 里）、Demucs、RVC 运行环境、ComfyUI
```

### 3.2 ComfyUI（画图）
1. 安装 ComfyUI，启动在 `http://127.0.0.1:8188`；
2. 把底模放进 `models/checkpoints/`，LoRA 放 `models/loras/`，ControlNet 放 `models/controlnet/`；
3. 把 `comfy_workflows/` 放在 `node.py` 同目录（或用 `SD_WORKFLOW_DIR` 指过去）；
4. 在 ComfyUI 里跑通一张图，确认模型名和 `node.env` 的 `SD_MODEL` 一致。

### 3.3 RVC（翻唱）
1. 准备 RVC 推理环境（`infer/`、`assets/rmvpe`、`assets/hubert`、`assets/pretrained_v2`、`assets/weights`）；
2. `my_kkI_rvc.py` 默认从**自身目录**找这些 assets；
3. 模型名通过任务下发（`task["model"]`），不需要写死在代码里；
4. `rvc_pipeline.py` 负责：
   - F0 分析（判男女 / 音域）；
   - 音域对齐（低音抬到下限、高音超上限才降）；
   - RVC 变调 / 变声；
   - 与伴奏混音、导出 mp3。

### 3.4 IndexTTS2（语音）
1. 启动 IndexTTS2，默认 `http://127.0.0.1:7860/api/v1/tts`；
2. 参考音放 `local_temp/prompt_kki_cut.wav`（或用 `TTS_PROMPT_WAV` 指定）；
3. `TTS_USE_RVC=true` 时合成完再过一遍 RVC，保持音色；
4. 端口不通时 `node.py` 会自动扫 `TTS_PORT_CANDIDATES`，仍不通则服务器降级云端 CosyVoice。

---

## 4. 配置：node.env

```bash
cp node.env.example node.env
# 编辑 node.env，至少填：
#   API_SECRET=与服务器 .env 的 API_SECRET 完全一致
#   DASHSCOPE_API_KEY=（如果要用 Qwen 音频判定）
#   TTS_URL / TTS_OUTPUT_DIR / SD_API_URL / SD_MODEL / SD_LORAS
```

常用配置分组（完整见 `node.env.example` 注释）：

| 分组 | 关键项 |
|---|---|
| 连接 | `API_SECRET`、`SERVER_IP`、`SERVER_PORT`、`NODE_PROXY` |
| 混音 | `MIX_VOCAL_OVER_INST_DB`、`MIX_DRUM_GAIN`、`MIX_VOCAL_TREBLE_DB` |
| 翻唱格式 | `COVER_WORK_FORMAT`、`COVER_WORK_BITRATE` |
| 语音版 | `COVER_VOICE_MAX_MB`、`COVER_VOICE_MAX_SEC`、`COVER_VOICE_BITRATE` |
| 音高判定 | `F0_MALE_MAX`、`F0_FEMALE_MIN`、`F0_ANALYZE_SEC` |
| RVC 音域 | `RVC_VOICE_F0_MAX`、`RVC_VOICE_F0_MIN`、`RVC_MALE_TARGET_F0`、`COVER_PITCH_MODE` |
| 超时 | `RVC_TIMEOUT`、`UPLOAD_TIMEOUT_*`、`DOWNLOAD_TIMEOUT_READ` |
| 语音识别 / 复盘 | `DASHSCOPE_API_KEY`、`DASHSCOPE_BASE_URL` |
| IndexTTS | `TTS_URL`、`TTS_PORT_CANDIDATES`、`TTS_OUTPUT_DIR`、`TTS_USE_RVC` |
| ComfyUI | `SD_API_URL`、`SD_WORKFLOW_DIR`、`SD_MODEL`、`SD_LORAS`、`SD_CN_*` |

> ⚠️ `API_SECRET` 两端必须一致，但**不要**把 `node.env` 提交到公开仓库。
> 发行仓只提供 `node.env.example`。

---

## 5. 启动与自检

```bash
python3 node.py --check    # 只跑依赖自检，不连服务器
python3 node.py            # 正常启动
```

启动日志会打印：
- 当前支持的 caps（`cover / split / rvc / tts / probe / draw …`）
- IndexTTS 端口是否在听
- ComfyUI 是否在线
- 心跳 / 拉任务是否正常

常见问题：

| 现象 | 原因 | 处理 |
|---|---|---|
| 服务器显示「画图离线」 | ComfyUI 没启动 / 8188 不通 | 启动 ComfyUI；`curl http://127.0.0.1:8188` |
| 翻唱一直转圈 | RVC 模型名不对 / 显存不够 | 看 `node.py` 日志里的 RVC 报错 |
| 语音是「龙呼呼」 | IndexTTS 不通，降级 CosyVoice | 检查 `TTS_URL` / `TTS_PORT_CANDIDATES` |
| 心跳 401 | `API_SECRET` 两端不一致 | 对齐服务器 `.env` 与 `node.env` |
| 任务回传失败 | 文件太大 / 上传超时 | 调 `UPLOAD_TIMEOUT_*`、`COVER_VOICE_MAX_MB` |

---

## 6. 参数一致性

`node.py` 和服务器 `core/tuning.py` 有一部分同名参数（音域、变调、时长）。
**两边必须一致**，否则会出现「服务器以为降了 6 个半音，算力机实际降了 3 个」。
完整对齐清单见 [node-params.md](node-params.md)。

原则：

> 业务阈值（音域 / 变调 / 时长）由服务器决定并下发；
> 算力机只保留「本机怎么跑」的参数（路径、端口、模型文件名、超时）。

---

## 7. 安全

- `node.py` 的 HTTP 服务不要直接暴露公网；只让服务器访问。
- `API_SECRET` 用长随机串，定期更换。
- `node.env`、`.env` 都不要提交到 git；`.gitignore` 已忽略。
- 画图默认带 `safe` / 排除 `nsfw`，需要改配方时改 `SD_NEGATIVE` / `node_config/prompt_presets.py`。
