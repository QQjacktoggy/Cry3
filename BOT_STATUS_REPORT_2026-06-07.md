# Cry3 Bot 狀態診斷報告
**生成時間**: 2026-06-07 (日期為文件時間戳)  
**基於**: 維護日誌 + 最新代碼審查 + Git 提交歷史

---

## 📊 I. 當前 VM Bot 運行狀態

### A. VM 環境信息
```
主機: cry3jack (34.80.75.138)
用戶: jack_shih
回購: /home/jack_shih/cry3
啟動路徑: /home/jack_shih/cry3/testnet
最後啟動時間: 2026-06-07 ~03:39 UTC+8
當前進程: PID 162963 (或最新的)
命令: ./.venv/bin/python /home/jack_shih/cry3/main.py
```

### B. 最新代碼狀態
| 指標 | 值 |
|------|-----|
| **最新提交** | `4eae221` (debug: add mainnet_buttons_enter/exit logs) |
| **之前版本** | `a9498a8` (feat: proactive SL maker) |
| **分支** | main |
| **DB 路徑** | `/home/jack_shih/testnet/data/gridbot_testnet.db` |

### C. Bot 健康狀況
✅ **正常運作** — 根據最後維護窗口
- Telegram polling 運行中
- Manual signal wait 狀態監控中
- 無 fatal errors

---

## 📈 II. 最近交易記錄

### 最後 5 個 Mainnet One-Run

| Run ID | 日期 | 策略 | 方向 | 狀態 | Entry | 數量 | 結果 |
|--------|------|------|------|------|-------|------|------|
| **cry3mn_1780799538307** | 2026-06-07 02:45-02:46 | S5_Stoch (score=77) | SHORT | COMPLETED | 1585.61 | 0.126 | ✅ SL maker 觸發後平倉 |
| **cry3mn_1780798188001** | 2026-06-07 02:15-02:24 | S1_BB_RSI (score=70) | LONG | COMPLETED | 1584.6 | 0.126 | ✅ 部分 TP + flat_detected |
| **cry3mn_1780790738810** | 2026-06-07 00:xx-xx:xx | S1_BB_RSI | LONG | COMPLETED(+BUG) | ~1584 | 0.126 | ⚠️ 20 次 TP sync 後炸裂 NameError |
| **cry3mn_1780791973100** | 2026-06-07 00:xx-xx:xx | (Unknown) | ? | FAILED | ? | ? | ❌ DCA 掛單後 TP cancel `-2011` 異常 |
| **cry3mn_1780794549534** | 2026-06-07 之後 | (Pending) | ? | ARMED | N/A | N/A | ⏳ 等待訊號觸發 |

### 交易績效（最近 7 天基準，來自報告文件）
```json
{
  "symbol": "ETHUSDC",
  "window_days": 7,
  "summary": {
    "manual_orders": 40,
    "realized_pnl": -33.01 USDC,
    "commission": 3.23 USDC,
    "net_pnl_ex_funding": -36.24 USDC,
    "maker_ratio": 50.7%,
    "win_rate": 70%,
    "profit_factor": 0.53
  },
  "best_trade": +9.05 USDC,
  "worst_trade": -65.10 USDC
}
```

---

## 🐛 III. 已修復的 Bug（2026-06-07 維護窗口）

### 1️⃣ TP Match Filter `NameError`
**提交**: `6ddeda5`  
**檔案**: `src/gridbot/mainnet/one_run.py:890`  
**問題**: 
```python
# 舊代碼：丟棄 qty 但仍引用它
desired_prices = {price for _, _, price in desired_orders if float(qty) > 1e-9}
```
**影響**: 導致 `cry3mn_1780790738810` 在 entry filled 後 20 次 TP sync 後崩潰  
**修復**:
```python
# 新代碼：正確解構 qty
desired_prices = {price for _, qty, price in desired_orders if float(qty) > 1e-9}
```

### 2️⃣ TP Cancel 缺失異常處理
**提交**: `bfb2c06`  
**檔案**: `src/gridbot/mainnet/one_run.py:782-795`  
**問題**: TP 訂單被市場吃掉後，再次 cancel 時拋 `-2011` / `-2022`，無 catch 導致 run FAILED  
**影響**: `cry3mn_1780791973100` DCA 後 TP sync 時失敗  
**修復**:
```python
for order in existing_tp:
    try:
        await self._client.cancel_order(position.symbol, int(order["orderId"]))
    except BinanceAPIException as exc:
        if exc.code in {-2011, -2022}:
            logger.info("tp_cancel_order_not_found", ...)
        else:
            raise
```

### 3️⃣ DB 路徑配置錯誤
**檔案**: `testnet/.env.testnet`  
**問題**: 相對路徑 `testnet/data/gridbot_testnet.db` + CWD = `testnet/` 導致雙重 testnet 目錄  
**修復**: 改為絕對路徑 `/home/jack_shih/testnet/data/gridbot_testnet.db`

---

## 🚀 IV. 最新功能（2026-06-07 新增）

### A. 主動式 SL Maker（Commit `a9498a8`）
✅ **已啟用** — `mainnet_sl_use_maker=True` 時生效

**變化**:
1. Entry 成交後立即掛 SL maker（之前是平倉時才掛）
2. DCA 後自動重掛 SL（用新平均價）
3. 平倉時取消 SL（避免 reduce-only 單殘留）

**預期效果**: 節省 ~0.04% taker fee（若 SL maker 成交）

### B. Loop 控制 + 冷卻（Commits `2918fbf` ~ `69cdd4b`）
✅ **已啟用** — 支援連續多個 one-run

