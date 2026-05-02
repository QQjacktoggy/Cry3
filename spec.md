# Binance Futures Grid Bot Monitor — 技術規格

> 最後更新：2026-05-02
> 本文件基於原始計畫、實際 API 回應資料分析、以及使用者需求確認後撰寫。

---

## 1. 專案概述

監控系統，用於追蹤幣安 **USD-M 永續合約** 網格交易機器人的表現，結合 Gemini AI 分析市況並建議策略調整，透過 Telegram Bot 接收報告。

### 關鍵決策

- **僅通知建議，不自動交易** — 所有調整由使用者手動在幣安完成
- **合約交易（非現貨）** — USD-M 永續合約，USDC 計價
- **逐倉模式（Isolated Margin）** — 每個交易對獨立保證金
- **AI 建議範圍包含槓桿倍數** — Gemini 可在預定義範圍內建議調整槓桿

---

## 2. 交易對與帳戶特性

### 交易對
| 交易對 | 類型 | 計價幣 |
|--------|------|--------|
| BTCUSDC | USD-M 永續合約 | USDC |
| ETHUSDC | USD-M 永續合約 | USDC |
| SOLUSDC | USD-M 永續合約 | USDC |

### 手續費結構（從 API 取得）
| 交易對 | Maker | Taker |
|--------|-------|-------|
| BTCUSDC | **0%** | 0.04% |
| ETHUSDC | **0%** | 0.04% |
| SOLUSDC | **0%** | 0.04% |

> 網格交易的限價單通常為 Maker（掛單成交），享受 0% 手續費。
> 但部分 taker 成交的訂單會被收取 0.04% 手續費。

### 保證金模式
- **逐倉（Isolated）** — 每個交易對有獨立保證金池
- **Position Side：BOTH** — 使用單向持倉模式

### Funding Rate
- 每 **8 小時** 結算一次（00:00, 08:00, 16:00 UTC）
- 費率可正可負，持多倉時正費率付費、負費率收費
- 從 API 觀察：BTC/ETH 的 funding rate 在 -0.005% ~ +0.01% 之間波動

---

## 3. API 架構

### 3.1 可用端點（已驗證）

#### FAPI 端點 ✅ 正常使用
| 端點 | 用途 | 備註 |
|------|------|------|
| `GET /fapi/v1/ticker/24hr` | 24h 行情 | `python-binance: futures_ticker()` |
| `GET /fapi/v1/markPrice` | 標記價/Funding Rate | `futures_mark_price()` |
| `GET /fapi/v1/fundingRate` | 歷史 Funding Rate | `futures_funding_rate()` |
| `GET /fapi/v1/klines` | K 線資料 | `futures_klines()` |
| `GET /fapi/v2/positionRisk` | 持倉資訊 | `futures_position_information()` |
| `GET /fapi/v2/account` | 帳戶餘額/保證金 | `futures_account()` |
| `GET /fapi/v1/userTrades` | 交易記錄 | `futures_account_trades()` |
| `GET /fapi/v1/allOrders` | 歷史訂單 | 含 grid 的 limit orders |
| `GET /fapi/v1/openOrders` | 當前掛單 | grid 機器人的掛單 |
| `GET /fapi/v1/income` | 損益流水 | REALIZED_PNL, COMMISSION, FUNDING_FEE 等 |
| `GET /fapi/v1/commissionRate` | 手續費率 | `futures_commission_rate()` |

#### SAPI Algo 端點 ❌ 不可用（已確認）

`/sapi/v1/algo/futures/` 系列端點需要「Strategy Trading」權限，**使用者的 API Key 無法開啟此權限**。
系統完全基於 FAPI 端點運作，不依賴 SAPI。

### 3.2 API 回應格式（實際資料）

