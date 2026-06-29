# Loop Chain Bug 診斷報告
**用戶情況**: 啟動 loop 3 次，第一個 run 做空，完成後沒有自動 arm 第二個 run  
**時間**: 2026-06-07 (推測)  
**狀態**: 🔴 Loop 可能已中止

---

## 📊 I. Loop 預期流程 vs 實際發生

### A. 預期流程
```
1. 用户: /mainnet 3
   ↓
2. Bot: 
   _loop_total = 3
   _loop_completed = 0
   _loop_cooldowns = {}
   ↓
3. Run #1 START → ENTRY_PENDING → RUNNING → COMPLETED (或 FAILED/CANCELLED)
   ↓
4. _finish_flat_run() 觸發
   ├─ _loop_completed: 0 → 1
   ├─ 檢查 exit_reason
   │  └─ 如果 SL → 設置冷卻 5 分鐘
   ├─ 發送完成通知
   ├─ 檢查條件：in_loop && completed(1) < total(3) ✅
   ├─ 運行 preflight() 檢查
   │  └─ 沒有持倉 ✅
   │  └─ 沒有非本系統掛單 ✅
   └─ ARM run #2
      ├─ 建立新 run 記錄
      ├─ 發送: "🔄 Loop 自動 arm 下一個 run"
      ├─ Run #2 進入 ARMED 狀態
      └─ 等待下一個 wildcat 信號
   ↓
5. 市場發出信號 → Run #2 自動進場
```

### B. 實際發生（根據你的描述）
```
1. ✅ /mainnet 3 啟動成功
2. ✅ Run #1 (SHORT) 已完成，顯示 (1/3)
3. ❌ 5 分鐘後，沒有「Loop 自動 arm 下一個 run」通知
4. ❌ 沒有新的 run #2 訊息
5. ❌ 沒有新的 wildcat 信號
```

---

## 🔍 II. 可能的原因 (按可能性排序)

### 🔴 **最可能 #1: Preflight 失敗（有殘留持倉或掛單）**

**檢查點**: `_preflight()` @ line 517-541

```python
# 第一個檢查：是否有持倉
position = await self._client.get_position(symbol)
if position:
    return "❌ mainnet 已有 {symbol} 持倉，拒絕啟動"

# 第二個檢查：是否有非本系統掛單
unmanaged = [row for row in open_orders if not row["clientOrderId"].startswith(prefix)]
if unmanaged:
    return "❌ mainnet 已有 {len(unmanaged)} 筆非本系統掛單"
```

**症狀**: 
- 如果 preflight 失敗，会发送通知: `"❌ Loop 自動 arm 失敗"`
- 並清空 loop 状态（`_loop_total = 0`）

**診斷方法**:
1. 登入 VM，查詢最新 Telegram 通知，看是否有 `"Loop 自動 arm 失敗"` 的紅色錯誤
2. 查詢 Binance，確認 ETHUSDC 是否真的平掉了（無持倉）
3. 查詢是否有殘留的掛單（特別是 TP 或 SL 的 reduce-only 單）

---

### 🟡 **可能 #2: 冷卻仍在進行中，但通知沒送到**

**檢查點**: `_finish_flat_run()` @ line 1277-1282, 1322-1338

```python
# 如果 exit_reason == "SL"，設置冷卻
if from_loop_chain and exit_reason == "SL":
    cooldown_until = now_ms + (5 * 60 * 1000)  # 5 分鐘
    self._loop_cooldowns[(side, strategy)] = cooldown_until

# 下一個 cycle，檢查冷卻
if cooldown_until > now_ms:
    # 發送: "⏳ Cooldown 中，跳過 arm"
    await self._notify(...)
    return  # 不 arm，只是等待
```

**症狀**:
- 5 分鐘內應該看到: `"⏳ Cooldown 中，跳過 arm"`
- 5 分鐘後應該自動 arm

**為什麼可能發生**:
- 第一個 run 的 `exit_reason` 可能是 `"SL"`（市價 close 後才平掉）
- 冷卻 key = `("SHORT", "S5_Stoch")` 可能沒有正確建立
- 或者 Telegram 通知丟失

**診斷方法**:
1. 查詢 VM 日誌，看有沒有 `mainnet_one_run_loop_cooldown_skip` log
2. 等待冷卻過期後，觀察是否自動 arm run #2

---

### 🟢 **可能 #3: 第一個 Run 的 Exit Reason 不是 SL（沒有設冷卻）**

**檢查點**: `_finish_flat_run()` @ line 1277

```python
if from_loop_chain and exit_reason == "SL":
    # 才設置冷卻
```

**症狀**:
- 如果 exit_reason 是 `"flat_detected"` 或 `"TP"`，不會設冷卻
- 應該立即 arm run #2（不用等 5 分鐘）

**診斷方法**:
1. 查詢 DB，看 `mainnet_runs` 中最後一個 run 的 `exit_reason` 是什麼
   ```sql
   SELECT run_id, exit_reason, status FROM mainnet_runs 
   WHERE run_id = 'cry3mn_1780816398170' LIMIT 1;
   ```

---

## 🛠️ III. 立即診斷步驟

### Step 1: 檢查最新的 Telegram 通知歷史