**新功能**:
- `/mainnet 3` 啟動 3 個連續 runs
- SL exit 後 N 分鐘內同側同策略被冷卻
- UI 顯示進度 `[1/3]` + cooldown 倒數

### C. UI 改進（Commits `861a058` ~ `4eae221`）
✅ **已部署**

- ⏹ Stop-loop 按鈕（在 idle 狀態也顯示，修復了隱藏 bug）
- Mainnet status 詳細 markup 追蹤（debug log）
- Telegram command 事件記錄

---

## ⚠️ V. 已知待辦 / 尚未修復

### 優先級 🔴 HIGH

#### 1. Exit Reason 不一致（`cry3mn_1780798188001` 案例）
**現象**: 
- Event log 最後：`completed` (trigger=flat_detected)
- `mainnet_runs.exit_reason`：被記為 `TP`
- **實際**: position 被清倉時無任何 TP 成交

**根因**: `flat_detected` close → 結束，但後續邏輯覆蓋 exit_reason 為 TP  
**影響**: 交易記錄不準確，難以分析真實終止原因  
**修復難度**: 🟡 中等（需要檢查 close 邏輯的順序）

#### 2. DCA 失敗通知誤報
**現象** (`cry3mn_1780798188001`):
```
⚠️ DCA #1 掛單失敗
🧩 DCA #1 已掛
```
**根因**: `_maybe_recovery()` vs `_sync_take_profit_orders()` 在同一 cycle 內競速  
**實際狀況**: DCA 已成功掛上 (orderId 68193833862, GTX 1583.66, 400 USDC)  
**用戶影響**: 收到假警告，造成不必要擔心  
**修復難度**: 🟡 中等（需要同步 state 或延遲通知）

### 優先級 🟡 MEDIUM

#### 3. TP Sync 過度頻繁
**現象**: `cry3mn_1780798188001` 共 31 次 `take_profit_synced` 事件  
**原因**: partial fill 時 match algo 頻繁觸發 rebuild  
**成本**: API 額度消耗快，潛在 rate limit 風險  
**修復難度**: 🟡 中等（需要優化 match 邏輯）

#### 4. Signal 缺值 Fallback
**現象**: 若 signal JSON 缺 `wildcat.sl_pct` 欄位，SL 掛單失敗  
**修復方案**: Fallback 至 `signal.stop_loss` 反推 sl_pct  
**修復難度**: 🟢 簡單

### 優先級 🟢 LOW

#### 5. DB 資料遷移
**背景**: 異常路徑 `/home/jack_shih/cry3/testnet/testnet/data/gridbot_testnet.db` 內有歷史 run  
**待辦**: 寫一次性 migrate script，回填主 DB  

#### 6. Systemd Service
**背景**: 目前靠 `nohup` 手動管理  
**待辦**: 整理成 systemd service，支援自動啟動 + 監控

---

## 📋 VI. 審查清單（對本分支 `claude/vm-bot-issues-review-UpItf`）

- [ ] 最新 4 個提交 diff 中的邏輯是否正確（特別是 SL maker 和 loop）
- [ ] 是否有新的 NameError 或未 catch 的異常
- [ ] Telegram HTML escape 是否完整（避免 `Can't parse entities`）
- [ ] DB write 邏輯是否仍指向絕對路徑 `/home/jack_shih/testnet/data/`
- [ ] 下一個 run 的 `mainnet_run_events` 是否包含 `sl_maker_placed` 事件

---

## 🔧 VII. 下一步行動建議

### 立即優先 (This Session)
1. **修復 exit_reason 不一致** — 追蹤 close 邏輯中何處被覆蓋
2. **修復 DCA 誤報** — 同步 state 或延遲通知邏輯
3. **優化 TP sync** — 改進 match 演算法，減少不必要的 cancel/replace

### 次優先
4. Signal sl_pct fallback
5. DB 資料遷移
6. Systemd service 配置

### 監控指標
- 每日 run 數量 & 成功率
- TP sync 次數 (avg per run)
- API error rate (特別是 rate limit)
- Telegram error 頻率

---

## 📎 附件：最近提交統計

```
4eae221  debug: add mainnet_buttons_enter/exit logs to trace markup creation
97b54fa  debug: log mainnet_status_reply markup presence
2c9ad3e  chore: add telegram_cmd_mainnet_received log entry to /mainnet handler
861a058  fix: show ⏹ stop-loop button in idle mainnet markup (was hidden until loop started)
288803d  feat: add stop_loop button + status loop progress + cooldown display
69cdd4b  feat: loop cooldown — skip same (side, strategy) for N min after SL exit
2918fbf  feat: support looping N consecutive one-runs via Telegram inline buttons
ba5d5f1  docs: log SL maker proactive refactor and cry3mn_1780799538307 analysis
a9498a8  feat: proactive SL maker — place at entry, update on DCA fill, cancel on close
73ac445  docs: refine 2026-06-07 findings for cry3mn_1780798188001
fc2d054  docs: update 2026-06-07 log with cry3mn_1780798188001 findings
3332728  docs: add 2026-06-07 maintenance log with patches
bfb2c06  fix: tolerate -2011/-2022 when cancelling missing TP orders in sync
6ddeda5  fix: use qty from desired_orders tuple in TP match filter
```

**代碼改動集中度**: `src/gridbot/mainnet/one_run.py` (159 行新增/修改), `src/gridbot/telegram/handlers.py` (9 行新增)

---

## 📞 聯絡資訊 / 備註

- **VM 管理員**: jack_shih (cry3jack)
- **Git 提交者**: QQjacktoggy (punktoggy@gmail.com)
- **維護分支**: `claude/vm-bot-issues-review-UpItf`
- **同步源**: GitHub `origin/main`