#### userTrades 回應
```json
{
  "symbol": "BTCUSDC",
  "id": 457044631,
  "orderId": 55383291033,
  "side": "SELL",
  "price": "76272.5",
  "qty": "0.001",
  "realizedPnl": "0.08250000",
  "quoteQty": "76.2725",
  "commission": "0.03050900",
  "commissionAsset": "USDC",
  "time": 1777379898552,
  "positionSide": "BOTH",
  "buyer": false,
  "maker": false
}
```

關鍵觀察：
- `realizedPnl` 在 BUY 單為 `"0"`，SELL 單才有數值（因為平倉時才實現損益）
- `commission` 在 Maker 單為 `"0"`，Taker 單才收費
- `maker: true/false` 可用於區分掛單/吃單
- `positionSide: "BOTH"` 代表單向持倉模式

#### income 回應（損益流水）
```json
// 已實現損益（每筆成交）
{"incomeType": "REALIZED_PNL", "income": "0.08250000", "symbol": "BTCUSDC", "tradeId": "457044631"}

// 手續費
{"incomeType": "COMMISSION", "income": "-0.03050900", "symbol": "BTCUSDC", "tradeId": "457044631"}

// Funding Fee
{"incomeType": "FUNDING_FEE", "income": "-0.00349400", "symbol": "ETHUSDC"}

// 網格策略轉帳（建立/關閉）
{"incomeType": "STRATEGY_UMFUTURES_TRANSFER", "income": "-168.00000000", "info": "UM_GRID_CREATE"}
{"incomeType": "STRATEGY_UMFUTURES_TRANSFER", "income": "169.00248924", "info": "UM_GRID_CLOSE"}
```

關鍵觀察：
- `STRATEGY_UMFUTURES_TRANSFER` 記錄了網格策略的建立（負值 = 投入資金）和關閉（正值 = 回收資金）
- `UM_GRID_CREATE` / `UM_GRID_CLOSE` 配對可追蹤每輪網格的投入/回收
- 累計 `REALIZED_PNL` + `COMMISSION` + `FUNDING_FEE` = 淨損益
- 從 CLOSE - CREATE 差值可算出每輪網格的總利潤

#### allOrders 回應（訂單記錄）
```json
{
  "orderId": 55381533839,
  "symbol": "BTCUSDC",
  "status": "FILLED",
  "clientOrderId": "aos_usdt_4FW2v7AdgU0RBngi1vGJ",
  "price": "76190",
  "avgPrice": "76190.0000",
  "origQty": "0.001",
  "executedQty": "0.001",
  "timeInForce": "GTX",
  "type": "LIMIT",
  "reduceOnly": false,
  "side": "BUY",
  "positionSide": "BOTH",
  "time": 1777379082048,
  "updateTime": 1777379097069
}
```

關鍵觀察：
- `clientOrderId` 前綴 `aos_` 或 `aos_usdt_` 表示是策略（algo）自動下的單
- `timeInForce: "GTX"` = Post-Only（確保是 Maker）
- `reduceOnly: true` 的 SELL 單 = 平倉/止盈單
- 可用 `clientOrderId` 前綴過濾出網格機器人的訂單

### 3.3 網格交易 vs 手動交易過濾

使用者可能同時有手動下的合約單和網格 bot 的自動單。
系統透過 `clientOrderId` 前綴區分：

| 前綴 | 來源 | 處理方式 |
|------|------|----------|
| `aos_` / `aos_usdt_` | 網格 Bot（策略自動單）| ✅ 納入分析 |
| 其他 | 手動交易 | ❌ 排除 |

過濾邏輯：
```python
def is_grid_trade(order: dict) -> bool:
    cid = order.get("clientOrderId", "")
    return cid.startswith("aos_")
```

此過濾同時應用於：
- `/fapi/v1/userTrades` 的交易記錄（透過關聯的 orderId 查 allOrders 的 clientOrderId）
- `/fapi/v1/income` 中 `REALIZED_PNL` 和 `COMMISSION` 記錄（透過 tradeId 關聯）
- `STRATEGY_UMFUTURES_TRANSFER` 和 `FUNDING_FEE` 不需過濾（本身就是策略/持倉層級）

