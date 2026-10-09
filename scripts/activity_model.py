"""
麦动平衡 · 运动消耗 → 加餐额度模型
M-Balance / activity_model.py

设计原则
--------
1. 学术依据：采用 ACSM（美国运动医学会）代谢当量公式
       kcal = MET × 体重(kg) × 时长(小时)
   MET（Metabolic Equivalent of Task）值取自《Compendium of Physical
   Activities》的常用区间中位值，用于日常估算而非医学用途。

2. 健康伦理：不鼓励"练多少吃多少"。运动后食欲上升、且运动消耗常被高估，
   因此默认只把消耗的 60% 折算为加餐额度（`credit_ratio=0.6`），
   并把单次加餐额度硬性封顶（`max_credit_kcal`，默认 700 kcal）。

3. 免责：本模块输出为膳食参考量级估算，不构成医疗、营养或运动处方建议。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional

# --------------------------------------------------------------------------
# METs 表：MET 值参考《Compendium of Physical Activities》常用中位值
# key 为规范化后的运动标识，alias 为用户可能说出的口语说法
# --------------------------------------------------------------------------
ACTIVITY_TABLE: Dict[str, Dict] = {
    "running_8kmh":   {"label": "跑步 · 8km/h（配速 7'30\"）", "met": 8.3,  "aliases": ["跑步", "慢跑", "跑", "run", "jog"]},
    "running_10kmh":  {"label": "跑步 · 10km/h（配速 6'00\"）", "met": 9.8, "aliases": ["快跑", "跑10", "配速6"]},
    "running_12kmh":  {"label": "跑步 · 12km/h（配速 5'00\"）", "met": 11.8, "aliases": ["冲刺跑", "跑12", "配速5"]},
    "walking":        {"label": "快走 · 5.6km/h", "met": 4.3,  "aliases": ["走路", "散步", "健走", "遛弯", "快步走", "快走", "步数", "walk"]},
    "cycling":        {"label": "骑行 · 通勤速度", "met": 6.8,  "aliases": ["骑车", "骑行", "单车", "动感单车", "bike"]},
    "swimming":       {"label": "游泳 · 自由泳休闲", "met": 7.0, "aliases": ["游泳", "自由泳", "蛙泳", "swim"]},
    "jump_rope":      {"label": "跳绳 · 中等速度", "met": 11.0, "aliases": ["跳绳", "rope"]},
    "strength":       {"label": "力量训练 · 器械/自重", "met": 5.0, "aliases": ["力量", "撸铁", "健身", "举铁", "无氧", "weights", "gym"]},
    "hiit":           {"label": "HIIT / 循环训练", "met": 8.5, "aliases": ["hiit", "燃脂", "tabata", "循环训练", "帕梅拉"]},
    "yoga":           {"label": "瑜伽 · 哈他", "met": 2.5, "aliases": ["瑜伽", "拉伸", "yoga"]},
    "pilates":        {"label": "普拉提", "met": 3.0, "aliases": ["普拉提", "pilates"]},
    "basketball":     {"label": "篮球 · 半场对抗", "met": 6.5, "aliases": ["篮球", "打球", "basketball"]},
    "football":       {"label": "足球 · 休闲", "met": 7.0, "aliases": ["足球", "踢球", "football"]},
    "badminton":      {"label": "羽毛球", "met": 5.5, "aliases": ["羽毛球", "打球", "badminton"]},
    "pingpong":       {"label": "乒乓球", "met": 4.0, "aliases": ["乒乓", "乒乓球", "pingpong"]},
    "tennis":         {"label": "网球", "met": 7.3, "aliases": ["网球", "tennis"]},
    "dance":          {"label": "舞蹈 / 健身操", "met": 5.5, "aliases": ["跳舞", "舞蹈", "健身操", "尊巴", "zumba", "dance"]},
    "climbing":       {"label": "攀岩 / 抱石", "met": 8.0, "aliases": ["攀岩", "抱石", "climb"]},
    "rowing":         {"label": "划船机", "met": 7.0, "aliases": ["划船", "赛艇", "rowing"]},
    "elliptical":     {"label": "椭圆机", "met": 5.0, "aliases": ["椭圆机", "elliptical"]},
    "stairs":         {"label": "爬楼梯", "met": 8.8, "aliases": ["爬楼", "爬楼梯", "stairs"]},
    "skating":        {"label": "轮滑 / 滑冰", "met": 7.0, "aliases": ["滑冰", "轮滑", "skate"]},
    "housework":      {"label": "家务 · 大扫除", "met": 3.3, "aliases": ["家务", "打扫", "拖地"]},
    "housework_light":{"label": "家务 · 轻度整理", "met": 2.3, "aliases": ["整理", "收拾", "叠衣服"]},
    "dog_walking":    {"label": "遛狗", "met": 3.0, "aliases": ["遛狗", "遛猫"]},
    "gardening":      {"label": "园艺 / 种花", "met": 3.8, "aliases": ["园艺", "种花", "浇花"]},
}

# 反向索引：口语说法 -> 规范化 key
_ALIAS_INDEX: Dict[str, str] = {}
for _key, _meta in ACTIVITY_TABLE.items():
    _ALIAS_INDEX[_key.lower()] = _key
    _ALIAS_INDEX[_meta["label"].lower()] = _key
    for _a in _meta["aliases"]:
        _ALIAS_INDEX[_a.lower()] = _key


class ActivityError(ValueError):
    """运动描述无法识别或参数不合法。"""


def list_activities() -> List[Dict]:
    """返回支持的运动清单，供 Skill 展示。"""
    return [
        {"id": k, "label": v["label"], "met": v["met"], "aliases": v["aliases"]}
        for k, v in ACTIVITY_TABLE.items()
    ]


def resolve_activity(text: str) -> Optional[str]:
    """
    把用户口语化的运动描述解析为规范化 key。
    先精确匹配，再按包含关系做模糊匹配（长别名优先，避免"跑步"误配"跳绳"）。
    """
    if not text:
        return None
    t = text.strip().lower()
    if t in _ALIAS_INDEX:
        return _ALIAS_INDEX[t]
    hits = [(alias, key) for alias, key in _ALIAS_INDEX.items() if alias in t]
    if not hits:
        return None
    # 别名越长越精确，优先
    hits.sort(key=lambda x: len(x[0]), reverse=True)
    return hits[0][1]


@dataclass
class ActivityEntry:
    """一条运动记录。"""
    activity: str
    minutes: float
    resolved_key: str = ""

    def __post_init__(self) -> None:
        key = resolve_activity(self.activity) if self.activity else None
        if key is None:
            raise ActivityError(
                f"无法识别的运动：{self.activity!r}。"
                f"可用的运动包括：{', '.join(sorted(ACTIVITY_TABLE))}"
            )
        self.resolved_key = key


@dataclass
class BurnResult:
    """运动消耗汇总结果。"""
    total_burn_kcal: float
    credit_kcal: float
    credit_ratio: float
    capped: bool
    breakdown: List[Dict] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)


def estimate_burn(
    entries: Iterable[ActivityEntry],
    weight_kg: float = 65.0,
) -> float:
    """
    计算总消耗：kcal = Σ MET × 体重(kg) × 时长(小时)
    未提供体重时按 65kg 估算。
    """
    if weight_kg <= 0:
        raise ActivityError("体重必须为正数")
    total = 0.0
    for e in entries:
        met = ACTIVITY_TABLE[e.resolved_key]["met"]
        total += met * weight_kg * (e.minutes / 60.0)
    return round(total, 1)


def compute_credit(
    entries: Iterable[ActivityEntry],
    weight_kg: float = 65.0,
    credit_ratio: float = 0.6,
    max_credit_kcal: float = 700.0,
) -> BurnResult:
    """
    把运动消耗折算为"加餐额度"。

    credit_ratio     默认 0.6 —— 保守折算，只奖励一半多的消耗，
                     原因是运动后摄入往往被低估、消耗往往被高估。
    max_credit_kcal  单次加餐额度封顶，防止"用运动换暴食"。
    """
    entries = list(entries)
    if not entries:
        raise ActivityError("至少需要一条运动记录")
    if not 0 < credit_ratio <= 1:
        raise ActivityError("credit_ratio 必须在 (0, 1] 区间内")
    if max_credit_kcal <= 0:
        raise ActivityError("max_credit_kcal 必须为正数")

    breakdown = []
    total = 0.0
    for e in entries:
        meta = ACTIVITY_TABLE[e.resolved_key]
        kcal = meta["met"] * weight_kg * (e.minutes / 60.0)
        total += kcal
        breakdown.append({
            "activity": meta["label"],
            "met": meta["met"],
            "minutes": e.minutes,
            "burn_kcal": round(kcal, 1),
        })

    raw_credit = total * credit_ratio
    capped = raw_credit > max_credit_kcal
    credit = min(raw_credit, max_credit_kcal)

    notes = [
        f"按体重 {weight_kg:g}kg、保守折算系数 {credit_ratio:g} 计算加餐额度。",
        "运动消耗为量级估算，个体代谢差异较大，仅供参考。",
    ]
    if capped:
        notes.append(
            f"本次折算额度已触及单次上限 {max_credit_kcal:g} kcal，"
            "超出部分不再计入——练得多不等于该吃得多。"
        )

    return BurnResult(
        total_burn_kcal=round(total, 1),
        credit_kcal=round(credit, 1),
        credit_ratio=credit_ratio,
        capped=capped,
        breakdown=breakdown,
        notes=notes,
    )
