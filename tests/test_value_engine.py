"""
麦动平衡 · 单元测试
M-Balance / tests/test_value_engine.py

运行：python3 -m pytest tests/ -v
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from activity_model import (  # noqa: E402
    ACTIVITY_TABLE, ActivityEntry, ActivityError, compute_credit,
    estimate_burn, resolve_activity,
)
from mcd_client import OfflineClient  # noqa: E402
from pipeline import NutritionIndex, parse_coupons, parse_products, run  # noqa: E402
from value_engine import (  # noqa: E402
    FAT_ENERGY_RATIO_WARN, Coupon, NoFeasiblePlan, Product, _build_plan,
    apply_coupon, optimize, summarize_plan,
)

MOCK = ROOT / "examples" / "demo_mock_data.json"


# ------------------------------------------------------------------ 运动模型
class TestActivityModel:
    def test_resolve_exact_and_alias(self):
        assert resolve_activity("跑步") == "running_8kmh"
        assert resolve_activity("撸铁") == "strength"
        assert resolve_activity("遛狗") == "dog_walking"
        assert resolve_activity("打羽毛球") == "badminton"

    def test_resolve_unknown_returns_none(self):
        assert resolve_activity("蹦极一百米") is None

    def test_alias_prefers_longer_match(self):
        # "跳绳" 不应被 "绳" 之外的短别名抢走
        assert resolve_activity("跳绳") == "jump_rope"
        # "快跑" 应命中快跑而不是慢跑
        assert resolve_activity("快跑") == "running_10kmh"

    def test_burn_matches_acsm_formula(self):
        # 跑步 8.3 MET × 70kg × 0.5h = 290.5 kcal
        entries = [ActivityEntry("跑步", 30)]
        assert estimate_burn(entries, weight_kg=70) == pytest.approx(290.5, abs=0.1)

    def test_credit_ratio_applied(self):
        entries = [ActivityEntry("跑步", 30)]
        res = compute_credit(entries, weight_kg=70, credit_ratio=0.6)
        assert res.total_burn_kcal == pytest.approx(290.5, abs=0.1)
        assert res.credit_kcal == pytest.approx(174.3, abs=0.2)
        assert res.capped is False

    def test_credit_cap_prevents_overeating(self):
        # 长时间高强度运动后额度必须被封顶，避免"运动换暴食"
        entries = [ActivityEntry("跳绳", 180)]
        res = compute_credit(entries, weight_kg=80, credit_ratio=0.6, max_credit_kcal=700)
        assert res.capped is True
        assert res.credit_kcal == 700

    def test_zero_weight_rejected(self):
        with pytest.raises(ActivityError):
            estimate_burn([ActivityEntry("跑步", 30)], weight_kg=0)

    def test_empty_entries_rejected(self):
        with pytest.raises(ActivityError):
            compute_credit([])

    def test_invalid_ratio_rejected(self):
        with pytest.raises(ActivityError):
            compute_credit([ActivityEntry("跑步", 30)], credit_ratio=1.5)

    def test_unknown_activity_in_entry_rejected(self):
        with pytest.raises(ActivityError):
            ActivityEntry("星际穿越", 30)

    def test_all_table_entries_callable(self):
        for key, meta in ACTIVITY_TABLE.items():
            assert resolve_activity(meta["label"]) == key
            assert meta["met"] > 0


# ------------------------------------------------------------------ 券结算
class TestCouponSettlement:
    def _p(self, code, price, kcal=200):
        return Product(code=code, name=code, price=price, kcal=kcal,
                       fat_g=5, protein_g=10, carb_g=20, nutrition_known=True)

    def test_discount_below_threshold_not_applied(self):
        c = Coupon(coupon_id="c1", name="满50减10", coupon_type="discount",
                   threshold=50, amount=10)
        assert apply_coupon([self._p("A", 30)], c)[0] == 0.0

    def test_discount_above_threshold(self):
        c = Coupon(coupon_id="c1", name="满35减8", coupon_type="discount",
                   threshold=35, amount=8)
        assert apply_coupon([self._p("A", 40)], c)[0] == 8.0

    def test_exchange_picks_most_expensive_eligible(self):
        c = Coupon(coupon_id="c2", name="麦乐鸡免费", coupon_type="exchange",
                   applicable_codes=("B", "C"))
        prods = [self._p("A", 20), self._p("B", 13), self._p("C", 9)]
        assert apply_coupon(prods, c)[0] == 13.0

    def test_exchange_respects_kcal_cap(self):
        c = Coupon(coupon_id="c3", name="低卡兑换", coupon_type="exchange",
                   applicable_codes=("A", "B"), exchange_kcal_cap=150)
        prods = [self._p("A", 20, kcal=400), self._p("B", 13, kcal=100)]
        assert apply_coupon(prods, c)[0] == 13.0

    def test_no_coupon(self):
        assert apply_coupon([self._p("A", 20)], None)[0] == 0.0

    def test_percent_capped(self):
        c = Coupon(coupon_id="c4", name="5折封顶10", coupon_type="percent",
                   percent=0.5, max_discount=10)
        assert apply_coupon([self._p("A", 100)], c)[0] == 10.0

    def test_discount_never_exceeds_subtotal(self):
        c = Coupon(coupon_id="c5", name="满10减100", coupon_type="discount",
                   threshold=10, amount=100)
        assert apply_coupon([self._p("A", 20)], c)[0] == 20.0


# ------------------------------------------------------------------ 优化引擎
class TestOptimizer:
    def _catalog(self):
        return [
            Product("H1", "巨无霸", 21.5, kcal=510, protein_g=26, fat_g=26, carb_g=43, sodium_mg=920),
            Product("H2", "板烧鸡腿堡", 20.5, kcal=400, protein_g=26, fat_g=15, carb_g=40, sodium_mg=890),
            Product("S1", "中薯条", 10.0, kcal=230, protein_g=3, fat_g=11, carb_g=30, sodium_mg=190),
            Product("D1", "零度可乐", 9.5, kcal=0, protein_g=0, fat_g=0, carb_g=0, sodium_mg=20, is_drink=True),
            Product("D2", "美式咖啡", 15.0, kcal=10, protein_g=1, fat_g=0, carb_g=1, sodium_mg=10, is_drink=True),
            Product("S2", "蔬菜沙拉", 9.5, kcal=40, protein_g=2, fat_g=1, carb_g=7, sodium_mg=15),
        ]

    def test_respects_kcal_ceiling(self):
        plans = optimize(self._catalog(), [], credit_kcal=600, budget=100)
        assert plans
        for p in plans:
            assert p.total_kcal <= 600 + 1e-6

    def test_respects_budget(self):
        plans = optimize(self._catalog(), [], credit_kcal=2000, budget=25)
        assert plans
        for p in plans:
            assert p.payable <= 25 + 1e-6

    def test_maximizes_credit_usage(self):
        # 额度 900、预算充足时，应优先吃满额度而不是只买最便宜的
        plans = optimize(self._catalog(), [], credit_kcal=900, budget=100)
        assert plans[0].credit_usage >= 0.55

    def test_prefers_cheaper_when_usage_equal(self):
        plans = optimize(self._catalog(), [], credit_kcal=400, budget=100)
        # 次排序为实付升序
        assert plans[0].payable <= plans[-1].payable + 1e-6

    def test_coupon_improves_payable(self):
        coupons = [Coupon("c1", "满35减8", coupon_type="discount", threshold=35, amount=8)]
        no_c = optimize(self._catalog(), [], credit_kcal=900, budget=60)[0]
        with_c = optimize(self._catalog(), coupons, credit_kcal=900, budget=60)[0]
        assert with_c.payable <= no_c.payable + 1e-6

    def test_high_fat_combo_needs_refreshing(self):
        fat_heavy = [
            Product("F1", "油炸拼盘A", 30, kcal=800, protein_g=10, fat_g=60, carb_g=20),
            Product("F2", "油炸拼盘B", 30, kcal=800, protein_g=10, fat_g=60, carb_g=20),
        ]
        with pytest.raises(NoFeasiblePlan):
            optimize(fat_heavy, [], credit_kcal=900, budget=200)

    def test_fat_warning_emitted(self):
        # 高脂 + 无解腻项 → 必须提示
        prods = [
            Product(code="F1", name="双层吉士汉堡", price=19.5, kcal=450, protein_g=25,
                    fat_g=25, carb_g=35, sodium_mg=1050),
            Product(code="S1", name="大薯条", price=12.5, kcal=340, protein_g=4,
                    fat_g=16, carb_g=44, sodium_mg=270),
        ]
        plan = _build_plan(prods, None, 900, 60)
        assert any("脂肪" in w for w in plan.warnings), \
            f"高脂无解腻项应提示，实际告警：{plan.warnings}"

    def test_optimizer_prefers_well_rounded_meal_over_plain_fries(self):
        """
        真实场景回归：汉堡+薯条热量 790 远超 900 额度内的最优解时，
        引擎不应为了"吃满额度"退回只点一份大薯条——那是配菜不是一餐。
        """
        prods = [
            Product(code="F1", name="双层吉士汉堡", price=19.5, kcal=450, protein_g=25,
                    fat_g=25, carb_g=35, sodium_mg=1050),
            Product(code="S1", name="大薯条", price=12.5, kcal=340, protein_g=4,
                    fat_g=16, carb_g=44, sodium_mg=270),
        ]
        top = optimize(prods, [], credit_kcal=900, budget=60)[0]
        assert top.anchor_count >= 1, "首选方案必须含有主食主体"
        assert top.digestibility >= 0.5

    def test_refreshing_item_suppresses_fat_warning(self):
        """配了美式咖啡就不该再念叨'加杯黑咖'——这本来就是它的作用。"""
        prods = [
            Product(code="F1", name="双层吉士汉堡", price=19.5, kcal=450, protein_g=25,
                    fat_g=25, carb_g=35, sodium_mg=1050),
            Product(code="D1", name="美式咖啡", price=15.0, kcal=10, protein_g=1,
                    fat_g=0, carb_g=1, sodium_mg=10, is_drink=True),
        ]
        plan = _build_plan(prods, None, 600, 60)
        assert not any("解腻" in w for w in plan.warnings)

    def test_sodium_warning_emitted(self):
        prods = [
            Product(code="F1", name="双层吉士汉堡", price=19.5, kcal=450, protein_g=25,
                    fat_g=25, carb_g=35, sodium_mg=1300),
        ]
        plan = _build_plan(prods, None, 600, 60)
        assert any("钠" in w for w in plan.warnings)

    def test_threshold_boundary_is_inclusive(self):
        """正好落在阈值线上也应触发告警（用 >= 且带容差，而非严格 >）。"""
        # 23g 脂肪 × 9 = 207kcal；207 / 460 = 45.0% == 阈值
        # 配中薯条作为"中性"搭配：它既不解腻、也不是甜品
        prods = [
            Product(code="F1", name="测试堡", price=20, kcal=450, protein_g=25,
                    fat_g=23, carb_g=35, sodium_mg=100),
            Product(code="S1", name="中薯条", price=10, kcal=10, nutrition_known=True),
        ]
        plan = _build_plan(prods, None, 600, 60)
        ratio = (plan.total_fat_g * 9.0) / plan.total_kcal
        assert abs(ratio - FAT_ENERGY_RATIO_WARN) < 1e-9, f"用例应落在阈值上，实际 {ratio}"
        assert any("脂肪" in w for w in plan.warnings), \
            f"踩线也应提示，实际告警：{plan.warnings}"

    def test_de_greasing_item_suppresses_warning(self):
        """零度可乐/美式咖啡本身就是解腻项，不该再提示'加杯黑咖'。"""
        prods = [
            Product(code="F1", name="测试堡", price=20, kcal=450, protein_g=25,
                    fat_g=23, carb_g=35, sodium_mg=100),
            Product(code="D1", name="零度可乐", price=9.5, kcal=10, nutrition_known=True),
        ]
        plan = _build_plan(prods, None, 600, 60)
        assert not any("解腻" in w for w in plan.warnings)

    def test_no_feasible_raises(self):
        with pytest.raises(NoFeasiblePlan):
            optimize([Product("X", "贵堡", 999, kcal=100)], [], credit_kcal=500, budget=10)

    def test_zero_credit_raises(self):
        with pytest.raises(NoFeasiblePlan):
            optimize(self._catalog(), [], credit_kcal=0, budget=50)

    def test_empty_pool_raises(self):
        with pytest.raises(NoFeasiblePlan):
            optimize([], [], credit_kcal=500, budget=50)

    def test_unknown_nutrition_disclosed(self):
        prods = [
            Product(code="U1", name="神秘新品", price=20, kcal=0, nutrition_known=False),
            Product(code="K1", name="麦乐鸡（5块）", price=13, kcal=230, protein_g=14,
                    fat_g=13, carb_g=13, sodium_mg=480),
        ]
        plans = optimize(prods, [], credit_kcal=500, budget=60)
        assert plans
        # 只要某个方案里含营养未知的餐品，就必须诚实披露，不能假装算过热量
        flagged = [p for p in plans if any(not x.nutrition_known for x in p.products)]
        assert flagged, "应至少有一个含未知营养餐品的方案"
        for plan in flagged:
            assert any("营养数据" in w for w in plan.warnings), \
                f"含未知营养餐品却未披露：{plan.warnings}"

    def test_top_n_respected(self):
        plans = optimize(self._catalog(), [], credit_kcal=900, budget=100, top_n=2)
        assert len(plans) <= 2

    def test_summarize_renders(self):
        plans = optimize(self._catalog(), [], credit_kcal=900, budget=100)
        text = summarize_plan(plans[0])
        assert "实付" in text and "额度利用率" in text

    def test_budget_zero_raises(self):
        with pytest.raises(NoFeasiblePlan):
            optimize(self._catalog(), [], credit_kcal=500, budget=0)


# ------------------------------------------------------------------ 解析层
class TestParsing:
    def test_nutrition_index_builds(self):
        raw = [{"name": "巨无霸", "energyKcal": 510, "protein": 26,
                "fat": 26, "carbohydrate": 43, "sodium": 920}]
        idx = NutritionIndex.build(raw)
        got = idx.lookup("巨无霸")
        assert got and got["kcal"] == 510 and got["protein_g"] == 26

    def test_nutrition_fuzzy_match(self):
        idx = NutritionIndex.build([{"name": "中薯条", "energyKcal": 230}])
        assert idx.lookup("中薯条（大份装）")["kcal"] == 230

    def test_nutrition_missing_returns_none(self):
        idx = NutritionIndex.build([{"name": "巨无霸", "energyKcal": 510}])
        assert idx.lookup("完全不存在的东西") is None

    def test_parse_products_from_nested_categories(self):
        idx = NutritionIndex.build([{"name": "巨无霸", "energyKcal": 510}])
        menu = {"data": [{"categoryName": "汉堡", "products": [
            {"code": "P1", "name": "巨无霸", "price": 21.5}]}]}
        prods = parse_products(menu, idx)
        assert len(prods) == 1 and prods[0].code == "P1" and prods[0].nutrition_known

    def test_parse_products_skips_no_price(self):
        idx = NutritionIndex.build([])
        menu = {"data": [{"code": "P1", "name": "无价商品"}]}
        assert parse_products(menu, idx) == []

    def test_parse_coupons_dedupes_across_sources(self):
        a = [{"couponId": "X", "name": "券A", "type": "discount", "amount": 5, "threshold": 0}]
        b = [{"couponId": "X", "name": "券A副本", "type": "discount", "amount": 5}]
        assert len(parse_coupons(a, b)) == 1

    def test_parse_coupons_exchange_detected(self):
        raw = [{"couponId": "E1", "name": "免费兑换", "type": "exchange",
                "applicableCodes": ["P1"]}]
        cs = parse_coupons(raw)
        assert cs[0].coupon_type == "exchange"


# ------------------------------------------------------------------ 端到端
class TestEndToEnd:
    def test_offline_pipeline_full_run(self):
        client = OfflineClient(str(MOCK))
        session = run(client, [ActivityEntry("跑步", 40)], weight_kg=68, budget=35)
        assert session.burn.credit_kcal > 0
        assert session.plans, f"应生成方案，告警：{session.warnings}"
        top = session.plans[0]
        assert top.credit_usage >= 0.55
        assert top.payable <= 35 + 1e-6
        assert top.verified is True, "Top1 必须经过 calculate-price 复核"
        assert any("calculate-price" in t for t in session.trace)

    def test_report_renders_markdown(self):
        from pipeline import render_report
        client = OfflineClient(str(MOCK))
        session = run(client, [ActivityEntry("力量训练", 50)], weight_kg=75, budget=40)
        md = render_report(session, 40, "pickup")
        assert "麦动平衡" in md and "运动账单" in md and "方案 1" in md

    def test_mock_file_has_no_secrets(self):
        import json as _json
        text = MOCK.read_text(encoding="utf-8")
        assert "Bearer" not in text
        assert "MCD_MCP_TOKEN" not in text
        data = _json.loads(text)
        assert data["tools"]["auto-bind-coupons"]["boundCount"] == 0

    def test_offline_client_records_trace(self):
        client = OfflineClient(str(MOCK))
        run(client, [ActivityEntry("快走", 45)], weight_kg=60, budget=30)
        for tool in ("list-nutrition-foods", "query-meals",
                     "query-my-coupons", "calculate-price"):
            assert tool in client.calls, f"{tool} 应被调用"

    def test_nearby_stores_fallback_when_mock_lacks_it(self):
        """mock 里没有 query-nearby-stores 时，应降级而不是崩溃。"""
        client = OfflineClient(str(MOCK))
        s = run(client, [ActivityEntry("快走", 45)], weight_kg=60, budget=30)
        assert any("query-nearby-stores" in w for w in s.warnings)
