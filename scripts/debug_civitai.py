# scripts/debug_civitai.py —— 调试 Civitai 下载 404 问题
import json
import os
import urllib.error
import urllib.request

UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.1 Safari/537.36"}
TOKEN = os.environ.get("CIVITAI_TOKEN", "") or "[REDACTED_SECRET]"


def get(url):
    req = urllib.request.Request(url, headers=dict(UA))
    if TOKEN:
        req.add_header("Authorization", f"Bearer {TOKEN}")
    return urllib.request.urlopen(req, timeout=30)


print("== 1) 模型 JSON ==")
m = json.load(get("https://civitai.com/api/v1/models/13941"))
print("model name:", m.get("name"))
for v in m.get("modelVersions") or []:
    print("version id:", v.get("id"))
    for f in v.get("files") or []:
        print("  file name     :", f.get("name"))
        print("  file type     :", f.get("type"))
        print("  file downloadUrl:", f.get("downloadUrl"))
        print("  file url      :", f.get("url"))
    print("---")

print("== 2) 下载接口实测 ==")
if m.get("modelVersions"):
    vid = m["modelVersions"][0]["id"]
    for label, url in [
        ("api/download?token", f"https://civitai.com/api/download/models/{vid}?token={TOKEN}"),
        ("api/download header", f"https://civitai.com/api/download/models/{vid}"),
    ]:
        try:
            req = urllib.request.Request(url, headers=dict(UA))
            if TOKEN and "header" in label:
                req.add_header("Authorization", f"Bearer {TOKEN}")
            r = urllib.request.urlopen(req, timeout=30)
            print(f"[{label}] status={r.status} final={r.geturl()}")
        except urllib.error.HTTPError as e:
            print(f"[{label}] HTTPError {e.code} {e.reason}")
            try:
                print("   body:", e.read()[:200])
            except Exception:
                pass
        except Exception as e:
            print(f"[{label}] {type(e).__name__}: {e}")
