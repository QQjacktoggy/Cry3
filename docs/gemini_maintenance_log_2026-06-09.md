# Gemini 系統維護日誌 - 2026-06-09

**作者**: Gemini (Advanced Agentic Coding Team)
**目的**: 修正實盤交易在 DCA（加碼）啟動後，多層級止盈訂單（TP1/TP2/TP3）沒有同步進行縮小（shrink）且未進行價格封頂，導致部分止盈單在最終止盈觸發後殘留在交易所，最後打到止損（SL）造成大額虧損的問題。

---

## 📌 問題分析與動機
在 `wildcat_v2_adverse_guard` 策略中：
1. **回測行為**:
   - 當 DCA（加碼）發生後，部位數量加倍，最終止盈百分比會乘以 `recovery_tp_shrink` (預設為 `0.45`，例如 `0.08%` 縮減為 `0.036%`)。
   - 回測只有兩層 TP 且最終 TP 被觸及即全平，因此能以極高勝率（WR 87-93%）鎖定微利出場。
2. **實盤行為與 Bug**:
   - 實盤會分批掛出三筆限價單：`tp1` (+0.05%)、`tp2` (+0.12%)、`tp3` (S1 策略最終 TP +0.08%)。
   - DCA 發生後，實盤僅縮小了 `tp3`（縮為 0.036%），但 `tp1` (0.05%) 和 `tp2` (0.12%) 仍保持原樣。
   - **導致結果**：價格反彈時，最貼近均價的 `tp3` (+0.036%) 首先成交，平倉了 **30% 的部位**。然而，剩餘的 **70% 部位（tp1 與 tp2）依然高掛在 +0.05% 和 +0.12% 處**。由於這兩層太遠，價格隨後反轉直下，這 70% 的重倉部位最終全部打到止損（SL）平倉，造成大額虧損（如 Run 5 `cry3mn_1780989304168`）。

---

## 🛠️ 修改內容

### 1. 儲存 `recovery_tp_shrink` 參數
* **檔案**: [one_run.py](file:///home/jack_shih/cry3/src/gridbot/mainnet/one_run.py) 中的 `_place_entry` 方法。
* **修改**: 將策略決定 (decision) 的 `recovery_tp_shrink` 寫入至資料庫的 `signal_json["wildcat"]` 中，使後續週期載入時可正確取得縮放因子。

### 2. 多層級止盈價格之 DCA 同步縮放與價格封頂
* **檔案**: [one_run.py](file:///home/jack_shih/cry3/src/gridbot/mainnet/one_run.py) 中的 `_desired_take_profit_orders`、`_partial_take_profit_price` 與 `_mid_take_profit_price`。
* **修改**:
   - 若 `dca_count > 0`，提取 `recovery_tp_shrink` 因子（若資料庫中無紀錄則 fallback 至配置檔的 `mainnet_recovery_tp_shrink`），並將 `tp_pct` 乘以該因子進行縮放。
   - 在調用 `_partial_take_profit_price` 與 `_mid_take_profit_price` 時傳入 `shrink` 因子，使其計算出來的部分止盈比率（tp1）與中等止盈比率（tp2）同步縮小。
   - **價格封頂邏輯**: 針對部分與中等止盈價格，強制進行方向性的封頂限制（LONG 用 `min`，SHORT 用 `max`），使其不得超過最終止盈 `tp3` 價格。這保證了當最終止盈價格觸發時，剩餘的所有分批訂單都已經在該價格或更近的價格全部被成交，不再有殘留部位。

### 3. 單元測試驗證
* **檔案**: [test_mainnet_one_run_maker.py](file:///home/jack_shih/cry3/tests/test_mainnet_one_run_maker.py)
* **修改**: 新增 `test_run_running_dca_shrinks_and_caps_take_profits` 測試，模擬 SHORT 方向在 `dca_count=1` 時的止盈計算，確認 `tp1` 與 `tp2` 分別正確被縮放與封頂。

---

## 🧪 驗證結果
- 本地 `pytest` 測試（共 269 個測試）全數通過，無任何 Regression。