### 3.4 網格輪次與交易對關聯

`STRATEGY_UMFUTURES_TRANSFER` 記錄的 `symbol` 欄位為空，無法直接知道是哪個交易對。

關聯策略（按優先順序）：
1. **時間區間關聯**：找出 CREATE 和 CLOSE 之間發生的 trades/income，從中取得 symbol
2. **同時只有一個活躍網格**（目前使用模式）：最新的 CREATE 對應當前唯一活躍的交易對
3. **多網格歷史**：使用者曾同時跑多個交易對，歷史資料需用方法 1 推斷

---

## 4. 資料模型（基於實際 API）

### 核心流程變更

**原計畫**：依賴 SAPI algo 端點取得 grid order → sub orders 的完整結構
**新方案**：使用 FAPI 端點重建交易記錄

```
資料來源：
1. /fapi/v1/income (incomeType=STRATEGY_UMFUTURES_TRANSFER)
   → 追蹤每輪網格的 CREATE/CLOSE 時間和資金流
   → 配對計算：每輪投入金額、回收金額、利潤

2. /fapi/v1/income (incomeType=REALIZED_PNL,COMMISSION,FUNDING_FEE)
   → 每筆交易的已實現損益、手續費、funding 費用
   → 按 symbol 和時間區間聚合

3. /fapi/v1/userTrades
   → 每筆成交的價格、數量、方向
   → 用於分析網格的格距、成交分布

4. /fapi/v2/positionRisk
   → 當前持倉、未實現損益、槓桿、清算價
   → 即時風險監控

5. /fapi/v2/account
   → 帳戶保證金餘額、可用餘額
   → 保證金率計算

6. /fapi/v1/markPrice + /fapi/v1/fundingRate
   → 標記價格、當前和歷史 funding rate
```

### 網格輪次追蹤

從 `STRATEGY_UMFUTURES_TRANSFER` 可以配對出每輪網格的生命週期：

```
輪次 1: CREATE -168.46 → CLOSE +168.46 → 利潤 0.00
輪次 2: CREATE -168.46 → CLOSE +169.00 → 利潤 +0.54
輪次 3: CREATE -168.00 → (運行中)
```

---

## 5. 策略模板

### 5.1 策略定義（含合約參數）

| 策略 | 風險 | 格距 | 格數 | 區間寬度 | 槓桿 | 方向 | 適用條件 |
|------|------|------|------|----------|------|------|----------|
| conservative | 低 | 0.5–1.5% | 10–20 | 5–15% | 1–3x | NEUTRAL | 低波動、橫盤 |
| moderate | 中 | 1.0–2.5% | 8–15 | 10–25% | 2–5x | NEUTRAL/方向 | 正常波動 |
| aggressive | 高 | 2.0–5.0% | 5–12 | 20–40% | 3–10x | 方向性 | 高波動、震盪 |
| range_bound | 中 | 0.3–1.0% | 20–50 | 3–10% | 1–3x | NEUTRAL | 窄幅盤整 |
| trending | 中高 | 1.5–4.0% | 6–10 | 15–35% | 2–7x | 方向性 | 趨勢行情 |

### 5.2 Gemini 可建議的參數

| 參數 | 類型 | 說明 |
|------|------|------|
| `recommended_strategy` | 策略選擇 | 5 種之一 |
| `grid_spacing_pct` | 格距百分比 | 在策略邊界內 |
| `num_grids` | 格子數 | 在策略邊界內 |
| `price_range_width_pct` | 價格區間寬度 | 在策略邊界內 |
| `leverage` | 槓桿倍數 | **新增** — 在策略邊界內 |
| `direction` | 偏多/偏空/中性 | **新增** — LONG / SHORT / NEUTRAL |

---

## 6. Gemini AI 整合

### 6.1 System Prompt 框架

