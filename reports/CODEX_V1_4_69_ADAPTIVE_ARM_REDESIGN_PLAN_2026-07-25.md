# Codex v1.4.69 Adaptive Arm 重整計畫

日期：2026-07-25  
對象：Cry3 Live Mainnet Adaptive  
狀態：**設計與 Live evidence 稽核完成；尚未修改交易路徑、尚未部署**

## Executive Summary

- **目前不是完整 Adaptive。** 系統先由舊 classifier 依 registry 順序挑出第一個 lane，之後才疊加 regime、allowlist、promotion、TP shadow 與 cap。重疊 lane 不會取得同一次市場機會的 paired evidence，前方 disabled／shadow-only lane 也可能遮住後方 lane。
- **Live evidence 已證明 starvation 與 cohort fragmentation。** 最近 500 筆 shadow-start（約 27 小時）去重為 360 個機會；至少 45 個本來匹配的 base-lane instances 沒有成為 selected lane。近 90 分鐘只有 3 個 lane 有完整 evidence，STUP-S 的 6 筆又被切成 4 個 exact cohorts。
- **下一版應把交易選擇單位改成 Adaptive Arm。** 每個 arm 由 lane、方向、穩定 coarse regime 與完整 execution profile 組成；同一次機會對所有 matched arms 做 paired shadow，最後由風險感知 arbiter 最多選一個 paid arm。
- **目標不是保證每天獲利。** 正確工程目標是最大化 fee-net daily expected value，同時限制虧損日尾部、鎖住已取得的日內利潤，並以 20 個 active trading days 的正收益日比例作 KPI。

## 1. Live 現況與已確認問題

### 1.1 最新 Adaptive session

最新 session `adaptive_1784908196647`：

- 模式：`adaptive_continuous`
- 期間：最長 72 小時、目標 20 paid fills
- 名義本金：50 USDC
- Session loss cap：0.30 USDC
- DCA／recovery：OFF
- 結果：使用者手動停止；terminal runs 0、paid closed fills 0、net PnL 0
- Session 內：26 個 v1.4.62 shadow opportunities、其中 25 個可歸因 durable opportunities、33 個 shadow starts、33 個 shadow outcomes、0 paid entry/fill event、0 active promotion lease

25 個可歸因 opportunities 分布為：STUP-S 8、W2A 7、ANCHOR-S 6、CNL-WPR-L 2、NL-UNCLASSIFIED 2；另 1 個 S1P-L 因 `market_state` identity mismatch 被丟棄。

33 starts 高於 26 opportunities，是 diagnostic／profile fan-out 與重複 intent 所致；績效計算必須以 durable opportunity ID 去重，不可直接加總 event rows。

前一個約 22.5 小時 session 只有 4 筆 paid fills，fee-net 合計 -0.0831335 USDC：S1P-L 3 筆合計 -0.01694678，CNL-WPR-L 1 筆 SL -0.06618672；另有 6 個 run 以 timeout／`ENTRY_EXPIRED` 結束。這證明問題同時包含樣本稀疏與當前 live payoff 為負，不能只靠放寬 fill gate 解決。

### 1.2 近 90 分鐘 promotion evidence

| Lane | Evaluable | Exact cohorts | TP-first | SL-first | No-fill | Max-hold | Fee-net USDC | EV/op |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| STUP-S | 6 | 4 | 1 | 4 | 0 | 1 | -0.2561 | -0.0427 |
| ANCHOR-S | 4 | 1 | 1 | 0 | 3 | 0 | +0.0180 | +0.0045 |
| W2A | 3 | 1 | 2 | 1 | 0 | 0 | -0.1047 | -0.0349 |

STUP-S 雖有最多 evidence，但四個 cohort 各自不足；W2A 最近為負；ANCHOR-S 小幅為正，但它仍是 disabled legacy route，現行 promotion contract 不允許把 upstream final reject 重新開啟。

### 1.3 近 24 小時 coverage

正式、完整、非 diagnostic 的 v1.4.64 evidence 只涵蓋 27 個 registry lanes 中的 8 個：

