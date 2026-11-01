"""7627 独立面板：静态页面 + JSON 接口 + 可选密码。

和官方插件页共用 views/mini 那一份界面与 PanelService，区别只在传输层。
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import secrets
import time
from pathlib import Path

from astrbot.api import logger

try:
    import uvicorn
    from fastapi import FastAPI, Request
    from fastapi.responses import FileResponse, JSONResponse, Response
except Exception:  # pragma: no cover - 依赖缺失时降级
    uvicorn = None

TAG = "[Minichat]"
COOKIE_NAME = "mini_panel_token"
TOKEN_TTL = 12 * 3600
MAX_TOKENS = 32
LOGIN_MAX_FAILS = 5
LOGIN_WINDOW = 300
# 这些 GET 接口需要读 query（其余 GET 一律 fn()）
_QUERY_METHODS = {"probability", "decisions"}


class _SignalSafeServer(uvicorn.Server if uvicorn else object):
    """屏蔽 uvicorn 对 SIGINT/SIGTERM 的接管。

    uvicorn 默认会在 serve() 里抢占这两个信号，导致 systemctl stop 卡住。
    面板只是插件的一部分，信号应当由 AstrBot 自己处理。
    """

    @contextlib.contextmanager
    def capture_signals(self):
        yield


class PanelHttpServer:
    def __init__(self, plugin, service) -> None:
        self.plugin = plugin
        self.service = service
        self.views_dir = Path(plugin.plugin_dir) / "views" / "mini"

        self.host = "127.0.0.1"
        self.port = 7627
        self.enabled = False
        self.running = False
        self.error: str | None = None

        self._server = None
        self._task = None
        self._tokens: dict[str, float] = {}
        self._fails: dict[str, list[float]] = {}
        self._app = self._build_app() if uvicorn else None

    # ------------------------------------------------------------------
    # 状态
    # ------------------------------------------------------------------
    def status(self) -> dict:
        cfg = self.plugin.config.get("web_panel") or {}
        return {
            "enabled": bool(cfg.get("enable", True)),
            "host": self.host,
            "port": self.port,
            "running": self.running,
            "url": f"http://{self.host}:{self.port}/",
            "has_password": bool(str(cfg.get("password") or "")),
            "allow_embed": bool(cfg.get("allow_embed", False)),
            "error": self.error,
            "insecure": self.host not in ("127.0.0.1", "localhost", "::1")
            and not str(cfg.get("password") or ""),
        }

    # ------------------------------------------------------------------
    # 鉴权
    # ------------------------------------------------------------------
    def _password(self) -> str:
        cfg = self.plugin.config.get("web_panel") or {}
        return str(cfg.get("password") or "")

    def _issue_token(self, ip: str) -> str:
        if len(self._tokens) >= MAX_TOKENS:
            oldest = min(self._tokens.items(), key=lambda kv: kv[1])[0]
            self._tokens.pop(oldest, None)
        token = secrets.token_urlsafe(32)
        self._tokens[token] = time.time() + TOKEN_TTL
        return token

    def _valid_token(self, request: Request) -> bool:
        if not self._password():
            return True
        raw = request.cookies.get(COOKIE_NAME) or ""
        if not raw:
            auth = request.headers.get("authorization") or ""
            if auth.lower().startswith("bearer "):
                raw = auth[7:].strip()
        exp = self._tokens.get(raw)
        if exp is None:
            return False
        if exp < time.time():
            self._tokens.pop(raw, None)
            return False
        return True

    def _rate_limited(self, ip: str) -> int:
        now = time.time()
        bucket = [t for t in self._fails.get(ip, []) if now - t < LOGIN_WINDOW]
        self._fails[ip] = bucket
        # 表不能无限长：大量伪造 IP 刷登录会把内存慢慢撑爆，超过上限就丢最早的一批
        if len(self._fails) > 512:
            for key in list(self._fails)[: len(self._fails) - 512]:
                self._fails.pop(key, None)
        if len(bucket) >= LOGIN_MAX_FAILS:
            return int(LOGIN_WINDOW - (now - bucket[0]))
        return 0

    # ------------------------------------------------------------------
    # FastAPI
    # ------------------------------------------------------------------
    def _build_app(self):
        app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, title="主动私聊Mini")

        @app.middleware("http")
        async def security_headers(request: Request, call_next):
            response = await call_next(request)
            response.headers["X-Content-Type-Options"] = "nosniff"
            response.headers["Referrer-Policy"] = "no-referrer"
            response.headers["Cache-Control"] = "no-store"
            allow_embed = bool((self.plugin.config.get("web_panel") or {}).get("allow_embed"))
            if allow_embed:
                response.headers["Content-Security-Policy"] = "object-src 'none'; base-uri 'self'"
            else:
                response.headers["Content-Security-Policy"] = (
                    "frame-ancestors 'none'; object-src 'none'; base-uri 'self'"
                )
            return response

        @app.get("/api/health")
        async def health():
            return {"status": "ok", "data": {"ok": True, "ts": time.time()}}

        @app.post("/api/login")
        async def login(request: Request):
            ip = request.client.host if request.client else "?"
            wait = self._rate_limited(ip)
            if wait > 0:
                return JSONResponse(
                    {"status": "error", "message": f"E_RATE_LIMIT: 请 {wait} 秒后再试"},
                    status_code=429,
                    headers={"Retry-After": str(wait)},
                )
            body = await self._json(request)
            given = str((body or {}).get("password") or "")
            expected = self._password()
            if not expected:
                return {"status": "ok", "data": {"token": "", "required": False}}
            # compare_digest 的 str 参数只支持 ASCII，中文密码会抛 TypeError → 500。
            # 统一按 UTF-8 字节比较，保持常量时间语义。
            if not hmac.compare_digest(given.encode("utf-8"), expected.encode("utf-8")):
                self._fails.setdefault(ip, []).append(time.time())
                return JSONResponse(
                    {"status": "error", "message": "E_AUTH: 密码不正确"}, status_code=401
                )
            token = self._issue_token(ip)
            self._fails.pop(ip, None)
            resp = JSONResponse(
                {"status": "ok", "data": {"token": token, "expires_in": TOKEN_TTL}}
            )
            resp.set_cookie(
                COOKIE_NAME, token, max_age=TOKEN_TTL, httponly=True, samesite="lax", path="/"
            )
            return resp

        @app.post("/api/logout")
        async def logout(request: Request):
            token = request.cookies.get(COOKIE_NAME) or ""
            self._tokens.pop(token, None)
            resp = JSONResponse({"status": "ok", "data": {"ok": True}})
            resp.delete_cookie(COOKIE_NAME, path="/")
            return resp

        @app.get("/api/{name:path}")
        async def api_get(name: str, request: Request):
            return await self._dispatch(name, request, "GET")

        @app.post("/api/{name:path}")
        async def api_post(name: str, request: Request):
            return await self._dispatch(name, request, "POST")

        @app.get("/")
        async def index():
            return self._file("index.html")

        @app.get("/{asset:path}")
        async def asset(asset: str):
            return self._file(asset)

        return app

    @staticmethod
    async def _json(request: Request):
        try:
            return await request.json()
        except Exception:
            return {}

    def _file(self, rel: str):
        target = (self.views_dir / rel).resolve()
        try:
            target.relative_to(self.views_dir.resolve())
        except ValueError:
            return Response(status_code=404)
        if not target.is_file():
            return Response(status_code=404)
        return FileResponse(str(target))

    async def _dispatch(self, name: str, request: Request, method: str):
        name = name.strip("/")
        mapping = {
            "status": ("status", "GET"),
            "tasks": ("tasks", "GET"),
            "decisions": ("decisions", "GET"),
            "config": ("config_get", "GET"),
            "config/save": ("config_save", "POST"),
            "reload": ("reload", "POST"),
            "reload/state": ("reload_state", "GET"),
            "preview": ("preview", "POST"),
            "send": ("send", "POST"),
            "fire": ("fire", "POST"),
            "reschedule": ("reschedule", "POST"),
            "tasks/action": ("task_action", "POST"),
            "probability": ("probability", "GET"),
            "probability/preview": ("probability_preview", "POST"),
            "schema": ("schema_get", "GET"),
            "debug/classify": ("debug_classify", "POST"),
            "rules": ("rules_get", "GET"),
            "rules/save": ("rules_save", "POST"),
            "overrides": ("overrides_get", "GET"),
            "overrides/save": ("overrides_save", "POST"),
            "panel/restart": ("panel_restart", "POST"),
        }
        entry = mapping.get(name)
        if entry is None:
            return JSONResponse(
                {"status": "error", "message": f"E_NOT_FOUND: 未知接口 {name}"}, status_code=404
            )
        handler_name, expect = entry
        if expect != method:
            return JSONResponse(
                {"status": "error", "message": "E_METHOD: 方法不允许"}, status_code=405
            )
        if not self._valid_token(request):
            return JSONResponse(
                {"status": "error", "message": "E_AUTH: 需要登录"}, status_code=401
            )

        try:
            fn = getattr(self.service, handler_name)
            if method == "GET":
                query = dict(request.query_params)
                data = fn(query) if handler_name in _QUERY_METHODS else fn()
            else:
                body = await self._json(request)
                if not isinstance(body, dict):
                    raise ValueError("请求体必须是对象")
                data = fn(body)
                if asyncio.iscoroutine(data):
                    data = await data
            return {"status": "ok", "data": data}
        except Exception as e:
            code = getattr(e, "code", "E_INTERNAL")
            message = getattr(e, "message", str(e))
            status = {
                "E_BAD_REQUEST": 400,
                "E_AUTH": 401,
                "E_NOT_FOUND": 404,
                "E_BUSY": 409,
                "E_PLUGIN_OFF": 409,
                "E_CONFIG_SAVE": 500,
                "E_RELOAD_FAIL": 500,
                "E_UPSTREAM": 502,
            }.get(code, 500)
            if code == "E_INTERNAL":
                logger.exception(f"{TAG} 面板接口 {name} 异常")
            return JSONResponse(
                {"status": "error", "message": f"{code}: {message}"}, status_code=status
            )

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    async def start(self, host: str | None = None, port: int | None = None) -> None:
        if uvicorn is None:
            self.error = "未安装 uvicorn / fastapi，独立面板不可用"
            logger.error(f"{TAG} {self.error}")
            return

        cfg = self.plugin.config.get("web_panel") or {}
        if not bool(cfg.get("enable", True)):
            self.enabled = False
            self.error = None
            logger.info(f"{TAG} 独立面板未启用（web_panel.enable=false）")
            return

        self.enabled = True
        self.host = host or str(cfg.get("host") or "127.0.0.1")
        self.port = int(port or cfg.get("port") or 7627)
        self.error = None

        config = uvicorn.Config(
            self._app,
            host=self.host,
            port=self.port,
            log_level="warning",
            access_log=False,
            lifespan="off",
            timeout_graceful_shutdown=3,
        )
        self._server = _SignalSafeServer(config)
        self._task = asyncio.create_task(self._serve())

        for _ in range(125):
            if getattr(self._server, "started", False):
                break
            if self._task.done():
                break
            await asyncio.sleep(0.04)

        if self._task.done():
            exc = None
            try:
                exc = self._task.exception()
            except Exception:
                exc = None
            self.error = f"启动失败: {exc}" if exc else "启动失败：端口可能被占用"
            self.running = False
            logger.error(f"{TAG} 面板启动失败 host={self.host} port={self.port}: {self.error}")
            return

        self.running = True
        logger.info(f"{TAG} 面板已启动 http://{self.host}:{self.port}/")

    async def _serve(self) -> None:
        try:
            await self._server.serve()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self.error = repr(e)
            logger.exception(f"{TAG} 面板服务异常退出")

    async def stop(self) -> None:
        self.running = False
        self._tokens.clear()
        if self._server is None or self._task is None:
            return
        try:
            self._server.should_exit = True
            await asyncio.wait_for(self._task, timeout=2.0)
        except Exception:
            with contextlib.suppress(Exception):
                self._server.force_exit = True
            self._task.cancel()
            with contextlib.suppress(Exception):
                await self._task
        finally:
            self._server = None
            self._task = None

    async def restart(self, host: str | None = None, port: int | None = None) -> dict:
        await self.stop()
        await asyncio.sleep(0.2)
        await self.start(host=host, port=port)
        return {"running": self.running, "host": self.host, "port": self.port, "error": self.error}