```
你是一個合約網格交易策略顧問，專精 USD-M 永續合約。你的角色嚴格限制在以下範圍：

1. 分析提供的市場數據和網格機器人表現指標
2. 從以下策略中選擇一個最適合當前市況的策略：{策略列表}
3. 在所選策略的參數邊界內建議調整，包含：
   - 格距百分比 (grid_spacing_pct)
   - 格子數 (num_grids)
   - 價格區間寬度 (price_range_width_pct)
   - 槓桿倍數 (leverage)
   - 方向偏差 (direction: LONG/SHORT/NEUTRAL)
4. 分析 Funding Rate 趨勢及其對持倉成本的影響
5. 評估清算風險，在距離清算價過近時發出警告
6. 以繁體中文提供清晰的分析理由

約束條件：
- 你不能建議創建新策略，只能從提供的列表中選擇
- 你不能建議超出邊界的參數值
- 你不能建議具體的買賣操作或市價單
- 你不能預測未來價格
- 你的輸出必須完全符合提供的 JSON schema
- 信心分數必須反映真實的不確定性
- 列出與當前條件相關的真實風險警告，特別是清算風險

合約交易專有分析維度：
- Funding Rate：持續正值表示多頭擁擠，持續負值表示空頭擁擠
- 保證金率：接近維持保證金時需降低槓桿或縮小區間
- 持倉方向：根據趨勢建議偏多/偏空/中性
- 清算風險：距離清算價的百分比
```

### 6.2 Response Schema

```python
class GeminiRecommendation(BaseModel):
    recommended_strategy: Literal["conservative", "moderate", "aggressive", "range_bound", "trending"]
    confidence: float  # 0.0–1.0
    parameter_adjustments: list[ParameterAdjustment]
    leverage_suggestion: int  # 在策略邊界內
    direction_suggestion: Literal["LONG", "SHORT", "NEUTRAL"]
    market_condition_summary: str  # 繁體中文
    reasoning: str  # 繁體中文
    risk_warnings: list[str]  # 繁體中文
    funding_rate_analysis: str  # Funding rate 趨勢分析
    liquidation_risk_assessment: str  # 清算風險評估
```

---

## 7. 資料庫 Schema

### 主要表（需要更新 migration）

```sql
-- 網格輪次追蹤（從 STRATEGY_UMFUTURES_TRANSFER 配對）
CREATE TABLE grid_sessions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol          TEXT,                -- NULL if not symbol-specific
    created_at_ms   INTEGER NOT NULL,    -- UM_GRID_CREATE time
    closed_at_ms    INTEGER,             -- UM_GRID_CLOSE time (NULL = running)
    invested_amount REAL NOT NULL,       -- CREATE transfer amount (absolute)
    returned_amount REAL,                -- CLOSE transfer amount
    net_profit      REAL,                -- returned - invested
    asset           TEXT NOT NULL,       -- USDC / USDT
    create_tran_id  INTEGER UNIQUE NOT NULL,
    close_tran_id   INTEGER UNIQUE,
    is_active       INTEGER DEFAULT 1
);

-- 交易記錄（從 /fapi/v1/userTrades）
CREATE TABLE futures_trades (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id        INTEGER UNIQUE NOT NULL,
    order_id        INTEGER NOT NULL,
    symbol          TEXT NOT NULL,
    side            TEXT NOT NULL,       -- BUY / SELL
    price           REAL NOT NULL,
    qty             REAL NOT NULL,
    quote_qty       REAL NOT NULL,
    realized_pnl    REAL NOT NULL,
    commission      REAL NOT NULL,
    commission_asset TEXT NOT NULL,
    time_ms         INTEGER NOT NULL,
    position_side   TEXT NOT NULL,
    is_maker        INTEGER NOT NULL,    -- 1=maker, 0=taker
    fetched_at_ms   INTEGER NOT NULL
);
CREATE INDEX idx_trades_symbol_time ON futures_trades(symbol, time_ms);

-- 損益流水（從 /fapi/v1/income）
CREATE TABLE income_records (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    tran_id         INTEGER UNIQUE NOT NULL,
    symbol          TEXT,
    income_type     TEXT NOT NULL,       -- REALIZED_PNL, COMMISSION, FUNDING_FEE, etc.
    income          REAL NOT NULL,
    asset           TEXT NOT NULL,
    time_ms         INTEGER NOT NULL,
    info            TEXT,
    trade_id        TEXT,
    fetched_at_ms   INTEGER NOT NULL
);
CREATE INDEX idx_income_type_time ON income_records(income_type, time_ms);
CREATE INDEX idx_income_symbol ON income_records(symbol, time_ms);

-- 保留既有表: market_snapshots, performance_snapshots, recommendations,
--            strategy_history, audit_log, app_config
-- 移除: grid_orders, grid_sub_orders（改用 futures_trades + income_records）
```

