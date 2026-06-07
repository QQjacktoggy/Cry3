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

## 5. 運行狀態確認

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

## 6. 已知待辦（未變更；待今晚維修）

1. **退出順序不一致**: `mainnet_run_events` 最後事件是 `completed`（trigger=flat_detected），但 `mainnet_runs.exit_reason` 被蓋成 `TP`。
   - 確認案例：`cry3mn_1780790738810`、`cry3mn_1780798188001`（2026-06-07，LONG S1_BB_RSI score=70, entry 1584.6, qty 0.126, realized +0.2368, fee 0.0488）
   - 現象：在 TP 尚未吃到前先被 `flat_detected` 收盤，但覆蓋邏輯最後把 `exit_reason` 寫成 `TP`
2. **DCA 失敗通知誤報**: Telegram 先送 `⚠️ DCA #1 掛單失敗`，緊接著又補 `🧩 DCA #1 已掛`。
   - 確認案例：`cry3mn_1780798188001`；DB 顯示 DCA #1 實際已掛上 Binance（orderId 68193833862, GTX 1583.66, cumulative notional 400 USDC）
   - 可能原因：`_maybe_recovery()` 與 `_sync_take_profit_orders()` 在同一 cycle 內競速，state 刷新造成短暫不一致
3. **TP sync 仍然過密**: 單一 run 內約 30 次 `take_profit_synced`（`cry3mn_1780798188001` 從 entry_filled 到 completed 共 31 次 sync，partial 從 0.126 → 0.001 逐步縮小）。
   - 今日 commit `bfb2c06` 只是容忍 cancel error，沒有減少必要性
   - 根因仍是 match algo 在 partial 被正常吃掉時頻繁觸發 rebuild
4. **DB 資料遷移**: 異常路徑 `/home/jack_shih/cry3/testnet/testnet/data/gridbot_testnet.db` 內有歷史 run（`cry3mn_1780790738810`, `cry3mn_1780791973100`）。如需回填主 DB，可寫一次性 migrate script。
5. **cry3.service / deploy.sh**: 目前仍靠 nohup 手動管理，尚未整理成 systemd 穩定流程。

---

## 審查要點

1. 確認 `bfb2c06` diff 中 cancel loop 僅 catch `-2011`/`-2022`，未 swallow 其他 BinanceAPIException
2. 確認 `.env.testnet` 變更有同步到 VM 與本機，且 deploy wrapper 不會被 `git reset --hard` 抹掉 symlink
3. 確認最新 run 確實寫入 `/home/jack_shih/testnet/data/gridbot_testnet.db`
4. 建議 review 時比對 VM `git log -3` 與 GitHub `origin/main` 三者一致

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
