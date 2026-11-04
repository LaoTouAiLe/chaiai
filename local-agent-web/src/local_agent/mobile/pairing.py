"""配对码与设备 Token（架构 §24；2026-11-03）。

三条口径，改动时别弄丢：

1. **只存摘要**：设备 Token 的原文只在配对那一次返回给手机，落盘的是 sha256。所以
   ``pairing.json`` 被人看到也不能拿去冒充设备。
2. **撤销是删记录**，不是打 ``revoked`` 标记。打标记的实现只要有一处忘了判断，撤销就形同
   虚设；这类"看起来撤销了其实没有"的故障在出事之前不会暴露。
3. **配对码短期且一次性**：它是可以被念出来、贴进聊天框的东西，天然会泄露。用过即作废，
   过期即作废；重新生成不作废新码之前签发的设备（那是两件事）。

存储是网关自己的状态，落在 ``data_root/mobile/pairing.json``：**不进 config.yaml**（会覆盖
用户注释、且与部署配置的生命周期不同），**也不进设置库**（那是模型连接的账本）。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from filelock import FileLock
from filelock import Timeout as FileLockTimeout

#: 配对码与设备 Token 的随机字节数。32 字节 → base64url 43 字符（与 dsh-mobile 同一量级），
#: 足够抗暴力枚举，又能整段显示给人看。
SECRET_BYTES = 32

STATE_DIR_NAME = "mobile"
PAIRING_FILE_NAME = "pairing.json"
STATE_VERSION = 1

#: 抢写锁的超时（秒）。正常竞争是毫秒级，5 秒足够；超时说明有进程卡住了。
LOCK_TIMEOUT_SECONDS = 5.0


class PairingError(Exception):
    """配对失败；消息可直接展示给用户（不含任何 Token 材料）。"""


@dataclass(frozen=True, slots=True)
class Device:
    """一个已配对的设备。``token_sha256`` 是摘要，不是 Token 本身。"""

    id: str
    label: str
    channel: str
    origin: str
    token_sha256: str
    created_at: str


@dataclass(frozen=True, slots=True)
class PairingCode:
    """一个待使用的配对码。``code`` 只在内存里出现，落盘的是它的摘要。"""

    code: str
    channel: str
    expires_at: float

    def remaining(self, *, now: float | None = None) -> float:
        return max(0.0, self.expires_at - (time.time() if now is None else now))


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class PairingStore:
    """配对状态：一次性配对码 + 已授权设备。

    一个实例对应一个文件。**每次操作都重新读文件**而不是长期持有内存副本：网关只有一个进程，
    但这份文件会被管理接口、反代中间件同时读到，且用户可能手工删它来"清空授权"——缓存一份
    就等于让"删文件"这个直觉动作失效。
    """

    def __init__(self, state_dir: Path) -> None:
        self.state_dir = state_dir
        self.path = state_dir / PAIRING_FILE_NAME
        #: 写侧互斥。**同一份文件现在有两个进程会写**（网关处理配对、主服务处理设置页的管理动作），
        #: 而这里是"读—改—写"，没有锁就会丢更新：用户刚生成的配对码可能被一次并发的配对写回覆盖。
        self._lock = FileLock(str(self.path) + ".lock", timeout=LOCK_TIMEOUT_SECONDS)

    @contextmanager
    def _transaction(self) -> Iterator[dict[str, Any]]:
        """持有文件锁的读—改—写。**只给写操作用**；读操作不需要（``os.replace`` 是原子的）。"""
        try:
            with self._lock:
                data = self._load()
                yield data
                self._save(data)
        except FileLockTimeout as exc:
            raise PairingError(
                "配对状态正被另一个进程写入，请稍后重试（若持续出现，检查是否有卡住的网关进程）"
            ) from exc

    # ---- 读写 -------------------------------------------------------------

    def _load(self) -> dict[str, Any]:
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {"version": STATE_VERSION, "codes": [], "devices": []}
        except OSError as exc:
            raise PairingError(f"读取配对状态失败：{exc.strerror or exc}") from exc
        try:
            raw = json.loads(text)
        except json.JSONDecodeError as exc:
            # 不静默重建：那会把"文件被写坏"变成"所有设备莫名失效"，且没有任何线索。
            raise PairingError(f"配对状态文件不是合法 JSON：{self.path}") from exc
        if not isinstance(raw, dict):
            raise PairingError(f"配对状态文件结构不对：{self.path}")
        raw.setdefault("version", STATE_VERSION)
        raw.setdefault("codes", [])
        raw.setdefault("devices", [])
        return raw

    def _save(self, data: dict[str, Any]) -> None:
        """原子写：先写同目录临时文件再 ``os.replace``。

        直接覆盖时若进程中途退出，会得到一份截断的 JSON——而这份文件里存着**全部设备授权**，
        恢复它比重新配对麻烦得多（每台手机都要再扫一次）。
        """
        self.state_dir.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
        tmp = self.path.with_name(f"{self.path.name}.{uuid4().hex[:8]}.tmp")
        try:
            tmp.write_text(payload, encoding="utf-8")
            os.replace(tmp, self.path)
        finally:
            tmp.unlink(missing_ok=True)

    # ---- 配对码 -----------------------------------------------------------

    def issue_code(self, channel: str, ttl_seconds: float) -> PairingCode:
        """签发一个一次性配对码。

        顺手清掉已过期的码：它们不再可能被兑换，留着只会让"还剩几个码"这类判断变成玄学。
        """
        if ttl_seconds <= 0:
            raise PairingError("配对码有效期必须大于 0 秒")
        now = time.time()
        code = secrets.token_urlsafe(SECRET_BYTES)
        expires_at = now + float(ttl_seconds)
        with self._transaction() as data:
            kept = [
                item
                for item in data["codes"]
                if isinstance(item, dict) and float(item.get("expires_at", 0)) > now
            ]
            kept.append(
                {
                    "digest": _digest(code),
                    "channel": channel,
                    "expires_at": expires_at,
                }
            )
            data["codes"] = kept
        return PairingCode(code=code, channel=channel, expires_at=expires_at)

    def pending_codes(self) -> list[dict[str, Any]]:
        """当前仍有效的配对码（只有摘要与截止时间，**不含原文**）。"""
        now = time.time()
        return [
            item
            for item in self._load()["codes"]
            if isinstance(item, dict) and float(item.get("expires_at", 0)) > now
        ]

    def redeem(self, code: str, *, label: str, origin: str, channel: str) -> tuple[Device, str]:
        """用配对码换一个设备 Token，返回（设备, Token 原文）。

        Raises:
            PairingError: 码不存在、已过期或已用过。
        """
        text = code.strip()
        if not text:
            raise PairingError("配对码不能为空")
        now = time.time()
        target = _digest(text)
        with self._transaction() as data:
            matched: dict[str, Any] | None = None
            remaining: list[dict[str, Any]] = []
            for item in data["codes"]:
                if not isinstance(item, dict):
                    continue
                if float(item.get("expires_at", 0)) <= now:
                    continue
                if hmac.compare_digest(str(item.get("digest", "")), target):
                    # 命中即消耗：同一个码不能被两台设备兑换（否则"一次性"就是假的）。
                    matched = item
                    continue
                remaining.append(item)
            if matched is None:
                # 文案不区分"不存在 / 已用过 / 已过期"：区分了就等于告诉暴力枚举者"这个码存在过"。
                raise PairingError("配对码无效或已过期，请在电脑上重新生成")

            token = secrets.token_urlsafe(SECRET_BYTES)
            device = Device(
                id=uuid4().hex[:12],
                label=label.strip()[:64] or "未命名设备",
                channel=channel or str(matched.get("channel", "")),
                origin=origin.strip(),
                token_sha256=_digest(token),
                created_at=_now_iso(),
            )
            data["codes"] = remaining
            data["devices"] = list(data["devices"]) + [asdict(device)]
        return device, token

    # ---- 设备 -------------------------------------------------------------

    def verify(self, token: str) -> Device | None:
        """校验设备 Token，返回对应设备；不通过返回 ``None``。

        **只认 Token，不按 Origin 拒绝**（2026-11-03 修订）。同一台手机的 Origin 会随通道变
        （局域网 ``http://192.168.x.x:8743`` → Tailscale ``https://xxx.ts.net``），按精确 Origin
        拒绝会把正常用户挡在门外，而它要防的是"Token 泄露后被别处使用"——那种情况由撤销机制
        兜底（删记录即失效），Token 本身也是 43 字符随机、只存摘要。``origin`` 字段保留在设备
        记录里，只作展示（"最后从哪儿连的"），不作判定依据。
        """
        text = token.strip()
        if not text:
            return None
        target = _digest(text)
        for item in self._load()["devices"]:
            if not isinstance(item, dict):
                continue
            if not hmac.compare_digest(str(item.get("token_sha256", "")), target):
                continue
            return Device(
                id=str(item.get("id", "")),
                label=str(item.get("label", "")),
                channel=str(item.get("channel", "")),
                origin=str(item.get("origin", "")),
                token_sha256=str(item.get("token_sha256", "")),
                created_at=str(item.get("created_at", "")),
            )
        return None

    def list_devices(self) -> list[Device]:
        return [
            Device(
                id=str(item.get("id", "")),
                label=str(item.get("label", "")),
                channel=str(item.get("channel", "")),
                origin=str(item.get("origin", "")),
                token_sha256=str(item.get("token_sha256", "")),
                created_at=str(item.get("created_at", "")),
            )
            for item in self._load()["devices"]
            if isinstance(item, dict)
        ]

    def revoke(self, device_id: str) -> bool:
        """删除一台设备的授权；返回是否真的删掉了。

        **物理删除**：文件里不再有这条记录，任何持有旧 Token 的请求立刻变成未知 Token。
        """
        with self._transaction() as data:
            before = len(data["devices"])
            data["devices"] = [
                item
                for item in data["devices"]
                if not (isinstance(item, dict) and str(item.get("id", "")) == device_id)
            ]
            removed = len(data["devices"]) != before
        return removed

    def revoke_all(self) -> int:
        """撤销全部设备，返回删除条数。

        与单条撤销同一语义：**物理删除**记录与摘要。这是"怀疑令牌泄露"时的动作——只停网关不够，
        因为重开之后旧 Token 依然有效。
        """
        with self._transaction() as data:
            removed = len(data["devices"])
            data["devices"] = []
        return removed

    def rename(self, device_id: str, label: str) -> Device | None:
        """改设备显示名；找不到返回 ``None``。

        设备名来自 UA，安卓那边抓到的常常是型号代号（``2509FPN0BC``），用户认不出是哪台。
        允许改名比"猜得更准"更实在——用户自己起的名字一定比任何 UA 解析准。
        """
        text = label.strip()[:64]
        if not text:
            raise PairingError("设备名不能为空")
        renamed: Device | None = None
        with self._transaction() as data:
            devices: list[Any] = []
            for item in data["devices"]:
                if isinstance(item, dict) and str(item.get("id", "")) == device_id:
                    item = {**item, "label": text}
                    renamed = Device(
                        id=str(item.get("id", "")),
                        label=text,
                        channel=str(item.get("channel", "")),
                        origin=str(item.get("origin", "")),
                        token_sha256=str(item.get("token_sha256", "")),
                        created_at=str(item.get("created_at", "")),
                    )
                devices.append(item)
            data["devices"] = devices
        return renamed
