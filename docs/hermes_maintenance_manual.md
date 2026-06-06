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

補充：
- 目前 VM 上 bot 的實際 working directory 會落在 `/home/jack_shih`
- `config/settings.py` 的 `db_path` 目前是相對路徑 `testnet/data/gridbot_testnet.db`
- 因此實際被打開的是 `/home/jack_shih/testnet/data/gridbot_testnet.db`
- 接手時查 `mainnet_runs` / `mainnet_run_events`，請優先查這份 DB，不要只看 `/home/jack_shih/cry3/testnet/data/gridbot_testnet.db`

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

4. 2026-06-06 補充：`mainnet one-run` 持倉計時修正已先同步到 VM runtime，但尚未推到 GitHub 主線
   - 本地最新 commit：`6bf5c42 fix: start mainnet one-run hold timer after entry fill`
   - 修正內容：`max_holding_bars` 與 `ADVERSE_EXIT` 改成從 `entry_filled` 開始計時
   - VM 已重新拉起 bot 並吃到這版 runtime
   - 但 `origin/main` 尚未更新，接手時請先對帳本地、VM、GitHub 三方版本再決定 merge / rebase / push

5. 2026-06-06 補充：`testnet` 訊號與 `mainnet` 真實可執行性可能存在時間差
   - 目前 `/signal` 顯示的即時診斷是以 testnet / 即時策略資料源為主
   - `mainnet one-run` 則在 mainnet 實盤環境下重新取 K 線與判斷
   - 因此 testnet 訊號不一定 1:1 等同 mainnet 可成交訊號，尤其在快速波動時，訊號與實盤掛單間可能出現延遲或偏移
   - 後續若要提升 one-run 可靠度，建議評估「建議開單區間」是否加入額外 buffer，避免訊號太貼近邊界時掛單失真或成交不穩

6. 2026-06-06 補充：`mainnet one-run` 仍可能遇到 GTX / Post-Only (`-5022`) 拒單
   - 錯誤訊息範例：`APIError(code=-5022): Due to the order could not be executed as maker, the Post Only order will be rejected.`
   - 這通常不是策略訊號本身錯誤，而是 entry / TP / DCA 掛單價格太貼近當下 bid/ask，送到 Binance 時已經不再是純 maker
   - 現行行為：
     - entry 先嘗試 `GTX` maker
     - TP / DCA 也優先 `GTX`
     - 若 `mainnet_entry_fallback_to_gtc=False`，entry 在重試後仍被拒就會直接讓 run 失敗
   - 後續接手者要優先確認：
     - 是否要在 entry / TP / DCA 加大 `buffer / slippage_bps`
     - 是否要允許 `mainnet_entry_fallback_to_gtc=True`
     - 是否要把 `limit_tolerance`、`mainnet_entry_slippage_bps`、`mainnet_tp_slippage_bps`、`mainnet_dca_slippage_bps` 做成更明確的風控規則

7. 2026-06-06 補充：近期實單樣本可直接拿來當排查案例
   - `cry3mn_1780720295120`
     - `entry_placed` → `entry_filled` → 多次 `take_profit_synced`
     - 中途觸發 `recovery_entry_placed`（DCA #1）
     - 後續 TP 會隨倉位縮小而反覆重掛，最後因 `SL` 直接 `close_submitted`
     - 這筆可用來觀察：
       - partial TP 後的 TP 重同步
       - DCA 後 TP qty / price 重算
       - 最終平倉前是否仍保留部分 TP 碎單
   - `cry3mn_1780720542104`
     - `entry_placed` 後沒有及時成交
     - 最後因 `entry_ttl_expired` 被取消
     - 可用來檢查 maker entry 在震盪或追價過慢時的掛單壽命
   - `cry3mn_1780720685933`
     - `entry_rejected`，原因是 `slippage_exceeded`
     - 具體訊息：`GTX retry attempt 2: slippage 8.05 bps exceeds tolerance 8.0 bps`
     - 可用來檢查 `mainnet_entry_slippage_bps` 與訊號貼價程度
   - `cry3mn_1780720697481`
     - 最新一筆 `ENTRY_PENDING`
     - 代表 entry maker 已掛出，但尚未成交
     - 可用來確認目前 live runtime 的 `ENTRY_PENDING` → `RUNNING` 轉換是否正常

8. 2026-06-06 補充：新一批樣本顯示 run 分布已變成「TP / SL / TTL / GTX 拒單」四類混合
   - `cry3mn_1780724499770`
     - `SHORT`，先成交、再同步 TP、再 `DCA #1`、最後因 `SL` 平倉
     - 可用來觀察：
       - SHORT 方向的 TP 重同步是否與 LONG 一致
       - DCA 後 TP 是否會在短時間內反覆縮量 / 重掛
   - `cry3mn_1780724396380`
     - `SHORT`，先成交、再同步 TP、再 `DCA #1`、最後因 `SL` 平倉
     - 與上一筆類似，屬於「先 partial / DCA，再被 stop」的典型案例
   - `cry3mn_1780723448304`
     - `SHORT`，成交後沒有再觸發 DCA，最後以 `TP` 收斂完成
     - 可用來對照「正常止盈完成」與「反覆重掛 TP」之間的差異
   - `cry3mn_1780723432263`
     - `entry_rejected`，原因是 `slippage_exceeded`
     - 具體訊息：`GTX retry attempt 2: slippage 9.89 bps exceeds tolerance 8.0 bps`
     - 表示訊號可用，但 entry 太貼，超過當前 8 bps 容忍度
   - `cry3mn_1780721444475`
     - `LONG`，成交後直接靠 TP 完成，沒有 DCA
     - 這是較乾淨的 TP 完成樣本
   - `cry3mn_1780721315425`
     - `LONG`，成交後觸發 DCA #1，最後因 `SL` 平倉
     - 與 `cry3mn_1780720295120` 類似，屬於完整的 partial + DCA + SL 流程
   - `cry3mn_1780721176723`
     - `ENTRY_EXPIRED`
     - 表示 entry maker 掛出後一直沒成交，最後 TTL 到期取消
   - `cry3mn_1780721066069`
     - `LONG`，成交後出現 DCA #1，最後因 `SL` 平倉
     - 這筆可對照 DCA 後 TP qty 被重算的行為
   - `cry3mn_1780720906513`
     - `LONG`，成交後多次重同步 TP，但沒有 DCA，最後直接完成
     - 可用來觀察「只重掛 TP、不補 DCA」的樣本
   - `cry3mn_1780720760056`
     - `LONG`，成交後多次重同步 TP，最後完成
     - 可用來觀察 TP 碎單與 completed 收斂是否穩定
   - `cry3mn_1780720697481`
     - 目前仍是 `ENTRY_PENDING`
     - 代表 entry maker 已掛出但尚未成交，仍可持續觀察
   - `cry3mn_1780720685933`
     - `ENTRY_REJECTED`
     - 原因是 `GTX` 重試後仍超過 8 bps 容忍度，屬於 maker-first 與價格貼近度衝突的代表案例

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
