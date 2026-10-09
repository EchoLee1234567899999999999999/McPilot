"""营养按需增强（Phase 3）。

菜单列表里的名称是 **UI 简名**，营养表使用**规范全名**；两者之间隔着规格、系列、
包装等差异。Phase 3 的策略是：

- **能静态判定**的（归一化 / 人工别名）直接匹配，**零额外调用**；
- **无法静态判定**的，用 ``query-meal-detail`` 拿"规范全名"与"套餐默认组成"再匹配；
- 每一步都**有界**（``max_calls``）且**有缓存**（同一名称只解析一次），
  避免把 125 个商品逐一解析造成的过量请求。

安全与合规
----------
- 只调用只读的 ``query-meal-detail``；任何失败都**不猜测**，只是"解析不到"。
- 不引入任何写操作。
"""

from __future__ import annotations

from typing import Callable, Optional

from .menu import MenuData, parse_meal_detail
from .models import SetComponent
from .nutrition import (
    NutritionIndex,
    ResolvedNutrition,
    resolve_name,
)

__all__ = ["NutritionEnricher"]

#: 详情获取器：``code -> query-meal-detail 的业务 JSON``
DetailFetcher = Callable[[str], dict]


class NutritionEnricher:
    """带缓存与调用上限的营养解析器。

    - :meth:`resolve`：先做静态分层匹配；若失败且还有调用配额，则拉取详情补齐
      "规范全名 / 套餐组成"，再匹配一次。
    - :attr:`calls` / :attr:`resolved_names`：用于报告与测试。
    """

    def __init__(
        self,
        index: NutritionIndex,
        menu: MenuData,
        fetcher: Optional[DetailFetcher] = None,
        *,
        max_calls: int = 10,
        use_alias: bool = True,
    ) -> None:
        self.index = index
        self.menu = menu
        self._fetcher = fetcher
        self.max_calls = max(0, int(max_calls))
        self.use_alias = use_alias

        self.canonical_names: dict[str, str] = {}
        self.compositions: dict[str, list[SetComponent]] = {}
        self.calls = 0
        self._tried: set[str] = set()  # 已尝试过拉取详情的名称（避免重复请求）

    # -- 内部 --------------------------------------------------------------
    def _resolve_static(self, name: str) -> ResolvedNutrition:
        return resolve_name(
            name,
            self.index,
            canonical_names=self.canonical_names,
            compositions=self.compositions,
            use_alias=self.use_alias,
        )

    def _fetch(self, name: str) -> bool:
        """拉取详情补齐解析信息；返回是否有新增可用信息。"""
        if self._fetcher is None or self.calls >= self.max_calls or name in self._tried:
            return False
        item = self.menu.by_name.get(name)
        if item is None:
            self._tried.add(name)
            return False
        self._tried.add(name)
        try:
            payload = self._fetcher(item.code)
        except Exception:  # 详情拉取失败 → 不猜测，仅视为"解析不到"
            return False
        self.calls += 1
        try:
            detail = parse_meal_detail(payload)
        except Exception:
            return False

        changed = False
        canon = str(detail.name or "").strip()
        if canon and canon != name:
            self.canonical_names[name] = canon
            changed = True
        if detail.has_composition:
            self.compositions[name] = list(detail.components)
            changed = True
        return changed

    # -- 公共 --------------------------------------------------------------
    def resolve(self, name: str, *, allow_fetch: bool = True) -> ResolvedNutrition:
        """解析单个名称的营养（必要时按需拉详情，受 :attr:`max_calls` 限制）。"""
        r = self._resolve_static(name)
        if r.ok or not allow_fetch:
            return r
        if self._fetch(name):
            return self._resolve_static(name)
        return r

    def resolve_all(self, names: list[str], *, allow_fetch: bool = True) -> dict[str, ResolvedNutrition]:
        """批量解析（同一名称只解析一次）。"""
        out: dict[str, ResolvedNutrition] = {}
        for n in names:
            if n not in out:
                out[n] = self.resolve(n, allow_fetch=allow_fetch)
        return out
