"""
麦动平衡 · MCP 编排流水线
M-Balance / pipeline.py

把「用户一句话」翻译成「一次可下单的方案」，串起 12 个麦当劳 MCP Tool。
每一步都做失败降级，保证任何单个 Tool 抖动都不会让整个流程崩掉。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence

from mcd_client import MCPError
from value_engine import Coupon, NoFeasiblePlan, Plan, Product, optimize, summarize_plan
from activity_model import ActivityEntry, BurnResult, compute_credit

# ---------------------------------------------------------------- 解析

# 一份基础营养数据的字段名在不同接口下可能不同，做多点兼容
_FIELD_ALIASES = {
    "kcal": ("energyKcal", "kcal", "energy", "calorie", "calories", "energy_kcal"),
    "protein_g": ("protein", "proteinG", "protein_g"),
    "fat_g": ("fat", "fatG", "fat_g"),
    "carb_g": ("carbohydrate", "carbohydrateG", "carb", "carb_g", "carbohydrates"),
    "sodium_mg": ("sodium", "sodiumMg", "sodium_mg", "na"),
}


def _pick(d: Dict[str, Any], aliases: Sequence[str], default: float = 0.0) -> float:
    for k in aliases:
        if k in d and d[k] not in (None, "", "-"):
            try:
                return float(d[k])
            except (TypeError, ValueError):
                continue
    return default


def _norm_name(s: str) -> str:
    """归一化餐品名，用于在菜单与营养库之间做匹配。"""
    return re.sub(r"[\s\-·（）()【】\[\]　]+", "", (s or "")).lower()


@dataclass
class NutritionIndex:
    """把 list-nutrition-foods 的结果做成可查询的索引。"""
    by_name: Dict[str, Dict[str, float]]

    @classmethod
    def build(cls, raw: Any) -> "NutritionIndex":
        rows: List[Dict[str, Any]] = []
        if isinstance(raw, list):
            rows = [r for r in raw if isinstance(r, dict)]
        elif isinstance(raw, dict):
            for key in ("data", "list", "foods", "items", "result"):
                if isinstance(raw.get(key), list):
                    rows = [r for r in raw[key] if isinstance(r, dict)]
                    break
            else:
                # 形如 {"巨无霸": {...}} 的字典
                rows = [dict(v, name=k) for k, v in raw.items() if isinstance(v, dict)]

        by_name: Dict[str, Dict[str, float]] = {}
        for r in rows:
            name = _norm_name(str(r.get("name") or r.get("foodName") or r.get("productName") or ""))
            if not name:
                continue
            by_name[name] = {
                "kcal": _pick(r, _FIELD_ALIASES["kcal"]),
                "protein_g": _pick(r, _FIELD_ALIASES["protein_g"]),
                "fat_g": _pick(r, _FIELD_ALIASES["fat_g"]),
                "carb_g": _pick(r, _FIELD_ALIASES["carb_g"]),
                "sodium_mg": _pick(r, _FIELD_ALIASES["sodium_mg"]),
            }
        return cls(by_name=by_name)

    def lookup(self, product_name: str) -> Optional[Dict[str, float]]:
        n = _norm_name(product_name)
        if not n:
            return None
        if n in self.by_name:
            return self.by_name[n]
        # 模糊匹配：菜单名常带规格后缀（如"（中）""套餐"），取包含关系中最长命中
        hits = [(k, v) for k, v in self.by_name.items() if k in n or n in k]
        if not hits:
            return None
        hits.sort(key=lambda kv: len(kv[0]), reverse=True)
        return hits[0][1]


def parse_products(menu_raw: Any, nutrition: NutritionIndex) -> List[Product]:
    """把 query-meals 的结果 + 营养索引，合成 Product 列表。"""
    items: List[Dict[str, Any]] = []
    if isinstance(menu_raw, list):
        items = [i for i in menu_raw if isinstance(i, dict)]
    elif isinstance(menu_raw, dict):
        for key in ("data", "meals", "list", "categories", "items", "products"):
            v = menu_raw.get(key)
            if isinstance(v, list):
                for it in v:
                    if not isinstance(it, dict):
                        continue
                    # 分类节点嵌套 products
                    if isinstance(it.get("products"), list):
                        items.extend(p for p in it["products"] if isinstance(p, dict))
                    elif isinstance(it.get("meals"), list):
                        items.extend(p for p in it["meals"] if isinstance(p, dict))
                    else:
                        items.append(it)
                break

    out: List[Product] = []
    for it in items:
        code = str(it.get("code") or it.get("productCode") or it.get("mealCode") or it.get("id") or "")
        name = str(it.get("name") or it.get("productName") or it.get("mealName") or "")
        if not code or not name:
            continue
        price = _pick(it, ("price", "salePrice", "currentPrice", "amount", "originalPrice"))
        if price <= 0:
            continue

        nut = nutrition.lookup(name)
        known = nut is not None
        nut = nut or {}
        cat = str(it.get("categoryName") or it.get("category") or "")
        is_drink = any(k in name for k in ("可乐", "咖啡", "茶", "奶茶", "果汁", "水", "奶昔", "啤酒", "雪碧")) \
            or any(k in cat for k in ("饮", "饮料", "咖啡", "饮品"))

        out.append(Product(
            code=code, name=name, price=price,
            kcal=nut.get("kcal", 0.0), protein_g=nut.get("protein_g", 0.0),
            fat_g=nut.get("fat_g", 0.0), carb_g=nut.get("carb_g", 0.0),
            sodium_mg=nut.get("sodium_mg", 0.0),
            category=cat, is_drink=is_drink, nutrition_known=known,
        ))
    return out


def parse_coupons(*raw_lists: Any) -> List[Coupon]:
    """合并多路券（我的券 / 门店券 / 麦麦省），去重。"""
    seen: Dict[str, Coupon] = {}
    for raw in raw_lists:
        rows: List[Dict[str, Any]] = []
        if isinstance(raw, list):
            rows = [r for r in raw if isinstance(r, dict)]
        elif isinstance(raw, dict):
            for key in ("data", "coupons", "list", "items", "availableCoupons"):
                if isinstance(raw.get(key), list):
                    rows = [r for r in raw[key] if isinstance(r, dict)]
                    break

        for r in rows:
            cid = str(r.get("couponId") or r.get("id") or r.get("code") or "")
            name = str(r.get("name") or r.get("couponName") or r.get("title") or "")
            if not cid or not name:
                continue
            if cid in seen:
                continue

            ctype = "discount"
            amount = _pick(r, ("amount", "discountAmount", "value", "reduceAmount"))
            threshold = _pick(r, ("threshold", "minAmount", "condition", "limitAmount"))
            percent = _pick(r, ("percent", "discountRate", "rate"))
            cap_kcal = _pick(r, ("exchangeKcalCap", "kcalCap"))
            codes = r.get("applicableCodes") or r.get("productCodes") or ()
            if isinstance(codes, str):
                codes = tuple(c.strip() for c in codes.split(",") if c.strip())
            codes = tuple(str(c) for c in codes)

            raw_type = str(r.get("type") or r.get("couponType") or "").lower()
            if "exchange" in raw_type or "兑换" in raw_type or (amount == 0 and not percent and codes):
                ctype = "exchange"
            elif percent and percent < 1:
                ctype = "percent"
            elif "percent" in raw_type or "折扣" in raw_type:
                ctype = "percent"
                percent = (100 - percent) / 100 if percent > 1 else percent

            if ctype == "discount" and amount <= 0:
                continue

            seen[cid] = Coupon(
                coupon_id=cid, name=name, coupon_type=ctype,
                threshold=threshold, amount=amount,
                percent=percent if ctype == "percent" else 0.0,
                max_discount=_pick(r, ("maxDiscount", "discountLimit")),
                exchange_kcal_cap=cap_kcal,
                applicable_codes=codes,
                source=str(r.get("source") or "my"),
                expires_at=str(r.get("expireTime") or r.get("endTime") or ""),
            )
    return list(seen.values())


# ---------------------------------------------------------------- 编排

@dataclass
class Session:
    """一次完整会话的中间状态。"""
    burn: Optional[BurnResult] = None
    products: List[Product] = None          # type: ignore[assignment]
    coupons: List[Coupon] = None            # type: ignore[assignment]
    plans: List[Plan] = None                # type: ignore[assignment]
    warnings: List[str] = None              # type: ignore[assignment]
    trace: List[str] = None                 # type: ignore[assignment]

    def __post_init__(self) -> None:
        self.products = self.products if self.products is not None else []
        self.coupons = self.coupons if self.coupons is not None else []
        self.plans = self.plans if self.plans is not None else []
        self.warnings = self.warnings if self.warnings is not None else []
        self.trace = self.trace if self.trace is not None else []


def _safe_call(client, tool: str, args: Optional[Dict] = None, session: Optional[Session] = None,
               soft: bool = True) -> Any:
    """带降级的调用。soft=True 时失败返回 None 并记录告警，不中断流程。"""
    try:
        result = client.call(tool, args or {})
        if session is not None:
            session.trace.append(f"OK  {tool}")
        return result
    except MCPError as exc:
        msg = f"{tool} 调用失败（{exc.code}）：{exc}｜建议：{exc.hint()}"
        if session is not None:
            session.trace.append(f"FAIL {tool}")
            session.warnings.append(msg)
        if not soft:
            raise
        return None


def run(
    client,
    activities: Sequence[ActivityEntry],
    weight_kg: float = 65.0,
    budget: float = 40.0,
    store_code: str = "",
    address_code: str = "",
    dining_mode: str = "pickup",
    credit_ratio: float = 0.6,
    top_n: int = 3,
) -> Session:
    """
    完整流水线：
      now-time-info → 额度计算 → list-nutrition-foods → query-meals
      → 三路取券 → available-coupons / auto-bind-coupons
      → optimize → calculate-price 复核 → campaign-calendar 彩蛋
    """
    session = Session()

    # Step 0：先拿时间与门店信息
    _safe_call(client, "now-time-info", {}, session)
    if dining_mode == "delivery":
        if not address_code:
            addrs = _safe_call(client, "delivery-query-addresses", {}, session)
            address_code = _extract_first_code(addrs, ("addressCode", "id", "code"))
        _safe_call(client, "delivery-query-stores", {"addressCode": address_code}, session)
    else:
        if not store_code:
            stores = _safe_call(client, "query-nearby-stores", {}, session)
            store_code = _extract_first_code(stores, ("storeCode", "code", "id"))

    # Step 1：运动 → 加餐额度（本地模型，无需网络）
    session.burn = compute_credit(activities, weight_kg=weight_kg, credit_ratio=credit_ratio)

    # Step 2：营养库
    nutrition_raw = _safe_call(client, "list-nutrition-foods", {}, session)
    nutrition = NutritionIndex.build(nutrition_raw)
    if not nutrition.by_name:
        session.warnings.append(
            "未取到营养数据，本次将以「不含热量约束」方式给出菜单推荐，请谨慎参考。"
        )

    # Step 3：菜单
    menu_args: Dict[str, Any] = {}
    if store_code:
        menu_args["storeCode"] = store_code
    if dining_mode == "delivery":
        menu_args["orderType"] = "delivery"
    menu_raw = _safe_call(client, "query-meals", menu_args, session, soft=False)
    session.products = parse_products(menu_raw, nutrition)

    # Step 4：三路取券 + 自动领取麦麦省
    my_coupons = _safe_call(client, "query-my-coupons", {"storeCode": store_code}, session)
    _safe_call(client, "available-coupons", {"storeCode": store_code}, session)
    _safe_call(client, "auto-bind-coupons", {"storeCode": store_code}, session)
    store_coupons = _safe_call(client, "query-store-coupons", {"storeCode": store_code}, session)
    # 领券后重查一次，拿到最新券包
    my_coupons_fresh = _safe_call(client, "query-my-coupons", {"storeCode": store_code}, session)

    session.coupons = parse_coupons(my_coupons, my_coupons_fresh, store_coupons)
    if not session.coupons:
        session.warnings.append("当前账户与门店均无可用优惠券，已按原价计算。")

    # Step 5：组合优化
    try:
        session.plans = optimize(
            session.products, session.coupons,
            credit_kcal=session.burn.credit_kcal, budget=budget, top_n=top_n,
        )
    except NoFeasiblePlan as exc:
        session.warnings.append(f"未找到可行方案：{exc}")
        return session

    # Step 6：对 Top1 做服务端核价（业务上必须：本地估算不等于真实结算价）
    if session.plans:
        top = session.plans[0]
        # 先查套餐详情，确认可替换选项（用来给"瘦身"建议提供依据）
        for p in top.products[:1]:
            _safe_call(client, "query-meal-detail", {"code": p.code, "storeCode": store_code}, session)
        priced = _safe_call(client, "calculate-price", {
            "storeCode": store_code,
            "orderType": "delivery" if dining_mode == "delivery" else "pickup",
            "products": [{"code": p.code, "quantity": 1} for p in top.products],
            "couponId": top.coupon.coupon_id if top.coupon else None,
        }, session)
        verified = _apply_price(top, priced)
        if not verified:
            session.warnings.append("服务端核价未返回可用金额，方案金额为本地估算值。")

    # Step 7：彩蛋 —— 看看今天有没有可叠加的活动
    _safe_call(client, "campaign-calendar", {}, session)

    return session


def _apply_price(plan: Plan, priced: Any) -> bool:
    """把 calculate-price 的结果回写到方案上。返回是否成功核价。"""
    if not isinstance(priced, dict):
        return False
    payable = _pick(priced, ("payableAmount", "payAmount", "totalAmount", "amount"))
    if payable <= 0:
        return False
    plan.payable = round(payable, 2)
    discount = _pick(priced, ("discountAmount", "discount", "couponAmount"))
    if discount > 0:
        plan.discount = round(discount, 2)
    plan.verified = True
    plan.price_source = "mcp"
    return True


def _extract_first_code(raw: Any, keys: Sequence[str]) -> str:
    rows: List[Dict[str, Any]] = []
    if isinstance(raw, list):
        rows = [r for r in raw if isinstance(r, dict)]
    elif isinstance(raw, dict):
        for k in ("data", "list", "stores", "addresses", "items"):
            if isinstance(raw.get(k), list):
                rows = [r for r in raw[k] if isinstance(r, dict)]
                break
    for r in rows:
        for k in keys:
            if r.get(k):
                return str(r[k])
    return ""


def render_report(session: Session, budget: float, dining_mode: str) -> str:
    """把会话结果渲染成 Markdown 报告（Skill 直接输出给用户的内容）。"""
    lines: List[str] = []
    burn = session.burn

    lines.append("## 麦动平衡 · 今日建议\n")
    if burn:
        lines.append("**你的运动账单**\n")
        lines.append("| 运动 | MET | 时长 | 消耗 |")
        lines.append("|---|---:|---:|---:|")
        for b in burn.breakdown:
            lines.append(f"| {b['activity']} | {b['met']} | {b['minutes']:.0f} min | {b['burn_kcal']:.0f} kcal |")
        lines.append(f"\n合计消耗 **{burn.total_burn_kcal:.0f} kcal**，"
                     f"折算加餐额度 **{burn.credit_kcal:.0f} kcal**"
                     f"（保守系数 {burn.credit_ratio:.0%}）。\n")

    if not session.plans:
        lines.append("抱歉，在给你的额度与预算下没凑出合适的方案。")
        lines.append("可以试试：抬高预算、放宽额度，或先领几张券再回来。\n")
    else:
        lines.append(f"**预算 ¥{budget:.0f} · {'麦乐送' if dining_mode == 'delivery' else '到店取餐'}**\n")
        for idx, plan in enumerate(session.plans, 1):
            lines.append(f"### 方案 {idx}\n")
            lines.append("```")
            lines.append(summarize_plan(plan))
            lines.append("```\n")

    if session.warnings:
        lines.append("**过程中的提示**\n")
        for w in session.warnings:
            lines.append(f"- {w}")
        lines.append("")

    lines.append("> 以上为膳食量级参考，不构成医疗或营养建议；"
                 "餐品、价格与供应状态以麦当劳官方渠道实时结果为准。")
    return "\n".join(lines)