| Lane | Evaluable | Exact cohorts | Samples/cohort | Fee-net USDC |
|---|---:|---:|---:|---:|
| STUP-S | 85 | 19 | 4.47 | -1.3394 |
| ANCHOR-S | 17 | 2 | 8.50 | +0.1260 |
| W6A | 11 | 1 | 11.00 | -0.0984 |
| CNL-WPR-L | 10 | 6 | 1.67 | -0.0600 |
| W1B | 9 | 2 | 4.50 | +0.1620 |
| W2A | 6 | 2 | 3.00 | -0.0507 |
| W1D | 1 | 1 | 1.00 | +0.0180 |
| ANCHOR-L | 1 | 1 | 1.00 | 0.0000 |

STUP-S 佔 140 筆 evidence 的 60.7%，卻被切成 19 個 cohorts；另外 19 個 registry lanes 在這個 window 沒有正式 evidence。

同一期間另有 77 個 shadow drops：`UNKNOWN` 53、`SH_NL_NEAR_W1D_LONG_LIVE200` 13、CNL-L1MR-L 10、S1P-L 1，多數原因是 `data_incomplete:exact_identity_mismatch:market_state`。因此下一版必須把 market-state identity 的產生、持久化與 join 規則納入第一階段修正。

### 1.4 First-match starvation

`select_codex_v1_lane()` 的 contract 是回傳第一個匹配 lane，且在第一個 match 後立即 return。HUE、STUP／stale short、mid-extension、SFD 等特殊分支還會在一般 `LANES` loop 前提早 return。

最近 500 筆 start（約 27 小時）重播 base lane predicates：

| Lane | Selected | Predicate matched | Matched but suppressed |
|---|---:|---:|---:|
| ANCHOR-S | 19 | 44 | 25 |
| W1B | 10 | 27 | 17 |
| W6A | 65 | 68 | 3 |
| W2A | 11 | 11 | 0 |

這是下限，不是完整 starvation 數：STUP-S、CNL-WPR-L 等特殊分支不在同一個 base matcher 中。它已足以證明 registry 順序目前是隱性優先級，零樣本不能被解讀成「該 lane 沒有市況」。

### 1.5 TP／SL／cap 尚未共同 Adaptive

- v1.4.59 regime overlay 有 TREND／RANGE profile，可包含 entry、TP1、full TP、SL、partial、TTL、hold、maker 與 exit mode，但 Live enforcement、runner、early-fail、one-step reprice 目前關閉。
- v1.4.60 lane matrix主要控制 admission、risk scale 與 25／50 cap，不比較完整 TP／SL profile。
- v1.4.64 promotion 只允許 upstream admission 全部通過的 exact cohort，不能重新開啟 disabled／final-reject lane。
- v1.4.65 只有 W6A 做三個 paired profiles；selector 開啟但 enforcement 關閉時，可能形成 authority ownership dead zone。
- 目前 cap 是多層固定上限，沒有統一按 `SL + fee + slippage` 正規化每筆最大損失；安全 cap 小於 exchange minimum 時，現行某些路徑可能向上 lift 到 minimum，這對 risk-based sizing 不安全。

## 2. Lane 重整原則

### 2.1 保留歷史 lane code，不立刻刪除

所有 27 個 registry lane code 先保留，避免歷史 evidence 失去 identity：

- 現行 controls：`RP1`、`S1P-L`
- 現行 mixed：`STUP-S`、`CNL-WPR-L`
- 現行 shadow-only：`HUE-L`、`ANCHOR-L`、`ANCHOR-S`、`W2A`、`W5A`、`W3A`、`W1A`、`W6A`、`W5B`、`W4A`、`W7A`、`W1B`、`W3B`、`W6B`、`W2B`、`W4B`、`W1C`、`W6C`、`W1D`、`W2C`、`W3C`、`W1E`、`SFD-S`

下一版不再把 `SHADOW_ONLY` 當作永久身份，而是把每個 lane/profile/regime arm 放入 `SHADOW / PROBATION / LIVE / COOLDOWN` lifecycle。只有 hard-safety rejects 與 `OUT_OF_REGISTRY` 永久禁止 paid。

### 2.2 分離「匹配」、「安全」與「是否有下單權」

同一次市場 snapshot：

