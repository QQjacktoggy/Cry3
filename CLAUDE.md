# cry3 — Claude 操作指南

## VM 連線資訊

- **IP**: `34.80.75.138`（GCP cry3jack，IP 可能隨 VM 重啟更換，換後請修改此行）
- **User**: `jack_shih`
- **SSH Key**: `~/.ssh/google_compute_engine`
- **Repo**: `/home/jack_shih/cry3`
- **DB**: `/home/jack_shih/testnet/data/gridbot_testnet.db`
- **Log (systemd)**: `/home/jack_shih/cry3/testnet/logs/service.log`
- **Log (手動 nohup)**: `/tmp/cry3_main.log`
- **venv python**: `/home/jack_shih/cry3/testnet/.venv/bin/python`
- **Bot 啟動 CWD**: `/home/jack_shih/cry3`

```bash
ssh -i ~/.ssh/google_compute_engine -o StrictHostKeyChecking=no jack_shih@34.80.75.138
```

---

## Bot 重啟（必讀）

**Gemini 已裝 systemd 服務（`cry3.service`），請用 systemctl 重啟**，不要再手動 nohup。
手動 nohup 會造成雙開（systemd 會自動重拉，兩進程搶同一帳戶）。

```bash
# 標準重啟（部署新檔後）
sudo systemctl restart cry3.service
sleep 8
ps -ef | grep 'main.py' | grep -v grep       # 確認只有一個進程
tail -f /home/jack_shih/cry3/testnet/logs/service.log
```

```bash
# 服務狀態查詢
sudo systemctl status cry3.service

# 看 log
tail -f /home/jack_shih/cry3/testnet/logs/service.log
```

> **雙開防護**：`pkill -f main.py` 後 systemd 會在 10s 內自動重啟，不需要也不應手動 nohup 啟動。若要暫停 bot，用 `sudo systemctl stop cry3.service`。

---

## 部署（scp，非 git pull）

VM git 目前不一定與本機同步，部署改用 scp：

```bash
scp -i ~/.ssh/google_compute_engine FILE jack_shih@34.80.75.138:/tmp/
ssh ... "cp /tmp/FILE /home/jack_shih/cry3/PATH/FILE && chown jack_shih: ..."
```

部署後先 `python -m py_compile` 驗語法，再 `sudo systemctl restart cry3.service`。

---

## 系統架構精華（截至 2026-06-09）

### 止損（SL）機制

- SL 使用 **STOP_MARKET**（非 GTX/Post-Only）
  - 原因：SL 方向天生在 taker 側，GTX 100% 會被 -5022 拒絕；GTC LIMIT at sl_price 是 crossing order，進場即成交（2026-06-09 maker SL 事故驗證）
  - STOP_MARKET 是 **algo order**（`openAlgoOrders`），不在 `openOrders`
  - 取消必須用 `cancel_algo_order`（`DELETE /fapi/v1/algoOrder`），`cancel_order` 查不到
  - python-binance SDK 對 STOP_MARKET 會丟棄 `newClientOrderId`，改用隨機 `clientAlgoId`
- 進場即掛 SL（entry_filled 後立即），DCA 後自動取消重掛（用新平均價重算）
- TP 部分成交（qty 縮小）不觸發 SL 重掛，只更新 qty 追蹤

### 出場優先順序

1. **TP 三層**：tp1(+0.05%, 40%) → tp3(signal tp, 30%) → tp2(+0.12%, 30%)，全部 reduce-only POST_ONLY (maker, fee 0%)；GTX 被拒 → GTC fallback (taker)
   - S1 tp_pct 從 0.12% 降到 0.08%（順勢快出，搭配 TRAIL 鎖利）
   - S1 啟用 `s1_allow_with_trend`：上漲可做多、下跌可做空（順勢均值回歸）
