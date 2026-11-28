"""B站视频理解：下载 → 抽帧 → 视觉理解 → 总结。

给 LLM 工具 video_understand 用的轮子：
  用户转 B 站卡片 / 发 B 站链接，LLM 判断「需要看视频本体」时调用工具 →
  入队 → 后台 worker 下载视频（B 站 player API 拿 mp4 分段直链，零额外依赖）→
  ffmpeg 抽 N 帧 → GLM 视觉逐帧描述 → DeepSeek 综合总结 → 发回结果。

依赖：ffmpeg（Dockerfile 已装）、httpx（已有）。不需要 yt-dlp。
"""
from core import tuning
import asyncio
import json
import math
import os
import re
from pathlib import Path

import httpx
from nonebot import logger

BILI_PLAYER_API = "https://api.bilibili.com/x/player/v2"
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
_REFERER = "https://www.bilibili.com/"
_BV_RE = re.compile(r"(BV[0-9A-Za-z]{10})")
_SHORT_RE = re.compile(r"https?://b23\.tv/[0-9A-Za-z]+")

DATA_ROOT = Path("data") / "video_understand"

# 整段视频均匀抽帧：默认每 5 秒一帧（用户 2026-11-17）。
# 帧数有上限——太长会自动变稀（60 帧 × 视觉逐帧已经很慢很贵）。
FRAME_INTERVAL = tuning.f("VIDEO_FRAME_INTERVAL")
MAX_FRAMES = tuning.i("VIDEO_MAX_FRAMES")
# 字幕密集处额外取的高清关键帧上限（配合均匀抽帧一起看）
MAX_KEY_FRAMES = tuning.i("VIDEO_MAX_KEY_FRAMES")
MAX_FRAME_DESC = tuning.i("VIDEO_MAX_FRAME_DESC")


def extract_bvid(url: str) -> str:
    return (_BV_RE.search(url or "") or _BV_RE.search(url or "")).group(1) if _BV_RE.search(url or "") else ""


async def _expand_short(url: str) -> str:
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=True,
                                     headers={"User-Agent": _UA}) as c:
            r = await c.get(url)
            return str(r.url)
    except Exception as e:
        logger.debug(f"[VIDEO] 短链展开失败: {type(e).__name__}: {e}")
        return url


def _bili_headers(buvid: str = "") -> dict:
    h = {"User-Agent": _UA, "Referer": _REFERER}
    if buvid:
        h["Cookie"] = f"buvid3={buvid}"
    return h


async def bili_stream_urls(bvid: str, cid: int, buvid: str = "") -> list:
    """
    拿 B 站视频 mp4 分段直链。返回 [[候选url...], ...]（外层=分段，内层=主/备 CDN）。
    失败返回空列表（不抛）。
    """
    if not bvid or not cid:
        logger.warning(f"[VIDEO] 拿视频流：bvid/cid 为空（bvid={bvid!r} cid={cid}）")
        return []
    try:
        async with httpx.AsyncClient(timeout=12, headers=_bili_headers(buvid)) as c:
            # 用 playurl + fnval=1 强制要 mp4 分段直链；
            # 直接打 x/player/v2 很多视频 durl 是空的（只给 DASH）。
            r = await c.get(
                "https://api.bilibili.com/x/player/playurl",
                params={"bvid": bvid, "cid": cid, "fnval": 1, "fnver": 0, "fourk": 1},
            )
            payload = r.json() or {}
            data = payload.get("data") or {}
            durl = data.get("durl") or []

            def _norm(u):
                u = str(u or "")
                if u.startswith("//"):
                    u = "https:" + u
                return u or ""

            segments = []
            for x in durl:
                cands = []
                # 主 url + backup_url 都收集，主 CDN 被 567 时能换备用的
                cands.append(_norm(x.get("url")))
                for _u in (x.get("backup_url") or []):
                    _n = _norm(_u)
                    if _n and _n not in cands:
                        cands.append(_n)
                cands = [u for u in cands if u]
                if cands:
                    segments.append(cands)
            if segments:
                logger.info(f"[VIDEO] 拿到 {len(segments)} 段视频直链"
                            f"（共 {sum(len(s) for s in segments)} 个候选 CDN）")
            else:
                logger.warning(
                    f"[VIDEO] 拿视频流返回空 durl：HTTP {r.status_code} "
                    f"code={payload.get('code')} msg={str(payload.get('message'))[:100]!r}"
                )
            return segments
    except Exception as e:
        logger.warning(f"[VIDEO] 拿视频流失败 bvid={bvid} cid={cid}: {type(e).__name__}: {e}")
        return []


