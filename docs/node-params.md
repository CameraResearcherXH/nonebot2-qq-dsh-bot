# 算力机（node.py / node.env）参数审计

> 发行仓只提供 `node.env.example`；复制成 `node.env` 再填。

> 只读分析，**本文件不改 node.py**。生成时间：2026-11-20。
> 目的：说清楚哪些参数是重复的、哪些没用上、哪些该保留。

## 0. 结论先看

| 类别 | 数量 | 处理建议 |
|---|---|---|
| 两端同名（node.py 与服务器 tuning 都读） | 7 | **必须保持一致**，否则同一件事两边算出不同结果 |
| `.env` 与 `node.env` 都定义 | 9 | 只保留一边；密钥类放各自 `.env`，业务阈值只放服务器 |
| `node.env` 定义了但 node.py 不读 | 15 | 确认是否被算力机侧其它脚本读；否则删除 |
| node.py 读了但 `node.env` 没定义 | 28 | 用代码默认值在跑；建议要么补进 node.env，要么删掉这个配置项 |
| node.py 独有（服务器不关心） | 68 | **保留**（算力机本地资源/路径/服务地址） |

一句话：**业务阈值（音域/变调/时长）由服务器决定并下发；算力机只保留「本机怎么跑」的参数。**

---

## 1. 两端同名：必须保持一致（7 个）

这些键在 `node.py` 和服务器 `core/tuning.py` 里各有一份默认值。
算法是同一套（`services/song_gender.py` ↔ `node.py` 的 pitch/F0 部分），
**任意一边改了另一边不改，就会出现「服务器以为降了 6 个半音、算力机实际降了 3 个」这类错位。**

| 键 | node.py 默认 | 服务器默认 | 谁权威 | 建议 |
|---|---|---|---|---|
| `F0_PERIODICITY_MIN` | `0.45` | `0.45` | 两边都要一致 | 写进 `docs/node-params.md` 的对齐清单；改一处必须改两处 |
| `RVC_PITCH_MAX` | `24` | `24` | 服务器（payload `pitch（服务器算好后下发半音数）`） | node 侧只作兜底；`node.env` 里可以删掉 |
| `RVC_PITCH_MIN` | `-24` | `-24` | 服务器（payload `pitch（同上，夹取上下限）`） | node 侧只作兜底；`node.env` 里可以删掉 |
| `RVC_SAME_GENDER_MAX_SEMIS` | `3` | `3` | 两边都要一致 | 写进 `docs/node-params.md` 的对齐清单；改一处必须改两处 |
| `RVC_TARGET_F0` | `220` | `220.0` | 两边都要一致 | 写进 `docs/node-params.md` 的对齐清单；改一处必须改两处 |
| `RVC_VOICE_F0_MAX` | `880` | `880.0` | 两边都要一致 | 写进 `docs/node-params.md` 的对齐清单；改一处必须改两处 |
| `RVC_VOICE_F0_MIN` | `135` | `130.0` | 两边都要一致 | 写进 `docs/node-params.md` 的对齐清单；改一处必须改两处 |

> 服务器侧说明：
> - `F0_PERIODICITY_MIN`：F0 周期度阈值：低于它的帧当噪声丢弃（0.45）
> - `RVC_PITCH_MAX`：RVC 变调上限（半音）
> - `RVC_PITCH_MIN`：自适应 pitch 的上下限，防止测错把歌毁了
> - `RVC_SAME_GENDER_MAX_SEMIS`：源是女声时的移调上限（半音）：女→女只换音色，不该大幅降调
> - `RVC_TARGET_F0`：目标音色（kikiv2 女声）的典型基频中位数。自适应 pitch 的基准点。
> - `RVC_VOICE_F0_MAX`：判定女声的基频上限（Hz）
> - `RVC_VOICE_F0_MIN`：判定女声的基频下限（Hz）

---

## 2. `.env` 与 `node.env` 都定义（9 个）

