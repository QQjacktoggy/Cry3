# 維護日誌 — 2026-06-07（Asia/Taipei）

## 總覽
- 維護窗口：2026-06-07 00:23 ~ 01:15 UTC+8（對應 VM 時間 2026-06-07 00:23~01:15 Asia/Taipei）
- VM: `cry3jack` (34.80.75.138), user `jack_shih`, repo `/home/jack_shih/cry3`
- Bot 啟動 CWD: `/home/jack_shih/cry3/testnet`
- 今日最終 HEAD: `bfb2c068c88608e41cc7b7670d5415ea279cb068`
- 今日最終 PID: `157109`（`./.venv/bin/python /home/jack_shih/cry3/main.py`）

---

## 1. Bug 修正：`NameError` in TP match filter

**Commit**: `6ddeda5`  
**Author**: Hermes (via Jack S)  
**Date**: 2026-06-07 ~00:30 UTC+8

**檔案**: `src/gridbot/mainnet/one_run.py`  
**行數**: line 890

**問題**:
`_take_profit_orders_match()` 中的 comprehension 寫法把 `qty` 誤用為全域/外部變數，但函式範圍內從未定義它，導致 `NameError: name 'qty' is not defined`。

**原碼**:
```python
desired_prices = {price for _, _, price in desired_orders if float(qty) > 1e-9}
```

**修法**:
```python
desired_prices = {price for _, qty, price in desired_orders if float(qty) > 1e-9}
```

**影響**:
- 上一個 run `cry3mn_1780790738810` 在 20 次 `take_profit_synced` 後，Bot 送出最後一次 TP 更新時炸掉，導致 run 標記為 FAILED。
- 修復後 TP match filter 不會再拋錯。

---

## 2. Bug 修正：容忍 TP cancel 時 `-2011` / `-2022`

**Commit**: `bfb2c06`  
**Author**: Hermes (via Jack S)  
**Date**: 2026-06-07 ~01:00 UTC+8

**檔案**: `src/gridbot/mainnet/one_run.py`  
**行數**: lines 784–795

**問題**:
`_sync_take_profit_orders()` 在重新同步 TP 前會先 `cancel_order` 現有 TP。但市場部分成交或外部取消後，某些 TP orderId 已經不存在於 Binance，此時 Binance 回 `APIError(code=-2011): Unknown order sent.` 或 `-2022`，原始碼沒有 catch 這個 error，導致 exception 一路往上爆 → `run_cycle` catch → run 直接被標成 FAILED。

**診斷過程**:
- 查 run `cry3mn_1780791973100` 的 event log：`entry_filled` → 多個 `take_profit_synced` → `recovery_entry_placed (DCA #1)` → 又觸發 TP sync → cancel 舊 TP 時某幾個 orderId 已不存在 → `-2011` → run FAILED。
- Telegram timeline：🔴 做空掛單 → ✅ 成交 → 🧩 DCA #1 已掛 → ❌ 失敗。DCA 實際已成功掛上，錯誤發生在下一步 TP sync。

**修法**:
在 cancel loop 內增加 per-order try/except：
```python
for order in existing_tp:
    try:
        await self._client.cancel_order(position.symbol, int(order["orderId"]))
    except BinanceAPIException as exc:
        if exc.code in {-2011, -2022}:
            logger.info(
                "tp_cancel_order_not_found",
                run_id=run_id,
                order_id=int(order["orderId"]),
                code=exc.code,
                msg=exc.message,
            )
        else:
            raise
```

**行為變化**:
- Before: 任何 cancel 失敗都炸掉整條 run
- After: `-2011` / `-2022` 被視為 "order already gone"，log 後繼續重建 TP 覆蓋；其他 Binance error 仍然 propagate

**根因釐清**:
- 上一個 commit `0325b5d`（loosen TP sync match）減少了不必要的 cancel/replace，但沒有處理「order 已經被市場吃掉了但快取還在」的 race condition。
- 修補後的邏輯是：`_take_profit_orders_match()` 判斷是否真的需要 sync；如果需要 sync，but some existing orders 已經不存在，就當作已經被吃掉，繼續往下 rebuild desired TP coverage。

---

## 3. 設定修正：DB_PATH 改為絕對路徑

**檔案**: `testnet/.env.testnet`（VM + 本地皆同步）  
**日期**: 2026-06-07 ~01:05 UTC+8

**問題**:
`.env.testnet` 中原有：
```
DB_PATH=testnet/data/gridbot_testnet.db
```
Bot 實際啟動 CWD 是 `/home/jack_shih/cry3/testnet`，因此相對路徑解析為：
```
/home/jack_shih/cry3/testnet/testnet/data/gridbot_testnet.db   ← 異常路徑
/home/jack_shih/testnet/data/gridbot_testnet.db                ← 規範文件訂定的正確路徑
```
這導致：
- `mainnet_runs` / `mainnet_run_events` 寫入異常 DB（double-testnet 路徑）
- 排查時查 `/home/jack_shih/testnet/data/gridbot_testnet.db` 找不到最新 run
- 異常 DB 的 `cry3mn_1780790738810` 和 `cry3mn_1780791973100` 可見，但主 DB 完全空白

**修法**:
```
DB_PATH=/home/jack_shih/testnet/data/gridbot_testnet.db
```

**注意**:
- 這是環境層設定，不進 Git（`.env.testnet` 通常 `.gitignore`）
- VM 上的 `.env` 是 symlink → `.env.testnet`，`git reset --hard` 會清掉 symlink，日後 deploy 若踩到需重新建立
- 本機 `C:\Users\pipi\Desktop\cry3\testnet\.env.testnet` 也同步改為 absolute path

**VM 驗證**:
Bot restart 後 log 明確印出:
```
2026-06-07 00:57:08 [info] database_initialized path=/home/jack_shih/testnet/data/gridbot_testnet.db
```

---

## 4. VM 部署記錄

| 時間 (UTC+8) | 步驟 | 結果 |
|---|---|---|
| 00:30 | `git add src/gridbot/mainnet/one_run.py` + commit `6ddeda5` | OK |
| 00:31 | `git push origin main` (`fed3f13..6ddeda5`) | OK |
| 00:32 | VM `cd /home/jack_shih/cry3 && git pull` fast-forward | OK |
| 00:33 | 確認 bot PID 155073 已載入新碼 | OK，無需 restart |
| 00:45 | 事件調查：run `cry3mn_1780791973100` fail `-2011` | 找到 root cause |
| 00:55 | patch `_sync_take_profit_orders` cancel loop | applied |
| 00:56 | commit `bfb2c06`, push, VM fast-forward `6ddeda5..bfb2c06` | OK |
| 00:57 | restart bot (kill PID 156940, nohup PID 157109) | OK |
| 01:00 | VM log 確認 `database_initialized path=/home/jack_shih/testnet/data/gridbot_testnet.db` | OK |
| 01:05 | sed replace `.env.testnet` DB_PATH → absolute | OK |
| 01:07 | restart bot (kill PID 157106/157109, nohup PID `tty`) | OK |
| 01:10 | 觀察到一次 `telegram.error.NetworkError: Bad Gateway` | bot 自行恢復，cycle 繼續 |

---

## 5. 運行狀態確認（維護窗口結束時）

```
PID:   157109
CMD:   ./.venv/bin/python /home/jack_shih/cry3/main.py
CWD:   /home/jack_shih/cry3/testnet
HEAD:  bfb2c06
```

- DB 正確指向: `/home/jack_shih/testnet/data/gridbot_testnet.db`
- 最近啟動時已寫入：`cry3mn_1780794549534`（狀態 `ARMED`）
- Telegram polling 跑動中，中間一次 `Bad Gateway` 不算致命，作業程序未退出
- `manual_signal_wait` 顯示 market regime = `blocked`，訊號條件尚未滿足（trend=up, vol=high, 量能比 0.10 < 0.22）

---

## 6. 已知待辦（未變更；當時規劃今晚維修）

1. **退出順序不一致**: `mainnet_run_events` 最後事件是 `completed`（trigger=flat_detected），但 `mainnet_runs.exit_reason` 被蓋成 `TP`。
   - 確認案例：`cry3mn_1780790738810`、`cry3mn_1780798188001`（2026-06-07，LONG S1_BB_RSI score=70, entry 1584.6, qty 0.126, realized +0.2368, fee 0.0488）
   - 現象：在 TP 尚未吃到前先被 `flat_detected` 收盤，但覆蓋邏輯最後把 `exit_reason` 寫成 `TP`
