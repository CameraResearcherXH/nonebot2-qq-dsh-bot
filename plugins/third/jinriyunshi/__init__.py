from nonebot.adapters.onebot.v11 import MessageEvent, PrivateMessageEvent
from nonebot.adapters.onebot.v11 import MessageEvent
from nonebot.adapters.onebot.v11 import MessageSegment, Message
from nonebot import logger
from nonebot_plugin_apscheduler import scheduler
from nonebot.matcher import Matcher
import asyncio
from concurrent.futures import ThreadPoolExecutor
import random
import os
import json
import datetime
from nonebot import on_command
import requests
import time
from PIL import Image
import hashlib
from nonebot import on_message


def average_hash(img, hash_size: int = 8) -> str:
    """
    imagehash.average_hash 的等价实现，只用 Pillow。

    为什么不用 imagehash 库：
      · 它会连带装 numpy + scipy + PyWavelets（约 80MB），只为算一个 64bit 指纹
      · 阿里云 pypi 镜像拉它的索引经常 ReadTimeout（实测 15s 超时连挂 5 次，
        直接把 docker build 卡在 pip 层 158 秒后失败）
    我们只需要「缩到 8x8 灰度 → 比均值 → 64 bit」这一件事，十行足够。

    返回 16 位小写十六进制字符串，和 str(imagehash.average_hash(img)) 格式完全一致，
    所以已有的 hash_cache.json 不需要清空，去重能接着用。
    """
    small = img.convert("L").resize((hash_size, hash_size), Image.Resampling.LANCZOS)
    px = list(small.getdata())
    avg = sum(px) / len(px)
    bits = 0
    for p in px:
        bits = (bits << 1) | (1 if p > avg else 0)
    return f"{bits:0{hash_size * hash_size // 4}x}"

ADMIN_QQ_LIST = ["填入你的管理员QQ号"]

# 可以自己改文案
YUNSHI_DATA = {
    0: {"title": "渊厄（深渊级厄运）", "texts": [
        "黑云蔽日戾气生，妄动恐遭意外横\n谨言慎行守斋戒，静待阳升化七成",
        "天狗食月乱神魂，钱财饮食需谨慎\n黄庭静诵三百字，仙真或可护命门",
        "六爻俱凶血光随，大事缓决病速医\n幸有东北贵人至，赠符解围破危机"
    ]},
    1: {"title": "坎陷（坎卦级险境）", "texts": [
        "如履薄冰暗流藏，投资情爱需明辨\n玄武暗中施庇佑，慎终如始可渡关",
        "迷雾锁江小人生，文书反复戌时成\n佩玉挡灾引紫气，运程渐明见转机",
        "卷舌星动惹风波，晨拜朱雀化灾厄\n戌狗属相暗相助，谋略得当转危安"
    ]},
    2: {"title": "陷厄（沉陷级困局）", "texts": [
        "丧门照命忌远行，卯辰慎防无名盟\n戌狗赠赤玉髓佩，可挡灾星破阴霾",
        "病符侵体饮食忌，西南莫留锁元气\n亥时焚艾净宅后，天医祛病运势起",
        "勾陈缠身流言穿，巳未慎言钱财缘\n正东青衫老者现，指点迷津解困玄"
    ]},
    3: {"title": "蹇难（蹇卦级阻滞）", "texts": [
        "天罗地网藏刀锋，决策延七情装聋\n午时面西拜白虎，铜铃三响破樊笼",
        "五鬼运财反噬凶，子寅紧闭防邪祟\n速请桃木刻鼠相，置于乾位镇厄空",
        "驿马倒悬行路难，五谷随身井卦言\n东北双鹊忽起舞，便是厄尽祥瑞显"
    ]},
    4: {"title": "中正（平衡之境）", "texts": [
        "阴阳和合运道平，守成持泰即功成\n虹霓贯东西时现，静待良机自有凭",
        "太极流转最安然，晨练卯时投土性\n故人忽传佳讯至，笑谈往昔续前缘",
        "星斗循轨循旧例，创新三思传机遇\n酉时双燕飞掠过，吉兆天机暗中藏"
    ]},
    5: {"title": "渐吉（渐进式祥兆）", "texts": [
        "三合局开旧债清，辰种财竹申小投\n红鸾初现含蓄应，运道渐开新财流",
        "文昌照曲正当时，朱砂点额增灵智\n西方捧书客偶遇，三问玄机得妙思",
        "玉堂贵人消恩怨，失物重现巽位显\n酉时备酒待客至，商机卦图暗中现"
    ]},
    6: {"title": "通明（通达级吉运）", "texts": [
        "禄存高照财门开，巳午投资翻番来\n分润马姓保长久，冷灶贵人送柴财",
        "驿马星动利远行，航班6/8最显灵\n异国鼠辈街头遇，竟是关键引路人",
        "天解星消法律业，文件三份印震歇\n亥时雨落洗净尘，新契前路自此开"
    ]},
    7: {"title": "鼎盛（巅峰级鸿运）", "texts": [
        "天乙贵人万事成，寅祭未提获重金\n双鱼跃门速购彩，所求皆得称人心",
        "将星坐镇展峥嵘，青绿战袍攻西锋\n戌时犬吠捷报至，竞技场上定输赢",
        "帝旺当头敢争锋，午地申科利不同\n分羹兔姓避亏空，盛极运道贯长虹"
    ]},
    8: {"title": "太和（终极祥瑞）", "texts": [
        "紫微开天门献瑞，三奇六合共相随\n功名正当九天月，鸾凤和鸣非梦哉",
        "河图洛书天降财，跨国冷门翻倍来\n红鸾星动良缘至，地涌甘泉金玉伴",
        "青龙盘柱文武彰，学术竞技破旧章\n亥子异梦先祖指，迷津得解镇八方"
    ]}
}

