# MCP 集成说明

本文件说明「麦动平衡 M-Balance」实际使用的麦当劳 MCP Server、Tool、调用流程与业务价值。

## 一、接入信息

| 项目 | 值 |
|---|---|
| MCP Server | 麦当劳中国 MCP Server（远程托管） |
| 接入地址 | `https://mcp.mcd.cn` |
| 传输协议 | Streamable HTTP |
| 鉴权方式 | 请求头 `Authorization: Bearer ${MCD_MCP_TOKEN}` |
| 协议版本 | `2025-06-18` |
| 限流 | 600 次/分钟（客户端已实现指数退避） |
| Token 申请 | https://open.mcd.cn/mcp |

客户端实现位于 `scripts/mcd_client.py`，包含：

- `initialize()`：MCP 握手 + `notifications/initialized`
- `tools/list`：能力探测
- `tools/call`：统一调用入口，自动解包 `content` / `structuredContent`
- SSE 与 JSON 两种响应格式的兼容解析
- 401 / 429 / 网络超时的分级处理与重试

`OfflineClient` 提供与在线客户端**完全一致的接口**，读取
`examples/demo_mock_data.json`，用于无 Token 演示与单元测试。

## 二、实际使用的 Tool（12 个）

| # | Tool | 在业务中的角色 | 是否关键路径 |
|---:|---|---|:---:|
| 1 | `now-time-info` | 判定当前时段，决定菜单口径（早餐/正餐） | 否 |
| 2 | `list-nutrition-foods` | **营养底盘**：热量、蛋白、脂肪、碳水、钠的量化依据 | **是** |
| 3 | `query-meals` | 拉取门店实时可售菜单与价格 | **是** |
| 4 | `query-meal-detail` | 套餐组成与可替换选项，支撑"瘦身"建议 | 否 |
| 5 | `query-nearby-stores` | 到店取餐场景的门店定位 | 否 |
| 6 | `delivery-query-addresses` | 外送场景：收货地址 | 否 |
| 7 | `delivery-query-stores` | 外送场景：可配送门店 | 否 |
| 8 | `query-my-coupons` | 我的券（领券前后各查一次） | **是** |
| 9 | `available-coupons` | 麦麦省当前可领取的券 | **是** |
| 10 | `auto-bind-coupons` | 一键领取麦麦省全部可领券 | 否 |
| 11 | `query-store-coupons` | 门店专享券 | **是** |
| 12 | `calculate-price` | **下单前强制核价**，杜绝"假装省钱" | **是** |
| 13 | `campaign-calendar` | 当天可叠加的营销活动（彩蛋提示） | 否 |
| 14 | `create-order` | 用户确认后创建订单，返回支付链接 | **是** |

### 未使用的 Tool 及原因

| Tool | 未使用原因 |
|---|---|
| `draw-lottery` / `query-lottery-info` / `query-my-prizes` | 积分抽奖类功能带有博彩观感，与"膳食运动平衡"主题无关，且存在合规风险，主动规避 |
| `mall-*` 系列（商城商品、订单、兑换） | 与"运动后加餐"场景无关，强行使用会稀释产品定位 |
| `query-party-*` / `party-order-create` | 主题活动预约场景与主线无关 |
| `query-my-account` / `order-list` | 未纳入当前版本；`order-list` 是后续「饮食节律」功能的预留位 |
| `cancel-order` / `query-order` | 订单管理属常规点餐流程，非本 Skill 的核心价值 |

**设计原则**：宁可少用，不硬凑。每个被使用的 Tool 都要能回答"它解决了哪个用户问题"。

## 三、调用流程

### 主流程（到店取餐）

```
用户输入「跑步 40min, 力量训练 25min / 68kg / 预算 35」
  │
  ├─[本地] compute_credit()  ── ACSM 公式 ── 消耗 518 kcal
  │                                            ↓
  │                                  加餐额度 311 kcal（系数 0.6）
  │
  ├─[MCP]  now-time-info                ← 当前时段
  ├─[MCP]  query-nearby-stores          ← 定位门店（可失败降级）
  ├─[MCP]  list-nutrition-foods         ← 营养底盘
  │           ↓ NutritionIndex.build()  建索引，支持模糊匹配
  ├─[MCP]  query-meals                  ← 门店菜单（关键路径，失败终止）
  │           ↓ parse_products()        菜单 × 营养 → Product[]
  │
  ├─[MCP]  query-my-coupons             ← 我的券
  ├─[MCP]  available-coupons            ← 麦麦省可领券
  ├─[MCP]  auto-bind-coupons            ← 一键领券
  ├─[MCP]  query-store-coupons          ← 门店券
  ├─[MCP]  query-my-coupons             ← 领券后重查券包
  │           ↓ parse_coupons()         四路合并去重 → Coupon[]
  │
  ├─[本地] optimize()                   ← 双约束组合优化
  │           硬约束：热量 ≤ 311 kcal  且  实付 ≤ ¥35
  │           硬过滤：脂肪供能比 ≥ 45% 且无解腻项 → 淘汰
  │           排序：饱腹合理度 → 额度利用率 → 实付
  │
  ├─[MCP]  query-meal-detail            ← 套餐可替换项
  ├─[MCP]  calculate-price              ← 强制核价（回写真实实付）
  ├─[MCP]  campaign-calendar            ← 当天活动彩蛋
  │
  └─[输出] render_report()              ← Markdown 报告，2–3 个方案
       │
       └─ 用户确认后 → [MCP] create-order → 支付链接
```