你的截圖中應該有這些訊息（按時間順序）：
```
1. ✅ Mainnet one-run 已啟動 (1/3)
2. 🟢 AUTO 做空 已掛 maker 單  [Run: cry3mn_1780816398170]
3. ✅ Mainnet one-run 已成交    [Entry price]
4. (多個 TP sync 或其他事件)
5. ※ Mainnet one-run 已啟平半   [SL maker 觸發或其他結束]
6. ✅ Mainnet one-run 已完成 (1/3)  [Exit reason: ?]
7. ??? 後續應該出現以下之一：
   a) "🔄 Loop 自動 arm 下一個 run (2/3)" ← 正常
   b) "⏳ Cooldown 中，跳過 arm" ← 冷卻中
   c) "❌ Loop 自動 arm 失敗" ← preflight 失敗
   d) (什麼都沒有) ← Bug！
```

**你的情況是** d) 什麼都沒有

### Step 2: 查詢 DB（在 VM 上執行）

```bash
# 連上 VM
ssh jack_shih@cry3jack

# 進入 DB
sqlite3 /home/jack_shih/testnet/data/gridbot_testnet.db

# 查詢最新的 runs
SELECT run_id, status, exit_reason, strategy_label, side 
FROM mainnet_runs 
ORDER BY armed_at_ms DESC LIMIT 5;

# 查詢第一個 run 的所有事件
SELECT event_type, event_time_ms, details_json 
FROM mainnet_run_events 
WHERE run_id = 'cry3mn_1780816398170'
ORDER BY event_time_ms;
```

### Step 3: 檢查 VM 日誌

```bash
# 查詢最新的錯誤
tail -100 /tmp/cry3_main.log | grep -iE "loop|cooldown|preflight|failed"

# 搜尋 run #1 的關鍵事件
grep "cry3mn_1780816398170" /tmp/cry3_main.log | tail -20
```

### Step 4: 手動檢查 Binance 狀態

```bash
# 或直接問我 API 情況
# - ETHUSDC 目前有沒有持倉？
# - 有沒有未平倉的掛單（特別是 reduce-only TP/SL）？
# - 最後的交易是什麼時候？
```

---

## 🐛 IV. 代碼中可能的 Bug

### A. Loop Cooldown 邏輯問題

**位置**: `_finish_flat_run()` line 1276-1282

```python
from_loop_chain = run.get("params", {}).get("actor") == "telegram_loop" or in_loop
if from_loop_chain and exit_reason == "SL":
    side = str((run.get("params") or {}).get("side") or run.get("side") or "").upper()
    ...
```

**潛在 bug**:
- 第一個 run 的 `params.actor` 是 `"telegram"`（不是 `"telegram_loop"`）
- 但 `in_loop = True`（因為 `_loop_total > 0`）
- 所以 `from_loop_chain` 應該是 True ✅

**但如果 `side` 或 `strategy` 無法正確提取呢？**
```python
if side and strategy:
    # 設置冷卻
else:
    # 不設置 — 直接進入 arm 邏輯
```

如果 `side` 為空，冷卻不會被設置，應該會立即 arm run #2。

---

### B. Exit Reason 覆蓋問題

**位置**: `_finish_flat_run()` line 1257

```python
exit_reason = run.get("exit_reason") or summary["exit_reason"] or reason
```

根據維護日誌，有已知的 exit_reason 不一致問題：
- `mainnet_run_events` 的最後事件可能是 `"flat_detected"`
- 但 `mainnet_runs.exit_reason` 被改為 `"TP"` 或其他

這可能導致冷卻 key 計算錯誤！

---

## 💡 V. 建議的修復

### 短期（立即做）

1. **檢查 Telegram 完整歷史** — 找出是否有 "Loop 自動 arm 失敗" 的通知
2. **查詢 DB** — 確認 exit_reason 和 loop 狀態
3. **檢查 Binance** — 確認持倉和掛單已清

### 中期（下次 run）

1. **加强 Loop Cooldown 日誌** — 在每個決策點都 log
   ```python
   logger.info("loop_chain_decision", 
       in_loop=in_loop, 
       completed=self._loop_completed,
       total=self._loop_total,
       side=side,
       strategy=strategy,
       cooldown_remaining=cooldown_remaining,
       will_arm_next=cooldown_remaining == 0)
   ```

2. **修復 Exit Reason 不一致** — （已知待辦項目 #1）

3. **改進冷卻超時通知** — 在冷卻結束時主動發送通知
   ```
   ✅ Cooldown 已結束，準備 arm run #3
   ```

---

## 🎯 VI. 你該立即做的事

1. **不要再觸發 loop**，先診斷這一次
2. **等待 5-10 分鐘**，觀察是否有自動 arm 的通知（或冷卻通知）
3. **查詢日誌和 DB**（用上面的命令）
4. **截圖完整的 Telegram 歷史** 給我
5. **確認 ETHUSDC 是否真的完全平掉** — Binance App 查一下

---

## 📝 參考代碼位置

| 檢查項 | 代碼位置 |
|--------|---------|
| Loop 初始化 | line 69-79 |
| Loop Status 顯示 | line 97-115 |
| Loop ARM | line 138-195 |
| Loop 結束邏輯 | line 1267-1293 |
| Loop Chain ARM | line 1308-1382 |
| Cooldown 邏輯 | line 1277-1282, 1318-1338 |
| Preflight 檢查 | line 517-541 |