2. **Trailing TP**：peak MFE × 0.7 後 arm，回吐 25% 觸發；先掛 reduce-only GTX top-of-book，TTL 12s 內每 2s 跟盤 reprice（盤口移動 >1 tick 就 cancel 重掛），逾時 → 市價兜底
3. **SL**：STOP_MARKET 靜置在交易所，交易所自動觸發
4. **碎倉清理**：剩餘名目 < 20 USDC → 掛 reduce-only POST_ONLY 貼盤口（maker, fee 0%），每 cycle 跟盤 reprice

### DCA 守門（2026-06-09 大改：DCA 重啟 + 趨勢過濾移除）

- DCA 從停用改回 **steps=3**（對齊回測 best），trigger 0.09%（遞增 ×(count+1)）、tp_shrink 0.45
- **DCA 後 SL 隨層放寬 25%/層**（`sl_pct × (1 + 0.25×dca_count)`，`mainnet_recovery_sl_widen_per_layer`），對齊回測，防剛攤平就被秒掃
- **趨勢過濾已移除**：回測證明 `trend != range` 阻擋扼殺獲利且不降 DD（30d full-guard +329/DD24.9/WR73% vs momentum-only +968/DD13.6/WR83%）。均值回歸進場後短暫翻 trend 是常態，過濾擋掉的正是會回歸的攤平機會
- 現存守門：
  1. **動能反向**：SHORT 遇 Stoch 金叉 / LONG 遇 Stoch 死叉 → 阻擋（唯一保留的最低防線）
  2. **Guard Cooldown**：guard 擋下後 300s 內封鎖
  3. **TP1 後禁 DCA**：qty 曾縮小後不再 DCA，防翻倍虧損

### Loop 機制

- 正常完成（TP/SL/flat_detected）→ `_finish_flat_run` → 自動 arm 下一個 run
- SL 後有 cooldown → 記 `_loop_resume`，到期後下個 tick 續接（Bug 7 修復）
- Entry 階段失敗（signal_timeout / entry_ttl / entry_not_open / GTX 全拒）→ 消耗計數、不套 cooldown、直接 arm 下一個（Bug 9 修復）
- Telegram 通知失敗不重置 loop 計數（loop arm 的 `_notify()` 移到 `else` 區塊）

### Rescue / Catchup

- Telegram `/rescue` 可即時開關（inline button，無需重啟）
- **開啟**：只要當日 PnL 未達標，立即放寬進場條件搶單（量能門檻 ×0.45、RSI 放寬、VWAP 軟訊號）
- **關閉**：只用嚴格 S1/S2/S5 訊號
- 時間閘門（12:00/14:00）已從 live 移除，只剩回測用

---

## 已知待辦

