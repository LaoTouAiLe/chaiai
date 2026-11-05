"""移动访问的状态与管理（2026-11-03；设置页「移动访问」，架构 §24）。

**这里为什么能写**：配对状态本来就是本机的一份 JSON（``data_root/mobile/pairing.json``），主服务
与网关在同一用户下跑、读同一份文件。把"生成配对码 / 撤销设备 / 改名"放进主服务，设置页就能把
二维码和设备列表**直接嵌进来**——不必跨 origin 请求网关，也不必让主服务做反向代理（后者会把
"主服务不为网关开例外"这条边界磨薄）。

代价是**同一份文件有两个写者**，所以 :class:`~local_agent.mobile.pairing.PairingStore` 的写路径
持有文件锁。这条比"少一个写者"重要：丢的可能是用户刚生成、正要去扫的那个配对码。

**这些端点是本机专用的**：既要求同源浏览器元数据（挡 curl 之类本机脚本），又要求 TCP 对端是
回环——万一有人打开 ``web.allow_non_loopback``，不能让局域网里任何人都能签发配对码。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import ValidationError

from local_agent import app_paths
from local_agent.api.container import ApiRuntime
from local_agent.api.schemas import (
    MobileChannelSettingView,
    MobileDevicesView,
    MobileDeviceView,
    MobileEmergencyStopRequest,
    MobileGatewayActionView,
    MobileInfoView,
    MobilePairingCodeView,
    MobilePairUrlView,
    MobileRenameRequest,
    MobileRevokeView,
    MobileSettingsRequest,
    MobileSettingsView,
)
from local_agent.api.security import is_allowed_origin
from local_agent.config import AppConfig, MobileChannelConfig, MobileConfig
from local_agent.mobile.channels import phone_base_urls
from local_agent.mobile.pairing import Device, PairingError, PairingStore
from local_agent.mobile.proxy import ADMIN_PREFIX, VIA_GATEWAY_HEADER
from local_agent.mobile.qr import svg_qr
from local_agent.mobile.runtime import RuntimeOutcome, gateway_runtime
from local_agent.mobile.settings import read_snapshot, write_snapshot

# 探活口径与"拉起网关时判断是否已在运行"必须一致，否则会出现"启动时认为在跑、页面上说没跑"
# 这种自相矛盾。所以只保留 supervisor 那一份实现。
from local_agent.mobile.supervisor import is_listening

#: 状态目录名，与 `local_agent.mobile.__main__` 同一口径（``data_root/mobile``）。
STATE_DIR_NAME = "mobile"

#: 回环对端地址。与 §24 的管理接口判定同一口径（不带端口，只看地址）。
_LOOPBACK_PEERS = frozenset({"127.0.0.1", "::1"})


def _reject_via_gateway(request: Request) -> None:
    """拒绝"经网关进来的"请求。

    **反代之后地址与 Origin 都不可信**：网关把 Host/Origin 改写成上游口径，对端也是 ``127.0.0.1``。
    所以只靠回环与同源判定，已配对的手机同样能调这些管理接口（列设备、撤销、签发新码）。
    `proxy` 在反代 ``/api/mobile/*`` 时打了标记头，这里据此拒绝——这是唯一还能分辨的依据。
    """
    if request.headers.get(VIA_GATEWAY_HEADER) is not None:
        raise HTTPException(
            status_code=403,
            detail="这个接口只服务电脑上的设置页：请在本机打开，不要从手机管理配对。",
        )


def _require_loopback_peer(request: Request) -> None:
    """只要求 TCP 对端是回环（且不是经网关转进来的）。

    给**读**操作用。写操作才需要 ``Origin``——浏览器对 GET/HEAD 不发这个头，读接口按写操作那套
    判定会一律 403（第一版就是这么错的：状态说"已配对 1 台"，列表却显示"还没有"）。
    """
    _reject_via_gateway(request)
    client = request.client
    if client is None or client.host not in _LOOPBACK_PEERS:
        raise HTTPException(status_code=403, detail="本接口只接受本机访问。")


def _require_local_write(request: Request) -> None:
    """写操作：回环对端 + 同源浏览器元数据。前者挡局域网，后者挡 CSRF 与本机脚本（curl）。"""
    _require_loopback_peer(request)
    origin = request.headers.get("origin")
    host = request.headers.get("host", "")
    if origin is None or not is_allowed_origin(origin, host, ()):
        raise HTTPException(status_code=403, detail="本接口只接受本机页面的同源请求。")


def _settings_view(
    mobile: MobileConfig, *, from_store: bool, restart_required: bool
) -> MobileSettingsView:
    return MobileSettingsView(
        enabled=mobile.enabled,
        gateway_host=mobile.gateway_host,
        gateway_port=mobile.gateway_port,
        pairing_ttl_seconds=mobile.pairing_ttl_seconds,
        ws_enabled=mobile.ws_enabled,
        channels=[
            MobileChannelSettingView(
                type=item.type,
                enabled=item.enabled,
                mode=item.mode,
                hostname=item.hostname,
                expose=item.expose,
                https_port=item.https_port,
                port=item.port,
                tls=item.tls,
            )
            for item in mobile.channels
        ],
        source="settings" if from_store else "config",
        restart_required=restart_required,
    )


def _snapshot_differs(config: AppConfig, mobile: MobileConfig, *, from_store: bool) -> bool:
    """网关手上的配置是否已经过期。

    **没有快照时不能一律返回 False**（2026-11-03 修）：设置库里改过、但快照写失败（磁盘满、
    权限、进程被杀）的时候，网关读的是 ``config.yaml``——那是旧值。此时若说"不需要重启"，
    用户会看到"已生效"而实际没生效，这是最难自查的一类错误。所以这种情况按"需要重启"报。
    """
    stored = read_snapshot(app_paths.data_root_of(config))
    if stored is None:
        # 没有快照：只有"生效值就来自 config.yaml"时，网关手上才恰好是同一份。
        return from_store
    return stored != mobile.model_dump()


def _device_view(device: Device) -> MobileDeviceView:
    return MobileDeviceView(
        id=device.id,
        label=device.label,
        channel=device.channel,
        created_at=device.created_at,
    )


def _store_of(runtime: ApiRuntime) -> PairingStore:
    """配对状态文件：网关与本服务读写的同一份，只存摘要。"""
    return PairingStore(app_paths.data_root_of(runtime.service.app_config) / STATE_DIR_NAME)


def _device_count_of(runtime: ApiRuntime) -> int:
    """设备数读的是落盘状态；文件被写坏时当 0，不让整张卡片跟着失败。"""
    try:
        return len(_store_of(runtime).list_devices())
    except PairingError:
        return 0


def _effective_of(runtime: ApiRuntime) -> tuple[MobileConfig, int]:
    """页面要显示的生效配置 + 设备数（设置库优先，config.yaml 是出厂默认）。"""
    mobile, _from_store = runtime.service.effective_mobile_config()
    return mobile, _device_count_of(runtime)


def _action_view(
    outcome: RuntimeOutcome,
    mobile: MobileConfig,
    device_count: int,
    *,
    detail: str | None = None,
) -> MobileGatewayActionView:
    return MobileGatewayActionView(
        ok=outcome.ok,
        detail=detail or outcome.detail,
        running=mobile.enabled
        and is_listening(mobile.gateway_host, mobile.gateway_port),
        device_count=device_count,
    )

def _register_overview_routes(router: APIRouter, runtime: ApiRuntime) -> None:
    service = runtime.service

    @router.get("/mobile/info", response_model=MobileInfoView, summary="移动访问状态（只读）")
    def mobile_info() -> MobileInfoView:
        """移动网关开着没、跑着没、配了几台、去哪儿管。

        设备数读的是落盘状态：网关没跑时**照样给出真实数字**——"有 3 台已授权但服务没开"
        正是用户最需要看到的那句话。这一个是只读的，所以不要求同源元数据（设置页轮询它）。
        """
        config = service.app_config
        mobile = config.mobile
        try:
            device_count = len(_store_of(runtime).list_devices())
        except PairingError:
            # 状态文件被写坏时不让整张卡片跟着失败：数量当 0，界面上仍能进管理页修。
            device_count = 0
        return MobileInfoView(
            enabled=mobile.enabled,
            running=mobile.enabled and is_listening(mobile.gateway_host, mobile.gateway_port),
            admin_url=f"http://{mobile.gateway_host}:{mobile.gateway_port}{ADMIN_PREFIX}/",
            device_count=device_count,
        )

    @router.post(
        "/mobile/pairing-code",
        response_model=MobilePairingCodeView,
        summary="生成一个配对二维码",
    )
    def create_pairing_code(request: Request) -> MobilePairingCodeView:
        """签发一次性配对码，连二维码一起返回。

        没有手机可达的通道时返回 409 并说明——给一个扫了打不开的二维码比不给更糟。
        """
        _require_local_write(request)
        config = service.app_config
        bases = phone_base_urls(config)
        if not bases:
            raise HTTPException(
                status_code=409,
                detail="还没有手机可达的通道：在配置里启用局域网或 Tailscale 通道，"
                "并确认手机与这台电脑在同一个网络。",
            )
        try:
            code = _store_of(runtime).issue_code("lan", float(config.mobile.pairing_ttl_seconds))
        except PairingError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        path = f"{ADMIN_PREFIX}/pair?code={code.code}"
        pair_url = f"{bases[0][1]}{path}"
        return MobilePairingCodeView(
            code=code.code,
            pair_url=pair_url,
            qr_svg=svg_qr(pair_url).decode("utf-8"),
            expires_in=round(code.remaining()),
            alternates=[
                MobilePairUrlView(label=label, url=f"{base}{path}")
                for label, base in bases[1:]
            ],
        )



def _register_gateway_routes(router: APIRouter, runtime: ApiRuntime) -> None:
    service = runtime.service

    # ---- 紧急启停（2026-11-03：用户要"出问题后能操控关闭"）-------------------

    @router.post(
        "/mobile/gateway/start",
        response_model=MobileGatewayActionView,
        summary="启动网关（立即生效）",
    )
    def start_gateway_now(request: Request) -> MobileGatewayActionView:
        """立刻拉起网关，不等重启。已在跑就如实说"已在运行"。"""
        _require_local_write(request)
        # 只读一次：启动前后设备数不会变，第二次 `_effective_of(runtime)` 是纯多余的磁盘访问，
        # 而且两次调用之间还有状态窗口。
        mobile, count = _effective_of(runtime)
        effective = service.app_config.model_copy(update={"mobile": mobile})
        outcome = gateway_runtime().start(effective)
        return _action_view(outcome, mobile, count)

    @router.post(
        "/mobile/gateway/stop",
        response_model=MobileGatewayActionView,
        summary="停止网关（立即断开所有手机）",
    )
    def stop_gateway_now(request: Request) -> MobileGatewayActionView:
        """立刻停掉网关。**幂等**：没在跑也返回成功，方便"多按几次确保关掉"。"""
        _require_local_write(request)
        outcome = gateway_runtime().stop()
        mobile, count = _effective_of(runtime)
        return _action_view(outcome, mobile, count)

    @router.post(
        "/mobile/emergency-stop",
        response_model=MobileGatewayActionView,
        summary="紧急关闭：停网关，可选撤销全部设备",
    )
    def emergency_stop(
        payload: MobileEmergencyStopRequest, request: Request
    ) -> MobileGatewayActionView:
        """一键断开移动访问。

        `revoke_devices=true` 时连设备授权一起清掉——**只停网关是不够的**：重开之后旧 Token
        依然有效，而"怀疑令牌泄露"正是最需要这个按钮的场景。
        """
        _require_local_write(request)
        outcome = gateway_runtime().stop()
        revoked = 0
        if payload.revoke_devices:
            try:
                revoked = _store_of(runtime).revoke_all()
            except PairingError as exc:
                raise HTTPException(status_code=500, detail=str(exc)) from exc
        mobile, count = _effective_of(runtime)
        detail = str(outcome.detail)
        if revoked:
            detail += f" 同时撤销了 {revoked} 台设备的授权。"
        return _action_view(outcome, mobile, count, detail=detail)



def _register_settings_routes(router: APIRouter, runtime: ApiRuntime) -> None:
    service = runtime.service

    @router.get(
        "/mobile/settings",
        response_model=MobileSettingsView,
        summary="移动访问配置（生效值）",
    )
    def read_mobile_settings(request: Request) -> MobileSettingsView:
        """页面显示的那一份：设置库优先、`config.yaml` 是出厂默认。"""
        _require_loopback_peer(request)
        mobile, from_store = service.effective_mobile_config()
        return _settings_view(
            mobile,
            from_store=from_store,
            restart_required=_snapshot_differs(
                service.app_config, mobile, from_store=from_store
            ),
        )

    @router.put(
        "/mobile/settings",
        response_model=MobileSettingsView,
        summary="保存移动访问配置",
    )
    def save_mobile_settings(
        payload: MobileSettingsRequest, request: Request
    ) -> MobileSettingsView:
        """整段保存，并把生效值写成快照交给网关。

        **保存不等于生效**：网关是独立进程，必须重启才会读到新配置。返回体里
        `restart_required` 恒为 true，页面据此提示——不提示的话用户会以为保存失败了。
        """
        _require_local_write(request)
        base, _from_store = service.effective_mobile_config()
        try:
            candidate = MobileConfig(
                enabled=payload.enabled,
                # 网关只监听回环，这一项不在页面上暴露（改了等于绕开通道）。
                gateway_host=base.gateway_host,
                gateway_port=payload.gateway_port,
                pairing_ttl_seconds=payload.pairing_ttl_seconds,
                ws_enabled=payload.ws_enabled,
                channels=[
                    MobileChannelConfig(**item.model_dump()) for item in payload.channels
                ],
            )
        except ValidationError as exc:
            details = exc.errors()
            message = str(details[0]["msg"]) if details else "配置不合法"
            raise HTTPException(status_code=422, detail=message) from exc
        running = is_listening(base.gateway_host, base.gateway_port)
        saved, from_store = service.save_mobile_config(candidate)
        # 快照写给网关；写失败不影响"已保存"这个事实（见 settings.write_snapshot 的说明）。
        write_snapshot(service.app_config, saved)
        # **改了就让它在跑的那份也跟上**（2026-11-03）：只提示"重启后生效"，用户还得自己记得去按，
        # 那和"保存即生效"的其它设置不一致。只在原本就在跑时重启——原本没跑说明用户是刻意关着的，
        # 保存配置不该顺手把它打开。
        restarted = False
        if not saved.enabled:
            gateway_runtime().stop()
            restarted = True
        elif running:
            gateway_runtime().stop()
            effective = service.app_config.model_copy(update={"mobile": saved})
            restarted = gateway_runtime().start(effective).ok
        return _settings_view(saved, from_store=from_store, restart_required=not restarted)



def _register_device_routes(router: APIRouter, runtime: ApiRuntime) -> None:
    @router.get("/mobile/devices", response_model=MobileDevicesView, summary="已配对设备")
    def list_devices(request: Request) -> MobileDevicesView:
        _require_loopback_peer(request)
        try:
            devices = _store_of(runtime).list_devices()
        except PairingError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        return MobileDevicesView(devices=[_device_view(item) for item in devices])

    @router.delete(
        "/mobile/devices/{device_id}",
        response_model=MobileRevokeView,
        summary="撤销一台设备",
    )
    def revoke_device(device_id: str, request: Request) -> MobileRevokeView:
        """删记录即撤销：那台手机的下一次请求会被拒。"""
        _require_local_write(request)
        try:
            removed = _store_of(runtime).revoke(device_id)
        except PairingError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        if not removed:
            raise HTTPException(status_code=404, detail="没有这台设备（可能已被删除）。")
        return MobileRevokeView(revoked=True)

    @router.patch(
        "/mobile/devices/{device_id}",
        response_model=MobileDeviceView,
        summary="重命名一台设备",
    )
    def rename_device(device_id: str, payload: MobileRenameRequest, request: Request) -> MobileDeviceView:
        """改名只影响显示，不动授权。"""
        _require_local_write(request)
        try:
            renamed = _store_of(runtime).rename(device_id, payload.label)
        except PairingError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if renamed is None:
            raise HTTPException(status_code=404, detail="没有这台设备（可能已被删除）。")
        return _device_view(renamed)



def build_mobile_router(runtime: ApiRuntime) -> APIRouter:
    """移动访问：状态（只读）+ 配对与设备管理（本机专用）。

    路由按业务分成四组注册：只读状态与配对码、紧急启停、配置读写、设备管理。
    """
    router = APIRouter(prefix="/api")
    _register_overview_routes(router, runtime)
    _register_gateway_routes(router, runtime)
    _register_settings_routes(router, runtime)
    _register_device_routes(router, runtime)
    return router


__all__ = ["build_mobile_router"]
