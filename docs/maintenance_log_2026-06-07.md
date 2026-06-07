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
