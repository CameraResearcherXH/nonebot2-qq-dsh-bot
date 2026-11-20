# scripts/test_comfy_draw.py
r"""
ComfyUI 出图冒烟测试（跑在算力机上，不依赖 node.py / 服务器）。

用法（在算力机，脚本旁边就是 comfy_workflows/ 目录）：
    H:\ComfyUI\venv\Scripts\python.exe test_comfy_draw.py
    （或 node.py 的 python，只要装了 requests）

它会：
  1. 读 comfy_workflows/txt2img.json
  2. 替换占位符（__MODEL__/__PROMPT__/__NEGATIVE__/__SEED__ 等）
  3. POST 到 http://127.0.0.1:8188/prompt
  4. 轮询 /history 拿图，存成本目录 test_output.png
"""
import json
import random
import time
from pathlib import Path

import requests

SD_API_URL = "http://127.0.0.1:8188"
BASE = Path(__file__).resolve().parent

# 目录名统一用 comfy_workflows（复数）；如果用户传成单数 comfy_workflow 也能找到
_WF_DIR = None
for _name in ("comfy_workflows", "comfy_workflow"):
    _cand = BASE / _name
    if _cand.is_dir():
        _WF_DIR = _cand
        break
if _WF_DIR is None:
    _WF_DIR = BASE / "comfy_workflows"
WF_DIR = _WF_DIR

MODEL = "NoobAI-XL-v1.1.safetensors"
UPSCALER = "4x-UltraSharp.pth"
PROMPT = "1girl, long hair, school uniform, looking at viewer, cherry blossom, masterpiece, best quality, newest, absurdres, highres, safe"
NEGATIVE = ("nsfw, worst quality, old, early, low quality, lowres, signature, username, logo, "
            "bad hands, mutated hands, blurry, rough sketch, unfinished")


def fill(wf: dict) -> dict:
    seed = random.randint(1, 2 ** 31 - 1)
    values = {
        "__MODEL__": MODEL,
        "__PROMPT__": PROMPT,
        "__NEGATIVE__": NEGATIVE,
        "__SEED__": seed,
        "__WIDTH__": 832,
        "__HEIGHT__": 1216,
        "__STEPS__": 28,
        "__CFG__": 5.5,
        "__SAMPLER__": "euler_ancestral",
        "__SCHEDULER__": "normal",
        "__DENOISE__": 1.0,
        "__HI_WIDTH__": 1248,
        "__HI_HEIGHT__": 1824,
        "__HI_STEPS__": 20,
        "__HI_DENOISE__": 0.4,
        "__UPSCALER__": UPSCALER,
        # LoRA 插槽：冒烟测试不挂 LoRA，全部填 None（ComfyUI LoraLoader 会透传）
        "__LORA1_NAME__": "None", "__LORA1_STRENGTH__": 0.0,
        "__LORA2_NAME__": "None", "__LORA2_STRENGTH__": 0.0,
        "__LORA3_NAME__": "None", "__LORA3_STRENGTH__": 0.0,
    }

    def walk(node):
        if isinstance(node, dict):
            for k, v in list(node.items()):
                if isinstance(v, str) and v in values:
                    node[k] = values[v]
                else:
                    walk(v)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(wf)
    return wf


def main():
    p = WF_DIR / "txt2img.json"
    if not p.exists():
        print(f"❌ 找不到 {p}，请把本脚本和 comfy_workflows/ 放一起。")
        return 1
    print(f"📂 使用 workflow 目录：{WF_DIR}")
    wf = fill(json.loads(p.read_text(encoding="utf-8")))

    print("📤 提交 ComfyUI /prompt ...")
    r = requests.post(f"{SD_API_URL}/prompt", json={"prompt": wf, "client_id": "smoke-test"}, timeout=30)
    if r.status_code != 200:
        print(f"❌ /prompt HTTP {r.status_code}: {r.text[:300]}")
        return 1
    pid = r.json().get("prompt_id")
    print(f"prompt_id = {pid}")

    deadline = time.time() + 600
    while time.time() < deadline:
        time.sleep(3)
        try:
            h = requests.get(f"{SD_API_URL}/history/{pid}", timeout=10).json()
        except Exception:
            continue
        item = h.get(pid)
        if not item:
            continue
        status = item.get("status", {})
        if status.get("status_str") == "error":
            msgs = [str(m[0])[:200] for m in status.get("messages", []) if isinstance(m, list) and m]
            print(f"❌ ComfyUI 执行出错: {'; '.join(msgs[:3])}")
            return 1
        if status.get("completed"):
            for _nid, out in item.get("outputs", {}).items():
                for im in out.get("images") or []:
                    fn = im.get("filename")
                    if not fn:
                        continue
                    sub = im.get("subfolder") or ""
                    ftype = im.get("type") or "output"
                    dl = requests.get(f"{SD_API_URL}/view",
                                      params={"filename": fn, "subfolder": sub, "type": ftype},
                                      timeout=120)
                    out_p = BASE / "test_output.png"
                    out_p.write_bytes(dl.content)
                    print(f"✅ 出图成功，已存到 {out_p}（{out_p.stat().st_size / 1024:.0f} KB）")
                    return 0
            print("❌ 执行完成但没找到输出图")
            return 1
    print("❌ 出图超时（600s）")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