# 扩充图池时的图片标签（refresh_wallhaven_pool内也有一个，最好同步改）
WALLHAVEN_TAGS = "genshin-impact OR honkai-star-rail OR zenless-zone-zero OR wuthering-waves OR punishing-gray-raven OR blue-archive OR arknights OR girls-frontline OR neural-cloud OR project-arklight OR snowbreak"
HEADERS = {"User-Agent": "Mozilla/5.0"}
# 数据统一放项目根的 data/jinriyunshi/（和其他三方插件一致，插件目录保持干净）
_DATA_ROOT = os.path.join("data", "jinriyunshi")
POOL_DIR = os.path.join(_DATA_ROOT, "wallhaven_download")

# 指令前缀：原版只认 `.` 开头（`.今日运势`）。
# 用户 2026-11-13 要求统一成 `/` —— 本项目所有自研指令都是 `/` 起手。
# 两种都保留，老的 `.今日运势` 继续能用，不会打断已有习惯。
_YUNSHI_CMDS = (
    ".今日人品", ".今日运势",
    "/今日人品", "/今日运势",
    "／今日人品", "／今日运势",
)


def yunshi_rule(event: MessageEvent) -> bool:
    text = event.get_plaintext().strip()
    return any(text.startswith(c) for c in _YUNSHI_CMDS)

yunshi_cmd = on_message(rule=yunshi_rule, priority=10, block=True)

# 路径与缓存
CACHE_DIR = os.path.join(_DATA_ROOT, "cache")
CACHE_PATH = os.path.join(CACHE_DIR, "daily_cache.json")

# 创建 cache 文件夹
os.makedirs(CACHE_DIR, exist_ok=True)

