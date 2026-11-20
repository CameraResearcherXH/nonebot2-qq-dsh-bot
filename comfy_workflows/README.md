# comfy_workflows —— 算力机画图 workflow 模板

node.py 的 `process_draw_task` 按 `task["mode"]` 加载 `{mode}.json`，
把里面的**占位符**替换成真实参数后 POST 给 ComfyUI 的 `/prompt`。

## 放哪

把这些 json 和 `node.py` 放同一目录（node.py 默认找同目录下的 `comfy_workflows/`），
或者放别处后在 `node.env` 里写 `SD_WORKFLOW_DIR=` 指过去。

## 占位符（只有「字符串值完全等于占位符」才会被替换）

| 占位符 | 含义 | 默认值来自 node.env |
|---|---|---|
| `__MODEL__` | checkpoint 文件名 | `SD_MODEL` |
| `__PROMPT__` / `__NEGATIVE__` | 正/负向 tags | 服务器下发 / `SD_NEGATIVE` |
| `__SEED__` | 随机种子（int） | 随机 |
| `__WIDTH__` / `__HEIGHT__` | 底图尺寸 | `SD_WIDTH` / `SD_HEIGHT` |
| `__STEPS__` / `__CFG__` / `__SAMPLER__` / `__SCHEDULER__` | 采样参数 | `SD_*` |
| `__DENOISE__` | 图生图重绘强度 | `SD_IMG2IMG_DENOISE` |
| `__HI_WIDTH__` / `__HI_HEIGHT__` / `__HI_STEPS__` / `__HI_DENOISE__` | HiRes 第二遍 | 按 `SD_HI_SCALE` 算 |
| `__UPSCALER__` | 放大模型名 | `SD_UPSCALER` |
| `__CONTROL_MODEL__` | ControlNet 文件名 | `SD_CN_*` |
| `__CONTROL_STRENGTH__` | ControlNet 强度 | `SD_CONTROL_STRENGTH` |
| `__IMAGE__` | 参考图在 ComfyUI input 目录里的文件名 | node.py 上传后自动填 |

## 已随仓库下发的模板（核心节点，装好 ComfyUI 就能跑）

- `txt2img.json` —— 文生图 + HiRes（`/画图`）
- `img2img.json` —— 图生图（重绘）+ HiRes（`/以图绘图 重绘`）
- `lineart.json` —— 线稿上色：用户发**草图/线稿**，ControlNet 直接约束（`/以图绘图 线稿`）

## 需要你在 ComfyUI 界面里搭一次、导出后放进来（姿势/景深/画风）

这三个模式依赖自定义节点，**节点的字段名会随版本变，手写 JSON 容易对不上**，
所以正确的做法是：在界面里拖一次（每个约 3 分钟）→ 跑通一张 → `Save (API Format)`
导出 → 把动态值替换成上面的占位符 → 存成 `pose.json` / `depth.json` / `style.json`。

### pose.json（姿势：照片 → 骨架 → ControlNet）

节点链（双点空白处搜名字）：

```
Load Checkpoint(选 noobai...) ─┐
Load Image(参考图) ──► OpenposePreprocessor(detect_hand/body/face=enable, resolution=512)
                              ──► ControlNetLoader(选 diffusion_pytorch_model_promax.safetensors)
                              ──► ControlNetApply(conditioning=正向, control_net, image=骨架图, strength=0.9)
CLIP Text Encode(正向 __PROMPT__) ─┘
CLIP Text Encode(负向 __NEGATIVE__)
Empty Latent Image(__WIDTH__ x __HEIGHT__)
KSampler(model, 正向=ControlNetApply 输出, 负向, latent) → VAE Decode → HiRes 链（照抄 txt2img.json 的 8~13 节点）
```

### depth.json（景深/透视：照片 → 深度图 → ControlNet）

把 `pose.json` 里的 `OpenposePreprocessor` 换成 **`Zoe-DepthMapPreprocessor`**（或
`DepthAnythingPreprocessor`），其余一模一样。

### style.json（画风参考：参考图 → IP-Adapter）

```
Load Checkpoint ──► IPAdapterUnifiedLoader(model, preset="PLUS (high strength)")
Load Image(参考图) ─┐
CLIP Text Encode(正向 __PROMPT__) ─┘
                       IPAdapterAdvanced(model=UnifiedLoader 的 model,
                                         ipadapter=UnifiedLoader 的 ipadapter,
                                         image=参考图, weight=0.8)
CLIP Text Encode(负向 __NEGATIVE__)
Empty Latent Image(__WIDTH__ x __HEIGHT__)
KSampler(model=IPAdapterAdvanced 输出, 正向, 负向, latent) → VAE Decode → HiRes 链
```

导出替换清单（用编辑器改导出的 JSON）：
- 正向 text → `__PROMPT__`，负向 text → `__NEGATIVE__`
- `ckpt_name` → `__MODEL__`，`control_net_name` → `__CONTROL_MODEL__`
- LoadImage 的 `image` → `__IMAGE__`
- seed → `__SEED__`，steps → `__STEPS__`，cfg → `__CFG__`，尺寸 → `__WIDTH__`/`__HEIGHT__`
- HiRes 第二遍的 steps/denoise → `__HI_STEPS__`/`__HI_DENOISE__`

节点 id 顺序无所谓，node.py 按「值完全相等」替换，不依赖编号。

不导出这三个模板也不影响主流程：对应模式会回传 `缺 workflow 模板` 的明确错误，
不会卡住任务队列。
