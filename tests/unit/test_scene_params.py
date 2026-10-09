"""参数规则测试（T6）。"""

from __future__ import annotations

import pytest

from mcpilot.mcp_client import (
    FORBIDDEN_WRITE_TOOLS,
    McpParamError,
    build_scene_args,
    order_type_for,
    validate_scene_params,
)


def test_order_type_mapping():
    assert order_type_for(1) == 1  # 到店自取
    assert order_type_for(5) == 1  # 得来速
    assert order_type_for(2) == 2  # 麦乐送
    assert order_type_for(6) == 2  # 团餐


@pytest.mark.parametrize("be_type", [2, 6, 5, 9])
def test_invalid_be_type(be_type):
    if be_type == 9:
        with pytest.raises(McpParamError):
            order_type_for(be_type)


def test_pickup_must_not_send_be_code():
    with pytest.raises(McpParamError):
        validate_scene_params(1, "SOMECODE")


def test_drive_thru_requires_be_code():
    with pytest.raises(McpParamError):
        validate_scene_params(5, None)
    validate_scene_params(5, "BE123")  # 不抛异常


def test_build_scene_args_pickup_omits_be_code():
    args = build_scene_args(1, None)
    assert args == {"beType": 1, "orderType": 1}
    assert "beCode" not in args


def test_build_scene_args_drive_thru_includes_be_code():
    args = build_scene_args(5, "BE123")
    assert args == {"beType": 5, "orderType": 1, "beCode": "BE123"}


def test_reservation_date_only_when_provided():
    assert "reservationDate" not in build_scene_args(1, None)
    assert build_scene_args(1, None, "2026-10-10 12:00")["reservationDate"] == "2026-10-10 12:00"


def test_forbidden_write_tools_are_listed():
    for name in ("create-order", "auto-bind-coupons", "draw-lottery", "mall-create-order"):
        assert name in FORBIDDEN_WRITE_TOOLS