| 键 | 服务器 `.env` | `node.env` | 判断 |
|---|---|---|---|
| `API_SECRET` | `（已隐藏）` | `（已隐藏）` | **各自保留**（两端各自鉴权，值应相同但不要合并成一个文件） |
| `COVER_FULL_TTL` | `600` | `600` | node.py 不读 → 算力机侧可删（服务器用它做成品缓存） |
| `COVER_VOICE_BITRATE` | `192k` | `192k` | node.py 不读 → 若 node 侧 ffmpeg 脚本不读则可删 |
| `COVER_VOICE_MAX_MB` | `3.0` | `3` | 服务器下发 payload 优先 → node.env 可删（保留代码兜底即可） |
| `COVER_VOICE_MAX_SEC` | `100` | `100` | 服务器下发 payload 优先 → node.env 可删（保留代码兜底即可） |
| `DASHSCOPE_API_KEY` | `（已隐藏）` | `（已隐藏）` | **各自保留**（两端各自鉴权，值应相同但不要合并成一个文件） |
| `DASHSCOPE_BASE_URL` | `（已隐藏）` | `（已隐藏）` | **各自保留**（两端各自鉴权，值应相同但不要合并成一个文件） |
| `RVC_VOICE_F0_MAX` | `880` | `550` | **必须一致**（音域上下限）；建议 node.env 保留，并与服务器同步 |
| `RVC_VOICE_F0_MIN` | `130` | `135` | **必须一致**（音域上下限）；建议 node.env 保留，并与服务器同步 |

---

## 3. `node.env` 里定义了但 node.py 不读（15 个）

| 键 | 值 | 说明 |
|---|---|---|
| `MIX_VOCAL_OVER_INST_DB` | `-2` | 确认后删除 |
| `MIX_DRUM_GAIN` | `0.7s` | 混音鼓组增益；若算力机侧混音脚本读它则保留 |
| `MIX_VOCAL_TREBLE_DB` | `0` | 确认后删除 |
| `COVER_VOICE_MAX_MB` | `3` | 确认后删除 |
| `COVER_VOICE_MAX_SEC` | `100` | 确认后删除 |
| `COVER_VOICE_BITRATE` | `192k` | 语音版码率；同上 |
| `COVER_FULL_TTL` | `600` | 成品缓存 TTL；服务器 `plugins/song_cover` 在用，算力机不需要 |
| `F0_ANALYZE_SEC` | `100` | F0 分析窗口；node.py 未读，确认是否被 rvc 脚本读 |
| `DASHSCOPE_API_KEY` | `（已隐藏）` | 确认后删除 |
| `TTS_PORT_CANDIDATES` | `7860,7861,7862,8000,9880` | 确认后删除 |
| `SD_PROMPT_QUALITY` | `masterpiece, best qualit` | 确认后删除 |
| `SD_NEGATIVE` | `nsfw, nsfw content, expl` | 确认后删除 |
| `SD_IMG2IMG_DENOISE` | `0.55` | 确认后删除 |
| `SD_LORAS` | `NOOB_EPSv1_detailer_by_v` | 确认后删除 |
| `SD_LORA_TRIGGERS` | `FlatColor_Illustrious.sa` | 确认后删除 |

---

## 4. node.py 读了但 `node.env` 没定义（28 个）

这些参数现在**跑的是代码里的默认值**，`node.env` 里改不了。
要么补进 `node.env`（想让它可调），要么就当常量用（不想暴露）。

