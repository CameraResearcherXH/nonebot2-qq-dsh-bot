# plugins/cover_api/__init__.py
"""
外部翻唱 API（租云算力模式）

给外部机器用：发个请求过来，服务器跑完「下载→分离→变声→混音」，
直接把 mp3 二进制回给它。**完全不经过 QQ**。

认证：Header `X-Api-Key`（或 `?key=` 查询参数）。

接口：
  GET  /api/cover/ping     健康检查 + 认证测试
  GET  /api/cover/stats    各 key 的调用统计（审计/计费用）
  POST /api/cover          跑一遍翻唱，返回 audio/mpeg

POST /api/cover
  Header: X-Api-Key: <key>
  Body (JSON):
    {
      "query":  "晴天"                        # 歌名 / 网易云链接 / 歌曲 id，必填
      "mode":   "cover" | "acapella",         # 默认 cover（人声+伴奏）
      "gender": "male" | "female" | "",       # 可选，强制覆盖性别判断
      "pitch":  0                             # 可选，强制覆盖音高（半音）
    }
  返回: audio/mpeg，附带响应头
    X-Cover-Title   歌名（URL 编码，中文安全）
    X-Cover-Pitch   实际使用的半音数
    X-Cover-Gender  判定出的原唱性别
    X-Cover-Cost    耗时（秒）
    X-Cover-Mode    cover / acapella
    X-Cover-Size-MB 成品大小
    X-Cover-Owner   这把这个 key 属于谁

⚠️ 这是同步长请求：正常 2~5 分钟。客户端超时必须设 ≥ 600 秒，
   httpx 的默认超时 5 秒会直接失败。

配置（服务器 .env）：
  # 多个 key，逗号/分号/换行分隔；写成 名字:key 就能在日志和统计里看到是谁在用
  COVER_API_KEYS=alice:ak_xxx,bob:ak_yyy,carol:ak_zzz
  # 兼容旧的单 key 写法
  COVER_API_KEY=ak_xxx
"""
import os
import re
import time
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, JSONResponse
from starlette.background import BackgroundTask
from nonebot import get_driver, logger

router = APIRouter()


# ================= key 管理 =================
def parse_keys(raw: str) -> dict:
    """
    解析 COVER_API_KEYS，返回 {key: owner}。

    支持三种写法（逗号 / 分号 / 换行分隔都行）：
        ak_aaa,ak_bbb,ak_ccc          → 自动命名 key1/key2/key3
        alice:ak_aaa,bob:ak_bbb       → 名字:key，日志和统计里能看出是谁
    """
    out = {}
    for i, item in enumerate(re.split(r"[,;\n]+", str(raw or ""))):
        item = item.strip()
        if not item:
            continue
        if ":" in item:
            name, _, key = item.partition(":")
            key = key.strip()
            name = name.strip() or f"key{i + 1}"
        else:
            key, name = item, f"key{i + 1}"
        if key:
            out[key] = name
    return out


COVER_API_KEYS_RAW = (os.getenv("COVER_API_KEYS", "").strip()
                      or os.getenv("COVER_API_KEY", "").strip())
KEYS = parse_keys(COVER_API_KEYS_RAW)
# owner -> {"calls": n, "fails": n, "last": ts, "mb": float}
STATS = {}


def identify(request: Request) -> tuple:
    """返回 (key, owner)；认证失败返回 ("", "")。定长比较，避免时序侧信道。"""
    key = (request.headers.get("x-api-key")
           or request.query_params.get("key")
           or "").strip()
    if not key or not KEYS:
        return "", ""
    for k, owner in KEYS.items():
        if len(key) == len(k) and key == k:
            return k, owner
    return "", ""


# gender 字段的别名表（外部机器可能传中文、单字母、英文全称）
_GENDER_ALIASES = {
    "male": "male", "m": "male", "man": "male", "男": "male", "男生": "male", "男声": "male",
    "female": "female", "f": "female", "woman": "female", "女": "female", "女生": "female",
    "女声": "female",
}


def _cleanup_files(paths) -> None:
    """FileResponse 发完就删。单段式会同时留下「语音版」和「完整版」两个文件，
    只删发出去的那个，另一个会一直堆在 data/temp_audio/ 里把磁盘吃掉。"""
    for p in paths:
        try:
            Path(p).unlink(missing_ok=True)
        except Exception:
            pass