| # | 問題 | 狀態 |
|---|------|------|
| 1 | `exit_reason` 不一致：flat_detected 平倉但 DB 寫成 TP | **已修**（06-09 v2：flat_detected + PnL<0 → SL，不限 inferred==TP） |
| 2 | DCA 失敗通知誤報（先 ⚠️ 失敗後補 🧩 已掛） | 未修 |
| 3 | TP sync 過密（單一 run 30+ 次） | **已修**（06-09：per-level price+qty match，平均 1.8 sync/run） |
| 4 | 異常 DB 資料遷移（雙 testnet 路徑殘留） | 未修 |
| 5 | 改用 systemd 服務（目前 nohup 手動） | **已修**（06-09：Gemini 裝 cry3.service，改用 systemctl restart） |
| 6 | VM git 與本機 commit 不同步（VM 仍是 `73a4b92`，本機 `49485a4`） | 未修 |
| 7 | `signal.wildcat.sl_pct` 缺值時 SL 靜默不掛 | 未修 |
| 8 | TP3 DCA 後未重算（用固定 signal.take_profit 而非 tp_pct × 新均價） | **已修**（06-09） |
| 9 | TRAIL maker 靜態掛單被盤口甩開 → taker fee 吃掉利潤 | **已修**（06-09：reprice loop，TTL 12s/2s） |
| 10 | 進場品質：深跌破 EMA50 接刀單（B_1.0 過濾） | **回測完成，暫不實裝**（砍 14% 利潤換 14% DD 降低，1:1 交換） |
| 11 | DCA 預掛被 guard 擋後不補掛：`_preplace_next_dca` 只在「進場/DCA 成交瞬間」試一次，Stoch 反向 cross 擋下後動能翻正也不回頭補，退回 10s poll 兜底（還吃 60s guard cooldown）。修法：manage cycle 加 top-up（RUNNING 中、無預掛、條件過、guard 放行 → 補掛） | 未修（06-10 發現，23:46/00:20 兩例） |
| 12 | DCA 第 2 層預掛 `-2019 Margin is insufficient`：75x 下持倉+預掛單保證金加總超過餘額，第 2 層起預掛可能靜默失敗（只 log warning），run 失去攤平能力 | 未修（06-10 00:13 一例，cry3mn_1781050335800） |
| 13 | DCA guard（Stoch 動能）誤殺疑慮：guard 擋下只發 TG 不寫 DB event，無法統計「擋下後 V 彈（誤殺）vs 續跌（救命）」。回測 off(+1077/DD14.5) 本就贏 momentum_only(+968/DD13.6)；live 證據混合：cry3mn_1781051773405 誤殺（擋 DCA → SL −0.29，3 分鐘後 V 彈超過原 TP，有 DCA 應 +0.13）vs cry3mn_1781055344337 救命（擋下後續跌到 1631.84，DCA 進去會用 2 倍部位再被掃） | **已修**（06-10：guard 擋下寫 `dca_guard_blocked` event，poll+preplace 兩路，含 mark/entry/trigger 供反事實統計；累積樣本後再決定開關） |
| 14 | Ladder 進場 GTC 穿價吃 taker：`_place_entry` 用 GTC LIMIT 掛 signal−3bp，訊號→下單延遲間價格跌超過 3bp 時掛單直接 cross 以 taker 成交（fee 0.04% = 0.080/200 名目）。06-10 凌晨 3 例（54933311/55014138/56958325）共 −0.24 = 當晚淨利 28%，其中 2 例把毛利 TRAIL 翻成淨虧 | **已修**（06-10：進場改 GTX post-only，-5022 被拒 → 貼盤口 bid(BUY)/ask(SELL) 重掛 GTX，再拒則 reject+loop advance） |
| 15 | Telegram TimedOut 把已完成 run 覆寫成 FAILED：`_notify` 例外會冒泡進 `run_cycle` 的 exception handler → `complete_run(FAILED)`。實例 cry3mn_1781053774815（flat +0.163 完美收場，🏁 通知逾時被改寫 FAILED）。更糟：mid-run 通知逾時會孤兒化活倉（manager 死了、預掛 DCA GTC 還在交易所等成交） | **已修**（06-10，**P0**：`_notify` 內 try/except 全吞、只 log `notify_failed_swallowed`） |
| 16 | TRAIL arming 延遲：2s watcher 只在 armed 後才啟動，arming 本身仍卡 10s manage cycle——急殺時 peak 沒記到、trigger 用舊 peak 算，TRAIL 出在 entry 之下。實例 cry3mn_1781054933311（entry 1640.36，TRAIL 出 1640.31） | **已修**（06-10：watcher 從進場成交就啟動，peak 追蹤 + arming + trigger 全收進 2s loop；`_run_running` 補拉防重啟遺失） |
| 17 | TRAIL 在水下開火 = 用 12s 慢動作 SL：V 崩時 mark 一個 2s step 從 trail_stop 之上 gap 到 entry 之下，trigger 照樣 fire → 取消真 SL、maker chase 追著下殺盤口跑，最後成交≈SL 價。06-10 凌晨 5 筆 TRAIL 虧損共 −1.40（2 筆明確誤殺：64598281 −0.34 應 +0.15、65877939 −0.25 後回 avg 上）。trail_c 回測用 bar close 永遠在 entry 上出場，live 2s 粒度才暴露 | **已修**（06-10：A 利潤地板——trigger 加 `mark>entry`(LONG)/`mark<entry`(SHORT)，水下不出場交還 SL/DCA；watcher+manage cycle 同步；A2 chase 地板——maker 追價跌破 cost basis 即放棄改市價） |
| 18 | 出場期間預掛 DCA 無人管：`_trail_exiting` 短路讓 manage cycle 全程裝死，`_close_position` 只取消 SL/TP，預掛 DCA GTC 還躺交易所→出場中可能反向加倉。實例 cry3mn_1781063317906（DCA 已成交但 DB 停在進場價、無 recovery_entry_filled、SL 沒重掛） | **已修**（06-10：B `_close_position` 開頭一律取消 `_dca_preloaded`，所有出場路徑） |
| 19 | DCA 預掛 GTC 也穿價吃 taker（#14 同類）：急跌中預掛單下單時價已穿過 trigger，GTC 直接 cross。實例 cry3mn_1781065747854 / 64156997 各吃 0.080 | **已修**（06-10：C 預掛改 GTX，-5022 → 貼盤口 bid 重掛 GTX，passive 等下一個 downtick 0 fee 成交） |
| 20 | 連敗無階梯冷卻 + cooldown 只認 SL 不認 TRAIL 淨虧：原 cooldown 只在 exit_reason==SL/flat-loss 觸發，TRAIL 淨虧（凌晨主要失血）完全不冷卻；且固定 3min 不隨連敗升級 | **已修**（06-10：D 改 net PnL 判定虧損（realized−commission<0），cooldown 階梯 base+step×(streak−1)=3/8/13…min，淨勝重置 streak；`mainnet_loop_cooldown_step_minutes=5`） |
| 21 | DCA 成交後 stale peak 秒 arm：攤平把均價拉近市價，舊 peak 對新均價立即滿足 arm_mfe，TRAIL 髮絲觸發在損平附近開火（06-10 08:32 偷走 1 分鐘後的 TP，−0.031 vs 應 +0.26） | **已修**（06-10 V4 E1：DCA 成交即重置 `_trail_peak`=mark + 解除 arm；watcher 2s 快路徑偵測 cost-basis 變動同步重置） |
| 22 | TRAIL 利潤地板零邊際：#17 的 `mark>entry` 地板被 0.002 穿過，損平開火沒有空間給 maker 出場 | **已修**（06-10 V4 E2：`mainnet_trail_profit_floor_bp=1.5`，trigger 需 mark > entry×(1±1.5bp)，manage cycle + watcher 同步） |
| 23 | TRAIL trigger 看 mark 但 maker 出場吃 book：急殺時 bid 已跌破成本而 mark 還在上方，開火會拆掉 SL/預掛 DCA 再追盤口虧出 | **已修**（06-10 V4 E3：`_close_position` 在拆單**前**驗 book 錨點（SELL 看 bid）過利潤地板，不過則 abort 開火、`_trail_exiting` 清除、交還 SL/DCA 管理；拆單後 `_try_trail_maker_exit` 的二次檢查仍走市價兜底） |

### Telegram 新功能（06-10 V4）

- **金額選擇器**：💰 200/300/500/1000 USDC（callback `mainnet:notional:`），改 equity_cap=initial_notional=金額、max_cumulative=金額×(recovery_steps+1)；run 進行中拒改；持久化 app_config `mainnet_notional_usdc`
- **Loop 次數**：新增 20/30 選項（1/3/5/10/20/30）
- **Loop 虧損保護**：🛡 關/2/5/10/20 USDC（callback `mainnet:losscap:`），loop 累計淨損益（realized−commission）≤ −cap 即斷鏈停止剩餘 run 並發統計；持久化 app_config `mainnet_loop_loss_cap_usdc`，`mainnet_loop_loss_cap_usdc=0` 預設關閉

### V4 上線紀錄

**06-10 10:00 UTC** 部署完成，**cry3mn_1781085902526** 開始是 V4 的交易單。E1/E2/E3 修補 + Telegram 三新功能已上線。

---

## 語言

所有回覆使用**繁體中文**。
