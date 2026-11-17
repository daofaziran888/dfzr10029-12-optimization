"""分析实验业务规则：样本科口、分组摘要预聚合、版本持久化与导出。

实验分组列表不再按全量样本逐条筛选。这里先把实验样本按“样本区间 + 分组字段”
预聚合成分组摘要，再让查询、翻页和导出共用同一份 ``snapshot``（样本口径）。
摘要版本按主版本边界持久化，回退时仍然可以追溯到原来的 snapshot。
"""
from __future__ import annotations

import hashlib
import json
import random
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable

from app.store import store

MODULE = "experiment"
REQUIRED_FIELDS = ["方案编号", "实验名称", "分析数据集"]
STATUS_ORDER = ["草稿", "已设计", "运行中", "已完成"]
ACTION_RULES = {"提交设计": "已设计", "运行实验": "运行中", "完成实验": "已完成"}
NEGATIVE_ACTIONS = []

SAMPLE_RANGES = ("最近7天", "最近30天", "最近90天")
DEFAULT_SAMPLE_RANGE = "最近30天"
DEFAULT_GROUP_FIELD = "区域"
GROUP_FIELDS = ("区域", "类型", "渠道")
DIMENSIONS: dict[str, tuple[str, ...]] = {
    "区域": ("华东", "华北", "华南", "西南", "东北", "西北"),
    "类型": ("新客", "老客", "回流"),
    "渠道": ("自然", "投放", "社交", "联盟"),
}
SAMPLES_PER_RANGE = 72
# 每个样本区间的分组样本量刻意不均匀，便于核对排序与分页是否作用于同一批摘要。
REGION_SAMPLE_COUNTS = (16, 14, 12, 10, 8, 12)
SAMPLE_ORDER_VERSION = "samples-v1"

VERSION_ACTIVE = "生效中"
VERSION_ARCHIVED = "已归档"
VERSION_CONFLICT = "冲突待核对"

BASE_VERSION_KEYS = ("base_version", "expected_version", "expected_main_version", "主版本", "基准版本")
SNAPSHOT_KEYS = ("snapshot", "snapshot_id", "口径编号", "query_id", "token")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _pick(values: dict[str, Any], *keys: str) -> str | None:
    """按业务字段名或英文别名取值，空字符串等同于未传。"""
    for key in keys:
        value = values.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return None


@dataclass(frozen=True)
class SampleFilter:
    """实验样本筛选条件业务对象。"""

    experiment_no: str | None = None
    sample_range: str | None = None
    group_field: str | None = None
    keyword: str | None = None
    dimensions: tuple[tuple[str, str], ...] = ()

    @classmethod
    def from_mapping(cls, values: dict[str, Any] | None = None) -> "SampleFilter":
        values = values or {}
        dimensions: list[tuple[str, str]] = []
        for name in DIMENSIONS:
            value = _pick(values, name)
            if value:
                dimensions.append((name, value))
        group_value = _pick(values, "分组值", "group_value", "groupValue")
        if group_value:
            dimensions.append(("__group_value__", group_value))
        return cls(
            experiment_no=_pick(values, "方案编号", "实验编号", "experiment_no", "experimentNo"),
            sample_range=_pick(values, "样本区间", "样本范围", "sample_range", "sampleRange", "range"),
            group_field=_pick(values, "分组字段", "group_field", "groupField", "group_by", "groupBy"),
            keyword=_pick(values, "keyword", "关键字"),
            dimensions=tuple(dimensions),
        )

    def effective_range(self) -> str | None:
        """数字型旧参数（例如并行数量）不当作样本区间，否则会误命中空口径。"""
        if not self.sample_range:
            return None
        if self.sample_range.isdigit():
            return None
        return self.sample_range

    def effective_group_field(self) -> str:
        if self.group_field and self.group_field in DIMENSIONS:
            return self.group_field
        return self.group_field or DEFAULT_GROUP_FIELD

    def canonical(self) -> dict[str, Any]:
        return {
            "方案编号": self.experiment_no or "",
            "样本区间": self.effective_range() or "",
            "分组字段": self.group_field or "",
            "关键字": self.keyword or "",
            "维度": dict(self.dimensions),
        }

    def has_conditions(self) -> bool:
        """调用方是否显式带了筛选条件；只带 snapshot 的单据视为翻页。"""
        if self.experiment_no or self.effective_range() or self.group_field or self.keyword:
            return True
        return bool(self.dimensions)