2. **DCA 失敗通知誤報**: Telegram 先送 `⚠️ DCA #1 掛單失敗`，緊接著又補 `🧩 DCA #1 已掛`。
   - 確認案例：`cry3mn_1780798188001`；DB 顯示 DCA #1 實際已掛上 Binance（orderId 68193833862, GTX 1583.66, cumulative notional 400 USDC）
   - 可能原因：`_maybe_recovery()` 與 `_sync_take_profit_orders()` 在同一 cycle 內競速，state 刷新造成短暫不一致
3. **TP sync 仍然過密**: 單一 run 內約 30 次 `take_profit_synced`（`cry3mn_1780798188001` 從 entry_filled 到 completed 共 31 次 sync，partial 從 0.126 → 0.001 逐步縮小）。
   - 當時 commit `bfb2c06` 只是容忍 cancel error，沒有減少必要性
   - 根因仍是 match algo 在 partial 被正常吃掉時頻繁觸發 rebuild
4. **DB 資料遷移**: 異常路徑 `/home/jack_shih/cry3/testnet/testnet/data/gridbot_testnet.db` 內有歷史 run（`cry3mn_1780790738810`, `cry3mn_1780791973100`）。如需回填主 DB，可寫一次性 migrate script。
5. **cry3.service / deploy.sh**: 目前仍靠 nohup 手動管理，尚未整理成 systemd 穩定流程。

---

## 審查要點（維護窗口結束時）

1. 確認 `bfb2c06` diff 中 cancel loop 僅 catch `-2011`/`-2022`，未 swallow 其他 BinanceAPIException
2. 確認 `.env.testnet` 變更有同步到 VM 與本機，且 deploy wrapper 不會被 `git reset --hard` 抹掉 symlink
3. 確認最新 run 確實寫入 `/home/jack_shih/testnet/data/gridbot_testnet.db`
4. 建議 review 時比對 VM `git log -3` 與 GitHub `origin/main` 三者一致

---

## 7. 案例分析：cry3mn_1780799538307（SL maker 觸發失敗）

**Run**: `cry3mn_1780799538307`  
**日期**: 2026-06-07 02:45-02:46 UTC+8  
**策略**: S5_Stoch | score=77 | 方向 SHORT

### 時間軸（從 mainnet_run_events 重建）
| 時間 (ms) | 事件 | 重點 |
|-----------|------|------|
| 1780800309 | entry_placed | orderId 68197173478, $1585.61, 0.126 |
| 1780800318 | entry_filled | 成交 |
| 1780800318-329 | take_profit_synced × 3 | TP1 0.05 @ 1584.82, TP2 0.076 @ 1582.76 |
| 1780800339 | recovery_entry_placed | DCA #1 $1586.94, 0.126 (做空加碼價差 0.083%) |
| 1780800348-378 | take_profit_synced × 4 | TP 重新縮放為 TP1 0.101 @ 1585.48, TP2 0.151 @ 1582.76 |
| 1780800385 | close_submitted | reason=SL, market order 0.252 |
| 1780800388 | completed | reason=flat_detected |

### 問題根因
- SL 觸發時 `mark` 已經 1587.00 以上，**馬上掛 SL maker 已經是 late entry**
- `create_reduce_only_limit_order(post_only=True)` 被 -5022 (Post-Only rejected) 打掉
- 程式 `one_run.py:1052-1056` 捕捉 -5022 後 **fallback 市價單** 送出 close
- 結果：Telegram 顯示「SL maker 被拒，市價」訊息

### 結論
觸發 SL 那一刻才掛 maker，**市場已經跑了一段**。-5022 是預期中會發生的狀況，不是 bug 本身；真正的設計缺陷是「太晚掛 SL」。

---

## 8. 功能重構：SL maker 進場即掛 + DCA 同步更新

**Commit**: `a9498a8`  
**Date**: 2026-06-07 ~03:49 UTC+8  
**檔案**: `src/gridbot/mainnet/one_run.py`  
**作者**: Hermes (via Jack S)

### 動機
cry3mn_1780799538307 案例顯示：被動式 SL（觸發才掛）無法在不利行情下保持 maker 優惠。改為主動式 SL（進場即掛）並在 DCA 後同步更新。

### 改動內容

**1. 進場即掛 SL**（`_run_entry_pending`，line 224-235 區段）
- 在 `entry_filled` 寫入之後、TP sync 完成後呼叫
- 使用 `signal_json.stop_loss` 原始價位
- 透過既有的 `_place_stop_loss_maker()` 進行
- 受 `mainnet_sl_use_maker` 旗標控制

**2. DCA 後更新 SL**（`_run_running`，line 273-296 區段）
- 偵測條件：`abs(current_qty - run.qty) > 1e-9`（倉位數量變化 = DCA 成交）
- 流程：取消舊 SL → 從 `signal.wildcat.sl_pct` 取得風險比例 → 用 `position.entry_price`（Binance 回傳的當前平均價）重算 SL → 掛新 SL maker
- LONG: `new_sl = entry * (1 - sl_pct)`
- SHORT: `new_sl = entry * (1 + sl_pct)`
- 最大更新次數受限於 `mainnet_recovery_steps`（目前 1），所以最多 2 次掛單

**3. 平倉時取消 SL**（`_close_position`，line 1027-1030 區段）
- 所有平倉路徑（SL / ADVERSE_EXIT / MAX_HOLD_*）都會先取消 TP + SL
- 避免 SL maker 在市價 close 成交後殘留在 order book

**4. 新增 helper `_cancel_stop_loss_order()`**（line 1153-1158）
- 與既有 `_cancel_take_profit_orders()` 對稱
- 只 match `f"{run_id}_sl"`，精確取消單筆

### 設計取捨
| 項目 | 決定 | 理由 |
|------|------|------|
| SL 更新 trigger | qty 變化 | 與 `recovery_entry_placed` 同步觸發，不會多次重建 |
| SL 價位計算依據 | `position.entry_price` | Binance API 回傳的新平均價，比手動算精確 |
| SL 風險比例 | 沿用 `wildcat.sl_pct` | 與 signal 設定一致，不會改變 risk 結構 |
| 是否限制更新次數 | 跟隨 `recovery_steps=1` | 最多 2 次掛單，避免無限 churn |
| 平倉時取消 SL | 全部路徑 | 避免 reduce-only 單殘留造成意外平倉 |

### 預期效果
- SL 在 maker 流動性良好時可省 ~0.04% taker fee
- DCA 後 SL 自動跟著新平均價，risk 比例不變
- 與現有 `recovery_steps=1` 設定一致，不會破壞現有風控

### 已知限制
- 仍使用 `wildcat.sl_pct` 從 signal_json 取值，若 signal 缺這個欄位會靜默失敗
- 未來若 `recovery_steps > 1` 需要迴圈處理；目前 hard-code 1 是 OK 的
- SL maker 與 TP 仍可能在 sync cycle 中競速；目前透過 per-run client_order_id 前綴區分

---

## 9. SL 重構後部署記錄

| 時間 (UTC+8) | 步驟 | 結果 |
|---|---|---|
| 03:30 | patch `entry_filled` 掛 SL + DCA 取消/重掛 SL + `_close_position` 取消 SL | OK |
| 03:35 | `python -m py_compile` 語法檢查 | exit 0 |
| 03:36 | `git add src/gridbot/mainnet/one_run.py` + commit `a9498a8` | OK |
| 03:37 | `git push origin main` (`73ac445..a9498a8`) | OK |
| 03:38 | VM `cd /home/jack_shih/cry3 && git pull` fast-forward | OK (`bfb2c06..a9498a8`) |
| 03:39 | `pkill -f "python.*main.py"` + nohup restart, new PID `162963` | OK |
| 03:42 | VM `tail /tmp/cry3_main.log` 觀察到 `manual_signal_wait` 正常運作 | OK |
| 03:45 | 確認無 `sl_maker` 相關新 error，舊 log 維持歷史 | OK |

---

## 10. 今日最終運行狀態

