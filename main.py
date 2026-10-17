#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
astrbot_plugin_CNTIR_bot — CNTIR（欧洲卡车模拟 2 联机平台）查询插件

数据来源：CNTIR 公开数据 API（只读、免鉴权）
文档地址：https://cntir.ltchedui.com/developers
使用约定：本项目注明数据来源为 CNTIR 并附上站点链接；字段以官方文档为准。

架构说明（借鉴 hmp_bot / tmp-bot 两项目的成熟实践）：
    1. 单监听器 + 正则路由：实际逻辑集中在 event_message_type(ALL) 的 on_message，
       同时注册一批 @filter.command 桩方法用于 AstrBot 指令列表展示。
    2. TTL 缓存限频：CNTIR 官方要求「服务器状态 >= 15s / 实时地图 >= 5s」，
       所有接口经 _TtlCache 去重，杜绝 429 限流。
    3. 多级降级：地图渲染失败 → 文字摘要；接口异常 → 友好文案，任何一处失败不影响其他命令。
    4. 数据目录规范：data/plugin_data/astrbot_plugin_CNTIR_bot/，插件更新不丢数据。
"""

import asyncio
import base64
import json
import math
import os
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

import aiohttp

# ----------------------------- AstrBot API 导入（含脱离环境的回退桩） -----------------------------
try:
    from astrbot.api.event import filter, AstrMessageEvent
    from astrbot.api.star import Context, Star, register
    from astrbot.api import logger
    from astrbot.api.message_components import Image, Plain
except ImportError:  # 允许脱离 AstrBot 环境 import（单元测试 / 静态检查）

    class _DummyFilter:
        class EventMessageType:
            ALL = "ALL"

        @staticmethod
        def command(_pattern, **_kw):
            def deco(func):
                return func
            return deco

        @staticmethod
        def event_message_type(_type, **_kw):
            def deco(func):
                return func
            return deco

    filter = _DummyFilter()
    AstrMessageEvent = object  # noqa: N816
    Context = object  # noqa: N816

    class Star:
        def __init__(self, context=None):
            pass

        async def html_render(self, *_a, **_k):
            return None

    def register(*_a, **_k):
        def deco(cls):
            return cls
        return deco

    class _Logger:
        @staticmethod
        def info(msg, *a):
            print("[INFO]", msg)

        @staticmethod
        def warning(msg, *a):
            print("[WARN]", msg)

        @staticmethod
        def error(msg, *a, **kw):
            print("[ERROR]", msg)

    logger = _Logger()

    class Image:
        @staticmethod
        def fromURL(url):
            return url

    class Plain:
        def __init__(self, text):
            self.text = text


# ----------------------------- 常量 -----------------------------
PLUGIN_NAME = "astrbot_plugin_CNTIR_bot"
API_BASE = "https://cntir.ltchedui.com"
USER_AGENT = "AstrBot-CNTIR-Plugin/1.0.2"
PLUGIN_PAGE = "https://cntir.ltchedui.com"

# ETS2 底图瓦片（默认源与故障回退源，均为公开镜像）
DEFAULT_TILE_URL = "https://ets_tiles.cnly.top/20260903/tmp-ets-yellow/Tiles/{z}/{x}/{y}.png"
FALLBACK_TILE_URL = "https://ets2.online/map/ets2map_157/{z}/{x}/{y}.png"

ROLE_MAP = {
    "player": "玩家",
    "support": "支持团队",
    "event": "活动组",
    "moderator": "管理员",
    "admin": "管理员",
    "owner": "站长",
}
KIND_MAP = {"convoy": "巡游", "truckfest": "卡车展", "both": "巡游+卡车展"}
LEVEL_MAP = {"info": "ℹ️", "warning": "⚠️", "critical": "🚨"}
CATEGORY_MAP = {"news": "新闻", "update": "更新", "event": "活动", "notice": "通知", "devblog": "开发日志"}
VTC_STATUS_MAP = {0: "普通", 1: "认证", 2: "官方认证"}
EVENT_TAB_KEYWORDS = {
    "today": ["today", "今日", "24小时"],
    "upcoming": ["upcoming", "即将", "即将开始"],
    "featured": ["featured", "精选"],
    "past": ["past", "结束", "已结束", "往期"],
}

_BJ_TZ = timezone(timedelta(hours=8))


# ----------------------------- 工具函数 -----------------------------
def _ts_to_bj(ts: Any, with_time: bool = True) -> str:
    """Unix 秒 → 北京时间字符串。"""
    try:
        if not ts:
            return "未知"
        dt = datetime.fromtimestamp(int(ts), tz=_BJ_TZ)
        if with_time:
            return dt.strftime("%Y-%m-%d %H:%M")
        return dt.strftime("%Y-%m-%d")
    except Exception:
        return "未知"


def _rel_time(ts: Any, now: Optional[float] = None) -> str:
    """Unix 秒 → 相对时间（刚刚 / 3分钟前 / 2小时前 / 5天前 / 日期）。"""
    try:
        if not ts:
            return "未知"
        delta = (now or time.time()) - int(ts)
        if delta < 0:
            delta = 0
        if delta < 60:
            return "刚刚"
        if delta < 3600:
            return f"{int(delta // 60)}分钟前"
        if delta < 86400:
            return f"{int(delta // 3600)}小时前"
        if delta < 86400 * 30:
            return f"{int(delta // 86400)}天前"
        return _ts_to_bj(ts, with_time=False)
    except Exception:
        return "未知"


def _event_is_finished(ev: Dict[str, Any], now: Optional[float] = None) -> bool:
    """活动是否已结束：有结束时间按结束时间判断；没有结束时间时，开始满 24 小时即视为过期。"""
    now = now or time.time()
    end = _fmt_int(ev.get("end_at"))
    if end > 0:
        return end <= now
    start = _fmt_int(ev.get("start_at"))
    return start > 0 and start + 86400 <= now


def _heading_arrow(heading: Any) -> str:
    """朝向弧度 → 箭头+方位（八向）。0 为北、顺时针，若与游戏实际定义有偏差可在此调整。"""
    try:
        if heading is None:
            return ""
        angle = math.degrees(float(heading)) % 360
        directions = [("↑", "北"), ("↗", "东北"), ("→", "东"), ("↘", "东南"),
                      ("↓", "南"), ("↙", "西南"), ("←", "西"), ("↖", "西北")]
        idx = int((angle + 22.5) % 360 / 45)
        arrow, text = directions[idx]
        return f"{arrow} {text}"
    except Exception:
        return ""


def _fmt_int(v: Any, default: int = 0) -> int:
    try:
        return int(v)
    except Exception:
        return default


def _fmt_float(v: Any, nd: int = 1) -> str:
    try:
        return f"{float(v):.{nd}f}"
    except Exception:
        return str(v or 0)


def _fmt_revenue(v: Any) -> str:
    """收入（游戏币）→ 千分位字符串。"""
    try:
        return f"{int(float(v)):,}"
    except Exception:
        return str(v or 0)


def _utc_offset_ts() -> float:
    return time.time()


def _dist_m(x1: float, z1: float, x2: float, z2: float) -> float:
    """游戏世界坐标（米）的欧氏距离。"""
    return math.hypot(float(x1) - float(x2), float(z1) - float(z2))


def _clean(s: Any) -> str:
    """清洗对外展示的单行文本。"""
    if s is None:
        return ""
    return re.sub(r"[\r\n\t]+", " ", str(s)).strip()


# ----------------------------- 异常 -----------------------------
class CntirApiError(Exception):
    """CNTIR API 请求异常。status 为 HTTP 状态码（可选）。"""

    def __init__(self, message: str, status: Optional[int] = None):
        super().__init__(message)
        self.message = message
        self.status = status


# ----------------------------- TTL 缓存（限频核心） -----------------------------
class _TtlCache:
    """带 TTL 与单 key 合并锁的异步缓存：并发请求同一 key 时只发起一次真实请求。"""

    def __init__(self) -> None:
        self._store: Dict[str, Tuple[float, Any]] = {}
        self._locks: Dict[str, asyncio.Lock] = {}

    async def get_or_set(self, key: str, ttl: float, factory: Callable[[], Any]) -> Any:
        hit = self._store.get(key)
        now = time.monotonic()
        if hit and now - hit[0] < ttl:
            return hit[1]
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            hit = self._store.get(key)
            now = time.monotonic()
            if hit and now - hit[0] < ttl:
                return hit[1]
            value = await factory()
            self._store[key] = (time.monotonic(), value)
            return value


@register("astrbot_plugin_CNTIR_bot", "鲸鱼", "CNTIR 查询机器人（欧卡2联机平台）", "1.0.2",
          "https://cntir.ltchedui.com")
class CntirBotPlugin(Star):
    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        self.config = config or {}
        self.session: Optional[aiohttp.ClientSession] = None
        self._cache = _TtlCache()
        self._tile_cache: Dict[str, str] = {}

        # 数据目录：AstrBot 规范位置，插件更新不丢数据
        self.data_dir = os.path.join(os.getcwd(), "data", "plugin_data", PLUGIN_NAME)
        self.bind_file = os.path.join(self.data_dir, "bindings.json")
        self.tpl_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")

        self._build_routes()

    # ----------------------------- 生命周期 -----------------------------
    async def initialize(self):
        timeout = aiohttp.ClientTimeout(total=self._cfg_int("api_timeout_seconds", 10))
        self.session = aiohttp.ClientSession(
            headers={"User-Agent": USER_AGENT},
            timeout=timeout,
            connector=aiohttp.TCPConnector(),
            trust_env=True,
        )
        logger.info(f"{PLUGIN_NAME} 初始化完成，API 源 {API_BASE}")

    async def terminate(self):
        if self.session and not self.session.closed:
            await self.session.close()
        self.session = None
        logger.info(f"{PLUGIN_NAME} 已停止")

    # ----------------------------- 配置读取 -----------------------------
    def _cfg_int(self, key: str, default: int) -> int:
        try:
            return int(self.config.get(key, default))
        except Exception:
            return default

    def _cfg_bool(self, key: str, default: bool) -> bool:
        v = self.config.get(key, default)
        if isinstance(v, bool):
            return v
        if isinstance(v, (int, float)):
            return bool(v)
        if isinstance(v, str):
            return v.strip().lower() in ("1", "true", "yes", "on")
        return default

    def _cfg_str(self, key: str, default: str = "") -> str:
        v = self.config.get(key, default)
        return str(v) if v is not None else default

    @property
    def _cache_ttl(self) -> int:
        """全部接口统一的缓存时长（秒），由 cache_ttl_seconds 一项控制。"""
        return max(5, self._cfg_int("cache_ttl_seconds", 30))

    def _ttl(self, minimum: int = 0) -> int:
        """统一缓存时长，并按接口下限钳制（官方限频：服务器 15s、地图 5s）。"""
        return max(minimum, self._cache_ttl)

    @property
    def _servers_ttl(self) -> int:
        return self._ttl(15)

    @property
    def _map_ttl(self) -> int:
        return self._ttl(5)

    # ----------------------------- HTTP 层 -----------------------------
    async def _fetch_json(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        if not self.session:
            raise CntirApiError("插件尚未初始化完成，请稍后再试")
        url = f"{API_BASE}{path}"
        try:
            async with self.session.get(url, params=params) as resp:
                if resp.status == 429:
                    raise CntirApiError("请求过于频繁，已被 CNTIR 平台限流，请稍后再试", 429)
                try:
                    data = await resp.json(content_type=None)
                except Exception:
                    raise CntirApiError(f"接口返回了非 JSON 内容（{resp.status}）", resp.status)
                if resp.status >= 400:
                    err = data.get("error") if isinstance(data, dict) else None
                    if err == "not_found" or resp.status == 404:
                        raise CntirApiError("请求的数据不存在", 404)
                    raise CntirApiError(f"接口返回错误 {err or resp.status}", resp.status)
                if isinstance(data, dict) and data.get("error") and len(data) == 1:
                    raise CntirApiError(f"接口返回错误：{data['error']}")
                return data
        except CntirApiError:
            raise
        except asyncio.TimeoutError:
            raise CntirApiError("请求 CNTIR 接口超时，请稍后重试")
        except aiohttp.ClientError as e:
            raise CntirApiError(f"网络请求失败：{e.__class__.__name__}")
        except Exception as e:
            logger.error(f"CNTIR 请求异常: {path} -> {e}")
            raise CntirApiError("请求 CNTIR 接口失败，请稍后重试")

    # ----------------------------- API 封装层（13 个公开接口 + TTL 限频） -----------------------------
    async def api_servers(self) -> List[Dict[str, Any]]:
        return await self._cache.get_or_set(
            "/api/servers", self._servers_ttl, lambda: self._fetch_json("/api/servers"))

    async def api_server_history(self, code: str, hours: int = 24) -> List[Dict[str, Any]]:
        hours = max(1, min(336, hours))
        key = f"/api/servers/{code}/history?hours={hours}"
        return await self._cache.get_or_set(
            key, self._cache_ttl,
            lambda: self._fetch_json(f"/api/servers/{code}/history", {"hours": hours}))

    async def api_map(self, server: Optional[str] = None) -> Dict[str, Any]:
        key = f"/api/map?server={server or ''}"
        return await self._cache.get_or_set(
            key, self._map_ttl, lambda: self._fetch_json("/api/map", {"server": server} if server else None))

    async def api_stats(self) -> Dict[str, Any]:
        return await self._cache.get_or_set(
            "/api/stats", self._cache_ttl, lambda: self._fetch_json("/api/stats"))

    async def api_user(self, uid: int) -> Dict[str, Any]:
        key = f"/api/users/{uid}"
        return await self._cache.get_or_set(
            key, self._cache_ttl, lambda: self._fetch_json(f"/api/users/{uid}"))

    async def api_news(self, page: int = 1, per: int = 5, category: str = "") -> Dict[str, Any]:
        per = max(1, min(30, per))
        key = f"/api/news?p={page}&per={per}&c={category}"
        params: Dict[str, Any] = {"page": page, "per": per}
        if category:
            params["category"] = category
        return await self._cache.get_or_set(
            key, self._cache_ttl, lambda: self._fetch_json("/api/news", params))

    async def api_news_detail(self, news_id: int) -> Dict[str, Any]:
        key = f"/api/news/{news_id}"
        return await self._cache.get_or_set(
            key, self._cache_ttl, lambda: self._fetch_json(f"/api/news/{news_id}"))

    async def api_announcements(self) -> List[Dict[str, Any]]:
        return await self._cache.get_or_set(
            "/api/announcements", self._cache_ttl, lambda: self._fetch_json("/api/announcements"))

    async def api_events(self, tab: str = "upcoming", page: int = 1) -> Dict[str, Any]:
        tab = tab if tab in ("featured", "today", "upcoming", "past") else "upcoming"
        key = f"/api/events?tab={tab}&page={page}"
        return await self._cache.get_or_set(
            key, self._cache_ttl, lambda: self._fetch_json("/api/events", {"tab": tab, "page": page}))

    async def api_event_detail(self, event_id: int) -> Dict[str, Any]:
        key = f"/api/events/{event_id}"
        return await self._cache.get_or_set(
            key, self._cache_ttl, lambda: self._fetch_json(f"/api/events/{event_id}"))

    async def api_vtc_list(self, q: str = "", page: int = 1, recruiting: bool = False,
                           sort: str = "") -> Dict[str, Any]:
        key = f"/api/vtc?q={q}&p={page}&r={int(recruiting)}&s={sort}"
        params: Dict[str, Any] = {"page": page}
        if q:
            params["q"] = q
        if recruiting:
            params["recruiting"] = 1
        if sort in ("members", "new", "name"):
            params["sort"] = sort
        return await self._cache.get_or_set(
            key, self._cache_ttl, lambda: self._fetch_json("/api/vtc", params))

    async def api_vtc_detail(self, vtc_id: int) -> Dict[str, Any]:
        key = f"/api/vtc/{vtc_id}"
        return await self._cache.get_or_set(
            key, self._cache_ttl, lambda: self._fetch_json(f"/api/vtc/{vtc_id}"))

    async def api_version(self) -> Dict[str, Any]:
        return await self._cache.get_or_set(
            "/api/version", self._cache_ttl, lambda: self._fetch_json("/api/version"))

    # ----------------------------- 绑定系统（多账号） -----------------------------
    # 存储结构：{sender_id: {"accounts": [{cntir_id, name, bind_time}, ...]}}
    # 第 0 个为主账号；兼容旧的单账号格式 {sender_id: {cntir_id, name, bind_time}}（读取时自动迁移）
    def _load_bindings(self) -> Dict[str, Dict[str, Any]]:
        try:
            if os.path.exists(self.bind_file):
                with open(self.bind_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                return data if isinstance(data, dict) else {}
        except Exception as e:
            logger.warning(f"读取绑定数据失败: {e}")
        return {}

    def _save_bindings(self, bindings: Dict[str, Dict[str, Any]]) -> bool:
        try:
            os.makedirs(self.data_dir, exist_ok=True)
            with open(self.bind_file, "w", encoding="utf-8") as f:
                json.dump(bindings, f, ensure_ascii=False, indent=2)
            return True
        except Exception as e:
            logger.error(f"保存绑定数据失败: {e}")
            return False

    @property
    def _max_bind(self) -> int:
        return max(1, min(10, self._cfg_int("max_bind_count", 3)))

    @property
    def _p(self) -> str:
        """提示文案中的命令前缀：开启免前缀时为空串。"""
        return "" if self._cfg_bool("enable_no_prefix", True) else "cnt"

    def _accounts(self, sender_id: Any) -> List[Dict[str, Any]]:
        """返回该用户的已绑定账号列表（自动兼容旧格式）。"""
        raw = self._load_bindings().get(str(sender_id))
        if not isinstance(raw, dict):
            return []
        if isinstance(raw.get("accounts"), list):
            return [a for a in raw["accounts"] if isinstance(a, dict) and a.get("cntir_id")]
        if raw.get("cntir_id"):
            return [{"cntir_id": int(raw["cntir_id"]),
                     "name": raw.get("name", ""),
                     "bind_time": raw.get("bind_time", 0)}]
        return []

    def _primary_account(self, sender_id: Any) -> Optional[Dict[str, Any]]:
        accounts = self._accounts(sender_id)
        return accounts[0] if accounts else None

    def _write_accounts(self, sender_id: Any, accounts: List[Dict[str, Any]]) -> bool:
        bindings = self._load_bindings()
        if accounts:
            bindings[str(sender_id)] = {"accounts": accounts}
        else:
            bindings.pop(str(sender_id), None)
        return self._save_bindings(bindings)

    # ----------------------------- 消息辅助 -----------------------------
    @staticmethod
    def _keep_text(result):
        """纯文本回复保持以文字形式发出。

        AstrBot 的「文本转图像」功能会把较长的文本自动渲染成图片（render 阶段
        result_decorate.stage 处理），插件在此声明文本类回复无需转图；
        含图片的回复（定位、路况等）不做任何干预，照常发送图片。
        """
        try:
            chain = getattr(result, "chain", None) or []
            has_image = False
            for comp in chain:
                # 类型名兜底，避免不同 AstrBot 版本下 Image 类不一致时误判
                if isinstance(comp, Image) or "image" in type(comp).__name__.lower():
                    has_image = True
                    break
            if not has_image:
                result.use_t2i_ = False
        except Exception:
            pass
        return result

    @staticmethod
    def _extract_at_id(event: AstrMessageEvent) -> Optional[str]:
        """解析消息链中的 @ 目标用户 ID。"""
        message_obj = getattr(event, "message_obj", None)
        if message_obj is None:
            return None
        try:
            chain = getattr(message_obj, "message", None) or []
            for seg in chain:
                seg_type = getattr(seg, "type", None)
                if isinstance(seg, dict):
                    seg_type = seg.get("type") or seg_type
                if isinstance(seg_type, str) and seg_type.lower() == "at":
                    uid = (getattr(seg, "qq", None) or getattr(seg, "user_id", None)
                           or getattr(seg, "id", None))
                    if isinstance(seg, dict):
                        uid = seg.get("qq") or seg.get("user_id") or seg.get("id") or uid
                    return str(uid) if uid is not None else None
        except Exception:
            return None
        return None

    def _resolve_target_id(self, event: AstrMessageEvent, arg: str) -> Tuple[Optional[int], Optional[str]]:
        """解析查询目标：数字参数 > @ 他人 > 自己的绑定。返回 (cntir_id, 错误消息)。"""
        arg = (arg or "").strip()
        if arg:
            if arg.isdigit():
                return int(arg), None
            return None, "请输入正确的 CNTIR 账号 ID（纯数字），或 @ 要查询的群友"

        at_id = self._extract_at_id(event)
        if at_id:
            bind = self._primary_account(at_id)
            if not bind:
                return None, "对方尚未绑定 CNTIR 账号"
            return int(bind["cntir_id"]), None

        bind = self._primary_account(event.get_sender_id())
        if not bind:
            return None, f"您还没有绑定 CNTIR 账号，使用「{self._p}绑定 <账号ID>」绑定后即可免输入查询"
        return int(bind["cntir_id"]), None

    def _abs_url(self, path: Any) -> str:
        """相对路径（头像/logo 等）→ 绝对 URL。"""
        p = str(path or "").strip()
        if not p:
            return ""
        if p.startswith("http://") or p.startswith("https://"):
            return p
        return f"{API_BASE}/{p.lstrip('/')}"

    # ----------------------------- 渲染层 -----------------------------
    MAP_W = 1000          # 地图视口宽（与 templates/locate.html 保持一致）
    MAP_H = 800           # 地图视口高
    TILE_SIZE = 512       # 瓦片像素（ETS2 底图为 512px 瓦片）

    async def _fetch_tile_uri(self, url: str) -> Optional[str]:
        """下载单张瓦片并转成 data URI（内存缓存），失败重试一次。"""
        cached = self._tile_cache.get(url)
        if cached:
            return cached
        if not self.session:
            return None
        data = b""
        for _ in range(2):
            try:
                # 瓦片镜像的证书与域名不匹配（Hostname mismatch），严格校验会直接失败，
                # 表现为「地图有时空白 / 只有一半」。仅对公开瓦片图片放宽校验，其余请求仍严格校验。
                async with self.session.get(url, timeout=aiohttp.ClientTimeout(total=10),
                                            ssl=False) as resp:
                    if resp.status == 200:
                        data = await resp.read()
                        if data:
                            break
            except Exception:
                await asyncio.sleep(0.3)
        if not data:
            return None
        uri = "data:image/png;base64," + base64.b64encode(data).decode()
        if len(self._tile_cache) >= 400:
            self._tile_cache.clear()
        self._tile_cache[url] = uri
        return uri

    @staticmethod
    def _fmt_tile(tmpl: str, z: int, x: int, y: int) -> str:
        """瓦片 URL 模板填充，模板不含占位符时原样返回。"""
        try:
            return tmpl.format(z=z, x=x, y=y)
        except Exception:
            return tmpl

    def _fit_zoom(self, xs: List[float], zs: List[float], margin: float = 0.85) -> float:
        """按点位范围选缩放级别（2~8），保证所有点都落在画面内（避免只看到一两个点）。"""
        try:
            span = max(max(xs) - min(xs), max(zs) - min(zs)) / 1.609055
        except (ValueError, ZeroDivisionError):
            return 8.0
        if span <= 0:
            return 8.0
        zoom = math.floor(8 + math.log2(self.MAP_W * margin / span))
        return float(max(2, min(8, zoom)))

    async def _tile_uri(self, primary: str, fallback: str) -> Optional[str]:
        uri = await self._fetch_tile_uri(primary)
        if uri is None and fallback:
            uri = await self._fetch_tile_uri(fallback)
        return uri

    async def _render_map(self, players: List[Dict[str, Any]], center: Tuple[float, float],
                          zoom: float = 8.0) -> Optional[str]:
        """渲染 ETS2 底图 + 玩家点，成功返回图片 URL，失败返回 None（由调用方降级）。

        底图改为「先在 Python 里下载瓦片并内联成 data URI，再用静态 HTML 摆位」，
        不再依赖 Leaflet/JS 异步取瓦片，避免截图时地图空白或只渲染一半。
        players: [{"axisX", "axisZ", "me"}]；center: (x, z)
        坐标换算：CNTIR 世界坐标（米）→ z8 瓦片像素（÷1.609055 + 平移），再按 zoom 缩放。
        """
        if not self._cfg_bool("map_enable", True) or not players or not self.session:
            return None
        try:
            z = max(2, min(8, int(round(zoom))))
            scale = 2 ** (8 - z)          # z8 像素 → 当前缩放级别的像素
            tile_px = self.TILE_SIZE
            max_idx = 2 ** z - 1

            def to_px(x: Any, y: Any) -> Tuple[float, float]:
                return ((float(x or 0) / 1.609055 + 70272) / scale,
                        (float(y or 0) / 1.609055 + 76157) / scale)

            cx, cz = to_px(center[0], center[1])
            left = cx - self.MAP_W / 2
            top = cz - self.MAP_H / 2

            tile_tmpl = self._cfg_str("tile_url_ets", "").strip() or DEFAULT_TILE_URL
            jobs: List[Tuple[int, int, str, str]] = []
            for i in range(int(math.floor(left / tile_px)),
                           int(math.floor((left + self.MAP_W) / tile_px)) + 1):
                for j in range(int(math.floor(top / tile_px)),
                               int(math.floor((top + self.MAP_H) / tile_px)) + 1):
                    if not (0 <= i <= max_idx and 0 <= j <= max_idx):
                        continue
                    jobs.append((i, j,
                                 self._fmt_tile(tile_tmpl, z, i, j),
                                 self._fmt_tile(FALLBACK_TILE_URL, z, i, j)))
            if not jobs:
                return None

            uris = await asyncio.gather(*[
                self._tile_uri(p, f) for _i, _j, p, f in jobs
            ])
            tiles = []
            missing: List[Tuple[int, int]] = []
            for (i, j, _p, _f), uri in zip(jobs, uris):
                if not uri:
                    missing.append((i, j))
                    continue
                tiles.append({
                    "src": uri, "size": tile_px,
                    "left": round(i * tile_px - left, 1),
                    "top": round(j * tile_px - top, 1),
                })

            # 缺块先用上一级（更粗）瓦片放大 2 倍补位，避免画面出现整块黑洞
            if missing and z > 2:
                parents = sorted({(i // 2, j // 2) for i, j in missing})
                puris = await asyncio.gather(*[
                    self._tile_uri(self._fmt_tile(tile_tmpl, z - 1, pi, pj),
                                   self._fmt_tile(FALLBACK_TILE_URL, z - 1, pi, pj))
                    for pi, pj in parents
                ])
                filled = set()
                for (pi, pj), uri in zip(parents, puris):
                    if not uri:
                        continue
                    tiles.append({
                        "src": uri, "size": tile_px * 2, "parent": True,
                        "left": round(pi * tile_px * 2 - left, 1),
                        "top": round(pj * tile_px * 2 - top, 1),
                    })
                    filled |= {(i, j) for i, j in missing if (i // 2, j // 2) == (pi, pj)}
                missing = [c for c in missing if c not in filled]

            # 仍缺块时改用下一级（更细）瓦片缩小 0.5 倍补位（z=2 没有上一级时也能补上）
            if missing and z < 8:
                kids: List[Tuple[int, int, int, int, int, int]] = []
                for i, j in missing:
                    for dx in (0, 1):
                        for dy in (0, 1):
                            kids.append((i * 2 + dx, j * 2 + dy, i, j, dx, dy))
                kuris = await asyncio.gather(*[
                    self._tile_uri(self._fmt_tile(tile_tmpl, z + 1, ci, cj),
                                   self._fmt_tile(FALLBACK_TILE_URL, z + 1, ci, cj))
                    for ci, cj, _i, _j, _dx, _dy in kids
                ])
                half = tile_px // 2
                for (ci, cj, pi, pj, dx, dy), uri in zip(kids, kuris):
                    if not uri:
                        continue
                    tiles.append({
                        "src": uri, "size": half,
                        "left": round(pi * tile_px + dx * half - left, 1),
                        "top": round(pj * tile_px + dy * half - top, 1),
                    })
            if not tiles:
                return None

            markers = []
            for p in players:
                mx, my = to_px(p.get("axisX"), p.get("axisZ"))
                x, y = mx - left, my - top
                if not (-16 <= x <= self.MAP_W + 16 and -16 <= y <= self.MAP_H + 16):
                    continue
                me = bool(p.get("me"))
                markers.append({
                    "x": round(x, 1), "y": round(y, 1),
                    "size": 14 if me else 8,
                    "color": "#57bd00" if me else "#3ca7ff",
                    "me": me,
                })

            tmpl = await asyncio.to_thread(
                self._read_file, os.path.join(self.tpl_dir, "locate.html"))
            if not tmpl:
                return None
            url = await self.html_render(tmpl, {
                "width": self.MAP_W, "height": self.MAP_H,
                "tiles": tiles, "markers": markers,
            }, options={
                "type": "jpeg", "quality": 92, "full_page": True,
                "timeout": 10000, "animations": "disabled",
            })
            return url if isinstance(url, str) and url else None
        except Exception as e:
            logger.warning(f"地图渲染失败，降级为文字: {e}")
            return None

    async def _render_server_image(self, data: Dict[str, Any]) -> Optional[str]:
        """渲染服务器卡片图（深蓝底 + 分服卡片 + 24h 曲线），失败返回 None（调用方降级文字）。"""
        try:
            tmpl_path = os.path.join(self.tpl_dir, "server_card.html")
            tmpl = await asyncio.to_thread(self._read_file, tmpl_path)
            if not tmpl:
                return None
            payload = dict(data)
            payload["time"] = _ts_to_bj(time.time())
            url = await self.html_render(tmpl, payload, options={
                "type": "png", "full_page": True, "timeout": 10000, "animations": "disabled",
            })
            return url if isinstance(url, str) and url else None
        except Exception as e:
            logger.warning(f"服务器卡片渲染失败，降级为文字: {e}")
            return None

    @staticmethod
    def _read_file(path: str) -> str:
        try:
            with open(path, "r", encoding="utf-8") as f:
                return f.read()
        except Exception:
            return ""

    @staticmethod
    def _find_player_in_map(map_data: Any, account: int) -> Optional[Dict[str, Any]]:
        """在全量地图快照中定位某账号。"""
        try:
            for server in (map_data or {}).get("servers", []):
                for p in server.get("players", []):
                    if str(p.get("account")) == str(account):
                        return p
        except Exception:
            return None
        return None

    # ================================================================
    # 业务命令实现（均为 async generator）
    # ================================================================
    async def do_menu(self, event: AstrMessageEvent, arg: str = ""):
        # 菜单按当前前缀策略自适应：开启免前缀时直接展示「查询」而非「cnt查询」
        p = self._p
        rows = [
            ("🔗", "绑定 <账号ID>", "绑定 CNTIR 账号（可绑多个）"),
            ("🔗", "解绑 [序号/账号ID/全部]", "解除绑定"),
            ("🔗", "切换 <序号/账号ID>", "切换主账号"),
            ("🔗", "我的", "查看我的全部绑定"),
            ("👤", "查询 [ID/@人]", "玩家资料（留空查主账号）"),
            ("📍", "定位 [ID/@人]", "实时位置地图"),
            ("🛣", "路况 [服务器]", "在线玩家分布统计"),
            ("🖥", "服务器", "服务器状态"),
            ("🚚", "车队 [ID/关键词]", "车队列表/搜索/详情"),
            ("🎉", "活动 [今日/精选/结束/页码]", "活动列表"),
            ("🎉", "活动详情 [序号]", "活动详情（列表第几个，不填看第 1 场）"),
            ("📰", "新闻 [页码]", "新闻列表"),
            ("📰", "新闻详情 [新闻ID]", "新闻正文（不填看最新一篇）"),
            ("🔔", "公告", "全站公告"),
            ("⚙️", "版本", "版本信息"),
            ("❓", "菜单", "本帮助"),
        ]
        body = "\n".join(f"{icon} {p}{cmd} — {desc}" for icon, cmd, desc in rows)
        tip = "直接发送中文指令即可\n" if not p else "指令需带 cnt 前缀\n"
        text = (
            f"🚛 CNTIR 查询机器人 指令一览\n"
            f"────────────────────\n{tip}"
            f"────────────────────\n{body}\n"
            f"────────────────────\n"
            f"数据来源：CNTIR（{PLUGIN_PAGE}），接口免费公开，请合理使用"
        )
        yield event.plain_result(text)

    async def do_bind(self, event: AstrMessageEvent, arg: str = ""):
        arg = (arg or "").strip()
        sender_id = str(event.get_sender_id())
        if not arg or not arg.isdigit():
            yield event.plain_result("请输入要绑定的 CNTIR 账号 ID（纯数字）\n"
                                     f"账号 ID 可在 CNTIR 官网个人主页查看，格式：{self._p}绑定 1024")
            return
        uid = int(arg)
        try:
            user = await self.api_user(uid)
        except CntirApiError as e:
            if e.status == 404:
                yield event.plain_result(f"账号 {uid} 不存在，请检查 ID 是否正确")
            else:
                yield event.plain_result(f"绑定失败：{e.message}")
            return
        name = _clean(user.get("name")) or f"玩家{uid}"
        accounts = self._accounts(sender_id)
        if any(int(a["cntir_id"]) == uid for a in accounts):
            yield event.plain_result(f"账号 {uid}（{name}）已经在您的绑定列表中了")
            return
        if len(accounts) >= self._max_bind:
            yield event.plain_result(
                f"最多只能绑定 {self._max_bind} 个账号，请先解绑：{self._p}解绑 <序号/账号ID>")
            return
        accounts.append({"cntir_id": uid, "name": name, "bind_time": int(_utc_offset_ts())})
        if self._write_accounts(sender_id, accounts):
            tip = "（主账号）" if len(accounts) == 1 else f"（第 {len(accounts)} 个账号，非主账号）"
            yield event.plain_result(
                f"绑定成功！\n已将您的账号与 CNTIR 玩家「{name}」(ID: {uid}) 绑定{tip}\n"
                f"当前共绑定 {len(accounts)}/{self._max_bind} 个账号\n"
                f"免输入查询默认使用主账号，切换：{self._p}切换 <序号/账号ID>")
        else:
            yield event.plain_result("绑定失败：绑定数据写入异常，请稍后重试")

    async def do_unbind(self, event: AstrMessageEvent, arg: str = ""):
        arg = (arg or "").strip()
        sender_id = str(event.get_sender_id())
        accounts = self._accounts(sender_id)
        if not accounts:
            yield event.plain_result("您还没有绑定任何 CNTIR 账号")
            return

        if arg.lower() in ("all", "全部", "*"):
            self._write_accounts(sender_id, [])
            yield event.plain_result(f"已解除全部绑定（共 {len(accounts)} 个账号）")
            return

        idx = 0  # 默认解绑主账号
        if arg:
            if arg.isdigit() and 1 <= int(arg) <= len(accounts):
                idx = int(arg) - 1
            elif arg.isdigit():
                matched = next((i for i, a in enumerate(accounts)
                                if int(a["cntir_id"]) == int(arg)), None)
                if matched is None:
                    yield event.plain_result(f"绑定列表中没有账号 {arg}，可用：{self._p}我的 查看")
                    return
                idx = matched
            else:
                yield event.plain_result(f"用法：{self._p}解绑 [序号/账号ID/全部]")
                return

        removed = accounts.pop(idx)
        if self._write_accounts(sender_id, accounts):
            rest = f"，剩余 {len(accounts)} 个账号" if accounts else "，已无绑定账号"
            yield event.plain_result(f"解绑成功！\n已解除「{removed.get('name')}」"
                                     f"(ID: {removed['cntir_id']}){rest}")
        else:
            yield event.plain_result("解绑失败：数据写入异常，请稍后重试")

    async def do_switch(self, event: AstrMessageEvent, arg: str = ""):
        """切换主账号：把指定账号移到列表首位。"""
        arg = (arg or "").strip()
        sender_id = str(event.get_sender_id())
        accounts = self._accounts(sender_id)
        if not accounts:
            yield event.plain_result("您还没有绑定任何 CNTIR 账号")
            return
        if len(accounts) == 1:
            yield event.plain_result("您只绑定了 1 个账号，无需切换")
            return
        if not arg:
            yield event.plain_result(f"用法：{self._p}切换 <序号/账号ID>\n" + self._fmt_account_list(accounts))
            return
        idx = None
        if arg.isdigit() and 1 <= int(arg) <= len(accounts):
            idx = int(arg) - 1
        elif arg.isdigit():
            idx = next((i for i, a in enumerate(accounts) if int(a["cntir_id"]) == int(arg)), None)
        if idx is None:
            yield event.plain_result(f"未找到「{arg}」，可用：{self._p}我的 查看绑定列表")
            return
        if idx == 0:
            yield event.plain_result("该账号已经是主账号")
            return
        accounts.insert(0, accounts.pop(idx))
        if self._write_accounts(sender_id, accounts):
            yield event.plain_result(
                f"已将「{accounts[0].get('name')}」(ID: {accounts[0]['cntir_id']}) 设为主账号")
        else:
            yield event.plain_result("切换失败：数据写入异常，请稍后重试")

    @staticmethod
    def _fmt_account_list(accounts: List[Dict[str, Any]]) -> str:
        lines = []
        for i, a in enumerate(accounts, start=1):
            mark = "⭐主账号" if i == 1 else f"  {i}."
            lines.append(f"{mark} {a.get('name', '未知')}（ID: {a['cntir_id']}）· "
                         f"绑定于 {_ts_to_bj(a.get('bind_time'), with_time=False)}")
        return "\n".join(lines)

    async def do_my(self, event: AstrMessageEvent, arg: str = ""):
        accounts = self._accounts(event.get_sender_id())
        if not accounts:
            yield event.plain_result(f"您还没有绑定 CNTIR 账号，使用「{self._p}绑定 <账号ID>」先绑定")
            return
        yield event.plain_result(
            f"🔗 我的 CNTIR 绑定（{len(accounts)}/{self._max_bind}）\n"
            + self._fmt_account_list(accounts)
            + f"\n切换主账号：{self._p}切换 <序号/账号ID>")

    async def do_query(self, event: AstrMessageEvent, arg: str = ""):
        uid, err = self._resolve_target_id(event, arg)
        if err:
            yield event.plain_result(err)
            return
        try:
            user = await self.api_user(uid)
        except CntirApiError as e:
            if e.status == 404:
                yield event.plain_result(f"玩家 {uid} 不存在，请检查 ID")
            else:
                yield event.plain_result(f"查询失败：{e.message}")
            return

        lines: List[str] = []
        lines.append(f"🆔 账号 ID: {user.get('id')}")
        lines.append(f"😀 名称: {_clean(user.get('name'))}")
        position = user.get("position") or {}
        pos_name = _clean(position.get("name")) if isinstance(position, dict) else ""
        role_txt = pos_name or ROLE_MAP.get(str(user.get("role") or ""), str(user.get("role") or ""))
        if role_txt:
            lines.append(f"🎭 身份: {role_txt}")
        tag = _clean(user.get("tag"))
        if tag:
            lines.append(f"🏷 标签: {tag}")
        vtc = user.get("vtc")
        if isinstance(vtc, dict) and vtc.get("name"):
            role_v = _clean(vtc.get("role"))
            lines.append(f"🚚 车队: {vtc.get('name')} ({_clean(vtc.get('tag')) or '-'})"
                         + (f" · {role_v}" if role_v else ""))
        country = _clean(user.get("country"))
        langs = _clean(user.get("languages"))
        if country or langs:
            lines.append(f"🌐 国家/语言: {country or '-'} / {langs or '-'}")
        lines.append(f"📅 注册时间: {_ts_to_bj(user.get('created'), with_time=False)}"
                     f" ({_rel_time(user.get('created'))})")
        lines.append(f"💤 最后活跃: {_rel_time(user.get('last_seen'))}")
        steam_name = _clean(user.get("steam_name"))
        if steam_name:
            lines.append(f"🎮 Steam: {steam_name}")

        online = user.get("online")
        if isinstance(online, dict) and online.get("name"):
            lines.append(f"📶 在线: 🟢 {online.get('name')} ({_clean(online.get('code'))})")
        else:
            lines.append("📶 在线: ⚫ 离线")

        if user.get("banned"):
            lines.append("🚫 状态: 已封禁（详情见官网主页）")
        else:
            bans = user.get("bans") or []
            lines.append(f"🚫 封禁: 无" + (f"（历史 {len(bans)} 次）" if bans else ""))

        totals = user.get("totals") or {}
        if totals:
            lines.append(
                f"📦 累计运单: {totals.get('deliveries', 0)} 次 · "
                f"里程 {totals.get('distance_km', 0)} km · "
                f"收入 {_fmt_revenue(totals.get('revenue'))} · "
                f"货物 {_fmt_float(totals.get('mass_t'))} 吨")

        deliveries = user.get("deliveries") or []
        if deliveries:
            lines.append("🚚 最近运单:")
            for d in deliveries[:3]:
                lines.append(
                    f"  · {_clean(d.get('cargo'))}: {_clean(d.get('src'))} → {_clean(d.get('dst'))} "
                    f"({_fmt_float(d.get('distance_km'))}km, {_fmt_revenue(d.get('revenue'))}) "
                    f"{_rel_time(d.get('at'))}")

        events = user.get("events") or []
        if events:
            names = ", ".join(_clean(e.get("title")) for e in events[:3])
            lines.append(f"🎉 即将参加活动: {len(events)} 场 — {names}")

        text = "👤 CNTIR 玩家资料\n" + "\n".join(lines)

        avatar = self._abs_url(user.get("avatar"))
        if avatar and self._cfg_bool("query_show_avatar", True):
            try:
                yield event.chain_result([Image.fromURL(avatar), Plain(text)])
                return
            except Exception as e:
                logger.warning(f"头像发送失败: {e}")
        yield event.plain_result(text)

    async def _servers_text(self) -> str:
        """服务器状态列表文本（原「服务器」指令的完整输出）。"""
        servers = await self.api_servers()
        if not servers:
            return "当前没有可用的 CNTIR 服务器"
        blocks = []
        for s in servers:
            online = bool(s.get("online"))
            head = f"{'🟢' if online else '⚫'} {_clean(s.get('name'))} ({_clean(s.get('code'))})" \
                   f" — {'欧卡2' if s.get('game') == 'ets2' else s.get('game', '?')}"
            block = [head]
            block.append(f"👥 人数: {s.get('players', 0)}/{s.get('max_players', 0)}"
                         + (f"（排队 {s.get('queue')}）" if s.get("queue") else "")
                         + f" · 历史峰值 {s.get('peak', 0)}")
            if online:
                up = _fmt_int(s.get("uptime_s"))
                up_txt = f"{up // 3600}小时{up % 3600 // 60}分" if up >= 3600 else f"{up // 60}分钟"
                block.append(f"🕐 游戏内时间: {s.get('game_time') or '-'} · 已运行 {up_txt}")
            feats = []
            if s.get("collisions"):
                feats.append("💥碰撞")
            sl = _fmt_int(s.get("speed_limit"))
            slc = _fmt_int(s.get("speed_limit_city"))
            if sl or slc:
                feats.append(f"限速{sl or '∞'}/{slc or '∞'}")
            if s.get("afk_kick"):
                feats.append("⏱挂机踢")
            if s.get("cars_for_players"):
                feats.append("🚗轿车")
            if s.get("police_for_players"):
                feats.append("🚓警车")
            if s.get("event"):
                feats.append("🎪活动服")
            if s.get("promods"):
                feats.append("🗺ProMods")
            if feats:
                block.append("⚙️ 特性: " + " ".join(feats))
            desc = _clean(s.get("desc"))
            if desc:
                block.append(f"📝 {desc}")
            blocks.append("\n".join(block))
        text = "🖥 CNTIR 服务器列表\n\n" + "\n\n".join(blocks)
        hist_blocks = []
        for s in servers:
            code = _clean(s.get("code"))
            if not code:
                continue
            points = await self._history_points(code)
            hist_blocks.append(self._history_block(_clean(s.get("name")), code, points))
        if hist_blocks:
            text += "\n\n" + "\n\n".join(hist_blocks)
        stats = await self._stats_text()
        if stats:
            text += "\n\n" + stats
        return text

    async def _stats_text(self) -> str:
        """平台统计文本（原「统计」指令的完整输出），失败返回空串（不影响服务器信息）。"""
        try:
            st = await self.api_stats()
        except CntirApiError:
            return ""
        return (
            "📊 CNTIR 平台统计\n"
            f"👥 当前在线: {st.get('players_online', 0)}（24h 峰值 {st.get('peak_today', 0)}）\n"
            f"🖥 服务器: {st.get('servers_online', 0)}/{st.get('servers', 0)} 在线\n"
            f"👤 注册用户: {st.get('users', 0)} · 车队 {st.get('vtcs', 0)}\n"
            f"📦 累计运单: {st.get('deliveries', 0)} · 累计里程 {st.get('distance_km', 0)} km\n"
            f"🎉 即将开始的活动: {st.get('events_upcoming', 0)}"
        )

    async def _stats_summary(self) -> List[Dict[str, str]]:
        """平台统计 → 图片顶部的摘要条目，失败返回空列表。"""
        try:
            st = await self.api_stats()
        except CntirApiError:
            return []
        return [
            {"label": "当前在线", "value": f"{_fmt_int(st.get('players_online'))}"},
            {"label": "24h 峰值", "value": f"{_fmt_int(st.get('peak_today'))}"},
            {"label": "服务器", "value": f"{_fmt_int(st.get('servers_online'))}/{_fmt_int(st.get('servers'))}"},
            {"label": "注册用户", "value": f"{_fmt_int(st.get('users'))}"},
            {"label": "车队", "value": f"{_fmt_int(st.get('vtcs'))}"},
            {"label": "累计运单", "value": f"{_fmt_int(st.get('deliveries'))}"},
            {"label": "累计里程", "value": f"{_fmt_int(st.get('distance_km'))} km"},
        ]

    async def _history_points(self, code: str, hours: int = 24) -> List[Dict[str, Any]]:
        """取某台服务器的在线采样点（按时间升序），失败返回空列表。"""
        hours = max(1, min(336, hours))
        try:
            points = await self.api_server_history(code.upper(), hours)
        except CntirApiError:
            return []
        points = [p for p in points if isinstance(p, dict)] if isinstance(points, list) else []
        points.sort(key=lambda p: _fmt_int(p.get("t")))
        return points

    @staticmethod
    def _history_stats(points: List[Dict[str, Any]]) -> Dict[str, Any]:
        """历史采样点 → 在线率 / 最长连续在线 / 上下线切换 / 当前状态。"""
        total = len(points)
        if not total:
            return {}
        online_n = sum(1 for p in points if p.get("online"))
        span = _fmt_int(points[-1].get("t")) - _fmt_int(points[0].get("t"))
        interval = max(60, span // (total - 1)) if total > 1 else 300
        # 最长连续在线时段 + 上下线切换次数
        best = cur = 0
        switches = 0
        prev = None
        for p in points:
            on = bool(p.get("online"))
            cur = cur + 1 if on else 0
            best = max(best, cur)
            if prev is not None and on != prev:
                switches += 1
            prev = on
        return {
            "total": total,
            "online_n": online_n,
            "rate": online_n / total * 100,
            "interval": interval,
            "best": best,
            "switches": switches,
            "last_on": bool(points[-1].get("online")),
            "last_value": _fmt_int(points[-1].get("online")),
        }

    @classmethod
    def _history_block(cls, name: str, code: str, points: List[Dict[str, Any]],
                       hours: int = 24) -> str:
        """服务器在线历史文本（原「历史」指令的完整输出）。"""
        title = name or code
        if not points:
            return (f"📈 服务器在线历史 — {title}（{code}）\n"
                    f"⏱ 最近 {hours} 小时没有历史采样数据")
        st = cls._history_stats(points)
        return (
            f"📈 服务器在线历史 — {title}（{code}）\n"
            f"⏱ 统计范围: 最近 {hours} 小时（{st['total']} 个采样点，约 {st['interval'] // 60} 分钟/点）\n"
            f"🟢 在线率: {st['rate']:.1f}%（{st['online_n']}/{st['total']}）\n"
            f"🥇 最长连续在线: 约 {st['best'] * st['interval'] // 3600} 小时"
            f"{st['best'] * st['interval'] % 3600 // 60} 分\n"
            f"🔁 上下线切换: {st['switches']} 次\n"
            f"📶 当前状态: {'🟢 在线' if st['last_on'] else '⚫ 离线'}"
        )

    @staticmethod
    def _server_tags(s: Dict[str, Any]) -> List[str]:
        """服务器特性 → 图片卡片上的小标签。"""
        tags = []
        if s.get("collisions"):
            tags.append("💥 碰撞")
        sl = _fmt_int(s.get("speed_limit"))
        slc = _fmt_int(s.get("speed_limit_city"))
        if sl or slc:
            tags.append(f"🚦 限速{sl or '∞'}/{slc or '∞'}")
        if s.get("afk_kick"):
            tags.append("⏱ 挂机踢")
        if s.get("cars_for_players"):
            tags.append("🚗 轿车")
        if s.get("police_for_players"):
            tags.append("🚓 警车")
        if s.get("event"):
            tags.append("🎪 活动服")
        if s.get("promods"):
            tags.append("🗺 ProMods")
        return tags

    @staticmethod
    def _spark(values: List[Any], height: float = 18.0) -> Optional[Dict[str, Any]]:
        """流量波形（卡片顶部的细线迷你图）：最近短窗口的人数序列。"""
        vals = [max(0, _fmt_int(v)) for v in values]
        if len(vals) < 2:
            return None
        w, h = 960.0, height
        ymax = max(1, max(vals))
        n = len(vals)
        pts = []
        for i, v in enumerate(vals):
            x = i * w / (n - 1)
            y = h - 2 - (v / ymax) * (h - 4)
            pts.append((x, y))
        line = "M " + " L ".join(f"{x:.1f} {y:.1f}" for x, y in pts)
        return {"line": line, "w": int(w), "h": int(h)}

    @staticmethod
    def _chart(values: List[Any]) -> Optional[Dict[str, Any]]:
        """人数序列 → SVG 折线/面积路径（960×110 视口）。"""
        vals = [max(0, _fmt_int(v)) for v in values]
        if len(vals) < 2:
            return None
        w, h = 960.0, 110.0
        ymax = max(1, max(vals))
        n = len(vals)
        pts = []
        for i, v in enumerate(vals):
            x = i * w / (n - 1)
            y = h - 6 - (v / ymax) * (h - 24)
            pts.append((x, y))
        line = "M " + " L ".join(f"{x:.1f} {y:.1f}" for x, y in pts)
        area = f"{line} L {w:.0f} {h:.0f} L 0 {h:.0f} Z"
        return {"line": line, "area": area, "w": int(w), "h": int(h), "max": ymax}

    async def _build_server_cards(self, hours: int = 24) -> Optional[Dict[str, Any]]:
        """构建图片数据：每台服务器一张卡片（状态 + 流量波形 + 历史曲线与统计）+ 平台统计。"""
        servers = await self.api_servers()
        if not servers:
            return None
        cards = []
        total = 0
        for s in servers:
            code = _clean(s.get("code"))
            players = _fmt_int(s.get("players"))
            max_players = _fmt_int(s.get("max_players"))
            total += players
            points = await self._history_points(code, hours) if code else []
            recent = await self._history_points(code, 1) if code else []
            st = self._history_stats(points)
            stats = []
            if st:
                stats = [
                    {"label": "在线率", "value": f"{st['rate']:.1f}%"},
                    {"label": f"最近 {hours} 小时", "value": f"{st['total']} 个采样点"},
                    {"label": "最长连续在线",
                     "value": f"{st['best'] * st['interval'] // 3600} 小时"
                              f"{st['best'] * st['interval'] % 3600 // 60} 分"},
                    {"label": "上下线切换", "value": f"{st['switches']} 次"},
                ]
            cards.append({
                "name": _clean(s.get("name")) or code or "未知服务器",
                "code": code,
                "online": bool(s.get("online")),
                "players": players,
                "max_players": max_players,
                "pct": round(players / max_players * 100, 1) if max_players else 0,
                "tags": self._server_tags(s),
                "mini": self._spark([p.get("online") for p in recent]),
                "chart": self._chart([p.get("online") for p in points]),
                "stats": stats,
            })
        return {
            "title": "CNTIR 服务器状态",
            "subtitle": f"实时人数 · 平台统计 · 最近 {hours} 小时在线历史",
            "total": total,
            "total_label": "在线",
            "summary": await self._stats_summary(),
            "cards": cards,
        }

    async def do_servers(self, event: AstrMessageEvent, arg: str = ""):
        """服务器状态 + 平台统计 + 在线历史：默认发深蓝图片卡片，渲染失败时降级为文字。"""
        try:
            text = await self._servers_text()
        except CntirApiError as e:
            yield event.plain_result(f"查询失败：{e.message}")
            return
        try:
            data = await self._build_server_cards()
        except CntirApiError as e:
            logger.warning(f"服务器卡片取数失败，改为文字输出: {e}")
            data = None
        if data:
            url = await self._render_server_image(data)
            if url:
                try:
                    yield event.chain_result([Image.fromURL(url)])
                    return
                except Exception as e:
                    logger.warning(f"服务器图片发送失败: {e}")
        yield event.plain_result(text)

    async def do_locate(self, event: AstrMessageEvent, arg: str = ""):
        uid, err = self._resolve_target_id(event, arg)
        if err:
            yield event.plain_result(err)
            return
        try:
            user = await self.api_user(uid)
            map_data = await self.api_map()
        except CntirApiError as e:
            yield event.plain_result(f"定位失败：{e.message}")
            return

        name = _clean(user.get("name")) or str(uid)
        online = user.get("online")
        if not (isinstance(online, dict) and online.get("code")):
            yield event.plain_result(
                f"📍 玩家「{name}」当前离线\n"
                f"💤 最后活跃: {_rel_time(user.get('last_seen'))}")
            return

        p = self._find_player_in_map(map_data, uid)
        if not p:
            yield event.plain_result(
                f"玩家「{name}」在线（{online.get('name')}），但实时地图快照中暂无其坐标，"
                f"地图约每 5 秒刷新一次，请稍后重试")
            return

        px, pz = float(p.get("x") or 0), float(p.get("z") or 0)
        speed = _fmt_int(p.get("speed"))
        heading = _heading_arrow(p.get("heading"))

        # 附近玩家（同账号排除，按配置半径）
        radius = max(500, self._cfg_int("nearby_radius_m", 5000))
        max_cnt = max(1, self._cfg_int("nearby_max_count", 10))
        nearby: List[Tuple[float, Dict[str, Any]]] = []
        try:
            for server in map_data.get("servers", []):
                if server.get("code") != online.get("code"):
                    continue
                for q in server.get("players", []):
                    if str(q.get("account")) == str(uid):
                        continue
                    d = _dist_m(px, pz, q.get("x") or 0, q.get("z") or 0)
                    if d <= radius:
                        nearby.append((d, q))
        except Exception:
            nearby = []
        nearby.sort(key=lambda t: t[0])

        lines = [
            f"📍 实时定位 — {name}（账号 {uid}）",
            f"🖥 服务器: {online.get('name')} ({_clean(online.get('code'))})",
            f"🚗 速度: {speed} km/h" + ("（行驶中）" if speed > 0 else "（静止）"),
            f"🧭 朝向: {heading or '-'}",
            f"🗺 坐标: ({_fmt_float(px)}, {_fmt_float(pz)})",
        ]
        if nearby:
            shown = [f"{_clean(q.get('name'))}（{_fmt_int(d / 1000)}km）" for d, q in nearby[:max_cnt]]
            lines.append(f"👥 附近玩家（{radius // 1000}km 内）: {len(nearby)} 人 — " + "、".join(shown))
        else:
            lines.append(f"👥 附近玩家（{radius // 1000}km 内）: 暂无")

        # 地图渲染（自己绿色，附近蓝色）
        points = [{"axisX": px, "axisZ": pz, "me": 1}]
        for d, q in nearby[:max_cnt]:
            points.append({"axisX": float(q.get("x") or 0), "axisZ": float(q.get("z") or 0), "me": 0})
        text = "\n".join(lines)
        xs = [p["axisX"] for p in points]
        zs = [p["axisZ"] for p in points]
        cx = (min(xs) + max(xs)) / 2
        cz = (min(zs) + max(zs)) / 2
        url = await self._render_map(points, (cx, cz), zoom=self._fit_zoom(xs, zs))
        if url:
            try:
                yield event.chain_result([Image.fromURL(url), Plain(text)])
                return
            except Exception as e:
                logger.warning(f"定位图片发送失败: {e}")
        yield event.plain_result(text)

    async def do_traffic(self, event: AstrMessageEvent, arg: str = ""):
        arg = (arg or "").strip()
        try:
            map_data = await self.api_map(arg.upper() if arg else None)
        except CntirApiError as e:
            yield event.plain_result(f"路况查询失败：{e.message}")
            return

        blocks: List[str] = []
        all_points: List[Dict[str, Any]] = []
        servers = map_data.get("servers", []) if isinstance(map_data, dict) else []
        if not servers:
            yield event.plain_result(f"所选范围当前没有在线玩家" + (f"（{arg.upper()}）" if arg else ""))
            return
        for server in servers:
            players = server.get("players", [])
            code = _clean(server.get("code"))
            block = [f"🟢 {_clean(server.get('name'))} ({code}) — 在线 {len(players)} 人"]
            if not players:
                blocks.append("\n".join(block))
                continue
            speeds = [_fmt_int(q.get("speed")) for q in players]
            moving = [s for s in speeds if s > 0]
            if moving:
                block.append(f"🚗 行驶中: {len(moving)} 人 · 平均速度 {_fmt_float(sum(moving) / len(moving))} km/h")
                fastest = sorted(
                    ((q, _fmt_int(q.get("speed"))) for q in players if _fmt_int(q.get("speed")) > 0),
                    key=lambda t: t[1], reverse=True)[:3]
                tops = "、".join(f"{_clean(q.get('name'))}({s})" for q, s in fastest)
                block.append(f"💨 最快: {tops}")
            else:
                block.append("🚗 行驶中: 0 人（全部停靠）")
            # 5km 网格聚集统计
            grid: Dict[Tuple[int, int], List[Dict[str, Any]]] = {}
            for q in players:
                gx = int(float(q.get("x") or 0) // 5000)
                gz = int(float(q.get("z") or 0) // 5000)
                grid.setdefault((gx, gz), []).append(q)
            hot = sorted(grid.items(), key=lambda kv: len(kv[1]), reverse=True)[:5]
            hot_txt = "、".join(
                f"({k[0] * 5}~{k[0] * 5 + 5}km, {k[1] * 5}~{k[1] * 5 + 5}km) {len(v)}人"
                for k, v in hot if len(v) >= 2)
            if hot_txt:
                block.append(f"🔥 热点区域（5km 网格）: {hot_txt}")
            blocks.append("\n".join(block))
            for q in players[: max(50, self._cfg_int("max_map_points", 400))]:
                all_points.append({"axisX": float(q.get("x") or 0),
                                   "axisZ": float(q.get("z") or 0), "me": 0})

        text = "🛣 CNTIR 实时路况\n\n" + "\n\n".join(blocks)
        if len(all_points) >= 2:
            xs = [p["axisX"] for p in all_points]
            zs = [p["axisZ"] for p in all_points]
            cx = (min(xs) + max(xs)) / 2
            cz = (min(zs) + max(zs)) / 2
            url = await self._render_map(all_points[: self._cfg_int("max_map_points", 400)],
                                         (cx, cz), zoom=self._fit_zoom(xs, zs))
            if url:
                try:
                    yield event.chain_result([Image.fromURL(url), Plain(text)])
                    return
                except Exception as e:
                    logger.warning(f"路况图片发送失败: {e}")
        yield event.plain_result(text)

    async def _event_default_items(self) -> Tuple[List[Dict[str, Any]], str]:
        """「活动」默认视图的活动列表：优先「即将开始」，为空时退到「24小时内」。"""
        data = await self.api_events("upcoming", 1)
        tab = "upcoming"
        if not data.get("items"):
            data = await self.api_events("today", 1)
            tab = "today"
        items = [it for it in (data.get("items") or []) if isinstance(it, dict)]
        return items, tab

    async def do_events(self, event: AstrMessageEvent, arg: str = ""):
        arg = (arg or "").strip()
        tab = "upcoming"
        page = 1
        if arg:
            matched = False
            for t_name, words in EVENT_TAB_KEYWORDS.items():
                if any(w in arg.lower() for w in words):
                    tab = t_name
                    matched = True
                    break
            if not matched and arg.isdigit():
                page = max(1, int(arg))
        try:
            data = await self.api_events(tab, page)
            # 平台「即将开始」为空时兜底查「今日」，避免默认视图一片空白
            if not data.get("items") and tab == "upcoming":
                data = await self.api_events("today", page)
                tab = "today"
        except CntirApiError as e:
            yield event.plain_result(f"查询活动失败：{e.message}")
            return
        items = data.get("items", [])
        total = _fmt_int(data.get("total"))
        tab_cn = {"today": "24小时内", "upcoming": "即将开始", "featured": "精选", "past": "已结束"}.get(tab, tab)
        if not items:
            yield event.plain_result(f"🎉 暂无{tab_cn}的活动")
            return
        blocks = [f"🎉 CNTIR 活动（{tab_cn}，共 {total} 场）"]
        for i, ev in enumerate(items[:5], start=1):
            marks = ""
            if ev.get("featured"):
                marks += "[精选] "
            if ev.get("official"):
                marks += "[官方] "
            kind = KIND_MAP.get(ev.get("kind"), ev.get("kind", ""))
            head = f"{i}. {marks}{_clean(ev.get('title'))} · {kind}"
            body = [
                f"🕐 开始: {_ts_to_bj(ev.get('start_at'))}"
                + (f"（集合 {_ts_to_bj(ev.get('meetup_at'))}）" if ev.get("meetup_at") else ""),
                f"🚚 路线: {_clean(ev.get('depart_city'))} → {_clean(ev.get('arrive_city'))}"
                + (f"（{_fmt_float(ev.get('distance_km'))}km）" if ev.get("distance_km") else "")
                + (f" · 服务器 {_clean(ev.get('server_code'))}" if ev.get("server_code") else ""),
                f"👥 报名: {ev.get('going', 0)} 人参加 / {ev.get('maybe', 0)} 人可能 · {ev.get('vtcs', 0)} 个车队",
            ]
            organizer = ev.get("organizer") or {}
            if isinstance(organizer, dict) and organizer.get("name"):
                body.append(f"👤 组织者: {_clean(organizer.get('name'))}")
            blocks.append(head + "\n" + "\n".join(body))
        footer = ""
        if not arg:
            footer = f"\n查看详情：{self._p}活动详情 <序号>（如：{self._p}活动详情 1）"
        if total > page * 18:
            footer += f"\n（第 {page} 页，回复「{self._p}活动 {page + 1}」查看更多）"
        yield event.plain_result("\n\n".join(blocks) + footer)

    async def do_news(self, event: AstrMessageEvent, arg: str = ""):
        arg = (arg or "").strip()
        page = 1
        if arg:
            if not arg.isdigit():
                yield event.plain_result(f"页码请输入数字，格式：{self._p}新闻 [页码]")
                return
            page = max(1, int(arg))
        try:
            data = await self.api_news(page, per=5)
        except CntirApiError as e:
            yield event.plain_result(f"查询新闻失败：{e.message}")
            return
        items = data.get("items", [])
        total = _fmt_int(data.get("total"))
        if not items:
            yield event.plain_result("📰 暂无新闻")
            return
        blocks = [f"📰 CNTIR 新闻（第 {page} 页，共 {total} 篇）"]
        for i, n in enumerate(items, start=1):
            cat = CATEGORY_MAP.get(n.get("category"), n.get("category", ""))
            pin = "📌" if n.get("pinned") else ""
            title_line = f"{i}.（新闻ID {n.get('id')}）{pin}[{cat}] {_clean(n.get('title'))}"
            meta_parts = [_ts_to_bj(n.get("created"), with_time=False)]
            author = n.get("author") or {}
            if isinstance(author, dict) and author.get("name"):
                meta_parts.append(f"作者 {_clean(author.get('name'))}")
            body = ["🕐 " + " · ".join(meta_parts)]
            summary = _clean(n.get("summary"))
            if summary:
                body.append(f"📝 {summary[:80]}" + ("…" if len(summary) > 80 else ""))
            blocks.append(title_line + "\n" + "\n".join(body))
        footer = f"\n查看全文：{self._p}新闻详情 <新闻ID>"
        if total > page * 5:
            footer += f"\n（回复「{self._p}新闻 {page + 1}」查看更多）"
        yield event.plain_result("\n\n".join(blocks) + footer)

    async def do_announcements(self, event: AstrMessageEvent, arg: str = ""):
        try:
            anns = await self.api_announcements()
        except CntirApiError as e:
            yield event.plain_result(f"查询公告失败：{e.message}")
            return
        if not anns:
            yield event.plain_result("🔔 当前没有生效中的公告")
            return
        blocks = ["🔔 CNTIR 公告"]
        for a in anns:
            level = LEVEL_MAP.get(a.get("level"), "ℹ️")
            lines = [f"{level} {_clean(a.get('body'))}",
                     f"🕐 {_ts_to_bj(a.get('created'))} · 详情见 {_clean(a.get('url')) or '官网'}"]
            blocks.append("\n".join(lines))
        yield event.plain_result("\n\n".join(blocks))

    async def do_vtc(self, event: AstrMessageEvent, arg: str = ""):
        arg = (arg or "").strip()
        try:
            if arg.isdigit():
                # 详情
                vtc = await self.api_vtc_detail(int(arg))
                lines = [f"🚚 {_clean(vtc.get('name'))}（{_clean(vtc.get('tag'))}）"
                         + (f" · {VTC_STATUS_MAP.get(vtc.get('status'), '')}" if vtc.get("status") else "")]
                slogan = _clean(vtc.get("slogan"))
                if slogan:
                    lines.append(f"💬 {slogan}")
                # 注意：列表接口 members 是人数(int)，详情接口实测为成员数组(list)，二者都要兼容
                members = vtc.get("members")
                if isinstance(members, list):
                    lines.append(f"👥 成员: {len(members)} 人")
                    staff: List[str] = []
                    for mb in members:
                        if not isinstance(mb, dict):
                            continue
                        pos = mb.get("position")
                        pos_name = _clean(pos.get("name")) if isinstance(pos, dict) else ""
                        if pos_name:
                            staff.append(f"{_clean(mb.get('name'))}（{pos_name}）")
                        if len(staff) >= 3:
                            break
                    if staff:
                        lines.append("🛠 管理: " + "、".join(staff))
                elif isinstance(members, int):
                    lines.append(f"👥 成员: {members} 人")
                stats = vtc.get("stats") or {}
                if stats and any(_fmt_int(stats.get(k)) for k in
                                 ("deliveries", "distance_km", "month_deliveries", "month_km")):
                    lines.append(
                        f"📦 累计运单 {stats.get('deliveries', 0)} · 里程 {stats.get('distance_km', 0)} km · "
                        f"本月 {stats.get('month_deliveries', 0)} 单 / {stats.get('month_km', 0)} km")
                links = []
                for key, label in (("website", "官网"), ("qq", "QQ群"), ("discord", "Discord")):
                    v = _clean(vtc.get(key))
                    if v:
                        links.append(f"{label}: {v}")
                if links:
                    lines.append("🔗 " + " · ".join(links))
                owner = vtc.get("owner") or {}
                if isinstance(owner, dict) and owner.get("name"):
                    lines.append(f"👤 创始人: {_clean(owner.get('name'))}")
                recruiting = vtc.get("recruiting")
                if recruiting is not None:
                    lines.append(f"🎯 招募: {'✅ 招募中' if recruiting else '❌ 未招募'}")
                lines.append(f"📅 创建时间: {_ts_to_bj(vtc.get('created'), with_time=False)}")
                yield event.plain_result("\n".join(lines))
                return
            # 列表 / 搜索
            data = await self.api_vtc_list(q=arg, page=1)
            items = data.get("items", [])
            total = _fmt_int(data.get("total"))
            if not items:
                yield event.plain_result(f"🚚 没有找到车队" + (f"「{arg}」" if arg else ""))
                return
            head = f"🚚 CNTIR 车队" + (f"搜索「{arg}」" if arg else "列表") + f"（共 {total} 个）"
            blocks = [head]
            for i, v in enumerate(items[:8], start=1):
                status = VTC_STATUS_MAP.get(v.get("status"), "")
                recruit = "🟢招募" if v.get("recruiting") else "⚪"
                blocks.append(
                    f"{i}. {v.get('id')} · {_clean(v.get('name'))}（{_clean(v.get('tag'))}）"
                    f" · {v.get('members', 0)}人 · {status} · {recruit}")
            blocks.append(f"查看详情：{self._p}车队 <车队ID>")
            yield event.plain_result("\n".join(blocks))
        except CntirApiError as e:
            if e.status == 404:
                yield event.plain_result("车队不存在，请检查 ID")
            else:
                yield event.plain_result(f"查询车队失败：{e.message}")

    async def do_news_detail(self, event: AstrMessageEvent, arg: str = ""):
        arg = (arg or "").strip()
        if not arg:
            # 不填 ID 时自动看最新一篇，省去记 ID
            try:
                items = (await self.api_news(1, 5)).get("items") or []
            except CntirApiError as e:
                yield event.plain_result(f"查询新闻详情失败：{e.message}")
                return
            if not items:
                yield event.plain_result("📰 暂无新闻")
                return
            arg = str(items[0].get("id"))
        if not arg.isdigit():
            yield event.plain_result(f"用法：{self._p}新闻详情 [新闻ID]（不填为最新一篇）")
            return
        try:
            n = await self.api_news_detail(int(arg))
        except CntirApiError as e:
            if e.status == 404:
                yield event.plain_result(f"新闻 {arg} 不存在")
            else:
                yield event.plain_result(f"查询新闻详情失败：{e.message}")
            return
        cat = CATEGORY_MAP.get(n.get("category"), n.get("category", ""))
        lines = [f"📰 {_clean(n.get('title'))}"]
        meta = [f"[{cat}]" if cat else "", "📌置顶" if n.get("pinned") else "",
                _ts_to_bj(n.get("created"))]
        author = n.get("author") or {}
        if isinstance(author, dict) and author.get("name"):
            meta.append(f"作者 {_clean(author.get('name'))}")
        lines.append(" · ".join([m for m in meta if m]))
        body = _clean(n.get("body"))
        if body:
            lines.append("────────────")
            lines.append(body[:800] + ("…（已截断）" if len(body) > 800 else ""))
        prev_n, next_n = n.get("prev") or {}, n.get("next") or {}
        nav = []
        if isinstance(prev_n, dict) and prev_n.get("id"):
            nav.append(f"上一篇 {prev_n.get('id')}: {_clean(prev_n.get('title'))[:16]}")
        if isinstance(next_n, dict) and next_n.get("id"):
            nav.append(f"下一篇 {next_n.get('id')}: {_clean(next_n.get('title'))[:16]}")
        if nav:
            lines.append("────────────\n" + "\n".join(nav))
        lines.append(f"🔗 查看全文：{PLUGIN_PAGE}/news/{arg}")
        yield event.plain_result("\n".join(lines))

    async def do_event_detail(self, event: AstrMessageEvent, arg: str = ""):
        arg = (arg or "").strip()
        # 编号含义 = 「活动」默认列表里的第几个（不是平台活动 ID），避免输入 1 就翻出库里第一条历史活动
        try:
            items, _tab = await self._event_default_items()
        except CntirApiError as e:
            yield event.plain_result(f"查询活动详情失败：{e.message}")
            return
        shown = items[:5]
        if not shown:
            yield event.plain_result("🎉 当前没有可查看的活动")
            return
        if arg and not arg.isdigit():
            yield event.plain_result(
                f"用法：{self._p}活动详情 [序号]（1-{len(shown)}，不填看第 1 场）\n"
                f"序号对应「{self._p}活动」列表里的编号")
            return
        idx = int(arg) if arg else 1
        if idx < 1 or idx > len(shown):
            yield event.plain_result(
                f"序号超出范围：当前列表只有 {len(shown)} 场（1-{len(shown)}），"
                f"可先发送「{self._p}活动」查看列表")
            return
        try:
            ev = await self.api_event_detail(_fmt_int(shown[idx - 1].get("id")))
        except CntirApiError as e:
            if e.status == 404:
                yield event.plain_result(f"活动 {idx} 已不存在（可能已被删除）")
            else:
                yield event.plain_result(f"查询活动详情失败：{e.message}")
            return
        title = _clean(ev.get("title")) or f"第 {idx} 场活动"
        when = _ts_to_bj(ev.get("start_at"))
        # 历史活动（已取消 / 已结束）不再输出任何详情内容
        if ev.get("cancelled"):
            yield event.plain_result(f"🚫 「{title}」已取消（原定 {when}），不再提供详情")
            return
        if _event_is_finished(ev):
            yield event.plain_result(f"🚫 「{title}」已结束（开始于 {when}），不再提供详情")
            return
        marks = []
        if ev.get("featured"):
            marks.append("[精选]")
        if ev.get("official"):
            marks.append("[官方]")
        kind = KIND_MAP.get(ev.get("kind"), ev.get("kind", ""))
        lines = [f"🎉 {(''.join(marks) + ' ' if marks else '')}{_clean(ev.get('title'))} · {kind}"]
        lines.append(f"🕐 开始: {_ts_to_bj(ev.get('start_at'))}"
                     + (f"（集合 {_ts_to_bj(ev.get('meetup_at'))}）" if ev.get("meetup_at") else "")
                     + (f" → 结束 {_ts_to_bj(ev.get('end_at'))}" if ev.get("end_at") else ""))
        def _place(city_key: str, place_key: str) -> str:
            city = _clean(ev.get(city_key))
            place = _clean(ev.get(place_key))
            # 平台常把两地字段都填成「待定」，此时不重复拼接
            if place and place != city and city not in ("待定", "TBD"):
                return f"{city}·{place}"
            return city or place

        dep = _place("depart_city", "depart_place")
        arr = _place("arrive_city", "arrive_place")
        srv = f" · 服务器 {_clean(ev.get('server_code'))}" if ev.get("server_code") else ""
        lines.append(f"🚚 路线: {dep} → {arr}"
                     + (f"（{_fmt_float(ev.get('distance_km'))}km）" if ev.get("distance_km") else "") + srv)
        lines.append(f"👥 报名: {ev.get('going', 0)} 参加 / {ev.get('maybe', 0)} 可能 · {ev.get('vtcs', 0)} 车队"
                     + (f" · 语言 {ev.get('language')}" if ev.get("language") else ""))
        organizer = ev.get("organizer") or {}
        if isinstance(organizer, dict) and organizer.get("name"):
            lines.append(f"👤 组织者: {_clean(organizer.get('name'))}")
        vtc = ev.get("vtc")
        if isinstance(vtc, dict) and vtc.get("name"):
            lines.append(f"🚚 主办车队: {vtc.get('name')}")
        dlcs = [_clean(d) for d in (ev.get("dlcs") or []) if _clean(d) and _clean(d) != "无"]
        if dlcs:
            lines.append(f"🗺 需要 DLC: {'、'.join(dlcs[:8])}")
        desc = _clean(ev.get("description"))
        if desc:
            lines.append("────────────\n📝 " + desc[:600] + ("…" if len(desc) > 600 else ""))
        rules = _clean(ev.get("rules"))
        if rules:
            lines.append("📋 规则: " + rules[:400] + ("…" if len(rules) > 400 else ""))
        attendees = ev.get("attendees") or []
        if attendees:
            shown = []
            for a in attendees[:8]:
                status = _clean(a.get("status")) if isinstance(a, dict) else ""
                nm = _clean(a.get("name")) if isinstance(a, dict) else str(a)
                shown.append(f"{nm}{'(' + status + ')' if status else ''}")
            lines.append(f"🙋 报名玩家（{len(attendees)}）: " + "、".join(shown))
        attending_vtcs = ev.get("attending_vtcs") or []
        if attending_vtcs:
            names = [_clean(v.get("name")) if isinstance(v, dict) else str(v)
                     for v in attending_vtcs[:6]]
            lines.append(f"🚚 参与车队（{len(attending_vtcs)}）: " + "、".join(names))
        for key, label in (("route_map", "🗺 路线图"), ("voice", "🎙 语音"), ("link", "🔗 链接")):
            v = _clean(ev.get(key))
            if v:
                lines.append(f"{label}: {v}")
        yield event.plain_result("\n".join(lines))

    async def do_version(self, event: AstrMessageEvent, arg: str = ""):
        try:
            v = await self.api_version()
        except CntirApiError as e:
            yield event.plain_result(f"查询版本失败：{e.message}")
            return
        ets2 = v.get("ets2") or []
        notes = v.get("notes") or []
        text = (
            f"⚙️ CNTIR 版本信息\n"
            f"🧩 联机插件: {v.get('cntir') or '未知'}\n"
            f"🚀 启动器: {v.get('launcher') or '未知'}\n"
            f"🎮 支持游戏版本(ETS2): {'、'.join(map(str, ets2)) or '未知'}"
        )
        if notes:
            text += "\n📝 " + "\n   ".join(_clean(n) for n in notes[:5])
        yield event.plain_result(text)

    # ================================================================
    # 路由层：单监听器 + 正则分发
    # ================================================================
    def _build_routes(self) -> None:
        """构建正则路由表。enable_no_prefix 决定是否允许省略 cnt 前缀。"""
        no_prefix = self._cfg_bool("enable_no_prefix", True)
        pfx = r"(?:cnt)?" if no_prefix else r"cnt"

        def rx(body: str, arg: str = "") -> "re.Pattern[str]":
            # 必须把 body 整体包成非捕获组，避免 body 内部的 | 把 ^/$ 锚点切到外层
            pattern = rf"^(?:/)?{pfx}\s*(?:{body}){arg}\s*$"
            return re.compile(pattern)

        self._routes: List[Tuple["re.Pattern[str]", str]] = [
            (rx(r"菜单|帮助|help|说明"), "do_menu"),
            (rx(r"绑定", r"\s*(\S*)"), "do_bind"),
            (rx(r"解绑", r"\s*(\S*)"), "do_unbind"),
            (rx(r"切换|主账号", r"\s*(\S*)"), "do_switch"),
            (rx(r"我的|绑定信息"), "do_my"),
            (rx(r"查询|查", r"\s*(\S*)"), "do_query"),
            (rx(r"定位", r"\s*(\S*)"), "do_locate"),
            (rx(r"路况", r"\s*(\S*)"), "do_traffic"),
            # 「历史」「统计」均已并入「服务器」：发「服务器」即输出图片（状态+统计+在线历史）
            (rx(r"服务器"), "do_servers"),
            (rx(r"车队", r"\s*(.*)"), "do_vtc"),
            # 详情命令必须排在列表命令之前，避免被「新闻/活动」的页码参数抢匹配
            (rx(r"新闻详情|新闻内容", r"\s*(\S*)"), "do_news_detail"),
            (rx(r"活动详情", r"\s*(\S*)"), "do_event_detail"),
            (rx(r"活动", r"\s*(\S*)"), "do_events"),
            (rx(r"新闻", r"\s*(\S*)"), "do_news"),
            (rx(r"公告"), "do_announcements"),
            (rx(r"版本"), "do_version"),
        ]

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent, *args, **kwargs):
        """统一入口：所有命令由此正则分发。"""
        msg = (getattr(event, "message_str", "") or "").strip()
        if not msg:
            return
        for pattern, handler_name in self._routes:
            m = pattern.match(msg)
            if not m:
                continue
            arg = m.group(1).strip() if m.groups() else ""
            handler = getattr(self, handler_name)
            try:
                async for result in handler(event, arg):
                    yield self._keep_text(result)
            except CntirApiError as e:
                yield self._keep_text(event.plain_result(f"出错了：{e.message}"))
            except Exception as e:
                logger.error(f"命令处理异常 [{handler_name}]: {e}", exc_info=True)
                yield self._keep_text(event.plain_result("处理命令时出现内部错误，请稍后重试"))
            return

    # ----------------------------- AstrBot 指令桩（仅用于指令列表展示） -----------------------------
    @filter.command("cnt绑定")
    async def cmd_bind(self, event: AstrMessageEvent, account_id: str = ""):
        """绑定 CNTIR 账号，格式：cnt绑定 <账号ID>"""
        return

    @filter.command("cnt解绑")
    async def cmd_unbind(self, event: AstrMessageEvent):
        """解除当前账号的 CNTIR 绑定"""
        return

    @filter.command("cnt切换")
    async def cmd_switch(self, event: AstrMessageEvent, target: str = ""):
        """切换主账号，格式：cnt切换 <序号/账号ID>"""
        return

    @filter.command("cnt我的")
    async def cmd_my(self, event: AstrMessageEvent):
        """查看我的全部 CNTIR 绑定"""
        return

    @filter.command("cnt查询")
    async def cmd_query(self, event: AstrMessageEvent, account_id: str = ""):
        """查询玩家资料（留空查自己，支持 @ 群友）"""
        return

    @filter.command("cnt定位")
    async def cmd_locate(self, event: AstrMessageEvent, account_id: str = ""):
        """查询玩家实时位置并渲染地图（留空查自己，支持 @ 群友）"""
        return

    @filter.command("cnt路况")
    async def cmd_traffic(self, event: AstrMessageEvent, server: str = ""):
        """查看服务器在线玩家分布与行驶统计，格式：cnt路况 [服务器编号]"""
        return

    @filter.command("cnt服务器")
    async def cmd_servers(self, event: AstrMessageEvent, args: str = ""):
        """查看服务器状态、平台统计与在线历史（图片卡片）"""
        return

    @filter.command("cnt车队")
    async def cmd_vtc(self, event: AstrMessageEvent, keyword: str = ""):
        """车队列表/搜索/详情，格式：cnt车队 [ID 或 关键词]"""
        return

    @filter.command("cnt活动")
    async def cmd_events(self, event: AstrMessageEvent, tab_or_page: str = ""):
        """活动列表，格式：cnt活动 [今日/精选/结束/页码]"""
        return

    @filter.command("cnt新闻")
    async def cmd_news(self, event: AstrMessageEvent, page: str = ""):
        """新闻列表，格式：cnt新闻 [页码]"""
        return

    @filter.command("cnt公告")
    async def cmd_announcements(self, event: AstrMessageEvent):
        """查看当前生效的全站公告"""
        return

    @filter.command("cnt版本")
    async def cmd_version(self, event: AstrMessageEvent):
        """查询 CNTIR 插件与启动器版本"""
        return

    @filter.command("cnt新闻详情")
    async def cmd_news_detail(self, event: AstrMessageEvent, news_id: str = ""):
        """查看新闻正文，格式：cnt新闻详情 <ID>"""
        return

    @filter.command("cnt活动详情")
    async def cmd_event_detail(self, event: AstrMessageEvent, event_id: str = ""):
        """查看活动详情（规则/路线/报名），格式：cnt活动详情 <列表序号>"""
        return

    @filter.command("cnt菜单")
    async def cmd_menu(self, event: AstrMessageEvent):
        """显示本插件全部指令"""
        return
