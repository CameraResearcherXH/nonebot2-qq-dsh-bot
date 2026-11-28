# ⚠️ 这里**故意不写** `# syntax=docker/dockerfile:1`：
#    那行会让 BuildKit 先去 Docker Hub 拉一个「Dockerfile 前端镜像」，
#    墙内/离线机器连 registry-1.docker.io 都是超时，构建在第一步就死。
#    本文件只用到 `RUN --mount=type=cache`，引擎内置前端（Docker 18.09+）就支持。
#
# 基础镜像也参数化：国内拉不动 python:3.11-slim 时，
#   .env 里写 PYTHON_IMAGE=docker.m.daocloud.io/library/python:3.11-slim
ARG PYTHON_IMAGE=python:3.11-slim
#
# ⚠️ 3.11 而不是 3.12 —— 这是被依赖链逼出来的，不是随便选的：
#    · nonebot-plugin-imageutils（petpet 依赖）**所有已发布版本**都锁 Pillow<10，
#      它最后发版是 2023-02，那时 Pillow 10 还没出（2023-07），内部还用了
#      Image.ANTIALIAS 这类 Pillow 10 已删除的 API。
#    · Pillow 9.x 最后一版 9.5.0 发布于 2023-04，而 Python 3.12 是 2023-10 才发布
#      → **Pillow 9.x 没有 cp312 wheel**，在 3.12 上只能源码编译。
#    · 本镜像为了瘦身删掉了 gcc/g++/python3-dev（见下面 apt 那段注释），
#      源码编译必然失败 → 就是 ResolutionImpossible / build error。
#    3.11 有完整的 Pillow 9.5 预编译 wheel，imageutils 和 petpet 都是为 Pillow 9 写的。
#    项目代码全是 3.10+ 语法，3.11 完全兼容。
FROM ${PYTHON_IMAGE}

# ---------------------------------------------------------------- 源参数化
# 三个源都**不写死**，默认值保持「海外最优」，墙内 build 时用 .env / --build-arg 覆盖：
#   APT_MIRROR                 https://mirrors.aliyun.com
#   PIP_INDEX_URL              https://mirrors.aliyun.com/pypi/simple/
#   PIP_TRUSTED_HOST           mirrors.aliyun.com
#   PLAYWRIGHT_DOWNLOAD_HOST   https://npmmirror.com/mirrors/playwright
#
# ⚠️ 为什么默认是 pypi.org 而不是阿里云：
#    线上服务器在**香港**，走国际线路访问 pypi.org 最快；
#    反过来访问阿里云/清华要绕回大陆，实测只有 200~900 kB/s（曾误判成「网络慢」）。
#    墙内机器恰恰相反 —— deb.debian.org 拉 chromium 实测 95 分钟还没完。
ARG APT_MIRROR=
ARG PIP_INDEX_URL=https://pypi.org/simple
ARG PIP_TRUSTED_HOST=pypi.org
ARG PLAYWRIGHT_DOWNLOAD_HOST=

# 日志不缓冲（docker logs 能实时看到），不生成 pyc
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Shanghai \
    LANG=C.UTF-8 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# apt 源：默认不动（deb.debian.org），给了 APT_MIRROR 才替换。
# ⚠️ python:3.11-slim 现在是 Debian trixie，用 **deb822** 格式的
#    /etc/apt/sources.list.d/debian.sources（不是老的 sources.list），
#    只 sed 老文件等于没改。这里两个都扫。
#    只替换主机名，路径（/debian、/debian-security）保持不变，
#    阿里云/清华/中科大 都符合这个规则。
#    改完会留在镜像层里，后面 playwright 那层的 apt-get update 也会跟着走国内源。

