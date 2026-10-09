"""
麦动平衡 · 额度最大化组合优化引擎
M-Balance / value_engine.py

与"省钱类"工具的根本区别
------------------------
省钱工具的优化目标是   min(实付金额)
本引擎的优化目标是     max(吃到额度比例)  在预算内，把"挣来的"热量吃到
                     s.t. 实付金额 ≤ 预算
                          总热量 ≤ 加餐额度

这是一个带双约束的背包变体。额外的健康约束：
  * 脂肪供能比过高时，强制搭配"解腻项"（黑咖 / 无糖茶 / 零度可乐）
  * 钠摄入超过阈值时给出提示

排序键：主排序 = 额度利用率（越高越好），次排序 = 实付金额（越低越好）
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

# --------------------------------------------------------------------------
# 健康阈值（参考中国居民膳食指南的餐次分摊，仅作量级参考）
# --------------------------------------------------------------------------
FAT_ENERGY_RATIO_WARN = 0.45      # 脂肪供能比警戒线
SODIUM_WARN_MG = 1200.0           # 单餐钠警戒线
MAX_ITEMS_PER_COMBO = 4           # 单方案最多餐品数，避免堆砌
MAX_COUPONS_PER_ORDER = 1         # 麦当劳券通常一单一券，按 1 张建模

# 主食属性：一餐的"饱腹主体"。缺乏主食的方案（如只点甜品+咖啡）在现实中
# 并不成立，即使数字上吃满了额度也不算好方案，因此在排序中降权。
MEAL_ANCHOR_KEYWORDS = ("汉堡", "堡", "卷", "鸡", "鱼", "牛", "沙拉", "饭", "粥", "派")
DESSERT_KEYWORDS = ("麦旋风", "冰淇淋", "圣代", "派", "蛋糕", "甜品", "新地")

# 「解腻项」关键词：识别低热量、零糖、可平衡脂肪感的饮品
REFRESHING_KEYWORDS = ("黑咖", "美式", "无糖", "零度", "零糖", "纯茶", "矿泉水", "气泡水")


@dataclass
class Product:
    """一个可售餐品。nutrition 与 price 均由 MCP 实时返回。"""
    code: str
    name: str
    price: float                       # 原价（元）
    kcal: float = 0.0
    protein_g: float = 0.0
    fat_g: float = 0.0
    carb_g: float = 0.0
    sodium_mg: float = 0.0
    category: str = ""
    is_drink: bool = False
    nutrition_known: bool = True       # 营养库未覆盖时为 False

    @property
    def fat_energy_ratio(self) -> float:
        """脂肪供能比 = 脂肪克数 × 9 / 总热量"""
        if self.kcal <= 0:
            return 0.0
        return (self.fat_g * 9.0) / self.kcal

    @property
    def is_refreshing(self) -> bool:
        return any(k in self.name for k in REFRESHING_KEYWORDS)

    @property
    def is_anchor(self) -> bool:
        """是否可担任一餐的主食主体（不是纯饮品/纯甜品）。"""
        if self.is_drink or self.is_dessert:
            return False
        return any(k in self.name for k in MEAL_ANCHOR_KEYWORDS)

    @property
    def is_dessert(self) -> bool:
        return any(k in self.name for k in DESSERT_KEYWORDS)


@dataclass
class Coupon:
    """一张可用优惠券。"""
    coupon_id: str
    name: str
    coupon_type: str = "discount"      # discount(满减) / exchange(兑换) / percent(折扣)
    threshold: float = 0.0             # 满减门槛
    amount: float = 0.0                # 减免金额
    percent: float = 0.0               # 折扣率，如 0.7 表示 7 折
    max_discount: float = 0.0          # 折扣上限
    exchange_kcal_cap: float = 0.0     # 兑换券：可兑换餐品热量上限
    applicable_codes: Tuple[str, ...] = ()   # 限定餐品编码；空表示全场
    source: str = "my"                 # my / store / maimai
    expires_at: str = ""

    def applies_to(self, code: str) -> bool:
        if not self.applicable_codes:
            return True
        return code in self.applicable_codes


@dataclass
class Plan:
    """一个候选点餐方案。"""
    products: List[Product]
    coupon: Optional[Coupon]
    subtotal: float
    discount: float
    payable: float
    total_kcal: float
    total_protein_g: float
    total_fat_g: float
    total_carb_g: float
    total_sodium_mg: float
    credit_kcal: float
    budget: float
    credit_usage: float = 0.0          # 额度利用率
    protein_ratio: float = 0.0         # 蛋白质供能比
    anchor_count: int = 0              # 主食件数
    digestibility: float = 0.0         # 饱腹合理性得分 0~1
    warnings: List[str] = field(default_factory=list)
    verified: bool = False             # 是否经 calculate-price 复核
    price_source: str = "local"        # local(本地估算) / mcp(服务端核价)

    def to_dict(self) -> Dict:
        return {
            "products": [
                {"code": p.code, "name": p.name, "price": round(p.price, 2),
                 "kcal": p.kcal, "nutrition_known": p.nutrition_known}
                for p in self.products
            ],
            "coupon": ({"coupon_id": self.coupon.coupon_id, "name": self.coupon.name,
                        "type": self.coupon.coupon_type} if self.coupon else None),
            "subtotal": round(self.subtotal, 2),
            "discount": round(self.discount, 2),
            "payable": round(self.payable, 2),
            "total_kcal": round(self.total_kcal, 1),
            "total_protein_g": round(self.total_protein_g, 1),
            "total_fat_g": round(self.total_fat_g, 1),
            "total_carb_g": round(self.total_carb_g, 1),
            "total_sodium_mg": round(self.total_sodium_mg, 1),
            "credit_kcal": round(self.credit_kcal, 1),
            "credit_usage": round(self.credit_usage, 3),
            "protein_ratio": round(self.protein_ratio, 3),
            "anchor_count": self.anchor_count,
            "digestibility": round(self.digestibility, 3),
            "budget": round(self.budget, 2),
            "warnings": self.warnings,
            "verified": self.verified,
            "price_source": self.price_source,
        }


class NoFeasiblePlan(Exception):
    """在给定额度与预算下找不到可行方案。"""


# --------------------------------------------------------------------------
# 优惠券结算
# --------------------------------------------------------------------------
def apply_coupon(products: Sequence[Product], coupon: Optional[Coupon]) -> Tuple[float, float]:
    """
    返回 (优惠金额, 实际抵扣的餐品原价)。
    兑换券按"抵扣该餐品原价"处理，且只能抵一单餐品。
    """
    subtotal = sum(p.price for p in products)
    if coupon is None:
        return 0.0, 0.0

    if coupon.coupon_type == "discount":
        # 限定餐品的满减券：购物车内必须真的含有该餐品，否则券不生效。
        # 这是真实业务里最容易算错的地方——券的名字写着某个汉堡，
        # 但结算时如果没点这个汉堡，是不能抵扣的。
        if coupon.applicable_codes:
            if not any(coupon.applies_to(p.code) for p in products):
                return 0.0, 0.0
        if subtotal < coupon.threshold:
            return 0.0, 0.0
        return min(coupon.amount, subtotal), 0.0

    if coupon.coupon_type == "percent":
        # 限定餐品的折扣券同理：必须命中限定编码才生效
        if coupon.applicable_codes:
            if not any(coupon.applies_to(p.code) for p in products):
                return 0.0, 0.0
        discounted = subtotal * (1 - coupon.percent)
        if coupon.max_discount > 0:
            discounted = min(discounted, coupon.max_discount)
        return min(discounted, subtotal), 0.0

    if coupon.coupon_type == "exchange":
        # 兑换券：挑一张符合限定的、原价最高的餐品抵扣
        eligible = [
            p for p in products
            if coupon.applies_to(p.code)
            and (coupon.exchange_kcal_cap <= 0 or p.kcal <= coupon.exchange_kcal_cap)
        ]
        if not eligible:
            return 0.0, 0.0
        best = max(eligible, key=lambda p: p.price)
        return best.price, best.price

    return 0.0, 0.0


def _coupon_candidates(coupons: Iterable[Coupon], products: Sequence[Product]) -> List[Optional[Coupon]]:
    """筛出对本组合可能生效的券，并始终保留"不用券"选项。"""
    cands: List[Optional[Coupon]] = [None]
    for c in coupons:
        codes = {p.code for p in products}
        # 限定餐品的券：购物车必须真的含有对应餐品
        if c.applicable_codes and not (codes & set(c.applicable_codes)):
            continue
        if c.coupon_type == "discount" and sum(p.price for p in products) < c.threshold:
            continue
        cands.append(c)
    return cands


# --------------------------------------------------------------------------
# 方案构造与健康约束
# --------------------------------------------------------------------------
def _digestibility_score(products: Sequence[Product]) -> float:
    """
    饱腹合理性得分（0~1）。
    解决一个真实问题：纯按"吃满额度"排序，会把「麦旋风 + 美式咖啡」选成最优解——
    数字上没错，但没人拿这个当一餐。因此引入该得分作为排序前置项。

    评分构成：
      +0.5  有主食主体（汉堡/鸡/鱼/沙拉等）
      +0.2  蛋白质供能比 ≥ 20%
      -0.3  甜品件数 ≥ 1
      +0.3  有非酒精饮品（配饮合理）
    """
    if not products:
        return 0.0
    score = 0.0
    if any(p.is_anchor for p in products):
        score += 0.5
    total_kcal = sum(p.kcal for p in products)
    if total_kcal > 0:
        protein_ratio = sum(p.protein_g for p in products) * 4.0 / total_kcal
        if protein_ratio >= 0.20:
            score += 0.2
    if any(p.is_dessert for p in products):
        score -= 0.3
    if any(p.is_drink and not p.is_dessert for p in products):
        score += 0.3
    return round(max(0.0, min(1.0, score)), 3)


def _build_plan(
    products: List[Product],
    coupon: Optional[Coupon],
    credit_kcal: float,
    budget: float,
) -> Plan:
    subtotal = sum(p.price for p in products)
    discount, _ = apply_coupon(products, coupon)
    payable = round(subtotal - discount, 2)

    total_kcal = sum(p.kcal for p in products)
    total_protein = sum(p.protein_g for p in products)
    total_fat = sum(p.fat_g for p in products)
    total_carb = sum(p.carb_g for p in products)
    total_sodium = sum(p.sodium_mg for p in products)

    # 关键：warnings 必须在这里按最终 products 现场生成。
    # 早期版本把告警算在另一处并挂在缓存对象上，导致排序后返回的 Plan
    # warnings 为空——用户界面因此看不到任何健康提示。
    warnings: List[str] = []

    # 健康约束 1：脂肪供能比过高 → 提示搭配解腻项
    # 用 >= 且带 1e-9 容差：23g×9/460 在浮点下是 0.4499999...，
    # 严格比较会漏判"正好踩线"的情况。营养告警应当宁多勿少。
    if total_kcal > 0:
        fat_ratio = (total_fat * 9.0) / total_kcal
        if fat_ratio >= FAT_ENERGY_RATIO_WARN - 1e-9:
            has_refresh = any(p.is_refreshing for p in products)
            if not has_refresh:
                warnings.append(
                    f"脂肪供能比偏高（{fat_ratio:.0%}），建议加一杯黑咖或无糖茶解腻"
                )
    else:
        fat_ratio = 0.0

    # 健康约束 2：钠偏高
    if total_sodium >= SODIUM_WARN_MG - 1e-9:
        warnings.append(
            f"钠含量偏高（{total_sodium:.0f}mg），本餐注意少酱，另配无糖饮品"
        )

    # 健康约束 3：蛋白质过少
    if total_kcal > 0 and (total_protein * 4.0) / total_kcal < 0.15:
        warnings.append("蛋白质供能比偏低，若在增肌期建议再加一份鸡类产品")

    # 健康约束 4：营养数据缺失，需诚实披露
    unknown = [p.name for p in products if not p.nutrition_known]
    if unknown:
        warnings.append(f"以下餐品未匹配到营养数据，热量未计入：{'、'.join(unknown)}")

    usage = (total_kcal / credit_kcal) if credit_kcal > 0 else 1.0
    protein_ratio = (total_protein * 4.0 / total_kcal) if total_kcal > 0 else 0.0

    return Plan(
        products=list(products),
        coupon=coupon,
        subtotal=round(subtotal, 2),
        discount=round(discount, 2),
        payable=payable,
        total_kcal=round(total_kcal, 1),
        total_protein_g=round(total_protein, 1),
        total_fat_g=round(total_fat, 1),
        total_carb_g=round(total_carb, 1),
        total_sodium_mg=round(total_sodium, 1),
        credit_kcal=credit_kcal,
        budget=budget,
        credit_usage=usage,
        protein_ratio=protein_ratio,
        anchor_count=sum(1 for p in products if p.is_anchor),
        digestibility=_digestibility_score(products),
        warnings=warnings,
    )


def _is_healthy_combo(products: Sequence[Product]) -> bool:
    """
    硬性健康过滤：脂肪供能比超标且无解腻项的组直接淘汰。
    注意：热量未知（kcal=0 且 nutrition_known=False）的餐品不参与该判断，
    否则「没查到营养数据」会被误判成「健康」或「不健康」。
    """
    total_kcal = sum(p.kcal for p in products)
    if total_kcal <= 0:
        # 全部营养未知：无从判断，放行但在方案里标注
        return True
    if not any(p.nutrition_known for p in products):
        return True
    total_fat = sum(p.fat_g for p in products)
    if total_fat <= 0:
        return True
    if (total_fat * 9.0) / total_kcal >= FAT_ENERGY_RATIO_WARN:
        return any(p.is_refreshing for p in products)
    return True


# --------------------------------------------------------------------------
# 主入口
# --------------------------------------------------------------------------
def optimize(
    products: Sequence[Product],
    coupons: Sequence[Coupon],
    credit_kcal: float,
    budget: float,
    top_n: int = 3,
    min_credit_usage: float = 0.55,
) -> List[Plan]:
    """
    在 [总热量 ≤ credit_kcal] 且 [实付 ≤ budget] 双约束下，
    最大化额度利用率，返回 Top-N 方案。

    min_credit_usage：额度利用率下限。低于该值的方案被视为"没吃够"，
    若全部不达标则放宽约束并给出提示（由调用方处理）。
    """
    if credit_kcal <= 0:
        raise NoFeasiblePlan("加餐额度为 0，无法生成方案。先去动一动吧。")
    if budget <= 0:
        raise NoFeasiblePlan("预算必须大于 0。")

    # 只保留有价格的餐品
    pool = [p for p in products if p.price > 0]
    if not pool:
        raise NoFeasiblePlan("当前门店没有可售餐品，或菜单拉取失败。")

    pool.sort(key=lambda p: p.kcal, reverse=True)
    candidates: List[Plan] = []
    seen: set = set()

    for size in range(1, MAX_ITEMS_PER_COMBO + 1):
        for combo in itertools.combinations(pool, size):
            combo_list = list(combo)

            total_kcal = sum(p.kcal for p in combo_list)
            # 快速剪枝：热量超出额度直接跳过
            if total_kcal > credit_kcal:
                continue
            if not _is_healthy_combo(combo_list):
                continue

            subtotal = sum(p.price for p in combo_list)
            # 快速剪枝：即使最大优惠也不可能进预算
            if subtotal < budget * 0.0:
                continue

            for coupon in _coupon_candidates(coupons, combo_list):
                _, _ = subtotal, coupon
                discount, _ = apply_coupon(combo_list, coupon)
                payable = round(subtotal - discount, 2)
                if payable > budget + 1e-9:
                    continue

                key = (
                    tuple(sorted(p.code for p in combo_list)),
                    coupon.coupon_id if coupon else None,
                )
                if key in seen:
                    continue
                seen.add(key)

                plan = _build_plan(combo_list, coupon, credit_kcal, budget)
                if plan.credit_usage < min_credit_usage:
                    continue
                candidates.append(plan)

    if not candidates:
        # 降级：放宽利用率下限再试一次（只降级一次，避免无限递归）
        if min_credit_usage > 0:
            return optimize(
                products, coupons, credit_kcal, budget,
                top_n=top_n, min_credit_usage=0.0,
            )
        raise NoFeasiblePlan(
            f"在额度 {credit_kcal:.0f} kcal、预算 ¥{budget:.0f} 下找不到可满足健康约束的组合。"
            "可以试试抬高预算或增加运动量。"
        )

    # 排序（三级）：
    #   1. 饱腹合理性得分 —— 先保证这是个"像样的一餐"，而不是只吃满卡路里
    #   2. 额度利用率（降序）—— 在合理的前提下，尽量把额度花掉
    #   3. 实付金额（升序）—— 同样吃满时，花得更少更好
    candidates.sort(key=lambda p: (-p.digestibility, -p.credit_usage, p.payable, len(p.products)))

    # 质量兜底：健康过滤会淘汰高脂组合，若淘汰后剩余的最好方案反而比
    # "单独点一个有主食的餐品"还差，则把后者补回来。
    # 否则会出现「宁可推荐一份大薯条，也不推荐双层吉士汉堡」这种荒唐结果。
    best_digest = candidates[0].digestibility
    if best_digest < 0.5:
        for p in pool:
            if not p.is_anchor:
                continue
            for coupon in _coupon_candidates(coupons, [p]):
                discount, _ = apply_coupon([p], coupon)
                payable = round(p.price - discount, 2)
                if payable > budget + 1e-9:
                    continue
                if p.kcal > credit_kcal:
                    continue
                single = _build_plan([p], coupon, credit_kcal, budget)
                candidates.append(single)
        candidates.sort(key=lambda p: (-p.digestibility, -p.credit_usage, p.payable, len(p.products)))

    # 去重：同一组餐品只保留最优券组合
    deduped: List[Plan] = []
    seen_combos: set = set()
    for plan in candidates:
        combo_key = tuple(sorted(p.code for p in plan.products))
        if combo_key in seen_combos:
            continue
        seen_combos.add(combo_key)
        deduped.append(plan)
        if len(deduped) >= top_n:
            break

    return deduped


def summarize_plan(plan: Plan) -> str:
    """把方案渲染成人类可读文本。"""
    lines = []
    lines.append(f"【方案】实付 ¥{plan.payable:.2f}　"
                 f"热量 {plan.total_kcal:.0f}/{plan.credit_kcal:.0f} kcal　"
                 f"额度利用率 {plan.credit_usage:.0%}　"
                 f"合理度 {plan.digestibility:.0%}")
    for p in plan.products:
        kcal_txt = f"{p.kcal:.0f} kcal" if p.nutrition_known else "营养数据待核"
        lines.append(f"  · {p.name}　¥{p.price:.2f}　{kcal_txt}")
    if plan.coupon:
        lines.append(f"  券：{plan.coupon.name}　立减 ¥{plan.discount:.2f}")
    else:
        lines.append("  券：未使用优惠券")
    lines.append(f"  营养：蛋白 {plan.total_protein_g:.0f}g / "
                 f"脂肪 {plan.total_fat_g:.0f}g / "
                 f"碳水 {plan.total_carb_g:.0f}g / "
                 f"钠 {plan.total_sodium_mg:.0f}mg")
    lines.append(f"  核价：{'已由 MCP calculate-price 复核' if plan.verified else '本地估算，下单前需核价'}")
    for w in plan.warnings:
        lines.append(f"  ⚠ {w}")
    return "\n".join(lines)
