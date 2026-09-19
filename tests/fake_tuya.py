"""A fake Tuya OpenAPI + a simulated sleepy BLE lock behind a gateway.

The cloud answers a write with success as soon as it has *queued* it; the
lock applies it `apply_delay` seconds later — or never, if `drop_writes`.
"""
from __future__ import annotations

import asyncio
import json
from collections import Counter
from typing import Any


class _Resp:
    hang = False

    def __init__(self, payload: dict, status: int = 200) -> None:
        self._payload, self.status = payload, status

    async def text(self) -> str:
        return json.dumps(self._payload)

    async def json(self) -> dict:
        return self._payload

    def raise_for_status(self) -> None:
        pass

    async def __aenter__(self) -> "_Resp":
        if self.hang:
            await asyncio.Event().wait()                # never answers
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None


class FakeTuyaCloud:
    """Stands in for aiohttp.ClientSession."""

    def __init__(self, *, sub: bool = True, apply_delay: float = 0.0) -> None:
        self.meta = {
            "name": "DL026HA Test", "online": True, "product_name": "DL026HA",
            "sub": sub, "node_id": "0123456789abcdef" if sub else "",
            "local_key": "k" * 16,
        }
        # Real DL026HA status codes.
        self.status: dict[str, Any] = {
            "residual_electricity": 71, "unlock_fingerprint": 0, "unlock_ble": 0,
            "alarm_lock": "wrong_finger", "hijack": False, "beep_volume": "mute",
            "automatic_lock": True, "auto_lock_time": 30, "lock_motor_state": False,
            "unlock_phone_remote": 0, "do_not_disturb": False,
        }
        self.apply_delay = apply_delay
        self.drop_writes = False           # cloud says OK, lock never gets it
        self.hang_posts = 0                # this many POSTs never get an answer
        self.fail_codes: list[tuple[int, str]] = []   # consumed per POST
        self.status_fail_codes: list[tuple[int, str]] = []
        self.calls: Counter[str] = Counter()
        self.writes: list[dict] = []
        self.tokens_issued = 0
        self._tasks: set[asyncio.Task] = set()

    # -- helpers ---------------------------------------------------------
    @staticmethod
    def _ok(result: Any) -> _Resp:
        return _Resp({"success": True, "result": result})

    @staticmethod
    def _err(code: int, msg: str) -> _Resp:
        return _Resp({"success": False, "code": code, "msg": msg})

    def _apply_later(self, code: str, value: Any) -> None:
        async def _go() -> None:
            await asyncio.sleep(self.apply_delay)
            self.status[code] = value
        t = asyncio.get_running_loop().create_task(_go())
        self._tasks.add(t); t.add_done_callback(self._tasks.discard)

    # -- aiohttp surface -------------------------------------------------
    def get(self, url: str, headers: dict | None = None) -> _Resp:
        path = url.split(".com", 1)[1]
        if path.startswith("/v1.0/token"):
            self.calls["token"] += 1
            self.tokens_issued += 1
            return self._ok({"access_token": f"tok{self.tokens_issued}", "expire_time": 7200})
        if path.endswith("/status"):
            self.calls["status"] += 1
            if self.status_fail_codes:
                return self._err(*self.status_fail_codes.pop(0))
            return self._ok(self._status_list())
        if "/logs" in path:
            self.calls["logs"] += 1
            return self._ok({"logs": []})
        if "/specifications" in path:
            return self._ok({})
        self.calls["info"] += 1
        if self.status_fail_codes:
            return self._err(*self.status_fail_codes.pop(0))
        # The real GET /v1.0/devices/{id} embeds the full status array.
        return self._ok({**self.meta, "status": self._status_list()})

    def _status_list(self) -> list[dict]:
        return [{"code": k, "value": v} for k, v in self.status.items()]

    @property
    def polls(self) -> int:
        """Reads of the lock's state, by either endpoint."""
        return self.calls["status"] + self.calls["info"]

    def post(self, url: str, headers: dict | None = None, data: str | None = None) -> _Resp:
        path = url.split(".com", 1)[1]
        kind = ("ticket" if path.endswith("password-ticket")
                else "door_operate" if path.endswith("door-operate") else "commands")
        self.calls[kind] += 1
        if self.hang_posts:
            self.hang_posts -= 1
            r = _Resp({}); r.hang = True
            return r
        if self.fail_codes:
            return self._err(*self.fail_codes.pop(0))
        if kind == "ticket":
            return self._ok({"ticket_id": "T1", "expire_time": 120})
        body = json.loads(data or "{}")
        if kind == "door_operate":
            self.writes.append({"door_operate": body["open"]})
            return self._ok(True)
        for cmd in body["commands"]:
            self.writes.append({cmd["code"]: cmd["value"]})
            if not self.drop_writes:
                self._apply_later(cmd["code"], cmd["value"])
        return self._ok(True)