# 系统依赖（只留真正需要的）
#   tzdata                 时区
#   ffmpeg                 ⚠️ 必须：混音、转 MP3、抽音频
#   fonts-wqy-microhei     ⚠️ 必须：petpet（头像表情包）要往图上写中文
#
# 关于字体（2026-11-13 修正）：
#   原来这里写着「项目里没有任何中文绘图需求」，所以删了 fonts-noto-cjk（200MB）。
#   但接了三方插件 petpet 之后这个判断不成立了 —— 它要渲染「摸头」「贴贴」这类
#   中文文案，没字体就刷几百行 "找不到可以显示字符'摸'的字体"，然后整个 matcher 报错。
#   这里只装文泉驿微米黑（约 5MB），不是 200MB 的 noto-cjk。
#   imageutils 的候选字体列表里本来就有「WenQuanYi Micro Hei」，装上即被识别。
#
# 仍然删掉的：
#   · gcc/g++/python3-dev/libffi-dev   约 150MB，而且原 Dockerfile 装完又 purge，
#                      纯浪费。requirements 里全是 manylinux 预编译 wheel。
#   万一以后某个包报 "error: command 'gcc' failed"，把下面这行加回来：
#       gcc g++ python3-dev libffi-dev \
# 关于 Chromium（2026-11-13 新增）：
#   steam_judger（/steam分析）要把 HTML 模板渲染成图片发给用户，靠 selenium 驱动浏览器。
#   不装的话插件会退回纯文本，出不了那张卡片图。
#   Debian 自带的 chromium 包同时提供 chromium-driver（chromedriver 在 /usr/bin/chromedriver），
#   比手动下 Google Chrome + 对应版本 chromedriver 省事，也不用担心版本对不上。
#   两个包合计约 400MB，是全镜像里最重的一块，能接受再装。
#   容器里跑浏览器必须 --no-sandbox（插件里已经加了）。
RUN set -eux; \
    if [ -n "${APT_MIRROR}" ]; then \
        for f in /etc/apt/sources.list /etc/apt/sources.list.d/*.sources /etc/apt/sources.list.d/*.list; do \
            if [ -f "$f" ]; then \
                sed -i "s|https\?://deb\.debian\.org|${APT_MIRROR}|g" "$f"; \
            fi; \
        done; \
        grep -rn "URIs\|^deb " /etc/apt/sources.list.d/ /etc/apt/sources.list 2>/dev/null || true; \
    fi; \
    apt-get update && apt-get install -y --no-install-recommends \
        tzdata \
        ffmpeg \
        fonts-wqy-microhei \
        chromium \
        chromium-driver \
    && rm -rf /var/lib/apt/lists/* /var/cache/apt/*

# 依赖层单独一层：只要 requirements.txt 不变，改代码不会让它失效
COPY requirements.txt .

# ⚠️ 加速的关键是 cache mount，不是 --no-cache-dir。
#    以前写 --no-cache-dir：requirements.txt 改一个字符 → 这一层缓存失效 →
#    200MB+ 全部重新下载，反复试错就累计成「两小时还没好」。
#    cache mount 把 pip 的 wheel 缓存挂到**构建之间**复用：第二次只下新增的包。
#    （日志里的 "load local bake definitions" 说明 buildx/BuildKit 已启用，支持该语法）
#
# --prefer-binary：优先用预编译 wheel，别去下 sdist 再本地编译（镜像里没有 gcc）。
# --retries / --timeout：国际线路偶发抖动，放宽后能自己扛过去。
# 不再 --upgrade pip：基础镜像自带的 pip 已经够新，省一次下载。
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --prefer-binary --retries 5 --timeout 60 \
        --index-url "${PIP_INDEX_URL}" --trusted-host "${PIP_TRUSTED_HOST}" \
        -r requirements.txt dashscope

# OCR（P1，2026-11-27）：
#   ① 依赖单独一个文件（**不要合并进 requirements.txt**：加一行会让上面那层缓存
#      整体失效、重新解析整棵依赖树，实测在 aioboto3 旧版本上回溯好几分钟）；
#   ② 本体 --no-deps：rapidocr 声明 opencv-python（带 GUI，容器缺 libGL 会 import 崩），
#      会覆盖 requirements 里的 opencv-python-headless。
COPY requirements-ocr.txt .
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --prefer-binary --retries 5 --timeout 60 \
        --index-url "${PIP_INDEX_URL}" --trusted-host "${PIP_TRUSTED_HOST}" \
        -r requirements-ocr.txt \
    && pip install --no-deps --prefer-binary --retries 5 --timeout 60 \
        --index-url "${PIP_INDEX_URL}" --trusted-host "${PIP_TRUSTED_HOST}" \
        rapidocr_onnxruntime==1.4.4

# Playwright 的 chromium（gspanel 原神面板出图用）
#   镜像里 apt 装的 chromium 是给 steam_judger（selenium）用的；
#   playwright 需要自己协议匹配的内核，这里预装，避免第一次查面板时才去下载（会卡 1~2 分钟）。
#   --with-deps 补 libnss3 等运行库（大部分已由 chromium 包带上）；装完清 apt 缓存。
#   PLAYWRIGHT_BROWSERS_PATH 固定到 /ms-playwright，运行时 htmlrender 直接能找到。
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
# ⚠️ 先 apt-get update：上面那层装完 chromium 就把 lists 删了，
#    而 playwright install-deps 只 apt-get install、不会自己 update。
# ⚠️ 墙内机器要把浏览器下载也指到国内镜像（npmmirror），否则这一步又是几十分钟：
#      PLAYWRIGHT_DOWNLOAD_HOST=https://npmmirror.com/mirrors/playwright
#    空值必须 unset —— playwright 拿到空字符串会拼出坏 URL。
RUN if [ -z "${PLAYWRIGHT_DOWNLOAD_HOST}" ]; then unset PLAYWRIGHT_DOWNLOAD_HOST; fi; \
    apt-get update \
    && playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/* /var/cache/apt/*

# 代码本体放最后（改代码只重跑这一层，几秒钟）
# ⚠️ 必须配合 .dockerignore，否则会把 napcat(1.4GB)/data(110MB)/venv(44MB) 全塞进来
COPY . .

CMD ["python", "bot.py"]

