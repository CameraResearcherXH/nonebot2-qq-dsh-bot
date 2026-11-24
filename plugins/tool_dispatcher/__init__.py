# 服务器端：plugins/tool_dispatcher/__init__.py
from core import tuning
import time
import json
import os
import asyncio
import subprocess
import aiohttp
import aiofiles
from pathlib import Path
from fastapi import APIRouter, UploadFile, File, Form
from fastapi.responses import FileResponse, JSONResponse
from nonebot import get_driver, on_message, logger
from nonebot.adapters.onebot.v11 import Bot, MessageEvent, MessageSegment, GroupMessageEvent
from nonebot.rule import Rule

logger.info("🚀 [加载检测] 音频分离调度中心正在初始化...")

# ⚠️ 从 .env 读，别用硬编码：
#    8080 一旦对外（比如给外部客户调 /api/cover），这个 secret 就是
#    「拉任意任务文件 / 伪造心跳 / 提交任务」的万能钥匙。
#    算力机 node.env 里的 API_SECRET 必须改成同一个值。
API_SECRET = os.getenv("API_SECRET", "").strip() or "my_super_secret_key_123"
if API_SECRET == "my_super_secret_key_123":
    logger.warning(
        "⚠️ [SECURITY] API_SECRET 用的是默认值 my_super_secret_key_123。"
        "8080 对外暴露前，务必在 .env 设一个长随机 API_SECRET，"
        "并同步改算力机 node.env 里的 API_SECRET。"
    )

# 心跳超时：算力机超过这么久没心跳就算离线。
# 原来写死 30 秒 —— 算力机心跳间隔只要稍长一点（或网络抖一下），
# 就会被判离线，于是语音直接降级到云端（听感上就是「怎么又是龙呼呼」）。
HEARTBEAT_TIMEOUT = tuning.i("HEARTBEAT_TIMEOUT_SEC")
TEMP_DIR = Path("data/temp_audio")
TEMP_DIR.mkdir(parents=True, exist_ok=True)

STATUS = {"last_heartbeat": 0.0, "tasks": {}, "caps": set()}
WAITING_FOR_UPLOAD = set()
USER_STATE = {}

# ================= 任务落盘（2026-11-24）=================
# 用户：「有几次的画图和翻唱都没出结果」。
# 根因：STATUS 只在内存里，机器人一重启：
#   ① 算力机算完回传 `/api/tasks/complete` → `task not found` → **成品直接丢**；
#   ② 等待协程被取消，就算文件还在也没人发到群里。
# 现在：任务表落盘 `data/node_tasks.json`，启动时恢复；
#   带 `deliver` 信息的任务（画图/翻唱）重启后会继续等，完成后补发到原会话。
TASKS_FILE = Path("data/node_tasks.json")
TASK_TTL = 6 * 3600


def save_tasks() -> None:
    try:
        data = {}
        now = time.time()
        for k, v in STATUS["tasks"].items():
            if not isinstance(v, dict):
                continue
            v.setdefault("created", now)
            data[k] = v
        TASKS_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = TASKS_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, default=str),
                       encoding="utf-8")
        tmp.replace(TASKS_FILE)
    except Exception as e:
        logger.debug(f"[算力机] 任务落盘失败: {type(e).__name__}: {e}")


