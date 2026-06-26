# Maintenance Log — v1.4.0 部署

**Date**: 2026-06-25  
**Deployed by**: Hermes Agent  
**VM HEAD before**: `df39aba`  
**Bot PID after restart**: `2394352`  
**Restart time**: 2026-06-25 16:38:58 UTC (2026-06-26 00:38 TPE)

## 變更背景

### 24h Live 數據（v1.3.18 micro50, 72 runs）

| 指標 | 值 |
|---|---|
| 總 runs | 72 |
| Completed | 40 (55.6%) |
| Entry expired | 32 (44.4%) |
| Wins / Losses | 25 / 15 (WR 62.5%) |
| Gross PnL | +0.0936 USDC |
| Commission | 0.3519 USDC |
| **Net PnL** | **-0.2583 USDC** |

### 核心問題

1. **STUP-S 3bp maker 進場太遠** → 51% 過期率（29/57），最大瓶頸
2. **手續費 3.75x 毛利** → 50U 微利結構不可持續
3. **W6A 勝率 42.9%**，淨虧 -0.1826（最大虧損 lane）
4. **signal_timeout 60min** → loop 週轉慢

### Shadow 證據

- Entry shadow 勝率 72.7%（112 tp1_first / 154 total）→ 被跳過的進場其實會賺
- TP policy shadow: `profit_b_runner45` 擊敗 baseline +0.46bp（promotion eligible）
- Replay 報告（CODEX_V1_3_18_EXPIRED_REPLAY_COMPARISON）: 1bp 讓 8/17 過期 run 變 TP

## 部署內容

### P0: STUP-S 進場 3bp → 1bp（最高優先）

**檔案**: `src/gridbot/strategy/codex_v1_live.py` L1326

```diff
-        entry_offset_bp=3.0,
+        entry_offset_bp=1.0,
```

**原因**: Replay 報告證實 1bp 讓 8/17 過期 run 變 TP（vs baseline 1/17）。平均最近 miss gap 僅 1.74bp，3bp 太遠。Shadow 72.7% tp1_first 勝率支持策略選股有效，問題在 maker 成交距離。

**預期**: Fill rate 49% → 65-70%

### P1: TP Policy Live Override 開啟

**檔案**: `config/settings.py` L343

```diff
-    mainnet_codex_tp_policy_live_override_enabled: bool = False
+    mainnet_codex_tp_policy_live_override_enabled: bool = True
```

**原因**: TP policy shadow 系統已收集足夠數據，`profit_b_runner45`（tp1=30%, full=25%, runner=45%）在 LONG/WATCH_PRE_REPRICE lane 上擊敗 baseline +0.46bp，MFE capture 40.3% → 42.7%，且 `primary_promotion_eligible=true`。

**預期**: MFE capture +2.4pp，runner 比例提高保留更多尾部利潤

### P3: W6A 風險門檻收緊 ≥4 → ≥3

**檔案**: `src/gridbot/mainnet/one_run.py` L1116-1118

```diff
-                elif risk_score >= 4:
-                    skip_reason = "v137_w6a_risk_score_block"
-                    regime = "v137_w6a_risk_score_4plus"
+                elif risk_score >= 3:
+                    skip_reason = "v137_w6a_risk_score_block"
+                    regime = "v137_w6a_risk_score_3plus"
```

**原因**: W6A 8 runs 中 4 敗（WR 42.9%），淨虧 -0.1826。risk_score=3 的 run 在舊版仍可 keep50 進場，現在直接 block。risk_score 5 個 flag: no_reclaim, vwap_lte_neg45, pullback_gte_25, setup_age_gte_300, d30_lte_neg30。

**預期**: 減少 W6A 邊際進場虧損

**副作用**: `risk_score == 3` 在 else 分支的 keep50 和 shadow_only 路徑成為 dead code（已被上方 ≥3 block 擷取），不影響功能。

### P4: signal_timeout 60min → 30min

**檔案**: `config/settings.py` L256

```diff
-    mainnet_one_run_signal_timeout_minutes: int = 60
+    mainnet_one_run_signal_timeout_minutes: int = 30
```

**原因**: 最近多筆 signal_timeout 佔用 60 分鐘等不到進場信號，浪費 loop slot。30 分鐘足夠判斷信號是否到來。

**預期**: Loop 週轉加速

## 排除的改動

| 項目 | 原因 |
|---|---|
| API -1003 retry | 用戶確認是其他專案回測卡住，非 bot 本身問題 |
| Notional 50U 調整 | 用戶嚴格指定不動 |
| 0bp 進場 | 先 shadow/canary，不直接上 live |
| TTL 45s 延長 | Replay 報告說 TTL 不是瓶頸 |

## 部署驗證

### Syntax check
```
codex_v1_live OK
one_run OK
settings OK
```

### Bot 啟動日誌
```
2026-06-25 16:38:58 binance_connected testnet=False
2026-06-25 16:38:58 app_initialized
2026-06-25 16:38:58 telegram_app_configured handlers=15
2026-06-25 16:38:58 scheduler_started
2026-06-25 16:38:59 starting_telegram_bot
2026-06-25 16:39:00 testnet_manage_cycle_skipped → 正常 idle
```

### 改動確認
```
P0: codex_v1_live.py L1326  entry_offset_bp=1.0      ✅
P1: settings.py L343       live_override=True        ✅
P3: one_run.py L1116       risk_score >= 3           ✅
P3: one_run.py L1118       risk_score_3plus          ✅
P4: settings.py L256       signal_timeout 30         ✅
```

## 備份

原始檔案已備份在 VM 上：
- `src/gridbot/strategy/codex_v1_live.py.bak_v1319`
- `src/gridbot/mainnet/one_run.py.bak_v1319`
- `config/settings.py.bak_v1319`

## 監控重點

部署後 4-8 小時內重點觀察：

1. **STUP-S fill rate** 是否從 49% 提升到 65%+
2. **TP policy shadow outcome** 是否出現 `live_tp_override` 的新事件類型
3. **W6A block 次數** 是否增加（`v137_w6a_risk_score_block` skip 數上升）
4. **signal_timeout** 是否從 60min 縮短到 30min
5. **淨 PnL** 是否改善（手續費佔比應隨 fill rate 提升而下降）
6. 無新增 FAILED 或 exception