async def download_video(bvid: str, cid: int, out_dir: Path, buvid: str = "") -> Path | None:
    """下载 B 站视频到 out_dir，返回视频文件路径；失败返回 None。"""
    segments = await bili_stream_urls(bvid, cid, buvid=buvid)
    if not segments:
        return None
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    parts: list[Path] = []
    try:
        logger.info(f"[VIDEO] 开始下载 {len(segments)} 段视频…")
        # B 站 CDN 对 Referer/Cookie 很敏感（567 = 风控）。
        # 多套请求头轮着试：视频页 Referer → 根域名 Referer → 不带 Referer。
        _base = {"User-Agent": _UA}
        if buvid:
            _base["Cookie"] = f"buvid3={buvid}"
        _v1 = dict(_base); _v1["Referer"] = f"https://www.bilibili.com/video/{bvid}"
        _v2 = dict(_base); _v2["Referer"] = "https://www.bilibili.com/"
        _v3 = dict(_base)
        header_variants = [_v1, _v2, _v3]
        async with httpx.AsyncClient(timeout=180, follow_redirects=True) as c:
            for i, cands in enumerate(segments):
                p = out_dir / f"part_{i}.mp4"
                got = False
                for u in cands:
                    for h in header_variants:
                        try:
                            r = await c.get(u, headers=h)
                        except Exception as e:
                            logger.warning(f"[VIDEO] 分段 {i + 1} 候选下载异常: {type(e).__name__}")
                            continue
                        if r.status_code == 200 and len(r.content) >= 1024:
                            p.write_bytes(r.content)
                            parts.append(p)
                            got = True
                            logger.info(f"[VIDEO] 分段 {i + 1}/{len(segments)} 下载完成"
                                        f"（{len(r.content) / 1024 / 1024:.1f} MB）")
                            break
                        logger.warning(f"[VIDEO] 分段 {i + 1} 候选 HTTP {r.status_code}，换下一套头")
                    if got:
                        break
                if not got:
                    raise RuntimeError(f"分段 {i} 所有候选 CDN / 请求头组合都失败")
        if not parts:
            return None
        final = out_dir / f"{bvid}.mp4"
        if len(parts) == 1:
            parts[0].rename(final)
            logger.info(f"[VIDEO] 单段视频就绪：{final.stat().st_size / 1024 / 1024:.1f} MB")
            return final
        # 多段：ffmpeg concat 拼起来
        logger.info(f"[VIDEO] {len(parts)} 段视频，ffmpeg 合并中…")
        list_file = out_dir / "concat.txt"
        list_file.write_text("".join(f"file '{p.resolve()}'\n" for p in parts), encoding="utf-8")
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y", "-f", "concat", "-safe", "0",
            "-i", str(list_file), "-c", "copy", str(final),
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()
        if final.exists() and final.stat().st_size > 1024:
            logger.info(f"[VIDEO] 合并完成：{final.stat().st_size / 1024 / 1024:.1f} MB")
            return final
        logger.warning("[VIDEO] 合并后文件缺失或过小")
        return None
    except Exception as e:
        logger.warning(f"[VIDEO] 下载失败 bvid={bvid}: {type(e).__name__}: {e}")
        return None
    finally:
        for p in parts:
            try:
                p.unlink(missing_ok=True)
            except Exception:
                pass
        try:
            (out_dir / "concat.txt").unlink(missing_ok=True)
        except Exception:
            pass


async def _probe_duration(video_path: Path) -> float:
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "quiet", "-show_entries", "format=duration",
            "-of", "csv=p=0", str(video_path),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await proc.communicate()
        return float(out.decode().strip() or 0)
    except Exception:
        return 0.0


