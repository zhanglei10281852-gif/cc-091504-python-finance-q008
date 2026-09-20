# 结构性票据核算服务

面向股票挂钩结构性票据（自动赎回票据）观察与现金流核算的 Python 引擎，
覆盖敲入、敲出、自动赎回、票息累计（含记忆）、到期交付与发行人更正的全生命周期。

## 核心原则

- **观察固定证据**：每个观察版本记录当时适用的条款版本与行情版本（价格 ID、
  版本、状态），历史版本不可改写，争议可复原。
- **缺价挂起而非跳过**：观察日缺价或仅有临时价（provisional）时，观察进入
  `pending` 待确认；只有官方价（official）能确认观察。临时价永不确定现金流。
- **更正即新版本**：行情补发、撤销、更正与条款修订都以新版本进入并触发重算。
  未付现金流直接换版；已付现金流保留原记录，差额生成 `adjustment` 现金流
  另行结算，原付款永不抹除。依据被撤销的已付款项先标记 `under_review`，
  待依据重新确认后再结算差额。
- **结算幂等**：结算指令以 `instruction_id` 为幂等键，同一指令重试返回原结果，
  绝不重复付款；不同指令用于已付款现金流返回冲突。
- **计算路径留痕**：节假日顺延、公司行动调整、篮子成分变更（条款版本）都在
  证据与 `calc_path` 中留下可读路径。

## 模块

| 模块 | 职责 |
| --- | --- |
| `src/models.py` | 领域记录：条款版本、行情、公司行动、观察、现金流、结算 |
| `src/calendars.py` | 交易日历与顺延（following / modified_following），留顺延路径 |
| `src/store.py` | 每产品一条 JSONL 追加式事件日志（`.runtime/`），只增不改 |
| `src/engine.py` | 观察评估、障碍判定、现金流推导、更正重算、幂等结算 |
| `src/queries.py` | 运营视图（逐日证据/障碍/下一观察日/预计现金流）与争议复原 |
| `src/app.py` | HTTP 接口与 `reference/` 引导 |

## 数据布局

- `reference/domain.json` —— 枚举与精度约定（金额 2 位、比率 8 位、ROUND_HALF_UP）
- `reference/products/*.terms.json` —— 产品条款（首个版本）
- `reference/calendars/*.json` —— 节假日日历
- `reference/prices/*.prices.jsonl` —— 行情种子（每行一条价格记录）
- `reference/corporate-actions/*.ca.json` —— 公司行动种子
- `reference/cashflows/*.expected.json` —— 现金流样例（演示时间线终态，用于对账）
- `.runtime/products/{id}/events.jsonl` —— 运行期事件日志（重启回放，幂等）

样例数据由 `python3 scripts/build_sample_data.py` 确定性生成。

## API

```
GET  /health
GET  /products
GET  /products/{pid}
GET  /products/{pid}/operations      运营视图：逐日证据、障碍、下一观察日、预计现金流
GET  /products/{pid}/observations    全部观察（含版本）
GET  /products/{pid}/cashflows       现金流当前状态
GET  /products/{pid}/reconcile       与 reference 现金流样例对账
POST /products/{pid}/prices          行情进入（provisional/official/revoked，更正用 corrects）
POST /products/{pid}/terms           条款修订（新版本的 payload + effective_from）
POST /products/{pid}/corporate-actions
POST /products/{pid}/recompute
GET  /cashflows/{pid}~{key}/explain  争议复原：付款依据快照 + 后续更正 + 差额调整
POST /cashflows/{pid}~{key}/settle   {"instruction_id": "...", "actor": "..."}（幂等）
```

错误以 JSON 返回：400 参数/状态非法，404 不存在，409 冲突（重复付款等）。

## 运行

需要 Python 3.11 或更高版本（仅标准库）：

```bash
python3 src/index.py                 # 服务默认监听 8000
python3 -m unittest discover -s tests
python3 scripts/demo_scenario.py     # 端到端演示：停牌补价→付款→更正→差额调整→撤销补发
docker compose up --build
```

演示脚本以移动时钟回放完整争议时间线，并与 `reference/cashflows/` 样例对账，
全部断言通过时退出码为 0。