1. `match_all_lanes(features)` 回傳所有 predicate matches、near matches、missing features 與 deny annotations。
2. Hard-safety layer 移除 identity、ownership、reconciliation、stale data、shock、方向失效等不可 reopen 的候選。
3. 每個剩餘 lane 產生 2–3 個封閉 execution profiles。
4. 所有 arms 共用同一個 opportunity group、tick envelope、fee/slippage model。
5. Shadow evaluator 對每個 arm terminalize。
6. Arbiter 從有 authority 的 arms 最多選一個 paid winner。

Registry 排序只能作穩定顯示或 tie-break，不能決定誰取得 evidence。

### 2.3 舊 lane 的合併／退役條件

先收集 2–4 週 paired evidence，再執行：

- **合併候選：** 同 side/strategy/profile、matched-opportunity Jaccard overlap ≥ 0.80，且其中一個 lane 沒有額外正 EV slice。
- **退役候選：** 至少 30 matched、20 evaluable，fee-net EV 的保守上界仍 ≤ 0，且不同 regime 都沒有正 slice。
- **Instrumentation bug：** 有 synthetic reachability witness，連續 7 個 active days 卻 0 match，或 required feature 永遠 missing。
- **保留：** 雖低頻但在明確 regime 有正 paired incremental EV；低頻本身不是刪除理由。

退役 lane 保留 historical alias 與 monitor row，不可刪除舊 evidence。

## 3. Adaptive Arm 與 Profile Identity

### 3.1 Arm identity

```text
arm_id =
  lane_code
  + effective_side
  + strategy
  + coarse_regime
  + execution_profile_id
  + execution_profile_schema
```

`execution_profile_hash` 必須包含：

- entry offset／TTL／maker mode／one-step reprice policy
- TP1／mid／full TP、partial fractions
- SL、BE
- max hold
- trail arm／giveback／profit floor
- runner／early-fail policy
- DCA policy（第一階段固定 OFF，但 identity contract 先保留）

絕對價格、timestamp、run/order/opportunity ID 與 cap tier不得進 execution cohort。

`risk_policy_hash` 另行記錄並在 submit 前驗證，但不切 execution evidence cohort；shadow reward 以 bp 正規化，避免 25／50 USDC cap 把同一幾何切成不同 evidence。

### 3.2 初始封閉 profile menu

第一階段只做 shadow comparison：

| Profile | 適用 regime | Entry | TP | SL | Exit | 初始 live authority |
|---|---|---|---|---|---|---|
| `RANGE_SCALP` | RANGE | passive 1 bp | TP1 5 / full 8 bp | 8 bp | 100% full、hold 360s | OFF |
| `TREND_PARTIAL` | TREND | passive 2 bp | TP1 6 / full 16 bp | 10 bp | 70% partial；runner/trail shadow | OFF |
| `PASSIVE_BALANCED` | RANGE/TREND challenger | passive 2 bp | full 8 bp | 12 bp | 100% full、TTL 120s | OFF |
| `RISK_OFF` | SHOCK/invalid | no entry | — | — | block | 永久 |

W6A 現有 `BASE / TIGHT / PASSIVE` 先相容保留；轉入通用 arm ledger 後才移除 lane-specific selector ownership。

Runner、trail、one-step reprice 各自必須先有 paired shadow ledger，不能因 profile 名稱出現就直接取得 live mutation authority。

## 4. 靈活但不拖延的時間窗

不再要求 2 個 UTC dates。所有 lanes 使用同一個多時間尺度 framework，避免不同 lane 產生不可比較的 promotion 規則：

- **Safety window：15 分鐘。** 最新結果為 hard loss、15 分鐘內兩個 SL、shock／direction invalid 立即 veto。
- **Authority window：45 分鐘。** 至少 4 個 paired evaluable、至少 3 個 TP-first、NO_FILL 以 0 納入 denominator、fee-net EV/opportunity > 0。
- **Guard window：180 分鐘。** 至少 6 個 evaluable；只防止短暫好轉忽略近期負尾部，不直接決定 winner。
- **Regime freshness：60 秒。** 兩次確認、15 秒 minimum dwell；submit snapshot ≤ 10 秒。
- **Lease：** probation 5 分鐘、live 10 分鐘；只有新 evidence revision 才能續租。
- **Drift：** regime 改變、evidence distribution 明顯切換或 safety breach 立即 revoke；不等待 lease 自然到期。