| 键 | node.py 默认 | 变量 | 行 |
|---|---|---|---|
| `SERVER_IP` | `127.0.0.1` | `SERVER_IP` | 128 |
| `SERVER_PORT` | `8080` | `SERVER_PORT` | 129 |
| `TEMP_DIR` | `./local_temp` | `TEMP_DIR` | 133 |
| `NODE_PROXY` | `—` | `_NODE_PROXY` | 216 |
| `HEARTBEAT_INTERVAL` | `15` | `HEARTBEAT_INTERVAL` | 224 |
| `POLL_INTERVAL` | `5` | `POLL_INTERVAL` | 225 |
| `TTS_ENGINE` | `indextts` | `TTS_ENGINE` | 259 |
| `TTS_PROMPT_WAV` | `str(TEMP_DIR /` | `TTS_PROMPT_WAV` | 261 |
| `TTS_CMD` | `—` | `TTS_CMD` | 263 |
| `TTS_TIMEOUT` | `300` | `TTS_TIMEOUT` | 264 |
| `TTS_RVC_PITCH` | `0` | `TTS_RVC_PITCH` | 267 |
| `TTS_RVC_F0` | `crepe` | `TTS_RVC_F0` | 270 |
| `RVC_VOICE_F0_MODE` | `keep` | `VOICE_F0_MODE` | 328 |
| `RVC_VOICE_F0_USE_MAX` | `false` | `VOICE_F0_USE_MAX` | 336 |
| `RVC_PITCH_MIN` | `-24` | `RVC_PITCH_MIN` | 342 |
| `RVC_PITCH_MAX` | `24` | `RVC_PITCH_MAX` | 343 |
| `COVER_KEY_CHECK` | `true` | `COVER_KEY_CHECK` | 352 |
| `COVER_STEMS` | `4` | `COVER_STEMS` | 357 |
| `LLM_URL` | `http://127.0.0.1:11434/v1/chat/completions` | `LLM_URL` | 367 |
| `LLM_MODEL` | `qwen2.5:7b` | `LLM_MODEL` | 368 |
| `LLM_TIMEOUT` | `300` | `LLM_TIMEOUT` | 369 |
| `F0_PERIODICITY_MIN` | `0.45` | `F0_PERIODICITY_MIN` | 1000 |
| `RVC_SAME_GENDER_MAX_SEMIS` | `3` | `SAME_GENDER_MAX_SEMIS` | 1001 |
| `RVC_TARGET_F0` | `220` | `TARGET_F0` | 1002 |
| `F0_OCTAVE_SUSPECT` | `340` | `F0_OCTAVE_SUSPECT` | 1028 |
| `QWEN_AUDIO_API_KEY` | `—` | `DASHSCOPE_API_KEY` | 2398 |
| `QWEN_PROBE_BITRATE` | `32k` | `br` | 2458 |
| `SD_I2I_STEPS` | `—` | `SD_I2I_STEPS` | 3881 |

---

## 5. node.py 独有：应当保留（68 个）

按用途分组（这些服务器不需要知道，属于「算力机本机怎么跑」）：

**服务/网络**（11）：`SERVER_IP`、`SERVER_PORT`、`API_SECRET`、`NODE_PROXY`、`HEARTBEAT_INTERVAL`、`POLL_INTERVAL`、`TEMP_DIR`、`UPLOAD_TIMEOUT_BASE`、`UPLOAD_TIMEOUT_MAX`、`UPLOAD_TIMEOUT_PER_MB`、`DOWNLOAD_TIMEOUT_READ`

**TTS**（9）：`TTS_ENGINE`、`TTS_CMD`、`TTS_TIMEOUT`、`TTS_URL`、`TTS_OUTPUT_DIR`、`TTS_PROMPT_WAV`、`TTS_USE_RVC`、`TTS_RVC_PITCH`、`TTS_RVC_F0`

**RVC/变声**（13）：`RVC_TIMEOUT`、`RVC_VOICE_F0_MODE`、`RVC_VOICE_F0_USE_MAX`、`RVC_VOICE_F0_CENTER`、`RVC_VOICE_F0_ALIGN_MAX`、`RVC_VOICE_F0_FEMALE_FLOOR`、`RVC_VOICE_F0_FEMALE_DOWN_TRIGGER`、`RVC_VOICE_F0_FEMALE_DOWN_FLOOR`、`RVC_VOICE_F0_FEMALE_MAX_DOWN`、`RVC_VOICE_F0_MALE_FLOOR`、`RVC_MALE_MIN_SEMIS`、`RVC_MALE_TARGET_F0`、`RVC_PITCH_LOW_FIRST`

**翻唱/混音**（6）：`COVER_KEY_CHECK`、`COVER_STEMS`、`COVER_PITCH_MODE`、`COVER_PITCH_EXACT`、`COVER_WORK_FORMAT`、`COVER_WORK_BITRATE`

**F0 分析**（5）：`F0_FEMALE_MIN`、`F0_MALE_MAX`、`F0_MIXED_LO`、`F0_MIXED_HI`、`F0_OCTAVE_SUSPECT`

**语音识别**（2）：`QWEN_AUDIO_API_KEY`、`QWEN_PROBE_BITRATE`