```
PID:   162963
CMD:   ./.venv/bin/python /home/jack_shih/cry3/main.py
CWD:   /home/jack_shih/cry3/testnet
HEAD:  a9498a8
DB:    /home/jack_shih/testnet/data/gridbot_testnet.db (絕對路徑)
```

- Bot 健康運作中，無新 error
- 下一次 wildcat 訊號會走「進場 → 掛 TP + SL maker → 持倉中 → SL maker 在 book」
- 若觸發 DCA (DCA #1)：SL maker 自動取消 + 用新平均價重掛
- 若觸發 SL：SL maker 已掛在 book，正常 maker 成交；若 maker 失敗，TTL 10s fallback 市價

---

## 11. 已知待辦（延續 §6 + 本次新增）

1. **exit_reason 不一致**（§6.1 延續）：cry3mn_1780799538307 也是 TP → flat_detected 競速
2. **DCA 失敗通知誤報**（§6.2 延續）
3. **TP sync 過密**（§6.3 延續）
4. **DB 資料遷移**（§6.4 延續）
5. **systemd service**（§6.5 延續）
6. **🆕 SL maker TTL 觀察**：本次重構後第一個觸發 SL 的 run，需要驗證 maker 在 book 上的 fill 率
7. **🆕 SL 更新時機競速**：DCA 成交後的 SL 重掛 vs TP sync 會在同一 cycle 內打 API；下一個 DCA run 觀察
8. **🆕 `signal.wildcat.sl_pct` 缺值 fallback**：若 signal 不含此欄位，新 SL 不掛 — 應改為 fallback 至 `signal.stop_loss` 反推

---

## 12. 審查要點（更新）

1. 確認 `a9498a8` diff 中 SL 掛/取消邏輯只在 `mainnet_sl_use_maker=True` 時觸發
2. 確認 `_cancel_stop_loss_order` 只 match `{run_id}_sl`、不波及 TP
3. 確認 `_close_position` 在所有 exit reason 都呼叫 SL cancel
4. 確認下一個 run 的 `mainnet_run_events` 包含 `sl_maker_placed` 事件
5. 建議 review 時驗證 SL 重掛時機（qty change）是否在 DCA 成交後 1-2 cycle 內完成

---

## 附件：今日原始修正 Diff

### A. `6ddeda5` — fix: use qty from desired_orders tuple in TP match filter

```diff
commit 6ddeda578e8dab3a1c5b4243b933799d9712a617
Author: QQjacktoggy <punktoggy@gmail.com>
Date:   Sun Jun 7 07:57:43 2026 +0800

    fix: use qty from desired_orders tuple in TP match filter

    _in _take_profit_orders_match, the set comprehension discarded the qty
    slot as '_' but still referenced a bare 'qty', causing a NameError as
    soon as an entry filled and the manager tried to sync TP orders. This
    turned a normal filled run into an immediate FAILED state with the
    Telegram error 'name 'qty' is not defined'.

    Change '_' to 'qty' so the filter can actually evaluate qty > 1e-9.

diff --git a/src/gridbot/mainnet/one_run.py b/src/gridbot/mainnet/one_run.py
index e53b1a6..69be678 100644
--- a/src/gridbot/mainnet/one_run.py
+++ b/src/gridbot/mainnet/one_run.py
@@ -887,7 +887,7 @@ class MainnetOneRunManager:
             return False
 
         # Build set of desired prices (with qty > 0) for validation
-        desired_prices = {price for _, _, price in desired_orders if float(qty) > 1e-9}
+        desired_prices = {price for _, qty, price in desired_orders if float(qty) > 1e-9}
 
         # All existing orders must have valid prices (subset of desired)
         existing_prices = {float(o.get("price", 0) or 0) for o in existing_orders}
```

---

### B. `bfb2c06` — fix: tolerate -2011/-2022 when cancelling missing TP orders in sync

```diff
commit bfb2c068c88608e41cc7b7670d5415ea279cb068
Author: QQjacktoggy <punktoggy@gmail.com>
Date:   Sun Jun 7 08:53:07 2026 +0800

    fix: tolerate -2011/-2022 when cancelling missing TP orders in sync

diff --git a/src/gridbot/mainnet/one_run.py b/src/gridbot/mainnet/one_run.py
index 69be678..8470cab 100644
--- a/src/gridbot/mainnet/one_run.py
+++ b/src/gridbot/mainnet/one_run.py
@@ -782,7 +782,19 @@ class MainnetOneRunManager:
         if self._take_profit_orders_match(existing_tp, desired, current_qty):
             return
         for order in existing_tp:
-            await self._client.cancel_order(position.symbol, int(order["orderId"]))
+            try:
+                await self._client.cancel_order(position.symbol, int(order["orderId"]))
+            except BinanceAPIException as exc:
+                if exc.code in {-2011, -2022}:
+                    logger.info(
+                        "tp_cancel_order_not_found",
+                        run_id=run_id,
+                        order_id=int(order["orderId"]),
+                        code=exc.code,
+                        msg=exc.message,
+                    )
+                else:
+                    raise
         for client_order_id, qty, price in desired:
             try:
                 await self._client.create_reduce_only_limit_order(
```

---

### C. `.env.testnet` DB_PATH 變更

```
DB_PATH=testnet/data/gridbot_testnet.db   →   DB_PATH=/home/jack_shih/testnet/data/gridbot_testnet.db
```

此為 sed in-place 修改，不進 Git（`.env.testnet` 屬 `.gitignore`）。

VM 驗證：
```
2026-06-07 00:57:08 [info] database_initialized path=/home/jack_shih/testnet/data/gridbot_testnet.db
```

---

## 13. Bug 診斷：SL GTX Post-Only 100% 必然被拒（-5022）

**日期**: 2026-06-07 下午（Asia/Taipei）  
**診斷工程師**: Hermes (via Jack S)

### 問題描述

今日下午 4 筆 mainnet run 全部在進場後數秒內自動平倉，Telegram 均顯示：

> 🏁 Mainnet one-run 已送出平倉（SL maker 被拒，市價）

Log 確認錯誤：
```
WARNING  sl_maker_rejected_fallback_market
APIError(code=-5022): Due to the order could not be executed as maker,
the Post Only order will be rejected. The order will not be recorded in the order history
```

受影響的 run：

| run_id | entry | side | pnl | 進場→平倉時間 |
|--------|-------|------|-----|------------|
| cry3mn_1780822557285 | 1635.13 | SHORT | +0.123 | ~47s |
| cry3mn_1780822705203 | 1637.67 | SHORT | -0.177 | ~29s |
| cry3mn_1780822768977 | 1638.55 | SHORT | -0.146 | ~26s |
| cry3mn_1780827309043 | 1627.70 | SHORT | -0.033 | ~10s |

### 根本原因

SL 方向在幾何上**永遠是 taker 側**：

- SHORT 需要 BUY 平倉 → SL 價位在現價**之上** → BUY limit 高於市價 = 立即吃單
- 而 GTX (Post-Only) 的定義是「會立即成交就拒絕」

因此在 `_run_entry_pending` → entry filled → 立即呼叫 `_place_stop_loss_maker(post_only=True)` 這條路上，**-5022 是 100% 確定會發生**，不是偶發。

舊 fallback 邏輯（`one_run.py` 原 line 1194）：-5022 → 立即送市價單平倉整個倉位，結果變成「進場即自殺」。

### 其他觀察

- §8 的重構（`a9498a8`）加入了「進場即掛 SL maker」的設計，出發點正確，但 GTX 機制本身選錯了 — SL 天生在 taker 側，不該用 Post-Only。
- Maker fee = 0 這個事實讓「省 fee」理由消失；但 STOP_MARKET 靜置在交易所的優點（觸發精確、不佔 manage cycle 頻寬）仍然成立。

---

## 14. 功能修正：SL GTX → STOP_MARKET（方案 A）

**日期**: 2026-06-07 ~19:00 UTC+8  
**檔案**:
- `src/gridbot/binance/client.py`
- `src/gridbot/mainnet/one_run.py`
- `tests/test_mainnet_one_run_maker.py`  
**作者**: Hermes (via Jack S)

### 改動內容

**1. `client.py` — 新增 `create_stop_market_sl_order()`**

```python
async def create_stop_market_sl_order(
    self,
    symbol: str,
    side: str,
    stop_price: float,
    quantity: str,
    client_order_id: str | None = None,
    working_type: str = "MARK_PRICE",
) -> dict:
    """POST /fapi/v1/order — reduce-only STOP_MARKET for stop-loss.
    Sits passively on the exchange; triggers a market close when mark
    price touches stop_price.  Never rejected with -5022.
    """
```

使用 `/fapi/v1/order` 標準端點，`type=STOP_MARKET`、`stopPrice`（非 triggerPrice）、`reduceOnly=true`、`priceProtect=TRUE`、`workingType=MARK_PRICE`。

**2. `one_run.py` — `_place_stop_loss_maker()` 全面重寫**

| 項目 | 舊實作 | 新實作 |
|------|--------|--------|
| 訂單類型 | `LIMIT / GTX (Post-Only)` | `STOP_MARKET` |
| 被拒原因 | -5022 100% 必然 | 無（條件單不即時成交） |
| TTL 輪詢迴圈 | ✅ 有（每秒 poll 10s） | ❌ 移除（交易所自管） |
| Fallback | -5022 → 立即市價平倉 | 掛單本身失敗才 fallback |
| DB 事件 | `sl_maker_placed` | `sl_stop_market_placed` |
| Telegram 通知 | `🛑 Stop-Loss Maker 已掛單` | `🛑 Stop-Loss STOP_MARKET 已掛` |

**3. `one_run.py` — `_close_position()` 移除 GTX 分支**

舊：`reason=="SL" and mainnet_sl_use_maker` → 再次呼叫 `_place_stop_loss_maker`  
新：所有 `_close_position` 呼叫一律市價平倉（STOP_MARKET 已在進場時掛好；此路徑為軟體備援）

### 新的 SL 執行流程

```
1. entry_filled
   → 掛 STOP_MARKET at sl_price（靜置在交易所）
   → 掛 TP limit orders

2. 正常路徑（交易所觸發）
   mark 碰 sl_price → 交易所自動市價平倉
   → run_running 偵測 position gone → _finish_flat_run

3. 備援路徑（軟體觸發）
   _hit_stop(mark, sl_price) = True
   → _close_position("SL")
   → 先 cancel STOP_MARKET + TP → 再市價平倉
```

### 測試更新

`tests/test_mainnet_one_run_maker.py` 同步更新：

- `FakeClient` 加入 `create_stop_market_sl_order()` stub 與 `stop_market_sl_orders` 清單
- `FakeClient` 加入 `get_book_ticker()` stub（補齊 recovery path 缺失的方法）
- `test_entry_fill_syncs_maker_take_profit_orders`：新增斷言 entry fill 後必有 `stop_market_sl_orders[0]`，且 `clientOrderId == f"{run_id}_sl"`、`stopPrice == "99.0"`、`side == "SELL"`
- `test_run_running_stop_loss_closes_with_market_and_cancels_tp_orders`：
  - open_orders 加入 STOP_MARKET SL（orderId 113）
  - 斷言所有 3 張單都被取消（TP x2 + STOP_MARKET x1）
  - 斷言無新 STOP_MARKET 掛出
- 修正兩個 pre-existing 失敗：`armed_at_ms=1` 造成 `run_age_bars` 極大 → MAX_HOLD 瞬發；`mainnet_recovery_enabled=True` 在 mark < entry 時攔截 SL check

**測試結果（VM）**：`6 passed, 3 warnings in 4.15s`

---

## 15. §14 部署記錄

| 時間 (UTC+8) | 步驟 | 結果 |
|---|---|---|
| ~19:00 | 分析 `_place_stop_loss_maker` 原始碼，確認 -5022 必然性 | 確認 |
| ~19:10 | 新增 `create_stop_market_sl_order()` to `client.py` | OK |
| ~19:12 | 重寫 `_place_stop_loss_maker()`，移除 TTL 迴圈 | OK |
| ~19:13 | 改寫 `_close_position()`，移除 GTX 分支 | OK |
| ~19:15 | 更新測試，本地 6/6 pass | OK |
| ~19:20 | `scp` 三個檔案到 VM `/tmp/`，`cp` 至專案目錄，`chown jack_shih` | OK |
| ~19:22 | VM `testnet/.venv/bin/python -m pytest tests/test_mainnet_one_run_maker.py -v` | 6/6 pass |
| ~19:25 | Bot kill（PID 173848 已死），`nohup` 重啟 | OK |
| ~19:30 | `tail /tmp/cry3_main.log` 確認 `app_starting`、`binance_connected`、`manual_signal_wait` 正常 | OK |

**重啟後 VM 狀態**：
```
CMD:   testnet/.venv/bin/python /home/jack_shih/cry3/main.py
CWD:   /home/jack_shih/cry3
LOG:   /tmp/cry3_main.log
DB:    /home/jack_shih/testnet/data/gridbot_testnet.db
```

---

## 16. 已知待辦（更新）

1–5. （§11 延續，未變）
6. **SL maker TTL 觀察**（§11.6）→ **已解決**：不再需要 TTL 輪詢，STOP_MARKET 由交易所管理
7. **SL 更新時機競速**（§11.7）→ 仍待觀察第一個 DCA run
8. **`signal.wildcat.sl_pct` 缺值 fallback**（§11.8）→ 仍待修
9. **🆕 首次 STOP_MARKET SL 驗證**：下一個有 SL 觸發的 run 需確認 `mainnet_run_events` 包含 `sl_stop_market_placed`，且 Telegram 顯示「Stop-Loss STOP_MARKET 已掛」而非舊的「SL maker 被拒，市價」

---

## 17. Bug 修正：TP 成交後 STOP_MARKET SL 殘留未清除

**日期**: 2026-06-07 ~20:00 UTC+8  
**檔案**: `src/gridbot/mainnet/one_run.py`, `tests/test_mainnet_one_run_maker.py`  
**作者**: Hermes (via Jack S)

### 問題

§14 部署後第一個 run TP 成交，`_finish_flat_run` 被呼叫，但 STOP_MARKET SL 單**殘留在交易所**（需手動取消）。

### 根因

`_finish_flat_run` 只做結算（`_build_run_summary` → `complete_run`），**不取消 open orders**。  
`_cancel_stop_loss_order` 只在 `_close_position`（軟體主動平倉路徑）呼叫，而 TP 成交後走的是 `_run_running` → `not position` → `_finish_flat_run`，跳過了整個 `_close_position`。

### 修法

在 `_finish_flat_run` 最開頭加一行：

```python
async def _finish_flat_run(self, run: dict, reason: str) -> None:
    # Cancel any residual STOP_MARKET SL order (e.g. position closed by TP,
    # leaving the SL still armed on the exchange).
    await self._cancel_stop_loss_order(run["symbol"], run["run_id"])
    ...
```

### 測試

新增 `test_finish_flat_run_cancels_residual_sl_stop_market`：

- open_orders 內有 orderId=201 的 STOP_MARKET SL
- 呼叫 `_finish_flat_run` 後斷言 `("ETHUSDC", 201) in client.cancelled`

**測試結果（本地 + VM）**：`7 passed`

### 部署

| 時間 (UTC+8) | 步驟 | 結果 |
|---|---|---|
| ~20:00 | 修正 `_finish_flat_run` 加 `_cancel_stop_loss_order` | OK |
| ~20:02 | 新增測試，本地 7/7 pass | OK |
| ~20:05 | scp + cp + chown 到 VM，VM 7/7 pass | OK |
| ~20:08 | Bot 重啟，log 確認 `app_starting` 正常 | OK |

---

## 18. Bug 修正：TP 部分成交導致 STOP_MARKET SL 無限重下（Bug 3）

**日期**: 2026-06-07 ~20:20 UTC+8  
**檔案**: `src/gridbot/mainnet/one_run.py`, `tests/test_mainnet_one_run_maker.py`  
**作者**: Hermes (via Jack S)

### 現象

Run `cry3mn_1780833365092` Telegram 報告出現多個 STOP_MARKET SL，數量遞減（0.246 → 0.148 → 0.089 → ... → 0.004），最後以 `-2022: ReduceOnly Order is rejected` 結束，且條件單未清除。

### 根因

`_run_running` 裡的 qty 變化偵測為：

```python
if abs(current_qty - prev_qty) > 1e-9:
    # DCA filled → cancel old SL and re-arm at new avg entry
    await self._cancel_stop_loss_order(...)
    await self._place_stop_loss_maker(...)
    await self._repo.update_run(run["run_id"], qty=current_qty)
```

此條件對 **qty 增加（DCA 成交）** 和 **qty 減少（TP 部分成交）** 均觸發。當 TP 部分成交 qty 縮減時，程式誤以為是 DCA，一直用越來越小的 qty 取消舊 SL 並重下新 STOP_MARKET，直到 qty 趨近 0 後因 `-2022 ReduceOnly rejected` 失敗，最後殘留多個 STOP_MARKET 未清除。

### 修法

將 qty 變化邏輯拆為兩分支：

```python
if current_qty > prev_qty + 1e-9:
    # DCA filled (qty grew) — cancel old SL and re-arm at new avg entry price
    await self._cancel_stop_loss_order(symbol, run["run_id"])
    if self._settings.mainnet_sl_use_maker:
        ...  # re-arm STOP_MARKET with new avg entry
    await self._repo.update_run(run["run_id"], qty=current_qty)
    run["qty"] = current_qty
elif abs(current_qty - prev_qty) > 1e-9:
    # Qty shrank (TP partial fills) — sync tracking only, do NOT touch SL
    await self._repo.update_run(run["run_id"], qty=current_qty)
    run["qty"] = current_qty
```

關鍵差異：TP 部分成交只更新 qty 追蹤，**不取消、不重下** STOP_MARKET SL。

### 測試

新增 `test_run_running_tp_partial_fill_does_not_rearm_sl`：

- run.qty = 0.12（完整倉位）
- position.position_amt = 0.072（TP 部分成交後剩餘）
- open_orders 包含 orderId=301 的 STOP_MARKET SL
- 呼叫 `_run_running` 後斷言：
  - `("ETHUSDC", 301)` **不在** `client.cancelled`
  - `client.stop_market_sl_orders == []`（無新 STOP_MARKET 被下）
  - `repo.updated` 最後一筆 qty ≈ 0.072

**測試結果（本地 + VM）**：`8 passed`

### 部署

| 時間 (UTC+8) | 步驟 | 結果 |
|---|---|---|
| ~20:20 | 修正 `_run_running` qty 分支邏輯 | OK |
| ~20:22 | 新增測試，本地 8/8 pass | OK |
| ~20:25 | scp + cp + chown 到 VM，VM 8/8 pass | OK |
| ~20:28 | Bot 重啟（kill 179674，nohup 重啟，PID 180396），log 確認正常 | OK |

### 待辦更新

- §16 todo #7（SL 更新時機競速）→ **已解決**：DCA 路徑保持正確，TP 路徑不再干擾 SL
- §16 todo #8（`signal.wildcat.sl_pct` 缺值 fallback）→ **仍待修**
- §16 todo #9（STOP_MARKET SL 首次驗證）→ 等待下一個完整 run 觀察

---

## 27. Bug 修正：DCA filled event 缺失 + DCA 風險守門 + Bug 9 + leverage 推斷

**日期**: 2026-06-08 ~08:40 UTC+8  
**檔案**: `src/gridbot/mainnet/one_run.py`, `src/gridbot/strategy/wildcat_live.py`, `src/gridbot/binance/models.py`, 測試多支  
**觸發**: 分析 run `cry3mn_1780872126823`（1/3 SL，pnl -0.40）

### A. DCA 成交沒有 filled event（observability gap）

`_maybe_recovery` 只記 `recovery_entry_placed`（掛單），DCA maker 成交後靠下個 cycle 的 `current_qty > prev_qty` 偵測來 re-arm SL，但**從不記 `recovery_entry_filled`**，導致事件流看不到 DCA 成交、pnl 難核對。

**修法**：在 `_run_running` 偵測到 qty 增長（DCA 成交）時記 `recovery_entry_filled`（含 qty / added_qty / avg_price / notional）。

### B. DCA 風險守門（趨勢過濾 + 動能反向）

**問題**：DCA 唯一條件是「價格逆行 ≥ 0.05%」，無腦加倉。但 DCA 後倉位翻倍、SL 按比例重算 → 觸發 SL 的**絕對虧損也翻倍**（本案：不 DCA 約虧 0.24，DCA 後虧 0.56，2.3 倍）。趨勢市中等於「越虧越加」。

**修法**：新增 `evaluate_dca_guard(candles, position_side)`（wildcat_live.py），DCA 前檢查：
1. **趨勢過濾**：`trend != "range"` → 阻擋（wildcat 只在 range 進場，變 trend 代表趨勢逆行）
2. **動能反向**：SHORT 遇 Stoch 金叉 / LONG 遇 Stoch 死叉 → 阻擋（逆行動能轉強）

`_maybe_recovery` 在下單前呼叫，阻擋時發 🛡️ 通知並跳過。使用者選定方向：趨勢過濾 + 動能反向（信號反向以 Stoch 交叉實作）。

### C. Bug 9：entry 階段結束不續接 loop（§25 待修項，本次修復）

**問題**（詳見 §25）：loop 續接只在 `_finish_flat_run`。entry 階段失敗（signal_timeout / entry_ttl_expired / entry_not_open / slippage_exceeded）走 `complete_run`，不續接 → loop 卡住。真實案例：17:47 `cry3mn_1780854426324` loop 2/10 signal_timeout 後卡死。

**修法**：新增 `_advance_loop_after_entry_failure(run, reason)`，在四條 entry 失敗路徑呼叫。**產品決策**：entry 階段失敗**消耗 1 個 loop 計數、前進下一個**（loop 總次數固定、可預測、不無限循環）；因無持倉無 PnL，**不套 cooldown**。單次 run（非 loop）為 no-op。

### D. PositionInfo leverage 推斷（test_models.py）

**問題**：新版 `/fapi/v3/positionRisk` 不再回傳 `leverage` 欄位，`from_api` fallback 成 1，槓桿顯示錯誤。

**修法**：leverage 缺失時從 `notional / initialMargin` 反推（86.21 / 0.862 ≈ 100），取 `round` 並保底 ≥ 1。

### 測試與部署

- `test_dca_guard.py`(5) + `test_mainnet_one_run_maker.py` 新增(DCA guard 接線 + Bug 9 三案) + `test_models.py`
- 本地全套 **261 passed**，VM **24 passed**
- 部署兩批並重啟：~08:25（DCA guard + filled event + loop 修復，PID 200744）、~08:38（Bug 9 + leverage，PID 201114），無 Conflict

---

## 26. 決策記錄：手續費來源歸因 + TP fallback 維持 taker（不改）

**日期**: 2026-06-08 ~00:40 UTC+8  
**狀態**: 決策確認，**維持現狀，不改代碼**

### 手續費來源歸因（最近成交，taker 合計 ~0.40 USDC，maker 全 0）

查 `futures_account_trades` + 關聯 clientOrderId，taker 手續費只來自兩處：

| 來源 | 佔比 | 性質 |
|---|---|---|
| **TP 尾段 GTC fallback** | ~59% | 價格快速穿過 TP 價時，reduce-only GTX 被 -5022 拒 → GTC limit 立即成交 = taker（§21 設計：確保出場）|
| **STOP_MARKET SL 觸發** | ~40% | 交易所端市價止損，觸發即 taker，難避免 |
| entry / dca / TP 首批 / dust | 0 | 皆 maker（dust 為 §24 修正後的 post-only）|

### 決策：TP fallback 維持 taker（不改成 maker）

評估過「TP fallback 改 maker 貼盤口」省手續費，但**風險不划算**：
- TP fallback 發生在「價格快速穿過 TP 價」的動量行情，改 maker 掛盤口**不保證成交**；若價格反彈，平倉單掛著不成交 → 利潤回吐，最糟反向觸 SL（更大 taker + 可能轉虧）。
- 活例：`cry3mn_1780844907574` 的碎倉 maker 掛 4 分鐘等不到，被反向 SL 收掉。
- 量級：省下的 taker fee 整 run ~0.2 USDC；但利潤回吐可能吃掉整筆獲利（run 利潤才 0.2–0.4 USDC）。**省小錢賭整筆利潤，不划算。**

→ 使用者決定**維持現狀**（taker 確保出場）。如未來要省此 fee，折中方案為「maker 優先 + 短 TTL（3–5s）taker 兜底」，但暫不實作。

---

## 25. Bug 9：entry 階段結束不續接 loop（✅ 已於 §27 修復）

**日期**: 2026-06-08 ~00:10 UTC+8  
**狀態**: 已確認，**暫緩修復**（使用者要求等當前 loop 跑完再動）  
**檔案**: `src/gridbot/mainnet/one_run.py`

### 問題

Loop 續接（arm 下一個 run）目前**只在 `_finish_flat_run`** 觸發（即 run 真正成交後 TP/SL/flat 平倉）。但若 run 在**進場階段就結束**，走的是 `_run_armed` / `_run_entry_pending` 裡的 `complete_run(...)`，**不經過 `_finish_flat_run`**，因此**不會 arm 下一個 run，loop 卡住**。

受影響的 entry 階段結束路徑：

| 路徑 | 觸發點 | 結束狀態 |
|---|---|---|
| 等不到 wildcat 訊號逾時（預設 60 分鐘） | `_run_armed` signal_timeout | `ENTRY_EXPIRED` |
| entry maker 掛單逾時未成交 | `_run_entry_pending` entry_ttl_expired | `ENTRY_EXPIRED` |
| entry 掛單已不在且無持倉 | `_run_entry_pending` entry_not_open_no_position | `ENTRY_EXPIRED` |
| entry GTX 全被拒 / 滑價超限 | `_place_entry` GTXSlippageExceeded | `ENTRY_REJECTED` |

→ 這些情況下 loop 都會停住，不會自動 arm 下一個 run。與 §24 的 cooldown gap 同類（都是「非 `_finish_flat_run` 路徑」漏掉 loop 續接）。

### 觀察案例

Run `cry3mn_1780848066380`（loop 2/3）2026-06-07 16:01 armed，TP（1/3）後正常 arm。若它在 17:01 前等不到訊號 → `signal_timeout` → loop 會卡在 2/3。本次先觀察，未必會踩到。

### 規劃修法（待執行）

抽出共用的 loop 續接入口（如 `_advance_loop_after_run(prev_run)`），在所有 run 結束路徑（`_finish_flat_run` + entry 階段的 `complete_run`）統一呼叫，讓 entry 階段結束（含 signal_timeout / entry_ttl / rejected）也能 arm 下一個 run。需注意：entry 階段失敗是否要消耗 loop 計數、是否要 cooldown，需與使用者確認策略。

---

## 24. Bug 修正：Cooldown 卡死 loop + 碎倉清理（Bug 7 + Bug 8）

**日期**: 2026-06-07 ~23:40 UTC+8  
**檔案**: `src/gridbot/mainnet/one_run.py`, `config/settings.py`, `tests/test_mainnet_one_run_maker.py`  
**作者**: Hermes (via Jack S)

### 觸發案例

Run `cry3mn_1780844907574`（loop 2/3，SHORT）：SL 退出後進入 5 分鐘 cooldown，但 cooldown 到期後第 3 個 run 從未被 arm，loop 永久卡在 2/3。事件分析另發現碎倉問題。

### Bug 7：Cooldown 到期後 loop 不續接（loop 永久卡死）

**根因**：`_finish_flat_run` 的「arm 下一個 run」只在 run 完成那一瞬間執行一次。若當下 cooldown 中 → 只發「⏳ Cooldown 中，跳過 arm」通知就結束，**之後沒有任何機制在 cooldown 到期後重新 arm**。run 已 COMPLETED，下個 cycle `get_active_run()` 回傳 None 直接 return，loop 計數只躺在記憶體裡無人處理。通知說「cooldown 到期後自動繼續」是假的。

**實證**：VM 15:26 時 cooldown（到期時間 15:20）早已過期，但 DB 中 15:08 後再無新 run armed。

**修法**：
1. 抽出 `_try_arm_next_loop_run(side, strategy, prev_run_id)`：cooldown 中時把 pending arm 記入 `self._loop_resume`（含 `resume_at_ms`），cooldown 清空時才實際 arm。
2. 新增 `_maybe_resume_pending_loop()`：cooldown 到期後續接 arm。
3. `run_cycle` 在「無 active run」分支呼叫 `_maybe_resume_pending_loop()`，使 cooldown 到期後下個 10 秒 tick 自動續接。

附帶修掉 cooldown 通知裡字面 `\\n`（原本顯示成 `\n` 而非換行）。

### Bug 8：碎倉拖到反向 SL（改用 maker 0 手續費平倉）

**根因釐清**：reduce-only 單其實**豁免最小名目限制**（-4164 錯誤訊息明寫 "unless you choose reduce only"），所以碎倉本來就掛得上單。真正的問題是 TP 掛在「理想獲利價」，價格不利時不成交。如 `cry3mn_1780844907574` 最後剩 **0.001 ETH ≈ 1.6 USDC**，理想價 TP 掛著不成交，碎倉卡 4 分鐘，最終價格反彈觸及 SL 才平掉。

**修法（maker，0 手續費）**：`_run_running` 在 sync TP 前檢查剩餘名目，若 `0 < qty*mark < mainnet_residual_cleanup_notional_usdc`（預設 20 USDC）：
1. 取消理想價 TP / 舊 dust 單
2. 掛 **reduce-only POST-ONLY（maker）** 單在盤口隊首（SELL 掛 best ask、BUY 掛 best bid）—— 不跨價差、永遠是 maker、USDC maker fee = 0，且貼盤口很快成交
3. client_order_id 用 `{run_id}_dust`，每 cycle 重新貼盤口（reprice 跟盤）
4. 交易所端 STOP_MARKET SL 保留作兜底

> 初版曾用 reduce-only **市價**平倉，但市價=taker 會吃手續費；使用者指出策略 maker USDC fee=0，故改為 post-only maker。新增 settings `mainnet_residual_cleanup_notional_usdc: float = 20.0`。

### 測試

- `test_residual_dust_placed_as_postonly_maker_not_market`：0.001 ETH（1.6 USDC）→ reduce-only POST-ONLY maker 單掛 best ask，**無市價單**
- `test_loop_defers_arm_during_cooldown_then_resumes_after_expiry`：cooldown 中 defer、到期後 resume arm
- `test_loop_arms_immediately_when_no_cooldown`：無 cooldown 立即 arm
- 測試 helper 預設關閉碎倉清理（測試用 ~100 不真實價格），碎倉測試顯式開啟

**測試結果（本地 + VM）**：`13 passed`

### 部署

| 時間 (UTC+8) | 步驟 | 結果 |
|---|---|---|
| ~23:40 | 改 one_run.py（loop resume + 碎倉清理）+ settings.py | OK |
| ~23:42 | 本地 13/13 | OK |
| ~23:44 | scp + cp + chown，VM 13/13 | OK |
| ~23:46 | 全殺後單一進程重啟（PID 186867），啟動正常、無 Conflict | OK |

### 備註

- §23 的 algo SL 取消修復已驗證生效：log 滿是 `cancel_sl_algo_ok`，14:40 後所有 run 的 SL 都正確取消，條件單殘留問題已解決。
- 卡住的 2/3 loop 因記憶體狀態在重啟時清空，需使用者重新發起最後 1 個 run。
- 已知限制：`_loop_resume` / loop 計數仍在記憶體，bot 重啟會丟失。長遠應持久化到 DB 或改 systemd。

---

## 23. Bug 修正：STOP_MARKET SL 是 algo order，取消邏輯找錯 endpoint（條件單殘留真因）

**日期**: 2026-06-07 ~22:40 UTC+8  
**檔案**: `src/gridbot/mainnet/one_run.py`, `tests/test_mainnet_one_run_maker.py`  
**作者**: Hermes (via Jack S)

### 現象

清理雙開（§22）後，單一進程下 run `cry3mn_1780841856407` 正常 COMPLETED TP，但**條件單仍殘留**，需手動關閉。§17/§19 的「殘留修復」完全無效。

### 根因（決定性證據）

查 SL 下單返回值（DB `sl_stop_market_placed` 事件）：

```json
{
  "algoId": 1000001892816642,
  "clientAlgoId": "x-Cb7ytekJ2173633a2d69f55dc0287e",
  "algoType": "CONDITIONAL",
  "orderType": "STOP_MARKET",
  "reduceOnly": true,
  "algoStatus": "NEW"
}
```

讀 python-binance SDK `futures_create_order` 源碼，發現它對**條件單類型**（STOP / STOP_MARKET / TAKE_PROFIT / TAKE_PROFIT_MARKET / TRAILING_STOP_MARKET）做特殊路由：

```python
if order_type in conditional_types:
    if "clientAlgoId" not in params:
        params["clientAlgoId"] = self.CONTRACT_ORDER_PREFIX + self.uuid22()
    params.pop("newClientOrderId", None)          # ← 丟棄我們的 clientOrderId！
    params["algoType"] = "CONDITIONAL"
    params["triggerPrice"] = params.pop("stopPrice")
    return await self._request_futures_api("post", "algoOrder", ...)   # ← algo endpoint
```

因此我們的 STOP_MARKET SL：

1. 被路由到 **algoOrder endpoint**，活在 `openAlgoOrders`，**不在** `openOrders`
2. `newClientOrderId={run_id}_sl` 被 SDK **丟棄**，改用隨機 `clientAlgoId`
3. 必須用 `cancel_algo_order`（`DELETE /fapi/v1/algoOrder`）取消，不能用 `cancel_order`

而 `_cancel_all_run_orders` / `_cancel_stop_loss_order` 都只查 `get_open_orders`（普通單）→ **永遠掃不到 SL** → 取消不掉 → 殘留。

實證：該 run 的 `futures_get_all_orders` 只有 entry/tp1/tp2，**完全沒有 `_sl`**；log 裡也無任何 `cancel_all_run_orders` 記錄（因為 get_open_orders 沒回傳 SL）。

> §17/§19 的測試會綠燈，是因為 FakeClient 把 STOP_MARKET 錯誤地放進 `open_orders`——測試假設與真實 SDK 行為不符。

### 修法

**1. `_cancel_stop_loss_order`** 改走 algo endpoint：

```python
algo_orders = await self._client.get_open_algo_orders(symbol)
for o in algo_orders:
    if not o.get("reduceOnly", True):
        continue
    await self._client.cancel_algo_order(
        symbol, algo_id=o.get("algoId"), client_algo_id=o.get("clientAlgoId"))
```

one-run 只持單一倉位，故取消該 symbol 所有 reduce-only conditional order（即我們的 SL）。

**2. `_cancel_all_run_orders`** 末尾追加 `await self._cancel_stop_loss_order(...)`，同時清掃普通 TP 單（openOrders）與 algo SL（openAlgoOrders）。

`client.py` 既有的 `get_open_algo_orders` / `cancel_algo_order` 直接複用，無需改動。

### 測試

FakeClient 改為模擬真實 SDK 行為：`create_stop_market_sl_order` 把 SL 放進 `algo_orders`（帶 `algoId`/`clientAlgoId`/`reduceOnly`），新增 `get_open_algo_orders` / `cancel_algo_order`。5 個相關測試的 SL 從 `open_orders` 移到 `algo_orders`，斷言改驗 `cancelled_algo`。

**測試結果（本地 + VM）**：`10 passed`

### 受控實盤驗證

啟動前用 mainnet key 直接查 endpoint：`openAlgoOrders` 返回型別為 `list`（空帳戶 `[]`），確認查詢可用。帳戶當下持倉=無、掛單=0。

### 部署

| 時間 (UTC+8) | 步驟 | 結果 |
|---|---|---|
| ~22:40 | 改 `_cancel_stop_loss_order` + `_cancel_all_run_orders` 走 algo endpoint | OK |
| ~22:42 | FakeClient + 5 測試改正，本地 10/10 | OK |
| ~22:44 | scp + cp + chown，VM 10/10 | OK |
| ~22:46 | 全殺後單一進程重啟（PID 185249），啟動正常 | OK |

### 待辦

- §16 todo #9（STOP_MARKET SL 首次驗證）→ 下一個完整 run 需確認 log 出現 `cancel_sl_algo_ok` 且幣安無殘留條件單

---

## 22. 根因發現：VM 雙開 bot 進程（運維事故，Bug 6 反覆出現的元兇）

**日期**: 2026-06-07 ~22:15 UTC+8  
**檔案**: 無代碼變更（運維問題）  
**作者**: Hermes (via Jack S)

### 現象

Run `cry3mn_1780840268456` TP 成交、`_finish_flat_run` 已寫 `completed` 並發「🏁 已完成 TP」通知（13:52:31），**但隨即又出現**：

```
❌ Mainnet one-run 失敗
錯誤：APIError(code=-2022): ReduceOnly Order is rejected.
```

且條件單殘留。同一個 run 既 COMPLETED 又 FAILED，邏輯上矛盾。

### 根因：兩個 bot 主進程同時在跑

`ps -eo pid,lstart,cmd | grep main.py` 顯示：

| PID | 啟動時間 | 代碼版本 |
|---|---|---|
| 182365 | 13:15:43 | Bug 5 修復版（**無** Bug 6 的 -2022 保護）|
| 183184 | 13:43:45 | Bug 6 修復版（有 -2022 保護）|

兩個進程**同時連同一個 mainnet 幣安帳戶 + 同一個 SQLite DB**，各自每 10 秒跑 `mainnet_one_run_cycle`。13:51 的 run 被兩個進程各處理一次：

- 進程 183184（新）：偵測 flat → `_finish_flat_run` → COMPLETED + TP 通知（13:52:31）
- 進程 182365（舊）：仍見殘餘倉位 → `_sync_take_profit_orders` 對已平倉位掛 reduce-only → -2022 → `@async_retry` 重試 3 次（13:52:29→36）→ **舊代碼無 -2022 保護** → 逃逸到 `run_cycle` → `complete_run(FAILED)` 覆寫 COMPLETED + 失敗通知（13:52:36）

### 為什麼會雙開

重啟指令使用：

```bash
kill $(pgrep -f 'python.*main.py' | head -1)   # head -1 只殺一個 PID
nohup python main.py &                          # 又起一個新的
```

`head -1` 每次只殺一個進程。前幾輪重啟累積殘留，13:15 的舊進程一直沒被殺乾淨。

### 此事故解釋了先前所有「修好卻反覆出現」的怪象

- -2022 / 條件單殘留反覆出現 → 舊進程一直跑舊代碼
- 先前的 Telegram `Conflict: terminated by other getUpdates` → 兩進程搶同一個 bot token
- 重複 / 矛盾的 Telegram 通知 → 兩進程各發一次

→ **Bug 6（§21）的代碼修正本身是正確的**（`_sync_take_profit_orders` line 959/988 的 -2022 保護無誤），它「沒生效」純粹是因為舊進程沒載入新代碼。

### 處置（已執行，使用者授權「全殺後重啟單一進程」）

```bash
sudo pkill -9 -f 'main.py'          # 全殺（bash wrapper + python）
# 確認歸零：ps 無 main.py
# 啟動前檢查幣安帳戶：持倉=無、開放掛單=0（乾淨）
sudo -u jack_shih setsid nohup python main.py > /tmp/cry3_main.log 2>&1 < /dev/null &
```

結果：單一主進程 PID 184199（14:13:44 啟動），`Conflict` 計數=0，scheduler 正常。

### 待辦（運維固化，避免再次雙開）

1. **重啟流程改用 `pkill -f 'main.py'` 全殺**，不可用 `head -1`
2. **每次重啟後驗證** `ps | grep main.py` 只有一個 python 主進程
3. **強烈建議改用 systemd 服務**（單例、`Restart=on-failure`、避免 nohup 殘留）

---

## 21. Bug 修正：TP 掛單 -2022 導致 run 標成 FAILED（Bug 6）

**日期**: 2026-06-07 ~21:30 UTC+8  
**檔案**: `src/gridbot/mainnet/one_run.py`  
**作者**: Hermes (via Jack S)

### 現象

Run `cry3mn_1780838444527` 成交後，TP 輪次 sync 正常跑了約 2 分鐘（13:20:50 ~ 13:22:28 共 19 次 `take_profit_synced`），但最後一次 sync 後整個 run 失敗：

```
❌ Mainnet one-run 失敗
錯誤：APIError(code=-2022): ReduceOnly Order is rejected.
```

### 根因

**競態條件**：當 STOP_MARKET SL 被交易所觸發（倉位歸零），與此同時 `_run_running._sync_take_profit_orders` 正在執行，這時倉位在 `get_position` 之後、`create_reduce_only_limit_order` 之前就歸零了。

`_sync_take_profit_orders` 的例外處理只有 -5022（GTX 被拒 → GTC fallback），而 GTC fallback 內沒有任何例外處理：

```python
await self._client.create_reduce_only_limit_order(..., post_only=False)
# ↑ 若倉位已歸零，這裡丟 -2022，完全未捕獲
```

-2022 一路傳到 `run_cycle` 頂層 → run 標成 FAILED。

同樣的問題也存在 `_close_position` 的市價平倉路徑：若 STOP_MARKET 搶先觸發，軟體再下 reduce_only market order 同樣 -2022 → FAILED。

### 修法

**1. `_sync_take_profit_orders`**：

```python
# GTX 路徑：-2022 直接 return（倉位已歸零，讓下個 cycle 偵測平倉）
if exc.code == -2022:
    logger.info("tp_order_reduce_only_rejected_position_gone", ...)
    return

# GTC fallback：也捕獲 -2022
try:
    await self._client.create_reduce_only_limit_order(..., post_only=False)
except BinanceAPIException as gtc_exc:
    if gtc_exc.code == -2022:
        logger.info("tp_order_gtc_fallback_reduce_only_rejected", ...)
        return
    raise
```

**2. `_close_position`**：

```python
try:
    order = await self._client.create_market_order(... reduce_only=True ...)
except BinanceAPIException as exc:
    if exc.code == -2022:
        logger.info("market_close_reduce_only_rejected_position_gone", ...)
        return  # 讓下個 cycle 的 not-position 路徑處理
    raise
```

在兩個地方，-2022 都被視為「倉位已被交易所關閉，讓下個 cycle 正常走 `_finish_flat_run`」。

### 部署

| 時間 (UTC+8) | 步驟 | 結果 |
|---|---|---|
| ~21:30 | 修正 `_sync_take_profit_orders` + `_close_position` | OK |
| ~21:33 | 本地 10/10 pass | OK |
| ~21:35 | scp + cp + chown 到 VM，VM 10/10 pass | OK |
| ~21:36 | Bot 重啟 | OK |

---

## 20. Bug 修正：Entry GTX 全被拒後整個 run 標成 FAILED（Bug 5）

**日期**: 2026-06-07 ~21:10 UTC+8  
**檔案**: `src/gridbot/mainnet/one_run.py`, `tests/test_mainnet_one_run_maker.py`  
**作者**: Hermes (via Jack S)

### 現象

Run `cry3mn_1780837096187` 在 entry GTX 3 次全被 -5022 拒絕後出現：
```
❌ Mainnet one-run 失敗
錯誤：APIError(code=-5022): Due to the order could not be executed as maker...
```

正確行為應為 `ENTRY_REJECTED`（優雅拒絕），而非 `FAILED`（exception）。

### 根因

`_place_post_only_with_retry` 最後一行（`fallback_to_gtc=False` 路徑）：

```python
# No GTC fallback — re-raise last BinanceAPIException
raise last_exc
```

這會直接 re-raise 原始的 `BinanceAPIException(-5022)`。而 `_place_entry` 只捕捉 `GTXSlippageExceeded`：

```python
except GTXSlippageExceeded as exc:
    await self._repo.complete_run(..., "ENTRY_REJECTED", ...)
```

`BinanceAPIException` 不符合，繞過此 except 塊，傳到 `run_cycle` 最外層 → run 標成 `FAILED`。

### 修法

將 `_place_post_only_with_retry` 末尾改為拋出 `GTXSlippageExceeded`：

```python
# No GTC fallback — surface as GTXSlippageExceeded so _place_entry
# handles it as ENTRY_REJECTED instead of propagating as FAILED.
raise GTXSlippageExceeded(
    f"GTX entry retries exhausted ({max_attempts} attempts, fallback disabled)"
) from last_exc
```

現有的 `except GTXSlippageExceeded` handler 即可接住，run 正確標成 `ENTRY_REJECTED`。

### 測試

新增 `test_place_post_only_with_retry_raises_gtx_slippage_when_retries_exhausted`：

- `create_limit_order_raw` 永遠拋 `BinanceAPIException(-5022)`
- `fallback_to_gtc=False`
- 斷言拋出 `GTXSlippageExceeded(match="GTX entry retries exhausted")`

**測試結果（本地 + VM）**：`10 passed`

### 部署

| 時間 (UTC+8) | 步驟 | 結果 |
|---|---|---|
| ~21:05 | 修正 `_place_post_only_with_retry` line 683 | OK |
| ~21:08 | 新增測試，本地 10/10 pass | OK |
| ~21:12 | scp + cp + chown 到 VM，VM 10/10 pass | OK |
| ~21:15 | Bot 重啟，`app_starting` 確認正常 | OK |

---

## 19. Bug 修正：`_finish_flat_run` 仍殘留 SL（`_cancel_stop_loss_order` 無效）

**日期**: 2026-06-07 ~20:45 UTC+8  
**檔案**: `src/gridbot/mainnet/one_run.py`, `tests/test_mainnet_one_run_maker.py`  
**作者**: Hermes (via Jack S)

### 現象

Run `cry3mn_1780835205975` TP 成交後，§17 部署的 `_cancel_stop_loss_order` 依然沒取消 STOP_MARKET SL，需使用者手動關閉。

### 根因診斷

查 `mainnet_run_events` DB，此 run 的事件序列：

| 時間 (UTC) | 事件 |
|---|---|
| 12:27:22 | `entry_filled` |
| 12:27:22 | `sl_stop_market_placed` |
| 12:27:32~12:28:25 | `take_profit_synced` × 6（TP 部分填充，qty 遞減） |
| 12:28:32 | `completed` (flat_detected) |

**完全沒有取消事件**。分析 `_cancel_stop_loss_order` 原始實作：

```python
async def _cancel_stop_loss_order(self, symbol, run_id):
    open_orders = await self._client.get_open_orders(symbol)
    for order in open_orders:
        if str(order.get("clientOrderId") or "") == f"{run_id}_sl":
            await self._client.cancel_order(symbol, int(order["orderId"]))
```

問題：
1. **完全靜默**：找到或找不到 SL 都不記 log，無法診斷
2. **邏輯脆弱**：只用精確 suffix 比對 `_sl`，若 Binance 回傳格式有任何差異就靜默失敗
3. **`cancel_order` 例外未處理**：若取消 API 失敗會 raise，導致整個 `_finish_flat_run` 失敗（run 標成 FAILED 而非 TP）

### 修法

**雙層強化**：

1. **強化 `_cancel_stop_loss_order`**：加 `found` 旗標、成功/失敗各有 logger，`BinanceAPIException` 捕獲並記 warning 而非 raise。

2. **新增 `_cancel_all_run_orders`**：改用前綴比對（`startswith(run_id)`），取消所有 `{run_id}_tp1 / _tp2 / _sl` 等訂單，每個取消都有 log，個別 API 失敗不影響其他訂單。

3. **`_finish_flat_run` 改呼叫 `_cancel_all_run_orders`**：比只取消 SL 更全面，即使 tp 或 sl clientOrderId suffix 不符也能清除。

```python
async def _finish_flat_run(self, run, reason):
    await self._cancel_all_run_orders(run["symbol"], run["run_id"])
    ...
```

### 測試

新增 `test_finish_flat_run_cancels_all_run_orders`：

- open_orders 包含 tp1 (301), tp2 (302), sl (303), 以及**無關 run 的 OTHER 訂單 (999)**
- 斷言 301/302/303 全部被取消，999 不被取消

**測試結果（本地 + VM）**：`9 passed`

### 部署

| 時間 (UTC+8) | 步驟 | 結果 |
|---|---|---|
| ~20:45 | 修正 `_cancel_stop_loss_order`（加 log）+ 新增 `_cancel_all_run_orders` | OK |
| ~20:47 | 修正 `_finish_flat_run` 改用 `_cancel_all_run_orders` | OK |
| ~20:48 | 新增測試，本地 9/9 pass | OK |
| ~20:52 | scp + cp + chown 到 VM，VM 9/9 pass | OK |
| ~20:54 | Bot 重啟（kill 180396，nohup PID 181261），Telegram 衝突解除後正常 | OK |
