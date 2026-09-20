# 结构性票据核算服务

面向股票挂钩结构性票据观察与现金流核算的 Python 后端服务。引擎读取产品条款、标的篮子、观察日历、价格来源、障碍水平与现金流样例，处理敲入、敲出、自动赎回、票息累计、到期交付与发行人更正。

核心原则：

- 每个观察点固定当时适用的条款版本与权威行情（证据记录价格版本）；
- 临时价格只能进入待确认（`pending_confirmation`），停牌缺价按条款顺延并留痕，绝不静默跳过；
- 补发/撤销行情通过新版本重算**尚未支付**的现金流；**已经付款**的项目保留原记录，以差额调整（`adjustment`）体现；
- 结算按幂等键执行，同一指令重试不重复付款；
- 篮子变更、公司行动、节假日顺延均留下可复原的计算路径。

## 运行

需要 Python 3.11 或更高版本（仅标准库，无第三方依赖）：

```bash
python3 src/index.py --seed reference/seed   # 首次启动加载种子数据
```

服务默认监听 `8000` 端口，持久化写入 `.runtime/`（可用 `--runtime` 或 `RUNTIME_DIR` 覆盖）。执行测试：

```bash
python3 -m unittest discover -s tests
```

演示争议场景（停牌 → 补价 → 敲出 → 更正 → 差额追回）的完整证据链：

```bash
python3 src/demo.py
```

也可以运行 `docker compose up --build` 启动容器（compose 默认加载种子数据）。

## 接口

所有接口均为 JSON。写操作幂等/版本化，读操作面向运营与争议复原。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | 健康检查 |
| GET | `/products` | 产品列表与状态 |
| GET | `/products/{id}` | 运营视图：逐日证据、障碍判定、下一观察日、预计现金流 |
| GET | `/products/{id}/observations` | 观察记录（最新版本） |
| GET | `/products/{id}/cashflows` | 现金流（含调整单） |
| GET | `/products/{id}/audit` | 审计事件流 |
| GET | `/cashflows/{id}/explanation` | 争议复原：付款依据与更正差额 |
| POST | `/terms/{id}/versions` | 发布条款版本（篮子变更等） |
| POST | `/corporate-actions` | 登记公司行动（拆分/股息因子） |
| POST | `/market-data/prices` | 登记行情（official / provisional / suspended） |
| POST | `/market-data/corrections` | 发行人更正：`reissue` 补发 / `cancel` 撤销 |
| POST | `/products/{id}/observations` | 运行观察：`{"date": ...}` 单点或 `{"as_of": ...}` 批量到期 |
| POST | `/settlements` | 结算：`{"idempotency_key", "cashflow_id"}`，重试不重复付款 |

## 目录

- `src/engine/` — 引擎：条款/行情/观察/现金流/结算（`engine.py`）、纯计算（`pricing.py`）、存储（`store.py`）、查询视图（`report.py`）
- `src/seed.py` + `reference/seed/` — 争议批次种子数据（条款、日历、行情网格、事件时间线、现金流样例）
- `docs/domain.md` — 领域模型与状态机说明
- `reference/domain.json` — 可公开枚举与精度约定
