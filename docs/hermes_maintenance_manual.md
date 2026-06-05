# Cry3 Hermes 維護手冊

最後更新：2026-06-06（Asia/Taipei）

## 1. 這份文件的用途

這份文件是給下一位維護者（例如 Hermes）使用的通用維護手冊。

目標：
- 快速理解 Cry3 的實際運行架構
- 知道正式 VM 目前怎麼部署、怎麼啟動、怎麼排錯
- 知道最近修過哪些問題、哪些地方仍有風險
- 避免再次踩到 Telegram HTML、VM repo 權限、VM-only 檔案遺失這幾類已知坑

原始交接概要仍保留在 [docs/walkthrough.md](/C:/Users/pipi/Desktop/cry3/docs/walkthrough.md)。
本文件補的是「目前實際運行狀態」與「最近維護結果」。

## 2. 專案目前在做什麼

Cry3 是 Binance Futures 交易機器人，現在主要有兩個運行面向：

1. `testnet_live`
   - 持續抓 Binance Testnet / Mainnet 資料
   - 產生 Telegram 訊號、診斷與 PnL 報表
   - 在 `TESTNET_TELEGRAM_SIGNAL_ONLY=true` 時，不自動下 testnet 單，只做監控、記錄與訊號推送

2. `mainnet one-run`
   - 使用 Telegram 手動啟動一次性的實盤驗證單
   - 只接「下一個符合條件的 wildcat 訊號」
   - 目前保留原策略的 partial TP / recovery(DCA) / adverse guard 結構
   - 但執行層已改成：
     - 進場用 `maker`
     - TP 先掛好 `maker reduce-only`
     - DCA 也走 `maker`
     - 只有 `SL / adverse exit / max hold` 才直接平倉

## 3. 關鍵程式結構

### 3.1 啟動入口

- 啟動檔：[main.py](/C:/Users/pipi/Desktop/cry3/main.py)
- 主應用：[src/gridbot/core/app.py](/C:/Users/pipi/Desktop/cry3/src/gridbot/core/app.py)

`App.start()` 會做這幾件事：
- 初始化 DB / repositories
- 建立 Telegram app
- 建立 `TestnetAutoTrader`
- 建立 `MainnetOneRunManager`
- 啟動 scheduler jobs：
  - fetch cycle
  - testnet trade cycle
  - testnet manage cycle
  - testnet daily report
  - mainnet one-run cycle

### 3.2 Telegram

- Telegram app：[src/gridbot/telegram/bot.py](/C:/Users/pipi/Desktop/cry3/src/gridbot/telegram/bot.py)
- handlers：[src/gridbot/telegram/handlers.py](/C:/Users/pipi/Desktop/cry3/src/gridbot/telegram/handlers.py)

重點：
- 使用 `python-telegram-bot` v20+
- 啟動時會用 `post_init()` 自動註冊 command menu
- `parse_mode="HTML"`

### 3.3 策略與診斷

- Wildcat live adapter：
  [src/gridbot/strategy/wildcat_live.py](/C:/Users/pipi/Desktop/cry3/src/gridbot/strategy/wildcat_live.py)

重點：
- `generate_wildcat_v2_adverse_guard_live_decision`
- `explain_wildcat_no_signal`
- S1 / S5 診斷輸出

### 3.4 Testnet PnL

- PnL helper：
  [src/gridbot/testnet/pnl.py](/C:/Users/pipi/Desktop/cry3/src/gridbot/testnet/pnl.py)

用途：
- 分離 realized pnl / funding / maker fee / non-maker fee
- 供 Telegram `/pnl` 與 testnet 相關摘要使用

注意：
- 這個檔一度只存在 VM，沒有進 Git
- 2026-06-05 已確認它是正式運行依賴，後續必須保持在 repo 中

### 3.5 Mainnet one-run

- 核心檔：
  [src/gridbot/mainnet/one_run.py](/C:/Users/pipi/Desktop/cry3/src/gridbot/mainnet/one_run.py)