---

## 8. Telegram Bot 指令

| 指令 | 說明 |
|------|------|
| `/start` | 啟動 Bot，顯示歡迎訊息 |
| `/status` | 所有交易對即時狀態總覽 |
| `/status <對>` | 特定交易對詳細狀態 |
| `/metrics` | 詳細表現報告 |
| `/risk` | **新增** — 風險儀表板（保證金率、清算距離） |
| `/strategy` | 當前策略與參數 |
| `/analyze` | 立即觸發 Gemini 分析 |
| `/ask <問題>` | 向 Gemini 提問 |
| `/history` | 最近建議紀錄 |
| `/sessions` | **新增** — 網格輪次歷史（CREATE/CLOSE 配對） |
| `/pnl` | **新增** — 累計損益報表 |
| `/interval <分鐘>` | 設定抓取間隔 |
| `/pause` | 暫停定期抓取 |
| `/resume` | 恢復定期抓取 |
| `/help` | 列出所有指令 |

### 報告格式

```
📊 合約網格報告 — 2026/05/02 23:30

━━ BTC/USDC 永續合約 ━━
💰 標記價: $78,402 | 入場均價: $78,000
📈 已實現損益: +$1.23
💸 手續費: -$0.45 | Funding: -$0.01
🏦 淨損益: +$0.77
⚡ 槓桿: 5x | 逐倉
⚠️ 清算價: $65,200 (距離 -16.8%)
📊 保證金率: 35%
🔄 交易: 12 筆 (Maker: 8 / Taker: 4)
📐 當前網格投入: $168.00

━━ ETH/USDC 永續合約 ━━
...

🤖 Gemini 分析建議：
策略：穩健型 → 保守型（信心: 0.78）
槓桿：5x → 3x
方向：NEUTRAL
原因：BTC funding rate 連續正值，多頭擁擠...
⚠️ 清算風險評估：安全（距離 >15%）

[此為建議，請自行在幣安平台調整]
```

---

## 9. 技術棧

| 項目 | 技術 |
|------|------|
| 語言 | Python 3.11+ |
| 幣安 API | `python-binance` (AsyncClient) — FAPI 端點為主 |
| SAPI 呼叫 | `aiohttp` + 手動 HMAC 簽名（繞過 python-binance 路徑問題）|
| AI 分析 | `google-genai` (Gemini 2.0 Flash) |
| Telegram | `python-telegram-bot` v21 (asyncio) |
| 資料庫 | SQLite via `aiosqlite` |
| 排程 | `APScheduler` AsyncIOScheduler |
| 設定 | `pydantic-settings` + `.env` |
| 日誌 | `structlog` (structured JSON) |

---

## 10. 專案結構