低頻 lane 未達最小樣本時維持 shadow，不以延長到數天或降低安全門檻強行轉正。

## 5. 唯一 Live Arbiter

每個 symbol/opportunity 最多一個 paid arm。Arbiter 排序：

```text
utility =
  recency_weighted_fee_net_EV_per_opportunity
  - uncertainty_penalty
  - tail_loss_penalty
  - regime_staleness_penalty
  - current_drawdown_penalty
```

必要條件：

- paired opportunity denominator 完整，NO_FILL = 0 reward
- hard safety、identity、data quality、regime freshness全部通過
- challenger 比 incumbent 至少多 +2 bp paired EV，且至少 3 個 paired wins
- tie-break 為明列、穩定且與 registry 順序無關
- DB CAS claim 成功後才可呼叫 order API
- submit 前重驗 lane、side、profile hash、regime、lease、risk budget

多個 lane 同方向同時合格時可作 confirmation signal，但 cap 永遠不因「投票數」超過全域 50 USDC。方向衝突時保持 shadow，除非明列的 arbiter rule 有足夠 evidence。

## 6. Dynamic TP／SL／cap 與日內風控

### 6.1 SL-normalized notional

```text
notional_cap = min(
  stage_cap,                 # SHADOW 0 / PROBATION 25 / LIVE 50
  global_cap,
  lane_cap,
  remaining_daily_risk
    / ((sl_bp + roundtrip_fee_bp + slippage_bp) / 10000)
)
```

- 安全計算結果低於 exchange minimum ticket：直接 block，不得向上 lift。
- TP／SL 變動先形成新的 execution profile hash；cap 只控制 exposure，不可偷偷改交易幾何。
- DCA／recovery 第一階段固定 OFF。

### 6.2 TPE 日內 guard

以 Asia/Taipei active trading day 切割 paid results：

- Soft loss：日內 fee-net ≤ -0.15 USDC，後續新 entry 降至最多 25 USDC。
- Hard loss：日內 fee-net ≤ -0.30 USDC，停止該日所有新 paid entries。
- Positive high-water：達 +0.15 USDC 後，`profit_floor = max(+0.02, high_water - 0.15)`；closed paid PnL 回落到 floor 時停止新 entry。
- Active position 不因日界或 lease revoke 被強制放棄保護；hard SL 與 risk-reducing exit 永遠繼續。

以上為 50 USDC canary 的 provisional bounds，必須以 paid fill、commission、funding、slippage 分布重新校準。

## 7. KPI 與版本目標

### Primary outcomes

1. **Rolling fee-net PnL > 0**：以 reconciled paid PnL − commission − funding 計算。
2. **20 個 active trading days 的正收益日比例 ≥ 55%。**
3. **Fee-net EV / deduplicated matched opportunity > 0。**

每日正收益是目標與觀察 KPI，不是可保證的結果。不得為了讓當日翻正而放寬 gate 或過度交易。

### Driver metrics

- raw matched → safe candidate → shadow complete → evaluable → authorized → submitted → filled → paid closed funnel
- accepted-to-fill rate與 NO_FILL rate
- 每 lane/regime/profile 的 opportunity rate、fee-net EV、MFE/MAE、SL tail
- `matched / selected / suppressed-by / starvation age`
- profile challenger paired wins與 winner-switch frequency
- 90m／6h／24h context panels；只用於穩定度與漂移判讀，不可取代 45m authority window

### Guardrails

- 每 opportunity 最多一個 paid order claim
- 每筆 ≤ 50 USDC、PROBATION ≤ 25 USDC
- 每日 hard loss ≤ 0.30 USDC
- 無 hard-safety reopen、無 stale regime submit
- 100% evidence 有 durable opportunity identity或明確 drop reason
- scheduler 無新增 max-instance skip；DB storage growth 有上限

## 8. 儲存與效能設計

Live DB 約 1.06 GB，VM 剩餘空間約 1.13 GB。不能把目前大型 `details_json` 直接乘上所有 lane × profile。

Migration 016 應建立 compact normalized tables：

