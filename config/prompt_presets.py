"""画图 prompt / 采样参数配方（算力机 node.py 使用，可选）。

用户 2026-11-16 提供的统一配方，node.py 启动时会尝试 import 这里：
  - import 成功 → 用这里的值作为画图默认参数（node.env 里的 SD_* 环境变量仍可覆盖）
  - 文件没部署 / import 失败 → node.py 自动回退到内置默认值，不影响出图

注意：
  - clip_skip 在 comfy_workflows/*.json 的 CLIPSetLastLayer 节点里已硬编码为 -2，
    这里只是记录下来，node.py 不会动态消费这个键（workflow 里写死了）。
  - I2I_PARAMS 的 steps=30 会覆盖 GEN_PARAMS 的 steps=32，用于图生图；
    denoise=0.7 用于「重绘/换角色」，想只换风格时手动降到 0.5。
"""

BASE_POSITIVE = (
    "masterpiece, best quality, very aesthetic, absurdres, "
    "1girl, solo, alone, "
    "detailed hands, five fingers, well-drawn hands, correct anatomy, "
    "clean linework, sharp focus, professional anime illustration, "
)

BASE_NEGATIVE = (
    "(worst quality:1.2), (low quality:1.2), lowres, jpeg artifacts, "
    "signature, watermark, text, username, blurry, out of focus, "
    "bad anatomy, (bad hands:1.3), (extra fingers:1.3), (missing fingers:1.2), "
    "(fused fingers:1.2), (mutated hands:1.2), extra limbs, extra arms, "
    "(multiple girls:1.2), (two girls:1.2), duplicate, cloning, extra person, crowd, "
    "(mini character:1.2), (small figure:1.2), (duplicate character:1.2), "
    "(character pattern:1.2), (tiny person:1.2), sticker, "
)

GEN_PARAMS = {
    "sampler": "dpmpp_2m_sde_gpu",
    "scheduler": "karras",
    "steps": 32,
    "cfg": 5.5,
    "clip_skip": -2,
}

# 图生图专用
I2I_PARAMS = {
    **GEN_PARAMS,
    "denoise": 0.7,        # 换角色时用；换风格用 0.5
    "steps": 30,
}