def bump(owner: str, ok: bool, mb: float = 0.0) -> None:
    st = STATS.setdefault(owner, {"calls": 0, "fails": 0, "last": 0, "mb": 0.0})
    st["calls"] += 1
    if not ok:
        st["fails"] += 1
    st["last"] = int(time.time())
    st["mb"] = round(st["mb"] + float(mb or 0), 2)


# ================= 路由 =================
@router.get("/api/cover/ping")
async def cover_ping(request: Request):
    """健康检查 + 认证测试。客户端调试时先打这个。"""
    configured = bool(KEYS)
    key, owner = identify(request)

    # 👇 把「算力机支持什么 / 会不会走单段式」一起返回。
    #    外部机器 ping 一下就知道当前请求会不会走「算力机一次到底」（single）
    #    还是「服务器两段式」（legacy，会用服务器 ffmpeg 兜底混音）。
    #    ⚠️ 整段包 try：ping 是最基础的探活接口，不能因为别的插件
    #       加载异常就 500 —— 那会让外部机器误判成「整个服务挂了」。
    caps = []
    hb = 0.0
    single = False
    try:
        from plugins.tool_dispatcher import STATUS as _NODE
        caps = sorted(_NODE.get("caps") or [])
        hb = float(_NODE.get("last_heartbeat") or 0)
        single = ("cover" in (_NODE.get("caps") or set())) and (
            os.getenv("COVER_SINGLE_STAGE", "true").strip().lower() != "false"
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[COVER-API] ping 读算力机状态失败: {type(e).__name__}: {e}")
    age = round(time.time() - hb, 1) if hb else None

    return JSONResponse(
        {
            "ok": bool(key),
            "auth": bool(key),
            "owner": owner,
            "key_configured": configured,
            "keys_total": len(KEYS),
            "time": int(time.time()),
            # 链路诊断
            "node_online": bool(hb) and age is not None and age < 30,
            "node_heartbeat_age_sec": age,
            "node_caps": caps,
            "stage": "single" if single else "legacy",
            "stage_hint": ("算力机一次到底，服务器不做音频处理"
                           if single else
                           "走旧两段式，服务器会用 ffmpeg 兜底（确认算力机 node.py 是新版）"),
            "hint": ("服务端未配置 COVER_API_KEYS，接口不可用"
                     if not configured else
                     (f"key 正确，你是 {owner}" if key else "key 不对")),
        },
        status_code=200 if key else (503 if not configured else 401),
    )


@router.get("/api/cover/stats")
async def cover_stats(request: Request):
    """各 key 的调用统计 + 当前并发占用。任何一个有效 key 都能查（方便你自己审计）。"""
    key, owner = identify(request)
    if not key:
        return JSONResponse({"ok": False, "error": "key 认证失败"}, status_code=401)
    try:
        from plugins.song_cover import cover_queue_state
        queue = cover_queue_state()
    except Exception:
        queue = {}
    return JSONResponse({
        "ok": True,
        "you": owner,
        "keys_total": len(KEYS),
        "usage": [{"owner": o, **s} for o, s in STATS.items()],
        "never_used": [o for o in KEYS.values() if o not in STATS],
        "queue": queue,
    })


@router.post("/api/cover")
async def cover_api(request: Request):
    if not KEYS:
        return JSONResponse(
            {"ok": False, "error": "服务端未配置 COVER_API_KEYS（.env）"},
            status_code=503,
        )
    key, owner = identify(request)
    if not key:
        peer = request.client.host if request.client else "?"
        logger.warning(f"[COVER-API] 认证失败 from={peer}")
        return JSONResponse({"ok": False, "error": "key 认证失败"}, status_code=401)

    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}

    query = str(body.get("query") or "").strip()
    if not query:
        bump(owner, False)
        return JSONResponse(
            {"ok": False, "error": "缺少 query（歌名 / 网易云链接 / 歌曲 id）"},
            status_code=400,
        )

    mode = str(body.get("mode") or "cover").strip().lower()
    with_inst = mode not in ("acapella", "clean", "vocal", "清唱", "纯人声")
    # gender 支持中文/英文多种写法；也可以直接写在 query 末尾
    # （`{"query": "晴天 女"}` 由 song_cover.run_cover 解析，两边行为一致）
    gender = _GENDER_ALIASES.get(str(body.get("gender") or "").strip().lower(), "")
    pitch = body.get("pitch")
    try:
        pitch = int(pitch) if pitch is not None else None
    except Exception:
        pitch = None

    from plugins.song_cover import run_cover

    peer = request.client.host if request.client else "?"
    logger.info(
        f"[COVER-API] {owner} 请求 query={query!r} "
        f"mode={'cover' if with_inst else 'acapella'} "
        f"gender={gender or 'auto'} pitch={pitch} from={peer}"
    )
    started = time.time()
    try:
        res = await run_cover(
            query, with_inst=with_inst, notify=None,
            gender_override=gender, pitch_override=pitch,
        )
    except Exception as e:
        cost = time.time() - started
        bump(owner, False)
        logger.error(f"[COVER-API] {owner} 异常（{cost:.1f}s）: {type(e).__name__}: {e}")
        return JSONResponse(
            {"ok": False, "error": f"{type(e).__name__}: {e}", "cost": round(cost, 1)},
            status_code=500,
        )

    cost = time.time() - started
    if not res.get("ok"):
        bump(owner, False)
        _err = str(res.get("error") or "未知错误")
        # 主人发了 /翻唱 off → 503（暂时不服务），别让客户端以为是它自己参数错了
        _closed = "翻唱已关闭" in _err
        logger.warning(f"[COVER-API] {owner} 失败（{cost:.1f}s）: {_err}")
        return JSONResponse(
            {"ok": False, "error": _err, "closed": _closed, "cost": round(cost, 1)},
            status_code=503 if _closed else 500,
        )

    # ⚠️ 单段式下 res["path"] 是「语音版」—— 算力机为了 QQ 语音消息
    #    已经把它截到高潮了（约 2 分钟）。完整版在 res["full_path"]。
    #    外部 API 必须给整首，绝不能返回截断版（否则客户拿到的就是半首歌）。
    full = res.get("full_path")
    path = Path(full) if (full and Path(full).exists()) else Path(res["path"])
    if not path.exists():
        bump(owner, False)
        return JSONResponse({"ok": False, "error": "成品文件已丢失"}, status_code=500)

    size_mb = round(os.path.getsize(path) / 1048576, 2)
    bump(owner, True, size_mb)
    logger.info(
        f"[COVER-API] {owner} 成功（{cost:.1f}s）《{res['title']}》"
        f"{size_mb}MB（{'完整版' if full else '成品'}）pitch={res['pitch']}"
    )

    headers = {
        # HTTP 头只能是 ASCII，中文歌名 URL 编码
        "X-Cover-Title": quote(str(res.get("title") or "")),
        # 单段式时 pitch 由算力机按实测 F0 现算，服务器拿不到数值 → 标 auto
        "X-Cover-Pitch": str(res.get("pitch") if res.get("pitch") is not None else "auto"),
        "X-Cover-Gender": str(res.get("gender") or ""),
        "X-Cover-Cost": f"{cost:.1f}",
        "X-Cover-Mode": "cover" if with_inst else "acapella",
        "X-Cover-Size-MB": str(size_mb),
        # single=算力机一次到底（服务器不碰 ffmpeg）
        # legacy=服务器两段式（会用 ffmpeg 兜底混音）
        "X-Cover-Stage": str(res.get("stage") or "unknown"),
        "X-Cover-Owner": quote(owner),
    }
    safe_name = f"{res.get('title') or 'cover'}.mp3".replace("/", "_").replace("\\", "_")

    # 单段式会留两个文件（语音版 + 完整版），两个都要删，否则磁盘会被堆满
    to_clean = [path]
    _voice = res.get("path")
    if _voice:
        _vp = Path(_voice)
        if _vp != path and _vp.exists():
            to_clean.append(_vp)

    return FileResponse(
        path,
        media_type="audio/mpeg",
        filename=safe_name,
        headers=headers,
        # 发完就删，别把磁盘堆满
        background=BackgroundTask(_cleanup_files, to_clean),
    )


if KEYS:
    logger.info(
        f"[COVER-API] 已加载 {len(KEYS)} 个 key：{', '.join(KEYS.values())}"
    )
else:
    logger.warning("[COVER-API] 未配置 COVER_API_KEYS，外部翻唱接口不可用")

driver = get_driver()
driver.server_app.include_router(router)