```
cry3/
├── .env / .env.example / .gitignore
├── pyproject.toml
├── spec.md                           # 本文件
├── main.py                           # 入口點
│
├── config/
│   ├── settings.py                   # Pydantic Settings
│   └── strategies.py                 # 預定義策略模板（含槓桿/方向）
│
├── src/gridbot/
│   ├── core/
│   │   ├── app.py                    # 應用程式協調器
│   │   └── scheduler.py             # APScheduler 包裝
│   │
│   ├── binance/
│   │   ├── client.py                 # 幣安 API 客戶端（FAPI + SAPI）
│   │   ├── fetcher.py                # 資料抓取/同步協調
│   │   └── models.py                 # API 回應資料模型
│   │
│   ├── grid/
│   │   ├── analyzer.py               # 指標計算
│   │   └── models.py                 # 指標資料類別
│   │
│   ├── ai/
│   │   ├── gemini.py                 # Gemini 客戶端
│   │   ├── prompts.py                # 提示詞建構
│   │   └── models.py                 # Response schema
│   │
│   ├── telegram/
│   │   ├── bot.py                    # Application 設定
│   │   ├── handlers.py               # 指令處理器
│   │   └── formatters.py             # 報告格式化
│   │
│   ├── storage/
│   │   ├── database.py               # 連接管理 + 遷移
│   │   ├── repositories.py           # 資料存取層
│   │   └── migrations/
│   │       ├── 001_initial.sql
│   │       └── 002_futures_trades.sql
│   │
│   └── utils/
│       ├── logging.py                # 結構化日誌
│       └── retry.py                  # 非同步重試
│
├── scripts/
│   └── fetch_explore.py              # API 探索腳本
│
└── tests/
    ├── conftest.py
    ├── test_grid_analyzer.py
    ├── test_ai_prompts.py
    └── test_storage.py
```

---

## 11. 實作順序

### Phase 1：基礎建設 ✅ 已完成
- pyproject.toml, .env, settings, strategies, logging, retry, database, repositories

### Phase 2：幣安整合 ⚠️ 需要重構
- **重構 client.py**：改用 FAPI 端點為主，新增 `aiohttp` SAPI fallback
- **重構 models.py**：基於實際 API 回應格式（userTrades, income, position）
- **重構 fetcher.py**：改為基於 income + trades 的同步模式

### Phase 3：網格分析 ⚠️ 需要重構
- **重構 analyzer.py**：基於 `income_records` 計算損益，取消依賴 sub_orders
- **新增網格輪次分析**：從 `STRATEGY_UMFUTURES_TRANSFER` 追蹤每輪績效
- **更新 models.py**：加入合約特有指標

### Phase 3.5：合約風險模組（新增）
- strategies.py 加入 leverage/direction 邊界
- settings.py 加入風險告警閾值
- analyzer.py 加入清算距離、保證金率計算
- 新增 migration：002_futures_trades.sql

### Phase 4：AI 整合
- ai/models.py — 含槓桿/方向/Funding/清算風險欄位
- ai/prompts.py — 合約交易專用 System Prompt
- ai/gemini.py — Gemini 客戶端

### Phase 5：Telegram Bot
- formatters.py — 合約風格報告
- handlers.py — 含 /risk, /sessions, /pnl 指令
- bot.py — Application 設定

### Phase 6：組裝與啟動
- scheduler.py, app.py, main.py

### Phase 7：測試
- 單元測試、整合測試、端對端測試

---

## 12. 風險與注意事項

1. **SAPI 不可用**：已確認 API Key 無法開啟 Strategy Trading 權限，系統完全基於 FAPI。

2. **時間戳同步**：本機時間與幣安伺服器差異約 2 秒，需使用 server time 同步或設定較大 recvWindow。

3. **Funding Rate 計算**：每 8 小時變動一次，使用 `/fapi/v1/income?incomeType=FUNDING_FEE` 取得實際已扣金額，不做估算。

4. **網格輪次無 symbol**：`STRATEGY_UMFUTURES_TRANSFER` 不帶 symbol，需透過時間區間內的 trades 推斷所屬交易對。

5. **手動交易混雜**：使用 `clientOrderId` 前綴 `aos_` 過濾出網格交易，排除手動單。

6. **資金規模**：每輪網格投入約 $160-170 USDC，帳戶餘額約 $1 USDC（大部分在策略中）。

7. **API Rate Limit**：FAPI 每分鐘 1200 weight，三個交易對的抓取需合理分配。