**SD 画图**（18）：`SD_API_URL`、`SD_MODEL`、`SD_WIDTH`、`SD_HEIGHT`、`SD_STEPS`、`SD_CFG`、`SD_SAMPLER`、`SD_SCHEDULER`、`SD_TIMEOUT`、`SD_UPSCALER`、`SD_HI_SCALE`、`SD_HI_STEPS`、`SD_HI_DENOISE`、`SD_I2I_STEPS`、`SD_CN_DEPTH`、`SD_CN_LINEART`、`SD_CN_POSE`、`SD_CONTROL_STRENGTH`

**其他**（4）：`DASHSCOPE_BASE_URL`、`LLM_MODEL`、`LLM_TIMEOUT`、`LLM_URL`

---

## 6. 建议的收敛动作（不改代码，只改配置/文档）

1. **音域/变调对齐**：`RVC_VOICE_F0_MIN`、`RVC_VOICE_F0_MAX`、`RVC_PITCH_MIN`、
   `RVC_PITCH_MAX`、`RVC_TARGET_F0`、`RVC_SAME_GENDER_MAX_SEMIS`、
   `F0_PERIODICITY_MIN` 这 7 个，两边默认值必须相同。
   以后要调，**先改服务器 `core/tuning.py`，再同步 `node.env`**，并记一笔到本文件。
2. **时长/大小上限**：`COVER_VOICE_MAX_MB`、`COVER_VOICE_MAX_SEC` 已由服务器
   payload 下发（`voice_max_mb` / `voice_max_sec`），`node.env` 里的两份可以删。
3. **密钥**：`API_SECRET`、`DASHSCOPE_API_KEY`、`DASHSCOPE_BASE_URL` 两端各自保留，
   但值必须一致；不要为了省事把 `.env` 复制到算力机（会带一堆服务器专用键）。
4. **`node.env` 瘦身**：删掉第 3 节的 4 个（确认后），补上第 4 节里确实想可调的。
5. **禁止**在算力机上再出现第二份「业务阈值」：凡是影响「她唱成什么样」的值，
   都应从服务器下发，算力机只做执行。

---

## 7. 附：node.py 全部 env 键（75 个）

