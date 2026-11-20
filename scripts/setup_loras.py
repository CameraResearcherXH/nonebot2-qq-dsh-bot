# scripts/setup_loras.py
"""
LoRA 一键下载（跑在香港服务器上，再拷到算力机）。

纯标准库，不用装任何依赖（python3 直接跑）。

用法：
  python3 setup_loras.py --token "你的CIVITAI_TOKEN" --out /tmp/loras
  （--token 可省略，但很多 LoRA 需要登录态，建议带上）
  （--dry-run 只看会下什么，不实际下载）

Civitai token 获取：civitai.com -> 头像 -> Account Settings -> API Keys。
⚠️ 注意：这不是 node.env 里的 API_SECRET，那个是算力机<->服务器的通信密钥。

每个条目支持两种来源：
  - Civitai：按 query 搜索（或给定模型 id），拉完整 JSON 后下 downloadUrl
  - HuggingFace：给 hf_repo/hf_file，优先 hf-mirror，失败回 hf.co
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

API = "https://civitai.com/api/v1"
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"}

# save 文件名 / 来源
LORAS = [
    # 之前按 query 搜索会选错成 Anima 版（下载还是拦截页），这里直接锁定 NoobAI_v5 的文件
    {"save": "AddMicroDetails_NoobAI.safetensors", "model_id": 1377820, "file_id": 2705511},
    {"save": "DetailTweakerXL.safetensors", "hf_repo": "PvDeep/Add-Detail-XL", "hf_file": "add-detail-xl.safetensors"},
    {"save": "ThickerLinesAnimeStyle.safetensors", "query": "Thicker Lines Anime Style"},
    {"save": "FlatColor_Illustrious.safetensors", "query": "Flat Color Illustrious"},
    {"save": "Nnegrat_2_5D_High.safetensors", "query": "Nnegrat 2.5D High"},
    {"save": "epi_noiseoffset.safetensors", "query": "epi_noiseoffset", "id": 13941},
]


def _build_opener(proxy):
    if proxy:
        return urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    return urllib.request.build_opener()


def get_json(url, token, opener):
    req = urllib.request.Request(url, headers=dict(UA))
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with opener.open(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def resolve_model(entry, token, opener):
    mid = entry.get("id")
    if not mid:
        q = urllib.parse.quote(entry["query"])
        data = get_json(f"{API}/models?query={q}&types=LORA&limit=20", token, opener)
        items = data.get("items") or []
        if not items:
            return None
        pick = None
        for it in items:
            if it.get("type") == "LORA" and any(
                k in (it.get("baseModel") or "").lower()
                for k in ("sdxl", "illustrious", "pony", "noobai")
            ):
                pick = it
                break
        if not pick:
            pick = items[0]
        mid = pick.get("id")
    # 搜索返回的是摘要，fileId 可能不准 → 再按模型 ID 拉完整 JSON
    return get_json(f"{API}/models/{mid}", token, opener)


def pick_file(model):
    """选一个可下载的模型文件，优先 .safetensors（ComfyUI 用，避开 .pt）。"""
    for v in model.get("modelVersions") or []:
        files = v.get("files") or []
        if not files:
            continue
        st = [f for f in files if (f.get("name") or "").lower().endswith(".safetensors")]
        pool = st or files
        for f in pool:
            t = (f.get("type") or "").lower()
            if "model" in t and "training" not in t:
                return v["id"], f
        return v["id"], pool[0]
    return None, None


def download_civitai(url, token, out, opener):
    if token:
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}token={urllib.parse.quote(token)}"
    last = None
    for _attempt in range(3):
        try:
            req = urllib.request.Request(url, headers=dict(UA))
            if token:
                req.add_header("Authorization", f"Bearer {token}")
            with opener.open(req, timeout=300) as r, open(out, "wb") as f:
                f.write(r.read())
            return
        except urllib.error.HTTPError as e:
            last = e
            if e.code in (400, 404, 429, 500, 502, 503):
                time.sleep(2)
                continue
            raise
        except Exception as e:
            last = e
            time.sleep(2)
    if last:
        raise last


def download_hf(repo, fname, out, opener):
    # 挂代理时优先直连 hf.co；没代理时优先 hf-mirror（国内快）
    bases = ("https://huggingface.co", "https://hf-mirror.com") if opener_has_proxy(opener) \
        else ("https://hf-mirror.com", "https://huggingface.co")
    for base in bases:
        url = f"{base}/{repo}/resolve/main/{urllib.parse.quote(fname)}"
        try:
            req = urllib.request.Request(url, headers=dict(UA))
            with opener.open(req, timeout=300) as r, open(out, "wb") as f:
                f.write(r.read())
            return
        except urllib.error.HTTPError:
            continue
    raise RuntimeError(f"HF 下载失败: {repo}/{fname}")


def opener_has_proxy(opener) -> bool:
    """判断 opener 是否挂了代理（决定 HF 走 hf.co 还是 hf-mirror）。"""
    try:
        handlers = [h for h in opener.handlers if isinstance(h, urllib.request.ProxyHandler)]
        if handlers:
            return bool(handlers[0].proxies)
    except Exception:
        pass
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--token", default=os.environ.get("CIVITAI_TOKEN", ""))
    ap.add_argument("--out", default=os.path.join("loras"))
    ap.add_argument("--proxy", default=os.environ.get("HTTPS_PROXY", ""),
                    help="走代理下载，如 http://127.0.0.1:2080（算力机直连不了 Civitai 时用）")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    opener = _build_opener(args.proxy or None)
    if args.proxy:
        print(f"🔌 使用代理: {args.proxy}")

    os.makedirs(args.out, exist_ok=True)
    for e in LORAS:
        save = e["save"]
        out = os.path.join(args.out, save)
        print(f"\n== {save}")
        if args.dry_run:
            print(f"  [DRY] would download to {out}")
            continue
        if os.path.exists(out) and os.path.getsize(out) > 100 * 1024:
            print(f"  [SKIP] 已存在 {out}（{os.path.getsize(out) // 1024} KB）")
            continue
        if os.path.exists(out):
            print(f"  [重下] {out} 异常（{os.path.getsize(out)} 字节，正常应>100KB），重新下载")
            try:
                os.remove(out)
            except Exception:
                pass
        try:
            if e.get("hf_repo"):
                print(f"  source: HuggingFace {e['hf_repo']}/{e['hf_file']}")
                download_hf(e["hf_repo"], e["hf_file"], out, opener)
            elif e.get("file_id"):
                # 直接按 fileId 下载，不走搜索（搜索会选错文件版本）
                url = f"{API}/download/models/{e['model_id']}?fileId={e['file_id']}"
                print(f"  source: Civitai model {e['model_id']} fileId {e['file_id']}")
                download_civitai(url, args.token, out, opener)
            else:
                model = resolve_model(e, args.token, opener)
                if not model:
                    print("  [WARN] 没搜到")
                    continue
                vid, f = pick_file(model)
                if not vid:
                    print("  [WARN] 没有可下载文件")
                    continue
                print(f"  model: {model.get('name')} (base {model.get('baseModel')})")
                print(f"  file : {f.get('name')}")
                url = f.get("downloadUrl") or (f"{API}/download/models/{vid}?fileId={f.get('id')}")
                download_civitai(url, args.token, out, opener)
            size = os.path.getsize(out)
            if size < 100 * 1024:
                # 9KB 之类基本是 Civitai 的登录页/Cloudflare 拦截页，不是模型
                head = ""
                try:
                    with open(out, "rb") as _f:
                        head = _f.read(200).decode("utf-8", errors="replace")
                except Exception:
                    pass
                print(f"  [FAIL] 下载内容异常（{size} 字节），可能是拦截页/错误页，已删除")
                if head:
                    print(f"  内容开头: {head[:200]!r}")
                try:
                    os.remove(out)
                except Exception:
                    pass
            else:
                print(f"  [OK] {out} ({size // 1024} KB)")
        except Exception as ex:
            print(f"  [FAIL] {type(ex).__name__}: {ex}")

    print("\ndone. 触发词备忘：")
    print("  addmicrodetails / add detail / thicker lines / flat color")
    print("  epi_noiseoffset: dark studio, rim lighting, two tone lighting, dimly lit, low key")


if __name__ == "__main__":
    main()
