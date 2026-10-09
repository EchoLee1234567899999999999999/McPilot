"""门店查询解析（Phase 4 新增）。

数据来源（只读）：``query-nearby-stores``。

真实 schema（实测）::

    {"success": true, "code": 200, "datetime": "...",
     "data": [
        {"storeCode": "1420762",
         "storeName": "麦当劳深圳同方信息港餐厅",
         "address": "科技园北区朗山路11号同方信息港E栋G层5号商铺",
         "distance": 403,
         "businessStatus": true,
         "businessStartTime": "07:00",
         "businessEndTime": "23:00",
         "reservation": true,
         "reservationTimeOptions": [...]},
        ...
     ]}

设计约定：只使用真实返回字段，缺失即留空 / None，**绝不臆造**。
"""

from __future__ import annotations

from typing import Any

from .models import Store


class StoreParseError(ValueError):
    """门店结构不符合预期。"""


def _to_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def parse_stores(payload: dict[str, Any]) -> list[Store]:
    """把 ``query-nearby-stores`` 返回解析为 :class:`~mcpilot.models.Store` 列表。

    ``be_code`` 仅在得来速（beType=5）等场景由返回携带；自取场景为空。
    """
    if not isinstance(payload, dict) or not payload.get("success", True):
        msg = payload.get("message") if isinstance(payload, dict) else payload
        raise StoreParseError(f"query-nearby-stores 返回失败：{msg}")

    data = payload.get("data")
    if data is None:
        return []
    if not isinstance(data, list):
        raise StoreParseError("query-nearby-stores.data 结构异常（应为数组）。")

    stores: list[Store] = []
    for row in data:
        if not isinstance(row, dict):
            continue
        code = str(row.get("storeCode") or "").strip()
        if not code:
            continue
        be_code = row.get("beCode") or row.get("be_code")
        options: list[str] = []
        for opt in row.get("reservationTimeOptions") or []:
            if isinstance(opt, dict):
                text = str(opt.get("reservationOptionText") or "").strip()
                if text:
                    options.append(text)
        stores.append(
            Store(
                store_code=code,
                name=str(row.get("storeName") or "").strip(),
                address=str(row.get("address") or "").strip(),
                be_code=str(be_code).strip() if be_code else None,
                reservation_options=options,
            )
        )
    return stores


def store_to_dict(store: Store, *, distance: Any = None, raw: dict[str, Any] | None = None) -> dict[str, Any]:
    """把门店转成可供前端消费的 dict（保留距离 / 营业状态等真实字段）。"""
    raw = raw or {}
    return {
        "store_code": store.store_code,
        "name": store.name,
        "address": store.address,
        "be_code": store.be_code,
        "distance": _to_int(raw.get("distance")),
        "business_status": bool(raw.get("businessStatus", False)),
        "business_start": str(raw.get("businessStartTime") or ""),
        "business_end": str(raw.get("businessEndTime") or ""),
        "reservation": bool(raw.get("reservation", False)),
    }


def stores_to_dicts(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """一步到位：原始 payload → 前端可用的门店 dict 列表（含距离等信息）。"""
    if not isinstance(payload, dict):
        return []
    raw_rows = payload.get("data")
    if not isinstance(raw_rows, list):
        raw_rows = []
    by_code = {
        str(r.get("storeCode")): r for r in raw_rows if isinstance(r, dict)
    }
    out: list[dict[str, Any]] = []
    for store in parse_stores(payload):
        out.append(store_to_dict(store, raw=by_code.get(store.store_code)))
    return out