# 加载缓存
def load_cache():
    if os.path.exists(CACHE_PATH):
        with open(CACHE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_cache(data):
    with open(CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

today_cache = load_cache()
today_date = datetime.date.today().isoformat()

@scheduler.scheduled_job("cron", hour=3, minute=0)
def refresh_wallhaven_pool():
    HASH_CACHE_PATH = os.path.join(POOL_DIR, ".hash_cache.json")

    def load_hash_cache():
        if os.path.exists(HASH_CACHE_PATH):
            with open(HASH_CACHE_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        return {}

    def save_hash_cache(cache):
        with open(HASH_CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=2)

    def compute_hashes(image_path: str) -> tuple[str, str]:
        with Image.open(image_path) as img:
            ahash = average_hash(img)
            md5 = hashlib.md5(img.tobytes()).hexdigest()
            return ahash, md5

    hash_cache = load_hash_cache()

    # 设置下载图片的词库tag
    TAGS = [
        "genshin-impact", "honkai-star-rail", "zenless-zone-zero", "wuthering-waves",
        "punishing-gray-raven", "blue-archive", "arknights", "girls-frontline",
        "neural-cloud", "project-arklight", "snowbreak"
    ]
    IMAGES_PER_TAG = 5
    HEADERS = {'User-Agent': 'Mozilla/5.0'}

    os.makedirs(POOL_DIR, exist_ok=True)

    def download_image(img_url, save_path) -> bool:
        try:
            resp = requests.get(img_url, headers=HEADERS, timeout=10)
            if resp.status_code != 200:
                return False

            temp_path = save_path + ".tmp"
            with open(temp_path, "wb") as f:
                f.write(resp.content)

            ahash, md5 = compute_hashes(temp_path)
            if ahash in hash_cache or md5 in hash_cache:
                print(f"⚠️ 跳过重复图像：{img_url}")
                os.remove(temp_path)
                return False

            os.rename(temp_path, save_path)
            hash_cache[ahash] = img_url
            hash_cache[md5] = img_url
            print(f"✅ 下载成功：{img_url}")
            return True
        except Exception as e:
            print(f"❌ 下载失败：{img_url} | 错误: {e}")
            return False

    def fetch_from_tag(tag):
        def try_fetch(query):
            url = "https://wallhaven.cc/api/v1/search"
            params = {
                "q": query,
                "sorting": "random",
                "purity": "100",
                "categories": "111",
                "page": 1
            }
            try:
                resp = requests.get(url, params=params, headers=HEADERS, timeout=10)
                return resp.json().get("data", [])
            except Exception as e:
                print(f"❌ 请求失败：{e}")
                return []

        try:
            data = try_fetch(f"tag:{tag}")
            if not data:
                print(f"⚠️ 标签精确匹配失败：tag:{tag}，尝试模糊搜索")
                data = try_fetch(tag)

            if not data:
                print(f"❌ 无法获取任何图像：{tag}")
                return

            for img in data[:IMAGES_PER_TAG]:
                img_url = img["path"]
                ext = img_url.split(".")[-1].split("?")[0]
                filename = f"{tag}_{img['id']}.{ext}"
                save_path = os.path.join(POOL_DIR, filename)
                download_image(img_url, save_path)
                time.sleep(0.2)
        except Exception as e:
            print(f"⚠️ 标签 [{tag}] 抓取失败：{e}")

    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = [executor.submit(fetch_from_tag, tag) for tag in TAGS]
        for f in futures:
            f.result()  # 确保完成

    save_hash_cache(hash_cache)

def get_random_pool_image():
    if not os.path.exists(POOL_DIR):
        return None
    files = [f for f in os.listdir(POOL_DIR) if f.lower().endswith((".jpg", ".png", ".jpeg"))]
    if not files:
        return None
    return os.path.join(POOL_DIR, random.choice(files))

# 指令处理
@yunshi_cmd.handle()
async def _(event: MessageEvent):
    global today_cache

    # 👇 跨天重置（原版只在 import 时 load 一次，**永不按天清** →
    #    一个人抽过一次就永远返回同一份，等于"每天只能抽一次"变成了"一辈子一次"）。
    #    2026-11-18 加：和 chat 的 daily_luck 工具共用同一个 last_reset.txt，
    #    两条路径（/今日运势 和 LLM 工具）看到的是同一份缓存。
    _today = datetime.date.today().isoformat()
    _marker = os.path.join(CACHE_DIR, "last_reset.txt")
    try:
        with open(_marker, "r", encoding="utf-8") as _f:
            _last = _f.read().strip()
    except Exception:
        _last = ""
    if _last != _today:
        today_cache = {}
        save_cache(today_cache)
        try:
            with open(_marker, "w", encoding="utf-8") as _f:
                _f.write(_today)
        except Exception:
            pass

    user_id = str(event.user_id)
    nickname = event.sender.nickname or f"用户{user_id[-4:]}"  # fallback 名字

    # 如果已有缓存直接返回
    if user_id in today_cache:
        data = today_cache[user_id]
    else:
        if random.random() < 0.7:
            a = random.randint(0, 2)
            b = random.randint(0, 2)
            c = random.randint(0, 2)
            d = random.randint(0, 2)
            level = a + b + c + d
        else:
            level = random.randint(0, 8)
            while True:
                a = random.randint(0, 2)
                b = random.randint(0, 2)
                c = random.randint(0, 2)
                d = level - (a + b + c)
                if 0 <= d <= 2:
                    break
        text_index = random.randint(0, 2)
        stars = "★" * level + "☆" * (8 - level)

        data = {
            "level": level,
            "text_index": text_index,
            "stars": stars,
            "detail": f"财运({a})+姻缘({b})+事业({c})+人品({d})"
        }
        today_cache[user_id] = data
        save_cache(today_cache)

    level_info = YUNSHI_DATA[data["level"]]
    title = level_info["title"]
    text = data.get("text") if "text" in data else level_info["texts"][data["text_index"]]

    image_path = get_random_pool_image()

    if image_path:
        image_segment = Message("\n") + MessageSegment.image(f"file://{image_path}")
    else:
        image_segment = Message("\n（图池为空，请联系管理员刷新）")

    # 发送消息的排版
    msg = (
        Message(f"@{nickname}，阁下的今日运势是：\n"
                f"{title}\n"
                f"{data['stars']}\n"
                f"{text}\n"
                f"{data['detail']}")
        + image_segment
        + Message("仅供娱乐｜相信科学｜请勿迷信")
    )

    await yunshi_cmd.finish(msg)

refresh_cmd = on_command("扩充图池", aliases={".扩充图池"}, priority=1, block=True)

@refresh_cmd.handle()
async def _(matcher: Matcher, event: MessageEvent):
    if str(event.user_id) not in ADMIN_QQ_LIST:
        return

    if not isinstance(event, PrivateMessageEvent):
        await matcher.finish("📢 请私聊我发送 `.扩充图池` 指令～")
        return

    await matcher.send("🧹 正在扩充图池，请稍等...")

    # ✅ 异步运行阻塞函数
    loop = asyncio.get_event_loop()
    try:
        await loop.run_in_executor(None, refresh_wallhaven_pool)
        await matcher.send("✅ 图池扩充完成！")
    except Exception as e:
        logger.error(f"扩充图池失败: {e}")
        await matcher.send(f"⚠️ 图池扩充失败：{e}")

