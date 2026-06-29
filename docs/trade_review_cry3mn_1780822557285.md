# 交易復盤:cry3mn_1780822557285(SHORT,reason=SL 但 PnL 為正)

> 狀態:**待對帳**。分析基於程式碼 `src/gridbot/mainnet/one_run.py`,尚未連線實際 SQLite 確認成交價。
> 使用者將回電腦提供 DB 連線,屆時用文末查詢驗證反推值。

## 摘要

| 項目 | 值 |
|---|---|
| 策略 | S1_BB_RSI(score=82) |
| 方向 | SHORT ETHUSDC |
| 進場價 | $1635.1300(maker Post-Only LIMIT) |
| 數量 | 0.122 ETH |
| SL(預掛 maker) | $1637.7024(進場上方) |
| TP(預掛 maker) | $1633.5534(進場下方) |
| 結束原因標籤 | SL(SL maker 被 -5022 拒 → 市價 fallback) |
| 已實現損益(毛) | **+$0.1229** |
| 手續費 | $0.0477(taker) |
| 淨損益 | +$0.0752 |
| Loop | 1/1 結束,觸發同 side+strat 5min cooldown |

## 邏輯鏈 vs 程式碼(已核對,正確)

- arm(1) → ARMED:`arm()` L151
- wildcat 訊號 → 入場:`_run_armed` L296 → `_place_entry` L449
- entry maker(GTX)+ SL/TP 都預掛 maker:`_place_post_only_with_retry` + `_sync_take_profit_orders` + `_place_stop_loss_maker(post_only=True)`
- SL maker 被 -5022 拒 → 市價 fallback:`_place_stop_loss_maker` L1194-1208(使用者收到的 `🏁…（SL maker 被拒,市價）` 來自 **L1207**)
- reason=SL、loop 結束、cooldown:`_finish_flat_run` L1268,cooldown L1290-1295

## 關鍵發現:為什麼 reason=SL 卻賺錢

`exit_reason="SL"` 只是「為什麼觸發平倉」的標籤,與「以什麼價格成交」**解耦**。
真正 PnL 來自 Binance 實際成交(`get_user_trades` 的 `realizedPnl`),見 `_build_run_summary` L1411-1426。

反推實際平倉價(SHORT,realizedPnl 為毛額不含手續費):

```
realized_pnl = (entry - exit) × qty
0.1229       = (1635.13 - exit) × 0.122
exit ≈ 1635.13 - 1.0074 ≈ $1634.12
```

**實際平倉價 ≈ $1634.12**(進場下方、靠近 TP $1633.55),**不是 $1637.70**。原復盤的「平倉≈$1637.70」推測錯誤。

### 最可能劇本

1. 急速上影針(很可能是 mark price 尖刺)把 mark 拉到 ≥ $1637.70 → `_hit_stop` 觸發(L1124,比較 `position.mark_price`)。
2. 嘗試 maker BUY @ 1637.70 平倉,該瞬間 ask ≤ 1637.70 → **-5022**。
3. fallback 市價單送出時價格已**回殺**到 ~$1634.12 → 市價 BUY 在此成交 → 倒賺 +$0.1229。
4. `exit_reason="SL"` 早在 L1206 寫死 → 報告顯示 SL。

毛 +$0.1229 − taker $0.0477 = 淨 +$0.0752。taker 手續費吻合市價單。

## 值得記錄的行為特性(非 bug,但會誤導判讀)

1. **停損用 mark 觸發、用市價成交** → 實際結果可能偏離 SL 價位(兩個方向都會;這次順向賺到,但同機制下也可能逆向吃滑價)。
2. **標籤優先序掩蓋真實出場**:`_finish_flat_run` L1270 用 `run.exit_reason or summary[...]`,`run.exit_reason("SL")` 永遠優先於 `_infer_flat_exit_reason` 從實際最後成交單推出的結果。→ 判讀真實出場要看**成交價 + PnL**,別只信 reason 標籤。

## 待辦:對帳驗證查詢(回電腦連 DB 後執行)

`futures_trades` schema 見 `src/gridbot/storage/migrations/002_futures_trades.sql`。

```sql
-- 1) 該 run 所有實際成交(關鍵看 closing trade 的 price 與 is_maker)
SELECT trade_id, order_id, side, price, qty, realized_pnl,
       commission, is_maker, time_ms
FROM futures_trades
WHERE symbol='ETHUSDC'
  AND time_ms BETWEEN <armed_at_ms-60000> AND <now>
ORDER BY time_ms;
-- 預期:進場 SELL(is_maker=1, realized_pnl=0)
--       平倉 BUY price ≈ 1634.12, is_maker=0(taker), realized_pnl ≈ +0.1229

-- 2) 事件流確認觸發路徑
SELECT event_type, payload, created_at_ms
FROM mainnet_run_events
WHERE run_id='cry3mn_1780822557285'
ORDER BY created_at_ms;
-- 應有: armed → entry_placed → entry_filled → take_profit_synced
--        → sl_maker_placed? → close_submitted + sl_maker_fallback_market → completed
```

**驗證標準**:只要 closing BUY 的 `price < 1635.13`,即證實「SL 觸發但獲利平倉」,反推值($1634.12)成立。

## 後續可選工作(待使用者授權)

- (A) 把上述查詢包成 `scripts/` 對帳小腳本。
- (B) 在 `run_events` 記錄「mark 觸發價 vs 實際 closing 成交價」的差距(fallback 滑價觀測),統計這類 fallback 的真實滑價分布。
