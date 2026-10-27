"""应用身份与维护的 HTTP 面（2026-10-27，设置页「关于」）。

**为什么单独一个 router**（与 `browsers.py` / `terminals.py` 同一装配方式）：

- 这三件事读的是**宿主进程的形态**（冻结与否、卸载器在不在、往哪个端口开窗口），
  不是业务状态；它们也没有对应的应用服务用例（§15.1 的分工）。
- `routes.py::build_router` 已经很长，规模棘轮盯着它；再塞一百多行会直接把
  那个函数推过上限，而它并不因此更清楚。

三个端点都**只服务本机页面**：检查更新要联网、卸载要结束进程并调外部卸载器。跨源网页
已被中间件挡住，这一层挡的是本机脚本——它想卸载自己也能做，没有理由借这条路。
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final, cast

from fastapi import APIRouter, HTTPException
from starlette.requests import Request

from local_agent import app_maintenance, app_paths
from local_agent.api.container import ApiRuntime
from local_agent.api.schemas import (
    AppInfoView,
    BuildInfoView,
    ErrorResponse,
    UninstallRequest,
    UninstallView,
    UpdateCheckView,
)
from local_agent.api.security import require_browser_origin
from local_agent.application import errors as app_errors

logger = logging.getLogger(__name__)

#: 卸载相关的拒绝理由：403 不是本机页面；409 当前形态没有可卸载的安装。
_UNINSTALL_RESPONSES: Final[dict[int | str, dict[str, Any]]] = {
    403: {"description": "不是本机页面的同源请求（本接口只服务于页面）"},
    409: {"model": ErrorResponse, "description": "当前形态没有可卸载的安装（绿色包 / 源码运行）"},
}

#: 检查更新的拒绝理由：只有 403（查不到不算错误，走 200 + `status=unavailable`）。
_UPDATE_RESPONSES: Final[dict[int | str, dict[str, Any]]] = {
    403: {"description": "不是本机页面的同源请求（本接口只服务于页面）"},
}


def build_maintenance_router(runtime: ApiRuntime) -> APIRouter:
    """应用身份与维护（版本、检查更新、卸载交接）。"""
    router = APIRouter(prefix="/api")
    service = runtime.service

    @router.get("/app/info", response_model=AppInfoView, summary="版本与安装形态")
    def app_info() -> AppInfoView:
        """「关于」页的全部本机事实：版本、装在哪、数据在哪、能不能卸、能否连数据一起删。

        数据根复用「数据位置」那套解析（`service.storage_location()`），不在这里另算一份——
        两处各算一次的话，页面显示的位置和真的删掉的位置会慢慢变成两个。
        """
        storage = service.storage_location()
        info = app_maintenance.app_info(
            data_root=Path(storage.data_root),
            data_root_source=storage.source,
        )
        build = (
            None
            if info.build is None
            else BuildInfoView(
                built_at=info.build.built_at,
                revision=info.build.revision,
                dirty=info.build.dirty,
                python=info.build.python,
            )
        )
        return AppInfoView(
            version=info.version,
            install_kind=info.install_kind,
            install_dir=info.install_dir,
            data_root=info.data_root,
            data_root_source=cast(Any, info.data_root_source),
            build=build,
            uninstall_available=info.uninstall_available,
            uninstall_note=info.uninstall_note,
            can_remove_data=info.can_remove_data,
            remove_data_note=info.remove_data_note,
        )

    @router.post(
        "/app/update/check",
        response_model=UpdateCheckView,
        responses=_UPDATE_RESPONSES,
        summary="检查更新（GitHub Release）",
    )
    def check_update(request: Request) -> UpdateCheckView:
        """查一次 GitHub 最新 Release 并与当前版本比较。

        **只检查、不下载、不安装**（2026-10-27 拍板）：本项目没有自升级器，替用户覆盖安装
        要能替换正在运行的 exe，那是另一整套东西。有新版就把发布页交给系统浏览器。

        失败**不抛错**：`status="unavailable"` + `note` 就是完整答复。设置页是只读提醒，
        不该因为 GitHub 连不上而变成一个错误页。
        """
        require_browser_origin(request, allowed_origins=runtime.web.dev_origins)
        result = app_maintenance.check_update()
        return UpdateCheckView(
            status=result.status,
            current_version=result.current_version,
            latest_version=result.latest_version,
            release_url=result.release_url,
            published_at=result.published_at,
            note=result.note,
        )

    @router.post(
        "/app/uninstall",
        response_model=UninstallView,
        status_code=202,
        responses=_UNINSTALL_RESPONSES,
        summary="卸载本机程序（交接给安装程序）",
    )
    def uninstall(payload: UninstallRequest, request: Request) -> UninstallView:
        """把卸载交给 Inno 卸载器，**本进程随后退出**。

        为什么不能自己删：`unins000.exe` 与 `chaiai.exe` 同级，Windows 不允许删除正在运行的
        文件。所以这里只写一个延迟批处理（等本进程退出 → 调卸载器 → 按勾选删数据）并返回 202，
        真正的退出由脚本里的 `taskkill` 完成。

        `remove_data` 是**用户在弹窗里勾过**的结果，默认 false（`packaging/README.md` 的老约定：
        用户数据不归安装包管）。数据目录不能安全删除时**拒绝整个请求**而不是偷偷降级——
        用户勾了"删数据"却删不掉，比直接报错更糟。
        """
        require_browser_origin(request, allowed_origins=runtime.web.dev_origins)
        uninstaller = app_paths.uninstaller_path()
        if uninstaller is None:
            raise app_errors.uninstall_unavailable(
                note=app_maintenance.uninstall_note(app_maintenance.install_kind())
                or "当前形态没有可卸载的安装。",
                occurred_at=datetime.now(timezone.utc),
            )
        storage = service.storage_location()
        data_root = Path(storage.data_root)
        if payload.remove_data:
            block = app_maintenance.removal_block_reason(data_root)
            if block is not None:
                # 传输层 422：勾选项与这台机器的事实不符，请求不该进业务逻辑。
                raise HTTPException(
                    status_code=422,
                    detail=f"{block}。取消勾选后重试，或卸载完成后手动删除。",
                )
        script = app_maintenance.schedule_uninstall(
            uninstaller=uninstaller,
            data_root=data_root,
            pointer=app_paths.data_root_pointer(),
            remove_data=payload.remove_data,
        )
        logger.info(
            "卸载已交接：脚本=%s，删除数据=%s，数据目录=%s",
            script,
            payload.remove_data,
            data_root,
        )
        return UninstallView(
            scheduled=True,
            remove_data=payload.remove_data,
            data_dir=str(data_root),
            message=(
                "卸载程序即将启动，本窗口会先退出；"
                + ("你的数据目录会一并删除。" if payload.remove_data else "你的数据目录不会被删除。")
            ),
        )

    return router
