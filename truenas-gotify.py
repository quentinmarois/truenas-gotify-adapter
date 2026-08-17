#!/usr/bin/env python3
import asyncio
import html
import json
import logging
import os
import re
import sys
from collections import deque
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse, urlunparse

from aiohttp import ClientSession, ClientTimeout, WSMsgType, web


LOG = logging.getLogger("truenas-gotify")

DEFAULT_PRIORITIES = {
    "INFO": 1,
    "NOTICE": 2,
    "WARNING": 4,
    "ERROR": 6,
    "CRITICAL": 8,
    "ALERT": 9,
    "EMERGENCY": 10,
}


class RpcError(RuntimeError):
    def __init__(self, method: str, error: Any):
        super().__init__(f"TrueNAS RPC call {method!r} failed: {error}")
        self.method = method
        self.error = error


def env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() not in {"0", "false", "no", "n", "off"}


def env_int(name: str, default: int) -> int:
    value = os.environ.get(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer, got {value!r}") from exc


def normalize_gotify_url(url: str) -> str:
    parsed = urlparse(url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("GOTIFY_URL must be an http:// or https:// URL")

    path = parsed.path.rstrip("/")
    if not path.endswith("/message"):
        path = f"{path}/message" if path else "/message"

    return urlunparse(parsed._replace(path=path))


def normalize_truenas_ws_url(url: str) -> str:
    raw = url.strip()
    if "://" not in raw:
        raw = f"https://{raw}"

    parsed = urlparse(raw)
    scheme_map = {
        "http": "ws",
        "https": "wss",
        "ws": "ws",
        "wss": "wss",
    }
    if parsed.scheme not in scheme_map or not parsed.netloc:
        raise ValueError("TRUENAS_URL must be a host or an http(s)/ws(s) URL")

    path = parsed.path.rstrip("/")
    if not path:
        path = "/api/current"
    elif not path.endswith("/api/current"):
        path = f"{path}/api/current"

    return urlunparse(parsed._replace(scheme=scheme_map[parsed.scheme], path=path))


def html_to_text(value: str | None) -> str:
    if not value:
        return ""
    text = re.sub(r"(?i)<br\s*/?>", "\n", value)
    text = re.sub(r"(?i)</li\s*>", "\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    return "\n".join(line.strip() for line in text.splitlines() if line.strip())


def alert_key(alert: dict[str, Any]) -> str:
    for field in ("uuid", "id"):
        value = alert.get(field)
        if value is not None:
            return str(value)
    return f"{alert.get('source', '')}:{alert.get('klass', '')}:{alert.get('key', '')}"


@dataclass(frozen=True)
class Config:
    gotify_url: str
    gotify_token: str
    gotify_verify_cert: bool
    truenas_url: str | None
    truenas_api_key: str | None
    truenas_username: str | None
    truenas_verify_cert: bool
    enable_legacy_webhook: bool
    listen_host: str
    port: int
    priority_map: dict[str, int]
    resolved_priority: int
    legacy_priority: int

    @property
    def native_enabled(self) -> bool:
        return bool(self.truenas_url and self.truenas_api_key)


def load_config() -> Config:
    gotify_url = os.environ.get("GOTIFY_URL")
    gotify_token = os.environ.get("GOTIFY_TOKEN")
    if not gotify_url:
        raise ValueError("Set GOTIFY_URL=http[s]://{host}:{port}/")
    if not gotify_token:
        raise ValueError("Set GOTIFY_TOKEN={token}")

    truenas_url = os.environ.get("TRUENAS_URL")
    truenas_api_key = os.environ.get("TRUENAS_API_KEY")
    truenas_username = os.environ.get("TRUENAS_USERNAME")

    if bool(truenas_url) != bool(truenas_api_key):
        raise ValueError("TRUENAS_URL and TRUENAS_API_KEY must be set together")

    native_enabled = bool(truenas_url and truenas_api_key)
    legacy_default = not native_enabled

    # VERIFY_CERT is retained as the old Gotify-specific setting.
    old_verify_cert = env_bool("VERIFY_CERT", True)
    gotify_verify_cert = env_bool("GOTIFY_VERIFY_CERT", old_verify_cert)

    priority_map = {
        level: env_int(f"GOTIFY_PRIORITY_{level}", priority)
        for level, priority in DEFAULT_PRIORITIES.items()
    }
    for level, priority in priority_map.items():
        if not 0 <= priority <= 10:
            raise ValueError(f"GOTIFY_PRIORITY_{level} must be between 0 and 10")

    resolved_priority = env_int("GOTIFY_PRIORITY_RESOLVED", 2)
    legacy_priority = env_int("GOTIFY_PRIORITY_LEGACY", 0)
    for name, value in {
        "GOTIFY_PRIORITY_RESOLVED": resolved_priority,
        "GOTIFY_PRIORITY_LEGACY": legacy_priority,
    }.items():
        if not 0 <= value <= 10:
            raise ValueError(f"{name} must be between 0 and 10")

    return Config(
        gotify_url=normalize_gotify_url(gotify_url),
        gotify_token=gotify_token,
        gotify_verify_cert=gotify_verify_cert,
        truenas_url=normalize_truenas_ws_url(truenas_url) if truenas_url else None,
        truenas_api_key=truenas_api_key,
        truenas_username=truenas_username,
        truenas_verify_cert=env_bool("TRUENAS_VERIFY_CERT", True),
        enable_legacy_webhook=env_bool("ENABLE_LEGACY_WEBHOOK", legacy_default),
        listen_host=os.environ.get("LISTEN_HOST", "127.0.0.1"),
        port=env_int("PORT", 31662),
        priority_map=priority_map,
        resolved_priority=resolved_priority,
        legacy_priority=legacy_priority,
    )


async def send_gotify_message(
    session: ClientSession,
    config: Config,
    message: str,
    *,
    title: str | None = None,
    priority: int | None = None,
) -> tuple[int, str]:
    payload: dict[str, Any] = {"message": message}
    if title:
        payload["title"] = title
    if priority is not None:
        payload["priority"] = priority

    ssl_option = None if config.gotify_verify_cert else False
    async with session.post(
        config.gotify_url,
        headers={"X-Gotify-Key": config.gotify_token},
        json=payload,
        ssl=ssl_option,
    ) as response:
        body = await response.text()
        return response.status, body


async def forward_to_gotify(
    session: ClientSession,
    config: Config,
    message: str,
    *,
    title: str,
    priority: int,
) -> None:
    try:
        status, body = await send_gotify_message(
            session, config, message, title=title, priority=priority
        )
    except Exception:
        LOG.exception("Failed to send notification to Gotify")
        return

    if 200 <= status < 300:
        LOG.info("Forwarded Gotify notification: %s (priority %d)", title, priority)
    else:
        LOG.warning("Gotify returned HTTP %d: %s", status, body[:500])


class TrueNASConnection:
    def __init__(self, session: ClientSession, config: Config):
        self.session = session
        self.config = config
        self._rpc_id = 0
        self.pending_notifications: deque[dict[str, Any]] = deque()

    async def call(self, ws: Any, method: str, params: list[Any]) -> Any:
        self._rpc_id += 1
        request_id = self._rpc_id
        await ws.send_json(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": method,
                "params": params,
            }
        )

        while True:
            message = await ws.receive()
            if message.type == WSMsgType.TEXT:
                data = json.loads(message.data)
                if data.get("id") == request_id:
                    if "error" in data:
                        raise RpcError(method, data["error"])
                    return data.get("result")
                if data.get("method") == "collection_update":
                    self.pending_notifications.append(data)
            elif message.type in {WSMsgType.CLOSED, WSMsgType.CLOSE, WSMsgType.CLOSING}:
                raise ConnectionError("TrueNAS WebSocket closed")
            elif message.type == WSMsgType.ERROR:
                raise ConnectionError(f"TrueNAS WebSocket error: {ws.exception()}")

    async def authenticate(self, ws: Any) -> None:
        if self.config.truenas_username:
            login_data = {
                "mechanism": "API_KEY_PLAIN",
                "username": self.config.truenas_username,
                "api_key": self.config.truenas_api_key,
                "login_options": {"user_info": False},
            }
            try:
                result = await self.call(ws, "auth.login_ex", [login_data])
                if isinstance(result, dict) and result.get("response_type") == "SUCCESS":
                    return
                raise RuntimeError(f"TrueNAS authentication failed: {result}")
            except RpcError as exc:
                LOG.warning(
                    "auth.login_ex failed; trying legacy auth.login_with_api_key: %s", exc
                )

        # Compatibility path for deployments where the API-key owner username
        # is not configured. This method is deprecated in TrueNAS 25.10 and is
        # removed in v27, so TRUENAS_USERNAME is strongly recommended.
        result = await self.call(
            ws, "auth.login_with_api_key", [self.config.truenas_api_key]
        )
        if result is not True:
            if self.config.truenas_username:
                raise RuntimeError("TrueNAS API key authentication failed")
            raise RuntimeError(
                "TrueNAS API key authentication failed. Set TRUENAS_USERNAME so "
                "the adapter can use auth.login_ex (required by newer TrueNAS versions)."
            )

    async def list_alerts(self, ws: Any) -> list[dict[str, Any]]:
        result = await self.call(ws, "alert.list", [])
        if not isinstance(result, list):
            raise RuntimeError(f"Unexpected alert.list result: {result!r}")
        return result


class AlertForwarder:
    def __init__(self, session: ClientSession, config: Config, connection: TrueNASConnection):
        self.session = session
        self.config = config
        self.connection = connection
        self.alerts: dict[str, dict[str, Any]] = {}

    def replace_snapshot(self, alerts: list[dict[str, Any]]) -> None:
        self.alerts = {alert_key(alert): alert for alert in alerts}

    def priority(self, alert: dict[str, Any]) -> int:
        return self.config.priority_map.get(str(alert.get("level", "NOTICE")).upper(), 2)

    def message(self, alert: dict[str, Any]) -> str:
        formatted = html_to_text(alert.get("formatted"))
        if formatted:
            return formatted
        text = html_to_text(alert.get("text"))
        if text:
            return text
        return str(alert.get("klass") or "TrueNAS alert")

    def title(self, alert: dict[str, Any], prefix: str = "TrueNAS") -> str:
        level = str(alert.get("level", "NOTICE")).upper()
        klass = alert.get("klass")
        return f"{prefix} · {level}" + (f" · {klass}" if klass else "")

    async def notify_alert(self, alert: dict[str, Any], *, prefix: str = "TrueNAS") -> None:
        await forward_to_gotify(
            self.session,
            self.config,
            self.message(alert),
            title=self.title(alert, prefix),
            priority=self.priority(alert),
        )

    async def notify_resolved(self, alert: dict[str, Any]) -> None:
        klass = alert.get("klass")
        title = "TrueNAS · Resolved" + (f" · {klass}" if klass else "")
        await forward_to_gotify(
            self.session,
            self.config,
            self.message(alert),
            title=title,
            priority=self.config.resolved_priority,
        )

    async def handle_event(self, ws: Any, notification: dict[str, Any]) -> None:
        params = notification.get("params") or {}
        if params.get("collection") != "alert.list":
            return

        event = str(params.get("event") or params.get("msg") or "").upper()
        fields = params.get("fields")

        if event == "ADDED" and isinstance(fields, dict):
            key = alert_key(fields)
            previous = self.alerts.get(key)
            self.alerts[key] = fields
            if previous is None:
                await self.notify_alert(fields)
            return

        if event == "CHANGED" and isinstance(fields, dict):
            key = alert_key(fields)
            previous = self.alerts.get(key)
            self.alerts[key] = fields
            if previous is None:
                await self.notify_alert(fields)
                return

            # TrueNAS can update bookkeeping fields frequently. Only notify on
            # a severity/priority change to avoid repeating the same alert.
            if self.priority(previous) != self.priority(fields):
                await self.notify_alert(fields, prefix="TrueNAS alert changed")
            return

        if event == "REMOVED":
            # The removal event does not include the full alert object. Re-read
            # the current alert list and compare stable UUIDs to identify what
            # actually cleared, including alerts that existed before startup.
            current = await self.connection.list_alerts(ws)
            current_map = {alert_key(alert): alert for alert in current}
            removed = [
                alert for key, alert in self.alerts.items() if key not in current_map
            ]
            self.alerts = current_map
            for alert in removed:
                await self.notify_resolved(alert)


async def run_native_connection(session: ClientSession, config: Config) -> None:
    assert config.truenas_url is not None
    ssl_option = None if config.truenas_verify_cert else False
    connection = TrueNASConnection(session, config)

    LOG.info("Connecting to TrueNAS at %s", config.truenas_url)
    async with session.ws_connect(
        config.truenas_url,
        ssl=ssl_option,
        heartbeat=30,
        autoping=True,
    ) as ws:
        await connection.authenticate(ws)
        LOG.info("Authenticated to TrueNAS")

        forwarder = AlertForwarder(session, config, connection)
        forwarder.replace_snapshot(await connection.list_alerts(ws))
        subscription_id = await connection.call(ws, "core.subscribe", ["alert.list"])
        LOG.info(
            "Subscribed to TrueNAS alert.list (%d existing alerts, subscription %s)",
            len(forwarder.alerts),
            subscription_id,
        )

        while True:
            if connection.pending_notifications:
                notification = connection.pending_notifications.popleft()
                await forwarder.handle_event(ws, notification)
                continue

            message = await ws.receive()
            if message.type == WSMsgType.TEXT:
                data = json.loads(message.data)
                if data.get("method") == "collection_update":
                    await forwarder.handle_event(ws, data)
                elif data.get("method") == "notify_unsubscribed":
                    raise ConnectionError(f"TrueNAS unsubscribed alert.list: {data}")
            elif message.type in {WSMsgType.CLOSED, WSMsgType.CLOSE, WSMsgType.CLOSING}:
                raise ConnectionError("TrueNAS WebSocket closed")
            elif message.type == WSMsgType.ERROR:
                raise ConnectionError(f"TrueNAS WebSocket error: {ws.exception()}")


async def run_native_forever(session: ClientSession, config: Config) -> None:
    delay = 1
    while True:
        try:
            await run_native_connection(session, config)
        except asyncio.CancelledError:
            raise
        except Exception:
            LOG.exception("TrueNAS connection failed; reconnecting in %d seconds", delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60)
        else:
            delay = 1


async def legacy_webhook(request: web.Request) -> web.Response:
    config: Config = request.app["config"]
    session: ClientSession = request.app["session"]

    try:
        content = await request.json()
        raw = content["text"].strip()
    except (KeyError, TypeError, json.JSONDecodeError):
        return web.Response(status=400, text="Expected JSON body with a 'text' field")

    first_line, separator, remainder = raw.partition("\n")
    title = first_line.strip() or "TrueNAS"
    message = remainder.strip() if separator else raw

    LOG.info("Received legacy TrueNAS webhook: %s", title)
    try:
        status, body = await send_gotify_message(
            session,
            config,
            message,
            title=title,
            priority=config.legacy_priority,
        )
    except Exception as exc:
        LOG.exception("Failed forwarding legacy webhook to Gotify")
        return web.Response(status=502, text=str(exc))

    if 200 <= status < 300:
        return web.Response(status=200, text="Forwarded successfully")
    return web.Response(status=status, text=body)


async def start_legacy_webhook(session: ClientSession, config: Config) -> web.AppRunner:
    app = web.Application()
    app["config"] = config
    app["session"] = session
    app.router.add_post("/", legacy_webhook)
    app.router.add_post("/message", legacy_webhook)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, config.listen_host, config.port)
    await site.start()
    LOG.info(
        "Legacy Slack-compatible webhook listening on http://%s:%d",
        config.listen_host,
        config.port,
    )
    return runner


async def async_main(config: Config) -> None:
    timeout = ClientTimeout(total=30)
    async with ClientSession(timeout=timeout) as session:
        runner: web.AppRunner | None = None
        native_task: asyncio.Task[None] | None = None
        try:
            if config.enable_legacy_webhook:
                runner = await start_legacy_webhook(session, config)

            if config.native_enabled:
                native_task = asyncio.create_task(run_native_forever(session, config))

            if native_task is not None:
                await native_task
            elif runner is not None:
                await asyncio.Event().wait()
            else:
                raise RuntimeError(
                    "Nothing to run: configure TRUENAS_URL/TRUENAS_API_KEY or enable the legacy webhook"
                )
        finally:
            if native_task is not None:
                native_task.cancel()
            if runner is not None:
                await runner.cleanup()


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        config = load_config()
        if config.native_enabled and not config.truenas_username:
            LOG.warning(
                "TRUENAS_USERNAME is not set; using deprecated auth.login_with_api_key. "
                "Set it for compatibility with future TrueNAS releases."
            )
        if not config.gotify_verify_cert:
            LOG.warning("Gotify TLS certificate validation is disabled")
        if config.native_enabled and not config.truenas_verify_cert:
            LOG.warning("TrueNAS TLS certificate validation is disabled")
        asyncio.run(async_main(config))
    except (ValueError, RuntimeError) as exc:
        sys.exit(str(exc))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