def key_times_from_segments(segments: list, max_n: int = MAX_KEY_FRAMES) -> list:
    """
    从字幕段里挑「关键时间点」（每段中点），均匀取 max_n 个。
    没有字幕返回 [] —— 这时候就只靠整段均匀抽帧。
    """
    if not segments:
        return []
    mids = []
    for seg in segments:
        try:
            s, e, txt = seg[0], seg[1], str(seg[2] or "").strip()
        except Exception:
            continue
        if not txt:
            continue
        try:
            mid = (float(s) + float(e)) / 2.0
        except Exception:
            continue
        if mid >= 0:
            mids.append(mid)
    if not mids:
        return []
    mids.sort()
    if len(mids) <= max_n:
        return mids
    step = (len(mids) - 1) / max(1, max_n - 1)
    return [mids[int(i * step)] for i in range(max_n)]


async def sample_frames(
    video_path: Path,
    out_dir: Path,
    interval: float = FRAME_INTERVAL,
    max_frames: int = MAX_FRAMES,
    key_times: list | None = None,
) -> list:
    """
    抽帧：整段视频每 interval 秒一帧（均匀、上限 max_frames），
    再在 key_times 这些关键时间点各取一张高清帧（配合字幕找到的关注位置）。
    返回 [(时间秒, 帧路径)]，按时间升序；可能为空列表。
    """
    dur = await _probe_duration(video_path)
    if dur <= 0:
        dur = 60.0
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    # 清掉上一次可能残留的帧
    for old in list(out_dir.glob("frame_*.jpg")) + list(out_dir.glob("key_*.jpg")):
        try:
            old.unlink(missing_ok=True)
        except Exception:
            pass

    # ① 整段均匀抽帧：每 interval 秒一帧，太长就按上限变稀
    n_uniform = max(1, min(math.ceil(dur / max(1.0, interval)), max_frames))
    fps = n_uniform / dur
    logger.info(f"[VIDEO] 视频时长 {dur:.0f}s → 均匀抽 {n_uniform} 帧（fps={fps:.4f}）")
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y", "-i", str(video_path),
            "-vf", f"fps={fps:.5f}", "-q:v", "3", str(out_dir / "frame_%d.jpg"),
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()
    except Exception as e:
        logger.warning(f"[VIDEO] 均匀抽帧失败: {type(e).__name__}: {e}")

    frames: list[tuple[float, Path]] = []
    for i, fp in enumerate(sorted(out_dir.glob("frame_*.jpg"))[:n_uniform], 1):
        # fps filter 的第 i 帧约在 (i-0.5)/fps 秒
        ts = (i - 0.5) / fps if fps > 0 else 0.0
        frames.append((round(ts, 1), fp))
    logger.info(f"[VIDEO] 均匀抽帧实际拿到 {len(frames)} 帧")

    # ② 关键时间点的高清帧（原分辨率，质量更高一点）
    for k, t in enumerate(key_times or []):
        try:
            t = max(0.0, min(float(t), max(0.0, dur - 0.1)))
        except Exception:
            continue
        kp = out_dir / f"key_{k:02d}.jpg"
        try:
            proc = await asyncio.create_subprocess_exec(
                "ffmpeg", "-y", "-ss", f"{t:.2f}", "-i", str(video_path),
                "-frames:v", "1", "-q:v", "2", str(kp),
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.wait()
        except Exception:
            continue
        if kp.exists() and kp.stat().st_size > 0:
            frames.append((round(t, 1), kp))
    logger.info(f"[VIDEO] 关键帧抽取完成，共 {len(frames)} 帧（均匀+关键）")

    frames.sort(key=lambda x: x[0])
    return frames


async def understand_frames(frames: list, title: str = "") -> str:
    """
    多帧 → GLM 视觉**按时间顺序**逐帧描述。
    frames 是 [(时间秒, Path)]，描述里带上时间戳，好让 DeepSeek 还原时间线。
    """
    try:
        from services import glm
    except Exception:
        return ""
    if not glm.available():
        return ""
    prompt = (
        "这是从一段视频里按时间顺序抽出的画面。只描述你**真正看到的**："
        "人物/场景/在做什么/画面里的关键信息（文字、道具、动作）。"
        "一句话，60 字内，不要猜剧情、不要加戏。"
    )
    # 并发上限 4：帧多时别一张张串行等；顺序靠返回后按索引重排，不乱。
    sem = asyncio.Semaphore(4)
    total = len(frames)
    logger.info(f"[VIDEO] 开始视觉理解 {total} 帧（并发 4）…")

    async def _one(item):
        idx, (ts, fp) = item
        async with sem:
            try:
                data = fp.read_bytes()
            except Exception:
                logger.warning(f"[VIDEO] 帧 {idx}/{total} 读文件失败")
                return idx, ""
            desc = await glm.describe_image(data, prompt, max_tokens=MAX_FRAME_DESC)
            desc = str(desc or "").strip()
            logger.info(f"[VIDEO] 帧 {idx}/{total}（≈{ts:.0f}s）完成："
                        f"{'有内容' if desc else '空'}")
            return idx, (f"≈{ts:.0f}s：{desc}" if desc else "")

    results = await asyncio.gather(*[_one(it) for it in enumerate(frames, 1)])
    descs = [d for _, d in sorted(results, key=lambda x: x[0]) if d]
    logger.info(f"[VIDEO] 视觉理解结束：{len(descs)}/{total} 帧有描述")
    return "\n".join(descs)


async def summarize(title: str, desc: str, frame_desc: str, subtitle: str = "") -> str:
    """DeepSeek 把标题/简介/字幕/抽帧描述综合成一段内容总结。"""
    try:
        from .llm import XAI_API_KEY, XAI_BASE_URL, XAI_MODEL
    except Exception:
        return ""
    if not XAI_API_KEY:
        return ""
    parts = [f"B站视频标题：{title or '未知'}"]
    if subtitle:
        parts.append(f"【CC字幕节选】\n{subtitle[:3000]}")
    if frame_desc:
        parts.append(f"【抽帧画面描述（{len(frame_desc.splitlines())} 帧，按时间顺序）】\n{frame_desc}")
    if desc:
        parts.append(f"【视频简介】\n{desc[:600]}")
    prompt = (
        "\n\n".join(parts)
        + "\n\n请综合以上信息，用中文总结这个视频讲了什么：先说核心内容（2~3句），"
        "再挑 2~3 个值得注意的亮点或槽点。不要客套、不要说『这个视频』开头，直接给结论。"
    )
    logger.info(f"[VIDEO] 开始 DeepSeek 总结（字幕 {len(subtitle or '')} 字 / "
                f"帧描述 {len(frame_desc or '')} 字）")
    try:
        async with httpx.AsyncClient(timeout=60) as c:
            r = await c.post(
                f"{XAI_BASE_URL}/chat/completions",
                headers={"Authorization": f"Bearer {XAI_API_KEY}",
                         "Content-Type": "application/json"},
                json={"model": XAI_MODEL,
                      "messages": [{"role": "user", "content": prompt}],
                      "temperature": 0.3, "max_tokens": 600},
            )
            if r.status_code != 200:
                logger.warning(f"[VIDEO] DeepSeek 总结 HTTP {r.status_code}")
                return ""
            out = str(r.json()["choices"][0]["message"].get("content") or "").strip()
            logger.info(f"[VIDEO] DeepSeek 总结完成：{len(out)} 字")
            return out
    except Exception as e:
        logger.warning(f"[VIDEO] 总结失败: {type(e).__name__}: {e}")
        return ""


async def cleanup_dir(d: Path, keep_files: int = 0) -> None:
    """清掉 video_understand 临时目录里的文件（跑完就删，防磁盘占用）。"""
    try:
        d = Path(d)
        if not d.exists():
            return
        for f in d.iterdir():
            if f.is_file():
                try:
                    f.unlink(missing_ok=True)
                except Exception:
                    pass
    except Exception as e:
        logger.debug(f"[VIDEO] 清理临时目录失败: {type(e).__name__}: {e}")