- `v1469_market_opportunities`：每個市場機會一份 feature snapshot／hash
- `v1469_lane_candidates`：每 opportunity × lane 的 match、安全、suppression、route
- `v1469_arm_evidence`：每 opportunity × execution profile 的 terminal outcome／bp reward
- `v1469_arm_leases`：唯一 authority、generation、expiry、cap與risk-policy hash
- `v1469_arm_events`：append-only lifecycle，payload 限制為小型 stable fields

大型 raw classifier JSON只保留一份；候選與 arm rows使用 foreign key，不重複 features。需加入每日 row/MB growth telemetry、低空間 fail-closed與可驗證 archive／retention 計畫。任何 retention 不得刪除尚在 authority/guard window內的 evidence。

## 9. 實作階段

### Phase A — v1.4.69 Observation Foundation

- 實作 registry-order-independent `match_all_lanes`
- compact schema與 durable opportunity group
- 所有 matched lanes取得 shadow attribution
- Telegram Lane Monitor新增 matched、selected、suppressed、zero-match reason、starvation age
- production order semantics完全不變；所有新 enforcement flags default OFF

### Phase B — Paired Arm Evidence

- 通用 2–3 profile paired evaluator
- 完整 execution profile schema/hash
- 15/45/180 windows、drift/revoke、data-quality checks
- W6A evidence遷移相容層
- 不下 paid order

### Phase C — Arbiter Probation

- deterministic unique-winner arbiter與CAS claim
- 25 USDC probation、5 分鐘 lease
- SL-normalized cap、daily soft/hard/high-water guard
- 先對既有 controls做 canary；其他 lanes依 evidence取得 authority

### Phase D — Live Expansion

- 3 paid probation fills、至少 2 wins、paid fee-net > 0才升到50 USDC
- 每 5 fills checkpoint；20 active days評估正收益日比例
- 2–4週 paired evidence後才合併／退役舊 lanes
- runner／trail／reprice分開 shadow → probation，不能同版全開

## 10. 驗收標準

1. Registry 重排後 match set與arbiter winner完全相同。
2. W2A＋W6A＋W1D、ANCHOR-S＋W1B 等重疊 fixtures全部取得同 opportunity evidence。
3. Disabled／shadow candidate不會阻止後方 lane收集資料。
4. 27 個 registry lanes全部有 synthetic reachability witness。
5. 每個零樣本 lane顯示「predicate未命中／feature缺失／被壓住／hard safety」之一。
6. 同 opportunity的所有 profiles共用相同 tick envelope與決策時間。
7. NO_FILL以0進入EV denominator。
8. Market-state identity 在 opportunity、candidate、evidence、lease 間可一致 round-trip；不再產生 `exact_identity_mismatch:market_state`。
9. BE、trail、runner、reprice、DCA或TP/SL任一 execution維度變更都會改 profile hash。
10. Cap tier或絕對價格變更不切 execution cohort。
11. 多個 authorized arms只能有一個CAS claim成功且最多一個order API call。
12. Regime stale／切換、lease mismatch、future/late evidence在submit前 fail closed。
13. SL或成本增加時notional只能下降；低於exchange minimum直接block。
13. Exact arm cooldown不封鎖其他符合當前市況的lane。
14. Daily soft loss降cap、hard loss停新entry、positive high-water floor可重啟後恢復。
15. Fan-out後10秒run cycle不超時，Lane Monitor不阻塞scheduler。
16. DB每日成長、free space與archive狀態有Telegram告警，低空間不建立新paid authority。

## 11. Caveats

- 目前 Live window短，不能從少量正值推論可獲利；尤其 ANCHOR-S 的 +0.018 USDC與 W1B 的 +0.162 USDC都仍是小樣本。
- Shadow fill model不是實際排隊成交；promotion與paid validation必須繼續使用實際 commission、funding、maker/taker與slippage。
- First-match overlap重播只覆蓋保存的 shadow starts，且STUP/CNL特殊分支不在base matcher中；45個 suppressed instances是已證明下限。
- 每日正收益無法保證。Adaptive的價值應由正收益日比例、fee-net EV與負尾部改善共同判斷。
- 任何 production enforcement／部署仍需在loop停止、帳戶FLAT、零open orders下另行驗證。