重點：
- 一次只允許一個 active run
- 進場單是 `maker`
- 成交後會同步掛兩段 `maker reduce-only TP`
- partial / DCA / final TP / SL 都記錄到 `mainnet_runs` / `mainnet_run_events`

### 3.6 Binance client

- [src/gridbot/binance/client.py](/C:/Users/pipi/Desktop/cry3/src/gridbot/binance/client.py)

重點：
- `create_limit_order(..., post_only=True)` 會走 `timeInForce=GTX`
- `create_reduce_only_limit_order(..., post_only=True)` 目前也支援 `GTX`

## 4. Telegram HTML 注意事項

這個專案最容易再次踩雷的點之一，是 Telegram HTML parse mode。

因為所有 Telegram 訊息都走 `parse_mode="HTML"`，所以任何文字中只要出現原始 `<` 或 `>`，
都可能觸發：

- `Can't parse entities`
- `unsupported start tag`

維護規則：
- 所有診斷字串、策略理由、動態插值內容都必須做 escape
- 優先使用 `html.escape`
- 或手動轉成 `&lt;` / `&gt;`

這條規則對：
- `/signal`
- `/mainnet`
- `/pnl`
- 各種診斷訊息與完成摘要
全部都適用

## 5. 資料庫與關鍵表

目前主要 DB 是：
- `testnet/data/gridbot_testnet.db`

重要表：
- `futures_trades`
- `income_records`
- `mainnet_runs`
- `mainnet_run_events`
- `_migrations`

### 5.1 `mainnet_runs`

用途：
- 記錄 one-run 的生命週期總表

重要欄位：
- `run_id`
- `status`
- `side`
- `signal_json`
- `entry_order_id`
- `entry_price`
- `avg_entry_price`
- `qty`
- `cumulative_notional_usdc`
- `realized_pnl_usdc`
- `commission_usdc`
- `exit_reason`
- `error`

### 5.2 `mainnet_run_events`

用途：
- 記錄 one-run 每一步事件

重要欄位：
- `run_id`
- `event_time_ms`
- `event_type`
- `details_json`

常見事件：
- `armed`
- `entry_placed`
- `entry_filled`
- `take_profit_synced`
- `partial_exit`
- `recovery_entry_placed`
- `close_submitted`
- `completed`

## 6. VM 實際運行架構

### 6.1 VM 基本資訊

- instance: `cry3jack`
- zone: `asia-east1-a`
- 使用者：`jack_shih`

### 6.2 正式 repo 路徑

正式執行路徑現在是：
- `/home/jack_shih/cry3`

### 6.3 目前啟動方式

目前 bot 是直接從這裡啟動：
- `/home/jack_shih/cry3/testnet/.venv/bin/python /home/jack_shih/cry3/main.py`

依賴：
- `testnet/.env.testnet`
- `testnet/.venv`
- `testnet/data/gridbot_testnet.db`
- `testnet/logs/`

### 6.4 systemd 狀態

`cry3.service` 仍存在，但這段維護期間不可靠，原因：
- `sudo systemctl restart cry3` 不是 passwordless
- 舊版 `scripts/deploy.sh` 在 service restart 失敗時，會 fallback 到 `nohup`
- 這曾經把 service 狀態打亂，導致 systemd inactive，但背景程序還活著

現況建議：
- 先把 `cry3.service` 視為「存在但不可信」
- 正式重整 deploy 流程前，不要再依賴舊 `deploy.sh` 直接操作 service

## 7. 2026-06-05 / 2026-06-06 維護結果

### 7.1 已完成

1. `mainnet one-run 200 USDC sizing`
   - 原本錯吃 `1000 USDC`
   - 已修成單筆 `200 USDC` 名目、75x 槓桿

2. `mainnet one-run maker-first 流程`
   - 進場 `maker`
   - TP 預掛 `maker reduce-only`
   - DCA 保持 `maker`
   - 只有止損型退出才 direct close

3. `mainnet one-run summary`
   - 完成摘要會回填：
     - `qty`
     - `realized_pnl_usdc`
     - `commission_usdc`
   - 已改成 API trades + 本地 `futures_trades` 合併補數