def load_tasks() -> None:
    if not TASKS_FILE.exists():
        return
    try:
        data = json.loads(TASKS_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning(f"[算力机] 任务文件读不了（忽略）: {type(e).__name__}: {e}")
        return
    now = time.time()
    n = 0
    for k, v in (data or {}).items():
        if not isinstance(v, dict):
            continue
        ct = float(v.get("complete_time") or v.get("created") or 0)
        if ct and now - ct > TASK_TTL:
            continue
        STATUS["tasks"][k] = v
        n += 1
    if n:
        logger.info(f"[算力机] 从磁盘恢复 {n} 个任务（重启前在跑的）")

# 任务被删掉之后，文件仍然要能被重试下载 → 单独记一份 task_id → input 路径
FILE_REGISTRY: dict[str, str] = {}
FILE_REGISTRY_MAX = 64

router = APIRouter()


def _registry_put(task_id: str, path: str):
    if not path:
        return
    FILE_REGISTRY[task_id] = path
    if len(FILE_REGISTRY) > FILE_REGISTRY_MAX:
        for k in list(FILE_REGISTRY.keys())[: len(FILE_REGISTRY) - FILE_REGISTRY_MAX]:
            FILE_REGISTRY.pop(k, None)


@router.get("/api/heartbeat")
async def heartbeat(secret: str = "", caps: str = ""):
    """
    caps：算力机上报自己支持的任务类型（逗号分隔），例如
          "audio_separation,cover,rvc,rvc_mix,split,tts"

    服务器靠它决定走单段式（cover，只传一次 mp3）还是旧两段式
    （split + rvc_mix，要往返两次 76MB wav）。
    老版本 node.py 不上报 caps，就会自动走两段式，不会卡住。
    """
    if secret != API_SECRET: return {"error": "auth failed"}
    STATUS["last_heartbeat"] = time.time()
    if caps:
        parsed = {c.strip() for c in caps.split(",") if c.strip()}
        if parsed != STATUS.get("caps"):
            logger.info(f"[算力机] 支持的任务类型: {', '.join(sorted(parsed))}")
        STATUS["caps"] = parsed
    return {"status": "ok"}

@router.get("/api/tasks/pull")
async def pull_task(secret: str = ""):
    if secret != API_SECRET: return {"error": "auth failed"}
    for task_id, task in list(STATUS["tasks"].items()):
        if task.get("status") == "pending":
            task["status"] = "processing"
            # 拉走时就把输入文件登记好：即使任务对象后面被删，node.py 也能重试下载
            _registry_put(task_id, task.get("input", ""))
            save_tasks()
            return {"has_task": True, "task_id": task_id, "task": task}
        elif task.get("status") == "completed" and time.time() - task.get("complete_time", 0) > 120:
            STATUS["tasks"].pop(task_id, None)
        elif task.get("status") in ("failed", "timeout") and time.time() - task.get("complete_time", 0) > 120:
            STATUS["tasks"].pop(task_id, None)
    return {"has_task": False}


@router.get("/api/tasks/status")
async def tasks_status(secret: str = ""):
    """调试用：一眼看清算力机心跳和所有任务状态。"""
    if secret != API_SECRET: return {"error": "auth failed"}
    age = time.time() - STATUS["last_heartbeat"] if STATUS["last_heartbeat"] else -1
    return {
        "heartbeat_age_sec": round(age, 1),
        "online": bool(STATUS["last_heartbeat"]) and age < HEARTBEAT_TIMEOUT,
        "caps": sorted(STATUS.get("caps") or []),
        "tasks": {
            tid: {"type": t.get("type"), "status": t.get("status"), "input": t.get("input")}
            for tid, t in STATUS["tasks"].items()
        },
    }


# 👇 用 FileResponse：Starlette 自己走线程池 + 支持 Range 断点续传，
#    比手写 StreamingResponse 稳得多（之前手写版一遇到慢连接就整个卡住）
@router.get("/api/download/{task_id}")
async def download_input(task_id: str, secret: str = "", field: str = ""):
    """
    field 参数：取任务里的其它文件字段。
      默认/空       → task["input"]
      extra_input  → task["extra_input"]（翻唱任务里的伴奏）
    这样算力机拉第二个文件不用新加路由。
    """
    if secret != API_SECRET:
        return JSONResponse({"error": "auth failed"}, status_code=403)

    task = STATUS["tasks"].get(task_id) or {}
    if field:
        file_path = task.get(field) or ""
    else:
        file_path = task.get("input") or FILE_REGISTRY.get(task_id) or ""

    if not file_path or not os.path.exists(file_path):
        logger.error(f"❌ [下载失败] 找不到任务文件: {task_id}（path={file_path!r}）")
        # 必须返回真错误码：原来返回 200+JSON，node.py 会把 JSON 当音频存下来，
        # 后面 ffmpeg 报一堆莫名其妙的错，看起来就像"下载被卡住"
        return JSONResponse(
            {"error": "file not found", "task_id": task_id},
            status_code=404,
        )

    file_size = os.path.getsize(file_path)
    logger.info(f"📥 [云端下载] 开始发送: {file_path}（{file_size / 1024 / 1024:.2f} MB）task={task_id}")
    return FileResponse(
        file_path,
        media_type="application/octet-stream",
        filename=Path(file_path).name,
        headers={"X-Task-Id": task_id},
    )


@router.post("/api/tasks/complete")
async def complete_task(
    task_id: str = Form(...),
    file: UploadFile = File(None),
    file2: UploadFile = File(None),
    vocal: UploadFile = File(None),
    inst: UploadFile = File(None),
    result: str = Form(""),
    secret: str = Form("")
):
    if secret != API_SECRET: return {"error": "auth failed"}
    task = STATUS["tasks"].get(task_id)
    if not task: return {"error": "task not found"}
    
    if vocal and inst:
        vocal_path = TEMP_DIR / f"vocal_{task_id}.wav"
        inst_path = TEMP_DIR / f"inst_{task_id}.wav"
        async with aiofiles.open(vocal_path, 'wb') as f:
            await f.write(await vocal.read())
        async with aiofiles.open(inst_path, 'wb') as f:
            await f.write(await inst.read())
        task["output"] = {"vocal": str(vocal_path), "inst": str(inst_path)}
        logger.info(f"[算力机] 分离任务(双文件)完成: {task_id}")
    elif file and task.get("type") == "probe":
        # /测歌 A/B：成品 mp3（最多 2 个）+ 判定 JSON 一起回。
        # 普通任务要么带文件要么带 JSON，只有测歌两种都要，所以单独一支。
        try:
            payload = json.loads(result) if result else {}
        except Exception:
            payload = {"error": "bad result json"}
        if not isinstance(payload, dict):
            payload = {"error": "bad result json"}
        # 算力机在 payload["files"] 里给了友好名（vocal / ab_rmvpe / ab_crepe），
        # 落盘时沿用它，否则用户收到的群文件叫 out_probe_1762_0.mp3。
        _meta = payload.get("files") or []
        out_files = []
        for _i, _uf in enumerate((file, file2)):
            if _uf is None or not hasattr(_uf, "filename") or not _uf.filename:
                continue
            _suffix = Path(_uf.filename).suffix or ".mp3"
            _nm = "file"
            if _i < len(_meta) and isinstance(_meta[_i], dict):
                _nm = str(_meta[_i].get("name") or "file")
            _p = TEMP_DIR / f"probe_{task_id}_{_nm}{_suffix}"
            async with aiofiles.open(_p, "wb") as f:
                await f.write(await _uf.read())
            out_files.append(str(_p))
        payload["files"] = out_files
        task["output"] = payload
        logger.info(f"[算力机] 测歌任务完成: {task_id}"
                    f"（{len(out_files)} 个文件 + JSON {len(result or '')} 字符）")
    elif file:
        # ⚠️ 必须保留上传文件的后缀：算力机现在直接回传 mp3，
        #    硬编码 .wav 会让后面按后缀判断的逻辑全部走错分支
        suffix = Path(file.filename or "out.wav").suffix or ".wav"
        out_path = TEMP_DIR / f"out_{task_id}{suffix}"
        async with aiofiles.open(out_path, 'wb') as f:
            await f.write(await file.read())
        task["output"] = str(out_path)
        logger.info(f"[算力机] 单文件任务完成: {task_id}（{out_path.name}）")
        # 单段式翻唱会同时传两个：「语音版（截到高潮）」+「完整版」
        # hasattr 判断是因为直接调用（自检）时默认值还是 File(None) 对象而不是 None
        if file2 is not None and hasattr(file2, "filename"):
            f2_suffix = Path(file2.filename or "").suffix or ".mp3"
            full_path = TEMP_DIR / f"out_{task_id}_full{f2_suffix}"
            async with aiofiles.open(full_path, 'wb') as f:
                await f.write(await file2.read())
            task["output2"] = str(full_path)
            logger.info(f"[算力机] 完整版已存: {full_path.name}")
    elif result:
        # 👇 纯 JSON 结果（复盘日记 / 核心印象 / 归档指令），不带文件
        try:
            task["output"] = json.loads(result)
        except Exception:
            task["output"] = {"journal": result}
        logger.info(f"[算力机] JSON 结果任务完成: {task_id}（{len(result)} 字符）")
    else:
        logger.info(f"[算力机] 清理幽灵任务: {task_id}")

    task["status"] = "completed"
    task["complete_time"] = time.time()
    save_tasks()
    return {"status": "success"}

driver = get_driver()
driver.server_app.include_router(router)

# 启动时恢复任务表（否则重启后算力机回传会 `task not found`，成品直接丢）
load_tasks()


async def _send_to(bot, gid: str, uid: str, msg) -> bool:
    try:
        if gid:
            await bot.send_group_msg(group_id=int(gid), message=msg)
        elif uid:
            await bot.send_private_msg(user_id=int(uid), message=msg)
        else:
            return False
        return True
    except Exception as e:
        logger.warning(f"[算力机] 重启后补发失败: {type(e).__name__}: {str(e)[:120]}")
        return False


async def _watch_and_deliver(bot, task_id: str, deliver: dict, typ: str) -> None:
    """重启前没发出去的任务：等算力机算完，补发到原会话。"""
    gid = str((deliver or {}).get("gid") or "")
    uid = str((deliver or {}).get("uid") or "")
    label = str(deliver.get("label") or "")[:40]
    deadline = time.time() + 900
    while time.time() < deadline:
        t = STATUS["tasks"].get(task_id) or {}
        st = t.get("status")
        if st == "completed":
            out = t.get("output")
            sent = False
            if typ == "draw" and isinstance(out, str) and os.path.exists(out):
                sent = await _send_to(
                    bot, gid, uid,
                    MessageSegment.image(file=f"file://{os.path.abspath(out)}"))
            elif typ == "cover":
                path = out if isinstance(out, str) else ""
                if path and os.path.exists(path):
                    sent = await _send_to(
                        bot, gid, uid,
                        MessageSegment.record(file=f"file://{os.path.abspath(path)}"))
            if sent:
                logger.info(f"[算力机] 重启后补发完成 {task_id}（{typ} {label}）")
            STATUS["tasks"].pop(task_id, None)
            save_tasks()
            return
        if st in ("failed", "timeout"):
            await _send_to(bot, gid, uid,
                           f"刚才那个{'画图' if typ == 'draw' else '翻唱'}没成，要不要再来一次？")
            STATUS["tasks"].pop(task_id, None)
            save_tasks()
            return
        await asyncio.sleep(3)
    await _send_to(bot, gid, uid,
                   f"刚才那个{'画图' if typ == 'draw' else '翻唱'}"
                   f"{('《' + label + '》') if label else ''}被重启打断了，"
                   f"没等到成品，你再发一次我就重来。")
    STATUS["tasks"].pop(task_id, None)
    save_tasks()


async def _periodic_save_tasks():
    while True:
        await asyncio.sleep(5)
        save_tasks()


_RESUME_STARTED = False


@driver.on_bot_connect
async def _on_bot_connect_resume(bot):
    """OneBot 连上后：恢复磁盘里的任务，带 deliver 的继续等结果并补发。

    用 on_bot_connect 而不是 on_startup：插件是在启动过程中才 import 的，
    on_startup 这时已经过了，注册了也不会跑（2026-11-24 实测没生成任务文件）。
    """
    global _RESUME_STARTED
    if _RESUME_STARTED:
        return
    _RESUME_STARTED = True
    asyncio.create_task(_periodic_save_tasks())
    n = 0
    for tid, t in list(STATUS["tasks"].items()):
        d = t.get("deliver") or {}
        if not d or t.get("status") in ("failed", "timeout"):
            continue
        asyncio.create_task(_watch_and_deliver(bot, tid, d, str(t.get("type") or "")))
        n += 1
    if n:
        logger.info(f"[算力机] 启动恢复：{n} 个画图/翻唱任务继续等结果，完成会补发")
    else:
        save_tasks()

async def queue_task_for_download(bot: Bot, event: MessageEvent, state_info: dict, choice: str):
    task_id = state_info["task_id"]
    file_id = state_info["file_id"]
    file_name = state_info["file_name"]
    file_url = state_info["file_url"]
    
    local_napcat_path = None
    if file_id:
        try:
            res = await bot.call_api("get_file", file_id=file_id)
            file_url = res.get("url") or file_url
            local_napcat_path = res.get("file") 
        except Exception as e:
            logger.error(f"[下载] get_file 失败: {e}")

    ext = Path(file_name).suffix or ".audio"
    input_path = TEMP_DIR / f"in_{task_id}{ext}"
    download_success = False

    if local_napcat_path and os.path.exists(local_napcat_path):
        logger.info(f"⚡ [下载] 发现 NapCat 本地临时文件，直接秒传复制: {local_napcat_path}")
        try:
            import shutil
            shutil.copy(local_napcat_path, input_path)
            download_success = True
        except Exception as e:
            logger.error(f"[下载] 本地文件复制失败: {e}")

    if not download_success and file_url:
        await bot.send(event, "⏳ 正在从 QQ 服务器下载音频（大文件可能需要时间）...")
        try:
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Accept": "*/*"
            }
            async with aiohttp.ClientSession() as session:
                async with session.get(file_url, headers=headers, timeout=120) as resp:
                    if resp.status == 200:
                        async with aiofiles.open(input_path, 'wb') as f:
                            async for chunk in resp.content.iter_chunked(1024 * 1024):
                                await f.write(chunk)
                        download_success = True
                    else:
                        logger.error(f"[下载] HTTP 状态码异常: {resp.status}")
        except Exception as e:
            logger.error(f"[下载] 网络请求异常: {e}")

    if not download_success:
        await bot.send(event, "❌ 下载文件失败，可能是网络波动或 QQ 接口限制，请重试。")
        USER_STATE.pop(event.user_id, None)
        return

    logger.info(f"✅ [下载] 音频已成功保存至临时目录: {input_path}")

    target_format = "mp3" if choice == "3" else "wav"
    
    STATUS["tasks"][task_id] = {
        "type": "audio_separation",
        "status": "pending",
        "input": str(input_path), 
        "output": "",
        "target_format": target_format
    }

    await bot.send(event, "⚙️ 文件已接收，任务下发至本地 3090Ti 算力机处理中...")

    for _i in range(150):
        await asyncio.sleep(2)
        st = STATUS["tasks"].get(task_id, {})
        # 算力机一直没拉走（离线）→ 别让用户干等 5 分钟
        if st.get("status") == "pending" and _i >= 30:
            await bot.send(event, "❌ 算力机离线（60 秒内没接任务），请稍后再试。")
            STATUS["tasks"].pop(task_id, None)
            return
        if st.get("status") == "completed":
            out_file_path = st.get("output")
            # 算力机失败时回传的是 {"error": ...} 字典，不是路径
            if not isinstance(out_file_path, str) or not out_file_path:
                logger.error(f"❌ 算力机回传失败: {out_file_path}")
                await bot.send(event, f"❌ 算力机处理失败：{out_file_path}")
                STATUS["tasks"].pop(task_id, None)
                return
            if not os.path.exists(out_file_path):
                logger.error(f"❌ 算力机回传的文件不存在: {out_file_path}")
                await bot.send(event, "❌ 算力机回传的文件丢失了。")
                STATUS["tasks"].pop(task_id, None)
                return
            abs_out_path = os.path.abspath(out_file_path)
            try:
                if choice == "1":
                    await bot.send(event, "🎵 这是分离出的人声语音：")
                    await bot.send(event, MessageSegment.record(file=f"file://{abs_out_path}"))
                elif choice == "2":
                    await bot.send(event, "📄 这是分离出的人声文件：")
                    file_name = f"vocals_{event.user_id}{Path(out_file_path).suffix}"
                    if isinstance(event, GroupMessageEvent):
                        await bot.call_api("upload_group_file", group_id=event.group_id, file=abs_out_path, name=file_name)
                    else:
                        await bot.call_api("upload_private_file", user_id=event.user_id, file=abs_out_path, name=file_name)
                elif choice == "3":
                    await bot.send(event, "🔄 正在压制 MP3...")
                    mp3_path = str(out_file_path).replace(".wav", ".mp3")
                    # ⚠️ 原来是 os.system 同步阻塞，会把整个事件循环（含 /api/download）一起卡死
                    await asyncio.to_thread(
                        subprocess.run,
                        ["ffmpeg", "-y", "-i", abs_out_path, "-b:a", "320k", mp3_path],
                        capture_output=True,
                    )
                    if os.path.exists(mp3_path):
                        file_name = f"vocals_{event.user_id}.mp3"
                        if isinstance(event, GroupMessageEvent):
                            await bot.call_api("upload_group_file", group_id=event.group_id, file=mp3_path, name=file_name)
                        else:
                            await bot.call_api("upload_private_file", user_id=event.user_id, file=mp3_path, name=file_name)
                        os.remove(mp3_path)
            except Exception as e:
                logger.error(f"发送失败: {e}")
                await bot.send(event, "❌ 发送文件失败。")

            try:
                if os.path.exists(input_path): os.remove(input_path)
                if os.path.exists(out_file_path): os.remove(out_file_path)
                del STATUS["tasks"][task_id]
                logger.info(f"🧹 已清理临时文件: {task_id}")
            except: pass
            return

    await bot.send(event, "❌ 算力机处理超时（超过5分钟）。")
    STATUS["tasks"][task_id]["status"] = "timeout"
    try: os.remove(input_path)
    except: pass

# ================= 拦截器加固 =================
async def check_command_only(event: MessageEvent) -> bool:
    text = event.get_plaintext().strip()
    has_file = any(seg.type == "file" for seg in event.get_message())
    return text == "/人声分离" and not has_file

cmd_handler = on_message(rule=Rule(check_command_only), priority=5, block=True)

@cmd_handler.handle()
async def _(bot: Bot, event: MessageEvent):
    if time.time() - STATUS["last_heartbeat"] > HEARTBEAT_TIMEOUT:
        await cmd_handler.finish("❌ 算力机离线，暂时无法提供分离服务。")
    WAITING_FOR_UPLOAD.add(event.user_id)
    await cmd_handler.finish("🎵 已就绪！请直接发送音频文件。\n如果不想分离了，请回复 `/取消`。")

async def check_waiting_user(event: MessageEvent) -> bool:
    if event.user_id not in WAITING_FOR_UPLOAD:
        return False
    text = event.get_plaintext().strip()
    if text.startswith("/") and text != "/取消":
        return False
    return True

waiting_handler = on_message(rule=Rule(check_waiting_user), priority=1, block=True)

@waiting_handler.handle()
async def _(bot: Bot, event: MessageEvent):
    text = event.get_plaintext().strip()
    if text == "/取消":
        WAITING_FOR_UPLOAD.discard(event.user_id)
        await waiting_handler.finish("✅ 已取消人声分离。")
    
    file_seg = None
    for seg in event.get_message():
        if seg.type == "file" and seg.data.get("file", "").lower().endswith(('.wav', '.flac', '.mp3', '.m4a', '.ogg')):
            file_seg = seg
            break
                
    if file_seg:
        WAITING_FOR_UPLOAD.discard(event.user_id)
        task_id = str(int(time.time()))
        USER_STATE[event.user_id] = {
            "status": "waiting_format",
            "task_id": task_id,
            "file_id": file_seg.data.get("file_id"),
            "file_name": file_seg.data.get("file", "audio.tmp"),
            "file_url": file_seg.data.get("url")
        }
        await bot.send(event, "✅ 收到音频！请选择最终结果发送格式（回复数字）：\n1️⃣ 发送语音消息（WAV）\n2️⃣ 发送文件（WAV）\n3️⃣ 发送文件（MP3）")
        return
    await waiting_handler.finish("⏳ 你还没有发送音频文件哦。如果不需要分离了，请回复 `/取消`。")

async def check_inline_file(event: MessageEvent) -> bool:
    text = event.get_plaintext().strip()
    has_audio = False
    for seg in event.get_message():
        if seg.type == "file" and seg.data.get("file", "").lower().endswith(('.wav', '.flac', '.mp3', '.m4a', '.ogg')):
            has_audio = True
            break
    return has_audio and "/人声分离" in text

inline_handler = on_message(rule=Rule(check_inline_file), priority=5, block=True)

@inline_handler.handle()
async def _(bot: Bot, event: MessageEvent):
    if time.time() - STATUS["last_heartbeat"] > HEARTBEAT_TIMEOUT:
        await inline_handler.finish("❌ 算力机离线，暂时无法提供分离服务。")

    file_seg = next((seg for seg in event.get_message() if seg.type == "file"), None)
    task_id = str(int(time.time()))
    USER_STATE[event.user_id] = {
        "status": "waiting_format",
        "task_id": task_id,
        "file_id": file_seg.data.get("file_id"),
        "file_name": file_seg.data.get("file", "audio.tmp"),
        "file_url": file_seg.data.get("url")
    }
    await bot.send(event, "✅ 收到音频！请选择最终结果发送格式（回复数字）：\n1️⃣ 发送语音消息（WAV）\n2️⃣ 发送文件（WAV）\n3️⃣ 发送文件（MP3）")

async def check_user_choice(event: MessageEvent) -> bool:
    text = event.get_plaintext().strip()
    return event.user_id in USER_STATE and USER_STATE[event.user_id]["status"] == "waiting_format" and text in ["1", "2", "3"]

choice_handler = on_message(rule=Rule(check_user_choice), priority=1, block=True)

@choice_handler.handle()
async def _(bot: Bot, event: MessageEvent):
    choice = event.get_plaintext().strip()
    state = USER_STATE.pop(event.user_id)
    await queue_task_for_download(bot, event, state, choice)

async def check_missing_trigger(event: MessageEvent) -> bool:
    text = event.get_plaintext().strip()
    if "/人声分离" in text or event.user_id in WAITING_FOR_UPLOAD:
        return False
    # /测歌 上传 也在等文件 —— 别在这时候插一句「请回复 /人声分离」，用户会懵
    try:
        from plugins.song_cover import PROBE_WAIT_UPLOAD
        if event.user_id in PROBE_WAIT_UPLOAD:
            return False
    except Exception:
        pass
    for seg in event.get_message():
        if seg.type == "file" and seg.data.get("file", "").lower().endswith(('.wav', '.flac', '.mp3', '.m4a', '.ogg')):
            return True
    return False

missing_handler = on_message(rule=Rule(check_missing_trigger), priority=15, block=False)

@missing_handler.handle()
async def _(bot: Bot, event: MessageEvent):
    await missing_handler.send("💡 检测到音频文件！如果你想分离人声，请回复 `/人声分离` 唤醒服务。")