@dataclass(frozen=True)
class ExperimentSample:
    """一条实验样本业务对象。"""

    id: int
    experiment_id: int
    experiment_no: str
    experiment_name: str
    sample_range: str
    region: str
    sample_type: str
    channel: str
    metric: float
    baseline: float

    def dimension(self, field: str) -> str:
        if field == "区域":
            return self.region
        if field == "类型":
            return self.sample_type
        if field == "渠道":
            return self.channel
        return "全量"

    def as_row(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "样本编号": f"SAMP-{self.id:05d}",
            "实验方案id": self.experiment_id,
            "方案编号": self.experiment_no,
            "实验名称": self.experiment_name,
            "样本区间": self.sample_range,
            "区域": self.region,
            "类型": self.sample_type,
            "渠道": self.channel,
            "目标度量": self.metric,
            "对比基准": self.baseline,
        }


class ExperimentService:
    """分析实验服务：样本生成、摘要预聚合、分页与版本管理。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._samples: list[ExperimentSample] = []
        self._sample_fingerprint = ""
        self._snapshots: dict[str, dict[str, Any]] = {}
        self._filter_snapshots: dict[str, str] = {}
        self._versions: list[dict[str, Any]] = []
        self._version_seq = 0

    # ------------------------------------------------------------------
    # 实验样本
    # ------------------------------------------------------------------
    def _ensure_samples(self) -> None:
        """按实验方案生成确定性的样本；同一份输入每次都得到同一批样本。"""
        schemes = store.rows(MODULE)
        fingerprint = "|".join(
            f"{row.get('id')}:{row.get('方案编号')}:{row.get('实验名称')}" for row in schemes
        )
        if self._samples and fingerprint == self._sample_fingerprint:
            return

        samples: list[ExperimentSample] = []
        next_id = 1
        for scheme in schemes:
            experiment_id = int(scheme.get("id", 0) or 0)
            experiment_no = str(scheme.get("方案编号", f"EXPE-{experiment_id:04d}"))
            experiment_name = str(scheme.get("实验名称", ""))
            for range_index, sample_range in enumerate(SAMPLE_RANGES):
                rng = random.Random(f"{SAMPLE_ORDER_VERSION}:{experiment_id}:{experiment_no}:{sample_range}")
                regions = DIMENSIONS["区域"]
                types = DIMENSIONS["类型"]
                channels = DIMENSIONS["渠道"]
                for index in range(SAMPLES_PER_RANGE):
                    offset = 0
                    for position, count in enumerate(REGION_SAMPLE_COUNTS):
                        if index < offset + count:
                            region = regions[position % len(regions)]
                            break
                        offset += count
                    sample_type = types[(index // 2 + range_index) % len(types)]
                    channel = channels[(index // 3 + range_index) % len(channels)]
                    metric = round(rng.uniform(0.58, 0.97), 4)
                    baseline = round(min(0.99, max(0.45, metric + rng.uniform(-0.09, 0.05))), 4)
                    samples.append(ExperimentSample(
                        id=next_id,
                        experiment_id=experiment_id,
                        experiment_no=experiment_no,
                        experiment_name=experiment_name,
                        sample_range=sample_range,
                        region=region,
                        sample_type=sample_type,
                        channel=channel,
                        metric=metric,
                        baseline=baseline,
                    ))
                    next_id += 1

        self._samples = samples
        self._sample_fingerprint = fingerprint
        # 样本变化后只让筛选缓存失效；历史版本的 snapshot 仍然保留，回退可追溯。
        self._filter_snapshots.clear()

    def _filtered_samples(self, filters: SampleFilter) -> list[ExperimentSample]:
        self._ensure_samples()
        rows = self._samples
        if filters.experiment_no:
            needle = filters.experiment_no.casefold()
            rows = [
                row for row in rows
                if needle in row.experiment_no.casefold() or needle in row.experiment_name.casefold()
            ]
        sample_range = filters.effective_range()
        if sample_range:
            rows = [row for row in rows if row.sample_range == sample_range]
        if filters.keyword:
            needle = filters.keyword.casefold()
            rows = [
                row for row in rows
                if needle in row.experiment_no.casefold()
                or needle in row.experiment_name.casefold()
                or needle in row.region.casefold()
                or needle in row.sample_type.casefold()
                or needle in row.channel.casefold()
            ]
        for field, expected in filters.dimensions:
            if field == "__group_value__":
                group_field = filters.effective_group_field()
                rows = [row for row in rows if row.dimension(group_field).casefold() == expected.casefold()]
            else:
                rows = [row for row in rows if row.dimension(field).casefold() == expected.casefold()]
        return rows

    # ------------------------------------------------------------------
    # 分组摘要预聚合
    # ------------------------------------------------------------------
    def _build_group_rows(self, filters: SampleFilter, snapshot_id: str) -> tuple[list[dict[str, Any]], int]:
        samples = self._filtered_samples(filters)
        group_field = filters.effective_group_field()
        range_label = filters.effective_range() or "全部区间"
        grouped: dict[tuple[str, str, str, str], list[ExperimentSample]] = {}
        for sample in samples:
            group_value = sample.dimension(group_field) if group_field in DIMENSIONS else "全量"
            key = (sample.experiment_no, sample.experiment_name, group_field, group_value)
            grouped.setdefault(key, []).append(sample)

        total_samples = len(samples)
        rows: list[dict[str, Any]] = []
        for (experiment_no, experiment_name, field, group_value), bucket in grouped.items():
            sample_count = len(bucket)
            metric_mean = round(sum(item.metric for item in bucket) / sample_count, 4)
            baseline_mean = round(sum(item.baseline for item in bucket) / sample_count, 4)
            group_key = f"{experiment_no}|{range_label}|{field}|{group_value}"
            row_id = "G" + hashlib.sha1(group_key.encode("utf-8")).hexdigest()[:10].upper()
            rows.append({
                "id": row_id,
                "方案编号": experiment_no,
                "实验名称": experiment_name,
                "样本区间": range_label,
                "分组字段": field,
                "分组值": group_value,
                "分组": group_value,
                "样本量": sample_count,
                "样本占比": round(sample_count / total_samples, 4) if total_samples else 0,
                "指标值": metric_mean,
                "指标均值": metric_mean,
                "对比基准": baseline_mean,
                "差异": round(metric_mean - baseline_mean, 4),
                "指标总和": round(sum(item.metric for item in bucket), 4),
                "口径编号": snapshot_id,
                "摘要版本": snapshot_id,
            })
        return self._sort_rows(rows, None, None), total_samples

    @staticmethod
    def _sort_rows(
        rows: Iterable[dict[str, Any]], sort: str | None, order: str | None,
    ) -> list[dict[str, Any]]:
        """固定默认次序，显式排序也带稳定兜底，避免翻页时行位置漂移。"""
        result = list(rows)
        aliases = {
            "样本量": "样本量",
            "指标值": "指标均值",
            "指标均值": "指标均值",
            "指标总和": "指标总和",
            "差异": "差异",
            "分组": "分组值",
            "分组值": "分组值",
            "方案编号": "方案编号",
            "实验名称": "实验名称",
            "样本区间": "样本区间",
            "分组字段": "分组字段",
        }
        field = aliases.get(sort or "")
        tiebreak = lambda row: (  # noqa: E731 - 稳定兜底，保证同值行跨页不漂移
            str(row.get("方案编号", "")),
            str(row.get("分组字段", "")),
            str(row.get("分组值", "")),
        )
        if field:
            def key(row: dict[str, Any]) -> tuple[int, Any]:
                value = row.get(field)
                if isinstance(value, (int, float)):
                    return (0, float(value))
                return (1, str(value))
            # 先按默认次序排一遍，再按目标字段排序，等值行保留既有次序。
            result.sort(key=tiebreak)
            result.sort(key=key, reverse=(order or "").lower() == "desc")
        else:
            result.sort(key=tiebreak)
        return result

    # ------------------------------------------------------------------
    # Snapshot / 口径
    # ------------------------------------------------------------------
    def _snapshot_for(self, filters: SampleFilter) -> dict[str, Any]:
        """按筛选条件复用分组摘要；相同条件固定同一份样本口径。"""
        self._ensure_samples()
        canonical = json.dumps(filters.canonical(), ensure_ascii=False, sort_keys=True)
        cache_key = f"{canonical}|{self._sample_fingerprint}"
        snapshot_id = self._filter_snapshots.get(cache_key)
        if snapshot_id and snapshot_id in self._snapshots:
            return self._snapshots[snapshot_id]

        snapshot_id = "SNAP-" + hashlib.sha1(
            f"{SAMPLE_ORDER_VERSION}|{cache_key}".encode("utf-8")
        ).hexdigest()[:12].upper()
        if snapshot_id in self._snapshots:
            self._filter_snapshots[cache_key] = snapshot_id
            return self._snapshots[snapshot_id]

        rows, total_samples = self._build_group_rows(filters, snapshot_id)
        snapshot = {
            "snapshot_id": snapshot_id,
            "filters": filters.canonical(),
            "rows": rows,
            "total_samples": total_samples,
            "total_groups": len(rows),
            "created_at": _now(),
            "checksum": hashlib.sha1(
                json.dumps(rows, ensure_ascii=False, sort_keys=True).encode("utf-8")
            ).hexdigest(),
        }
        self._snapshots[snapshot_id] = snapshot
        self._filter_snapshots[cache_key] = snapshot_id
        return snapshot

    def _resolve_snapshot(
        self, filters: SampleFilter, snapshot: str | None = None, version: int | str | None = None,
    ) -> dict[str, Any] | None:
        candidates: list[dict[str, Any]] = []
        if snapshot:
            found = self._snapshots.get(str(snapshot))
            if found:
                candidates.append(found)
            elif str(snapshot).isdigit():
                # 兼容把版本号当口径编号传入的调用。
                by_version = self._snapshot_of_version(int(snapshot))
                if by_version:
                    candidates.append(by_version)
        if version is not None and str(version).isdigit():
            by_version = self._snapshot_of_version(int(version))
            if by_version:
                candidates.append(by_version)
        for candidate in candidates:
            # 筛选条件已切换时不再复用旧 snapshot，避免串到另一批样本。
            if not filters.has_conditions() or self._snapshot_matches(candidate, filters):
                return candidate
        return None

    def _snapshot_of_version(self, version_id: int) -> dict[str, Any] | None:
        version = self._find_version(version_id)
        if version is None:
            return None
        return self._snapshots.get(str(version.get("snapshot_id")))

    @staticmethod
    def _snapshot_matches(snapshot: dict[str, Any], filters: SampleFilter) -> bool:
        stored = snapshot.get("filters") or {}
        return stored == filters.canonical()

    # ------------------------------------------------------------------
    # 查询与分页
    # ------------------------------------------------------------------
    def query_summary(
        self,
        filters: dict[str, Any] | None = None,
        *,
        snapshot: str | None = None,
        version: int | str | None = None,
        page: int = 1,
        size: int = 20,
        sort: str | None = None,
        order: str | None = None,
    ) -> dict[str, Any]:
        """查询分组摘要。翻页时传入 snapshot，样本口径与首次查询完全一致。"""
        parsed = SampleFilter.from_mapping(filters)
        with self._lock:
            found = self._resolve_snapshot(parsed, snapshot=snapshot, version=version)
            snap = found or self._snapshot_for(parsed)
            rows = self._sort_rows(snap["rows"], sort, order)
            total = len(rows)
            page = max(1, int(page or 1))
            size = max(1, min(int(size or 20), 500))
            start = (page - 1) * size
            items = rows[start:start + size]
            main = self._main_version()
            metrics = {
                "样本总量": int(snap["total_samples"]),
                "分组数量": int(snap["total_groups"]),
                "实验数量": len({row.get("方案编号") for row in rows}),
                "摘要版本": int(main["version"]) if main else 0,
            }
            return {
                "items": items,
                "groups": items,
                "total": total,
                "page": page,
                "size": size,
                "snapshot": snap["snapshot_id"],
                "snapshot_id": snap["snapshot_id"],
                "口径编号": snap["snapshot_id"],
                "filters": snap["filters"],
                "metrics": metrics,
                "version": dict(main) if main else None,
                "row_count": total,
                "sample_count": int(snap["total_samples"]),
            }

    def list_samples(
        self, filters: dict[str, Any] | None = None, *, page: int = 1, size: int = 20,
    ) -> dict[str, Any]:
        """只读实验样本，方便核对摘要与样本口径是否来自同一批数据。"""
        parsed = SampleFilter.from_mapping(filters)
        with self._lock:
            rows = self._filtered_samples(parsed)
            total = len(rows)
            page = max(1, int(page or 1))
            size = max(1, min(int(size or 20), 500))
            start = (page - 1) * size
            items = [row.as_row() for row in rows[start:start + size]]
            return {"items": items, "total": total, "page": page, "size": size}

    # ------------------------------------------------------------------
    # 版本持久化 / 回退 / 冲突核对
    # ------------------------------------------------------------------
    def _find_version(self, version_id: int) -> dict[str, Any] | None:
        for version in self._versions:
            if int(version.get("id", 0)) == int(version_id):
                return version
        return None

    def _main_version(self) -> dict[str, Any] | None:
        for version in self._versions:
            if version.get("main"):
                return version
        return None

    def _archive_active_versions(self) -> None:
        for version in self._versions:
            if version.get("main"):
                version["main"] = False
                version["status"] = VERSION_ARCHIVED
                version["archived_at"] = _now()

    def _create_version(
        self,
        snapshot: dict[str, Any],
        *,
        status: str,
        main: bool,
        base_version: int | None,
        remark: str | None = None,
    ) -> dict[str, Any]:
        if main:
            self._archive_active_versions()
        self._version_seq += 1
        version_no = max((int(item.get("version", 0)) for item in self._versions), default=0) + 1
        version = {
            "id": self._version_seq,
            "version": version_no,
            "摘要版本": f"v{version_no}",
            "status": status,
            "main": main,
            "snapshot_id": snapshot["snapshot_id"],
            "snapshot": snapshot["snapshot_id"],
            "口径编号": snapshot["snapshot_id"],
            "filters": snapshot["filters"],
            "row_count": int(snapshot["total_groups"]),
            "sample_count": int(snapshot["total_samples"]),
            "checksum": snapshot["checksum"],
            "source": "实验样本",
            "source_snapshot": snapshot["snapshot_id"],
            "base_version": base_version,
            "remark": remark or "",
            "created_at": _now(),
            "conflict": status == VERSION_CONFLICT,
        }
        self._versions.append(version)
        return version

    def persist_summary(
        self,
        values: dict[str, Any] | None = None,
        *,
        base_version: int | str | None = None,
        snapshot: str | None = None,
        remark: str | None = None,
    ) -> dict[str, Any]:
        """消费实验样本摘要并持久化；主版本边界不一致时落一条冲突待核对记录。"""
        values = dict(values or {})
        filters = SampleFilter.from_mapping(values)
        requested_base: int | None = None
        raw_base = base_version if base_version is not None else _pick(values, *BASE_VERSION_KEYS)
        if raw_base is not None:
            try:
                requested_base = int(str(raw_base))
            except (TypeError, ValueError):
                requested_base = None
        requested_snapshot = snapshot or _pick(values, *SNAPSHOT_KEYS)

        with self._lock:
            found = self._resolve_snapshot(filters, snapshot=requested_snapshot)
            snap = found or self._snapshot_for(filters)
            current = self._main_version()
            current_version = int(current.get("version", 0)) if current else 0

            if requested_base is not None and requested_base != current_version:
                version = self._create_version(
                    snap,
                    status=VERSION_CONFLICT,
                    main=False,
                    base_version=requested_base,
                    remark=remark or "主版本边界不一致，等待核对",
                )
                return {
                    "ok": False,
                    "conflict": True,
                    "message": (
                        f"主版本已变化：期望 v{requested_base}，当前 v{current_version}，"
                        "摘要已记录为冲突待核对"
                    ),
                    "version": dict(version),
                    "snapshot": snap["snapshot_id"],
                    "snapshot_id": snap["snapshot_id"],
                }

            version = self._create_version(
                snap,
                status=VERSION_ACTIVE,
                main=True,
                base_version=current_version or None,
                remark=remark,
            )
            return {
                "ok": True,
                "conflict": False,
                "message": f"实验样本摘要已持久化为 v{version['version']}",
                "version": dict(version),
                "snapshot": snap["snapshot_id"],
                "snapshot_id": snap["snapshot_id"],
                "row_count": version["row_count"],
                "sample_count": version["sample_count"],
            }

    def list_versions(self) -> dict[str, Any]:
        with self._lock:
            items = [dict(version) for version in reversed(self._versions)]
            return {
                "items": items,
                "total": len(items),
                "main": dict(self._main_version()) if self._main_version() else None,
            }

    def get_version(self, version_id: int) -> dict[str, Any] | None:
        with self._lock:
            version = self._find_version(version_id)
            if version is None:
                return None
            snapshot = self._snapshots.get(str(version.get("snapshot_id")))
            payload = dict(version)
            payload["items"] = list(snapshot["rows"]) if snapshot else []
            payload["total"] = len(payload["items"])
            payload["filters"] = snapshot["filters"] if snapshot else version.get("filters", {})
            return payload

    def activate_version(self, version_id: int) -> tuple[dict[str, Any] | None, str]:
        """回退/采纳一个摘要版本，并保证最终只有一个有效主版本。"""
        with self._lock:
            target = self._find_version(version_id)
            if target is None:
                return None, f"摘要版本 {version_id} 不存在"
            if target.get("status") == VERSION_CONFLICT:
                return None, f"摘要版本 {version_id} 处于冲突待核对状态，请先核对冲突"
            self._archive_active_versions()
            target["main"] = True
            target["status"] = VERSION_ACTIVE
            target["conflict"] = False
            target["activated_at"] = _now()
            return dict(target), f"已回退到摘要版本 v{target.get('version')}"

    def resolve_conflict(self, version_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        """核对冲突结果：采纳后成为有效主版本，弃用后归档。"""
        with self._lock:
            target = self._find_version(version_id)
            if target is None:
                return None, f"摘要版本 {version_id} 不存在"
            if target.get("status") != VERSION_CONFLICT:
                return None, f"摘要版本 {version_id} 不是冲突待核对记录"
            normalized = action.strip()
            if normalized in {"采纳", "激活", "adopt", "approve", "accept"}:
                self._archive_active_versions()
                target["main"] = True
                target["status"] = VERSION_ACTIVE
                target["conflict"] = False
                target["activated_at"] = _now()
                return dict(target), f"冲突已采纳，摘要版本 v{target.get('version')} 生效"
            if normalized in {"弃用", "驳回", "reject", "discard", "ignore"}:
                target["main"] = False
                target["status"] = VERSION_ARCHIVED
                target["conflict"] = False
                target["resolved_at"] = _now()
                return dict(target), f"冲突已弃用，摘要版本 v{target.get('version')} 归档"
            return None, f"动作「{action}」不属于冲突核对可执行范围"

    def list_conflicts(self) -> dict[str, Any]:
        with self._lock:
            items = [
                dict(version) for version in reversed(self._versions)
                if version.get("status") == VERSION_CONFLICT
            ]
            return {"items": items, "total": len(items)}

    # ------------------------------------------------------------------
    # 结果运行 / 导出
    # ------------------------------------------------------------------
    def run_analysis(self, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """运行实验：预聚合分组摘要、持久化摘要版本并返回首屏结果。"""
        values = dict(params or {})
        persisted = self.persist_summary(values)
        query = self.query_summary(
            values,
            snapshot=persisted["snapshot_id"],
            page=1,
            size=max(50, int(persisted.get("row_count") or 0) or 50),
        )
        return {
            **query,
            "ok": bool(persisted.get("ok")),
            "message": persisted.get("message", ""),
            "conflict": bool(persisted.get("conflict")),
            "version": persisted.get("version"),
        }

    def export_rows(
        self,
        filters: dict[str, Any] | None = None,
        *,
        snapshot: str | None = None,
        version: int | str | None = None,
        sort: str | None = None,
        order: str | None = None,
    ) -> tuple[list[dict[str, Any]], str]:
        """导出与页面同一份 snapshot 的全量分组摘要，而不是当前页。"""
        parsed = SampleFilter.from_mapping(filters)
        with self._lock:
            found = self._resolve_snapshot(parsed, snapshot=snapshot, version=version)
            snap = found or self._snapshot_for(parsed)
            return self._sort_rows(snap["rows"], sort, order), snap["snapshot_id"]

    # ------------------------------------------------------------------
    # 其它读取
    # ------------------------------------------------------------------
    def kpi_metrics(self) -> list[dict[str, Any]]:
        with self._lock:
            self._ensure_samples()
            snapshot = self._snapshot_for(SampleFilter(group_field=DEFAULT_GROUP_FIELD))
            main = self._main_version()
            return [
                {"label": "实验样本", "value": int(snapshot["total_samples"]), "trend": 6},
                {"label": "分组摘要", "value": int(snapshot["total_groups"]), "trend": 3},
                {"label": "生效版本", "value": int(main["version"]) if main else 0, "trend": 1},
            ]

    def metadata(self) -> dict[str, Any]:
        with self._lock:
            self._ensure_samples()
            return {
                "sample_ranges": list(SAMPLE_RANGES),
                "group_fields": list(GROUP_FIELDS),
                "dimensions": {name: list(values) for name, values in DIMENSIONS.items()},
                "defaults": {
                    "样本区间": DEFAULT_SAMPLE_RANGE,
                    "分组字段": DEFAULT_GROUP_FIELD,
                },
                "experiments": [
                    {
                        "id": int(row.get("id", 0)),
                        "方案编号": row.get("方案编号"),
                        "实验名称": row.get("实验名称"),
                    }
                    for row in store.rows(MODULE)
                ],
                "sample_total": len(self._samples),
            }

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)


# 进程内单例：路由层与其它服务模块共用同一份样本口径与摘要版本。
experiment_service = ExperimentService()