4. `VM repo 修復`
   - 舊 `/home/jack_shih/cry3/.git/objects` 有 root-owned 物件
   - 造成 `git fetch` / `git reset` 失敗
   - 已用乾淨 clone 重建正式 `~/cry3`
   - 舊壞 repo 已備份到：
     - `/home/jack_shih/cry3_backups/cry3_bad_20260605_163350`

5. `VM-only 遺漏依賴補回`
   - `src/gridbot/testnet/pnl.py` 原本只存在 VM
   - clean clone 起不來後確認它是正式依賴
   - 現在已補回正式 repo

### 7.2 已知仍需注意

1. `cry3.service` / `deploy.sh`
   - 尚未整理成穩定、可重複的正式部署流程
   - 目前可跑，但不是最乾淨狀態

2. VM 本地仍可能存在未納入 Git 的輔助腳本或分析產物
   - 例如過去曾存在：
     - `src/gridbot/testnet/pnl.py`
   - 未來若再看到 clean clone 起不來，要優先懷疑 VM-only 檔案遺失

3. 本機 `pytest` 環境不穩
   - 這台維護機器曾出現 `aiohttp` import 問題
   - 所以有些測試只能做語法檢查或針對性驗證，無法保證每次本地都能完整跑綠

## 8. 最近關鍵 commit

重要 commit（按時間）：
- `ce3dcd2` `fix: maker-manage mainnet one-run exits`
- `f8248ce` `fix: support post-only reduce-only limit orders`
- `50be424` `fix: merge one-run trade summary sources`

如果 Hermes 接手時要確認 VM 與 GitHub 是否一致，先從這三個 commit 往下查最合理。

## 9. 常用排查方式

### 9.1 看 bot 是否活著

```bash
pgrep -af "/home/jack_shih/cry3/testnet/.venv/bin/python /home/jack_shih/cry3/main.py"
```

### 9.2 看最近 log

```bash
tail -n 120 ~/cry3_manual.log
```

### 9.3 看正式 repo 版本

```bash
cd ~/cry3
git log -1 --oneline
git status -s
```

### 9.4 看最新 mainnet one-run

```bash
sqlite3 /home/jack_shih/cry3/testnet/data/gridbot_testnet.db \
  "select run_id,status,side,entry_price,avg_entry_price,qty,cumulative_notional_usdc,realized_pnl_usdc,commission_usdc,exit_reason,error from mainnet_runs order by armed_at_ms desc limit 5;"
```

### 9.5 看某筆 run 事件

```bash
sqlite3 /home/jack_shih/cry3/testnet/data/gridbot_testnet.db \
  "select event_time_ms,event_type,details_json from mainnet_run_events where run_id='YOUR_RUN_ID' order by id asc;"
```

## 10. 下次維護的優先順序

Hermes 若接手，建議優先順序如下：

1. 修正 `scripts/deploy.sh`
   - 明確區分：
     - systemd restart 成功
     - systemd restart 失敗
     - 不允許無條件 fallback 把程序狀態打亂

2. 把 VM 目前實際依賴與本地 repo 再做一次完整對帳
   - 確認不再存在 VM-only 必要檔案

3. 完成 `mainnet one-run` 的更完整測試覆蓋
   - partial TP
   - DCA recovery
   - TP / SL 完成摘要
   - maker fee / taker fee 回填

4. 若要改 Telegram 診斷輸出
   - 第一優先先做 HTML escape 檢查

## 11. 接手時的最低檢查清單

每次正式接手或換維護者時，至少做這些檢查：

1. `~/cry3` 的 `git log -1 --oneline`
2. bot 主程序是否在跑
3. `~/cry3_manual.log` 是否持續更新
4. `mainnet_runs` 最近一筆是否合理
5. Telegram HTML 訊息是否有 `<` / `>` 未 escape
6. clean clone 是否能單獨啟動，不依賴 VM-only 檔案

---

如果之後由 Hermes 維護，建議把本文件當作「實際運行狀態手冊」，而 `docs/walkthrough.md` 則保留作為原始交接背景資料。