### 双约束优化问题的形式化描述

这是一个带健康约束的背包变体：

```
maximize    Σ kcal(i) / credit_kcal          (额度利用率)
subject to  Σ kcal(i) ≤ credit_kcal          (热量不超额度)
            Σ price(i) - discount ≤ budget   (实付不超预算)
            fat_ratio(combo) < 0.45  或  combo 含解腻项   (健康硬约束)
            |combo| ≤ 4                      (不做无意义的堆砌)
            anchor_count(combo) ≥ 1          (首选方案须有主食主体)
```

与"省钱类"工具的差异：后者的目标函数是 `min 实付金额`，
本 Skill 的目标是在**预算内把运动换来的额度吃到最合适**——
同样花 24 块，钱花得更"值"。

## 四、业务价值

### 对用户

| 价值 | 说明 |
|---|---|
| 解决真实纠结 | 「练完能不能吃麦当劳」是高频决策困境，此前无工具覆盖 |
| 量化而非说教 | 用 MET 模型给出具体额度，而不是笼统的"少吃点" |
| 吃得便宜 | 四路合券 + 强制核价，多场景下实付降至 ¥24 |
| 有健康护栏 | 额度封顶、脂肪/钠预警、蛋白质提示，避免"练完放开吃" |
| 诚实 | 营养数据缺失时明确标注，不假装算过 |

### 对麦当劳

| 价值 | 说明 |
|---|---|
| 拓展增量场景 | 触达"运动后"这一此前未被主动服务的消费时刻 |
| 提升转化质量 | 用户带着明确需求和额度来，决策更快、客单价更可控 |
| 券的高效分发 | 四路券合并展示，提升麦麦省领券率与用券率 |
| 品牌正向关联 | 把品牌与"运动、自律、健康生活方式"关联，而非单纯"高热量" |
| 场景化活动曝光 | `campaign-calendar` 让当天活动在决策时刻自然露出 |

## 五、降级与容错

| 故障 | 影响 | 处理 |
|---|---|---|
| `now-time-info` 失败 | 无法判断时段 | 用对话上下文推断 |
| `query-nearby-stores` 失败 | 无门店编码 | 继续，用门店默认口径或请用户指定 |
| `list-nutrition-foods` 失败 | 无营养数据 | **明确告知**用户本次无法做热量约束，不静默降级 |
| `query-meals` 失败 | 无菜单 | 关键路径，终止并提示稍后重试 |
| 三路取券全失败 | 无券 | 按原价计算，提示可先领券 |
| `calculate-price` 失败 | 无法核价 | 保留本地估算金额，**明确标注"未核价"** |
| `campaign-calendar` 失败 | 无彩蛋 | 静默跳过 |
| 401 | 全部失败 | 提示重新申请 Token，引导用 `--demo` |
| 429 | 请求被拒 | 指数退避重试 3 次；仍失败则提示稍后 |

**降级原则**：能给部分结果就给部分结果，但**绝不假装拿到的数据是完整的**。

## 六、安全与合规

- Token 仅从环境变量 `MCD_MCP_TOKEN` 读取，任何文件中都不出现真实值
- `mcp-config.example.json` 只使用 `${MCD_MCP_TOKEN}` 占位符
- `examples/demo_mock_data.json` 全为示意数据，不含真实订单号、手机号、地址
- 下单前强制二次确认，不自动创建订单
- 不调用博彩观感类工具
- 输出固定附带免责声明，明确不构成医疗或营养建议

## 七、扩展方向

| 方向 | 说明 | 所需 Tool |
|---|---|---|
| 饮食节律 | 结合历史订单判断近期营养结构，避免连续高脂 | 新增 `order-list` |
| 训练周期配餐 | 增肌/减脂/备赛不同周期采用不同额度策略 | 现有能力即可 |
| 团餐平衡 | 团队点餐时做整体营养平衡 | `query-meal-assistance` |
| 门店个性化 | 按门店库存动态调整推荐 | 现有能力即可 |

---

最后更新：2026-10-09
