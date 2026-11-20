# core/config.py
"""配置收口：一个地方读环境变量，启动时打印「key=生效值(来源)」。

不引入 pydantic-settings（避免新依赖）；只做：
- `get(key, default, cast)`：带类型转换与默认值
- `dump(keys=None)`：脱敏后打印，密钥只显示前 4 位
- `SECRET_HINTS`：命中即脱敏
"""
import os

SECRET_HINTS = ("KEY", "TOKEN", "SECRET", "COOKIE", "PASSWORD", "PASSWD")


def get(key: str, default=None, cast=str):
    raw = os.getenv(key)
    if raw is None or raw == "":
        return default
    try:
        if cast is bool:
            return str(raw).strip().lower() in ("1", "true", "yes", "on", "是", "开")
        if cast is int:
            return int(float(str(raw).strip()))
        if cast is float:
            return float(str(raw).strip())
        return cast(raw)
    except Exception:
        return default


def is_secret(key: str) -> bool:
    k = str(key or "").upper()
    return any(h in k for h in SECRET_HINTS)


def mask(value: str) -> str:
    v = str(value or "")
    if len(v) <= 6:
        return "***"
    return v[:4] + "***" + v[-2:]


def dump(keys=None) -> str:
    keys = list(keys or sorted(os.environ.keys()))
    lines = []
    for k in keys:
        v = os.getenv(k)
        if v is None:
            continue
        lines.append("%s=%s" % (k, mask(v) if is_secret(k) else v))
    return "\n".join(lines)


def data_dir() -> str:
    """本期固定 data/；以后要迁只改这里。"""
    return os.getenv("DATA_DIR", "data")