| 键 | 默认 | 行 | 归属 |
|---|---|---|---|
| `API_SECRET` | my_super_secret_key_123 | 130 | node.env |
| `COVER_KEY_CHECK` | true | 352 | 仅代码默认 |
| `COVER_PITCH_EXACT` | false | 364 | node.env |
| `COVER_PITCH_MODE` | rvc | 348 | node.env |
| `COVER_STEMS` | 4 | 357 | 仅代码默认 |
| `COVER_WORK_BITRATE` | 320k | 3072 | node.env |
| `COVER_WORK_FORMAT` | mp3 | 2772 | node.env |
| `DASHSCOPE_BASE_URL` | — | 2399 | node.env |
| `DOWNLOAD_TIMEOUT_READ` | 180 | 236 | node.env |
| `F0_FEMALE_MIN` | 185 | 1021 | node.env |
| `F0_MALE_MAX` | 155 | 1020 | node.env |
| `F0_MIXED_HI` | 235 | 1023 | node.env |
| `F0_MIXED_LO` | 165 | 1022 | node.env |
| `F0_OCTAVE_SUSPECT` | 340 | 1028 | 仅代码默认 |
| `F0_PERIODICITY_MIN` | 0.45 | 1000 | 两端同名 |
| `HEARTBEAT_INTERVAL` | 15 | 224 | 仅代码默认 |
| `LLM_MODEL` | qwen2.5:7b | 368 | 仅代码默认 |
| `LLM_TIMEOUT` | 300 | 369 | 仅代码默认 |
| `LLM_URL` | http://127.0.0.1:11434/v1/chat/completions | 367 | 仅代码默认 |
| `NODE_PROXY` | — | 216 | 仅代码默认 |
| `POLL_INTERVAL` | 5 | 225 | 仅代码默认 |
| `QWEN_AUDIO_API_KEY` | — | 2398 | 仅代码默认 |
| `QWEN_PROBE_BITRATE` | 32k | 2458 | 仅代码默认 |
| `RVC_MALE_MIN_SEMIS` | 6 | 321 | node.env |
| `RVC_MALE_TARGET_F0` | 200 | 320 | node.env |
| `RVC_PITCH_LOW_FIRST` | true | 341 | node.env |
| `RVC_PITCH_MAX` | 24 | 343 | 两端同名 |
| `RVC_PITCH_MIN` | -24 | 342 | 两端同名 |
| `RVC_SAME_GENDER_MAX_SEMIS` | 3 | 1001 | 两端同名 |
| `RVC_TARGET_F0` | 220 | 1002 | 两端同名 |
| `RVC_TIMEOUT` | 1800 | 379 | node.env |
| `RVC_VOICE_F0_ALIGN_MAX` | false | 332 | node.env |
| `RVC_VOICE_F0_CENTER` | 0 | 325 | node.env |
| `RVC_VOICE_F0_FEMALE_DOWN_FLOOR` | 180 | 303 | node.env |
| `RVC_VOICE_F0_FEMALE_DOWN_TRIGGER` | 0.08 | 310 | node.env |
| `RVC_VOICE_F0_FEMALE_FLOOR` | 100 | 284 | node.env |
| `RVC_VOICE_F0_FEMALE_MAX_DOWN` | 4 | 312 | node.env |
| `RVC_VOICE_F0_MALE_FLOOR` | 140 | 309 | node.env |
| `RVC_VOICE_F0_MAX` | 880 | 323 | 两端同名 |
| `RVC_VOICE_F0_MIN` | 135 | 277 | 两端同名 |
| `RVC_VOICE_F0_MODE` | keep | 328 | 仅代码默认 |
| `RVC_VOICE_F0_USE_MAX` | false | 336 | 仅代码默认 |
| `SD_API_URL` | http://127.0.0.1:8188 | 3858 | node.env |
| `SD_CFG` | — | 3882 | node.env |
| `SD_CN_DEPTH` | diffusion_pytorch_model_promax.safetensors | 3897 | node.env |
| `SD_CN_LINEART` | diffusion_pytorch_model_promax.safetensors | 3895 | node.env |
| `SD_CN_POSE` | diffusion_pytorch_model_promax.safetensors | 3896 | node.env |
| `SD_CONTROL_STRENGTH` | 0.9 | 3893 | node.env |
| `SD_HEIGHT` | 1024 | 3878 | node.env |
| `SD_HI_DENOISE` | 0.4 | 3887 | node.env |
| `SD_HI_SCALE` | 1.5 | 3888 | node.env |
| `SD_HI_STEPS` | 20 | 3886 | node.env |
| `SD_I2I_STEPS` | — | 3881 | 仅代码默认 |
| `SD_MODEL` | NoobAI-XL-v1.1.safetensors | 3862 | node.env |
| `SD_SAMPLER` | — | 3883 | node.env |
| `SD_SCHEDULER` | — | 3884 | node.env |
| `SD_STEPS` | — | 3879 | node.env |
| `SD_TIMEOUT` | 600 | 3859 | node.env |
| `SD_UPSCALER` | 4x-UltraSharp.pth | 3889 | node.env |
| `SD_WIDTH` | 1024 | 3877 | node.env |
| `SERVER_IP` | 127.0.0.1 | 128 | 仅代码默认 |
| `SERVER_PORT` | 8080 | 129 | 仅代码默认 |
| `TEMP_DIR` | ./local_temp | 133 | 仅代码默认 |
| `TTS_CMD` | — | 263 | 仅代码默认 |
| `TTS_ENGINE` | indextts | 259 | 仅代码默认 |
| `TTS_OUTPUT_DIR` | E:/deepseek/IndexTTS2.0_v3/outputs | 262 | node.env |
| `TTS_PROMPT_WAV` | str(TEMP_DIR / | 261 | 仅代码默认 |
| `TTS_RVC_F0` | crepe | 270 | 仅代码默认 |
| `TTS_RVC_PITCH` | 0 | 267 | 仅代码默认 |
| `TTS_TIMEOUT` | 300 | 264 | 仅代码默认 |
| `TTS_URL` | http://127.0.0.1:7860/api/v1/tts | 260 | node.env |
| `TTS_USE_RVC` | true | 266 | node.env |
| `UPLOAD_TIMEOUT_BASE` | 300 | 232 | node.env |
| `UPLOAD_TIMEOUT_MAX` | 3600 | 234 | node.env |
| `UPLOAD_TIMEOUT_PER_MB` | 12 | 233 | node.env |
