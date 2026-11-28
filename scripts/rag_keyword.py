#!/usr/bin/env python3
# scripts/rag_keyword.py
"""RAG 关键词通道（不重建向量，2026-11-28）。

背景（用户）：
  「没有 BM25/关键词通道，但是我们本地有个 Ollama qwen2 7B 可以做关键词通道啊。」
  RAG 现在的向量模型是 nomic-embed-text（英文为主），中文召回基本随机：
  问「杜甫 月夜」会命中群聊 txt，问「李清照 如梦令」也命中群聊。
  重建 20GB 语料要一天，所以先加一条**纯增量**的关键词通道：
      · 离线：把 documents/ 清洗成纯文本，建 SQLite FTS5（不碰向量库）；
      · 在线：qwen2.5:7b 从问句里抽 3~5 个关键词/实体 → FTS 匹配 → 片段；
      · 融合：和向量结果做 RRF（在 RAG 服务侧合并，本脚本只负责关键词这一路）。

用法（在算力机上跑）：
    # 1) 建索引（documents 目录，支持 .md/.txt/.json/.jsonl）
    python rag_keyword.py build --documents "<知识库根目录>\\documents" --db keyword.db

    # 2) 查询
    python rag_keyword.py search "杜甫的月夜写的是什么" --db keyword.db --top 5

    # 3) 起 HTTP（给 RAG 服务的 rag_search 内部调用）
    python rag_keyword.py serve --db keyword.db --port 8011

HTTP：
    GET /search?q=杜甫+月夜&top=5
    GET /search?kw=杜甫,月夜&top=5      # 跳过抽词，直接给关键词（排障用）
    → {"ok": true, "q","keywords": [...], "hits": [{"path","kind","snippet","text","score"}], "ms"}

2026-11-28 修复（第二轮，算力机实测）：
  · http.server 用 iso-8859-1 解请求行 → curl 里裸中文 query 变乱码，
    qwen 拿到乱码就随机编词（问「杜甫 月夜」返回 keywords=["法国","雨果"]）。
    现在 do_GET 里先按 latin-1 还原字节，再试 utf-8 / gb18030。
  · 抽词默认改「规则抽词」（毫秒级、确定性、不会编实体），
    qwen 抽词降级为可选：RAG_KW_LLM=1 才开，且只保留原句里出现过的词（防幻觉）。
  · /search 多返回 text（整块原文，供 reranker 用）与 ms；
    /health 多返回 docs（块数），便于确认连的是哪个库。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import time
from pathlib import Path

VERSION = "2026-11-28c"   # 版本标记：/health 与 /search 都会回，用来确认跑的是不是新代码
OLLAMA = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")
KW_MODEL = os.getenv("RAG_KW_MODEL", "qwen2.5:7b")
KW_TIMEOUT = float(os.getenv("RAG_KW_TIMEOUT", "12"))
TEXT_EXT = {".md", ".txt", ".json", ".jsonl"}
MAX_BODY = int(os.getenv("RAG_KW_MAX_BODY", "0"))  # 0 = 不截断（块由 CHUNK 控制）


# ---------------------------------------------------------------- 清洗
_MD = re.compile(r"```.*?```", re.S)
_MD2 = re.compile(r"[#>*_`~\-]{1,}")


def _clean_md(t: str) -> str:
    t = _MD.sub(" ", t)
    t = _MD2.sub(" ", t)
    return re.sub(r"[ \t]+", " ", t)


def _walk_json(obj, out: list) -> None:
    if isinstance(obj, str):
        s = obj.strip()
        if len(s) >= 2:
            out.append(s)
    elif isinstance(obj, dict):
        for k, v in obj.items():
            if str(k).lower() in ("title", "author", "paragraphs", "notes",
                                  "text", "content", "summary", "body"):
                _walk_json(v, out)
    elif isinstance(obj, list):
        for x in obj:
            _walk_json(x, out)


def _read_text(path: Path) -> str:
    """Windows 上的 txt 可能是 GBK/UTF-16，不能只用 utf-8（会整段变乱码/丢字）。"""
    try:
        raw = path.read_bytes()
    except Exception:
        return ""
    for enc in ("utf-8", "utf-8-sig", "gb18030", "utf-16"):
        try:
            return raw.decode(enc)
        except Exception:
            continue
    return raw.decode("utf-8", errors="ignore")


def _text_of(path: Path) -> str:
    raw = _read_text(path)
    if not raw:
        return ""
    ext = path.suffix.lower()
    if ext == ".jsonl":
        out = []
        for line in raw.splitlines():
            try:
                d = json.loads(line)
            except Exception:
                continue
            if isinstance(d, dict):
                out.append(str(d.get("text") or d.get("content") or ""))
        return " ".join(x for x in out if x)
    if ext == ".json":
        try:
            out: list = []
            _walk_json(json.loads(raw), out)
            return " ".join(out)
        except Exception:
            return raw
    if ext == ".md":
        return _clean_md(raw)
    return raw


# ---------------------------------------------------------------- 索引
CHUNK = int(os.getenv("RAG_KW_CHUNK", "1200"))
OVERLAP = int(os.getenv("RAG_KW_OVERLAP", "120"))


def _seg_cn(text: str) -> str:
    """汉字之间插空格（FTS5 unicode61 会把连续汉字当一个 token，导致
    「纳什」搜不到「纳什男爵」）。英文/数字保持原样。"""
    return re.sub(r"([\u4e00-\u9fff])", r" \1 ", str(text or ""))


_CJKISH = r"[\u4e00-\u9fff\u3000-\u303f\uff00-\uffef]"


def _deseg(text: str) -> str:
    """还原 _seg_cn 插的空格：只删「中文（含中文标点）之间的空格」，
    英文/数字之间的空格保留（否则 text 会粘成一坨）。"""
    s = re.sub(rf"(?<={_CJKISH})[ \t]+(?={_CJKISH})", "", str(text or ""))
    return re.sub(r"\s+", " ", s).strip()


def _chunks(text: str) -> list:
    """按长度切块（带重叠）。一个 20MB 的 jsonl 不能只索引前 4000 字。"""
    t = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(t) <= CHUNK:
        return [t] if len(t) >= 10 else []
    out, i = [], 0
    step = max(200, CHUNK - OVERLAP)
    while i < len(t):
        seg = t[i:i + CHUNK]
        if len(seg.strip()) >= 10:
            out.append(seg)
        i += step
    return out


def build(documents: Path, db: Path) -> int:
    db.parent.mkdir(parents=True, exist_ok=True)
    if db.exists():
        db.unlink()
    con = sqlite3.connect(str(db))
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("CREATE VIRTUAL TABLE docs USING fts5("
                "path UNINDEXED, kind UNINDEXED, body, tokenize='unicode61')")
    n = 0
    t0 = time.time()
    for p in documents.rglob("*"):
        if not p.is_file() or p.suffix.lower() not in TEXT_EXT:
            continue
        rel = p.relative_to(documents).as_posix()
        kind = p.suffix.lower().lstrip(".")
        body = _text_of(p)
        if MAX_BODY > 0:
            body = body[:MAX_BODY]
        chunks = _chunks(body)
        for i, seg in enumerate(chunks):
            con.execute("INSERT INTO docs(path, kind, body) VALUES (?,?,?)",
                        (f"{rel}#{i}" if i else rel, kind, _seg_cn(seg)))
            n += 1
        if n and n % 2000 < len(chunks):
            con.commit()
            print(f"  … {n} 块 / {time.time() - t0:.0f}s", flush=True)
    con.commit()
    con.execute("INSERT INTO docs(docs) VALUES('optimize')")
    con.commit()
    con.close()
    print(f"✅ 索引完成：{n} 个文件 → {db}（{time.time() - t0:.1f}s）")
    return n


# ---------------------------------------------------------------- 抽词
_KW_SPLIT = re.compile(r"[\s,，、。．.;；:：!！?？\"'“”‘’()（）\[\]【】<>《》/|\\]+")
_CJK_STOP = {
    "什么", "怎么", "怎样", "如何", "为何", "为什么", "哪些", "哪个", "多少",
    "是否", "可以", "请问", "帮我", "一下", "告诉", "介绍", "关于", "以及",
    "还有", "的是", "了吗", "的呢", "这个", "那个", "我们", "你们", "他们",
}
# 以这些字开头/结尾的二字片段基本都是语法碎片（的/了/是/在…），直接丢
_BAD_EDGE = set("的了是在吗呢吧啊哦嗯着过们你我他她它这那就都也很还和与或把被给对从向往")
_KW_CACHE: dict = {}


def _rule_keywords(q: str) -> list:
    """规则抽词：按标点/空格切，2~4 字整词保留，长串退化成二元片段。"""
    out = []
    for t in _KW_SPLIT.split(str(q or "")):
        t = re.sub(r"^(请问|请|帮我|麻烦|我想知道|想问一下|想问|查一下|找一下)", "", t.strip())
        if not t:
            continue
        if re.fullmatch(r"[\u4e00-\u9fff]+", t):
            if len(t) <= 4:
                out.append(t)
            else:
                for i in range(len(t) - 1):
                    g = t[i:i + 2]
                    if g[0] in _BAD_EDGE or g[1] in _BAD_EDGE:
                        continue
                    out.append(g)
        elif re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_\-.]*", t):
            out.append(t)
    return list(dict.fromkeys(
        w for w in out if len(w) >= 2 and w not in _CJK_STOP))[:8]


def _llm_keywords(q: str) -> list:
    """qwen 抽词（可选）。只保留原句里出现过的词 —— 否则模型会编实体。"""
    prompt = (
        "从下面这句话里抽出 3~5 个用于检索的关键词或实体（人名、书名、地名、专名优先，"
        "不要解释、不要标点，只输出用空格分隔的词，且必须来自原句）：\n" + q[:200]
    )
    try:
        import requests
        r = requests.post(f"{OLLAMA}/api/generate", timeout=KW_TIMEOUT, json={
            "model": KW_MODEL, "prompt": prompt, "stream": False,
            "options": {"temperature": 0, "num_predict": 40},
        })
        if r.status_code == 200:
            txt = str((r.json() or {}).get("response") or "")
            words = [w.strip() for w in re.split(r"[\s,，、;；/|]+", txt)]
            words = [w for w in words if 1 < len(w) <= 12 and w in q]
            if words:
                return list(dict.fromkeys(words))[:5]
    except Exception as e:
        print(f"⚠️ qwen 抽词失败（用规则结果）: {type(e).__name__}: {e}", file=sys.stderr)
    return []


def keywords_of(query: str) -> list:
    """默认走规则抽词（毫秒级、确定性）；RAG_KW_LLM=1 时才问 qwen。"""
    q = str(query or "").strip()
    if not q:
        return []
    hit = _KW_CACHE.get(q)
    if hit:
        return list(hit)
    kws = _rule_keywords(q)
    use_llm = os.getenv("RAG_KW_LLM", "0") == "1"
    # 短词表（「杜甫 月夜」）规则已足够准确，不必再花几秒问模型
    if use_llm and not (len(kws) >= 2 and len(q) <= 24):
        kws = _llm_keywords(q) or kws
    if not kws:
        kws = [q[:20]]
    _KW_CACHE[q] = tuple(kws)
    return list(kws)


# ---------------------------------------------------------------- 检索
def search(query: str, db: Path, top: int = 5, kw: str = "") -> dict:
    t0 = time.time()
    if not db.exists():
        return {"ok": False, "error": f"索引不存在：{db}", "q": query, "hits": []}
    if kw.strip():
        kws = [w.strip() for w in re.split(r"[\s,，、;；/|]+", kw) if w.strip()][:8]
    else:
        kws = keywords_of(query)
    if not kws:
        return {"ok": False, "error": "没有可用关键词", "q": query, "hits": []}
    # FTS5 查询：每个词加引号（短语匹配），OR 连接
    def _phrase(w: str) -> str:
        return '"' + _seg_cn(w).strip().replace('"', "") + '"'
    match = " OR ".join(_phrase(w) for w in kws if w.strip())
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        # text 用「以命中点为中心」的窗口（≈120 汉字），不要整块 1200 字：
        # reranker 是 512 token 截断的，整块会把命中段截掉。
        rows = con.execute(
            "SELECT path, kind, snippet(docs, 2, '', '', '', 120), "
            "snippet(docs, 2, '[', ']', '…', 24), rank "
            "FROM docs WHERE docs MATCH ? ORDER BY rank LIMIT ?",
            (match, max(1, int(top)))).fetchall()
    except Exception as e:
        con.close()
        return {"ok": False, "error": f"{type(e).__name__}: {e}", "q": query,
                "keywords": kws, "hits": []}
    con.close()
    hits = [{"path": p, "kind": k,
             "snippet": _deseg(s),
             "text": _deseg(b)[:1200],          # 命中点附近窗口，给 reranker 用
             "score": round(float(-(r or 0)), 4)} for p, k, b, s, r in rows]
    out = {"ok": True, "ver": VERSION, "q": query, "keywords": kws, "hits": hits,
           "ms": round((time.time() - t0) * 1000, 1)}
    print(f"[kw] q={query!r} kw={kws} hits={len(hits)} {out['ms']}ms", flush=True)
    return out


# ---------------------------------------------------------------- HTTP
def _fix_bytes(s: str) -> str:
    """http.server 用 iso-8859-1 解请求行：curl 里裸中文 query 会被拆成
    「æ\x9d\x9c…」这类乱码。这里把它还原回真实字节，再试 utf-8 / gb18030。
    （百分号编码的 query 是纯 ASCII，走这里是无损 no-op。）"""
    try:
        raw = s.encode("iso-8859-1")
    except UnicodeEncodeError:
        return s
    for enc in ("utf-8", "gb18030"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return s


def serve(db: Path, port: int, host: str = "0.0.0.0") -> None:
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from urllib.parse import parse_qs, urlparse

    class H(BaseHTTPRequestHandler):
        _docs = None

        def do_GET(self):  # noqa: N802
            u = urlparse(self.path)
            if u.path == "/health":
                body = {"ok": True, "ver": VERSION}
                try:
                    if H._docs is None:
                        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
                        H._docs = con.execute("SELECT count(*) FROM docs").fetchone()[0]
                        con.close()
                    body["docs"] = H._docs
                except Exception as e:
                    body["docs_error"] = f"{type(e).__name__}: {e}"
            elif u.path == "/search":
                qs = parse_qs(_fix_bytes(u.query))
                q = (qs.get("q") or [""])[0]
                kw = (qs.get("kw") or [""])[0]
                top = int((qs.get("top") or ["5"])[0])
                body = search(q, db, top, kw=kw)
            else:
                body = {"ok": False, "error": "not found"}
            raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *a):
            pass

    print(f"🔎 关键词通道 http://{host}:{port}/search?q=…（db={db}）", flush=True)
    ThreadingHTTPServer((host, port), H).serve_forever()


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("--documents", required=True)
    b.add_argument("--db", default="keyword.db")
    s = sub.add_parser("search")
    s.add_argument("query")
    s.add_argument("--db", default="keyword.db")
    s.add_argument("--top", type=int, default=5)
    s.add_argument("--kw", default="", help="跳过抽词，直接给关键词（逗号分隔）")
    v = sub.add_parser("serve")
    v.add_argument("--db", default="keyword.db")
    v.add_argument("--port", type=int, default=8011)
    v.add_argument("--host", default="0.0.0.0")
    args = ap.parse_args()

    if args.cmd == "build":
        build(Path(args.documents), Path(args.db))
    elif args.cmd == "search":
        out = search(args.query, Path(args.db), args.top, kw=args.kw)
        print(json.dumps(out, ensure_ascii=False, indent=1))
    else:
        serve(Path(args.db), args.port, args.host)
    return 0


if __name__ == "__main__":
    sys.exit(main())
