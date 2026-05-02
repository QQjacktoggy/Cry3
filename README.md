# Binance Futures Grid Bot Monitor (Cry3)

一個基於 Google Gemini 3 Flash AI 的幣安合約網格交易監控系統。自動追蹤 USDC-M 合約網格表現，分析市況並提供策略優化建議。

## 🌟 核心功能

- **即時監控**：追蹤 BTC/USDC、ETH/USDC、SOLUSDC 等合約交易對。
- **AI 策略分析**：使用 Gemini 3 Flash Preview 進行深度市場分析，提供格距、格子數、槓桿與方向建議。
- **風險管理**：即時計算保證金率 (Margin Ratio)、清算距離 (Liquidation Distance) 並發出警告。
- **Telegram 互動**：透過 Telegram Bot 接收報告、觸發分析、提問 AI 策略問題。
- **精確損益**：直接抓取 Binance Income 記錄，精確計算已實現損益、手續費、資金費 (Funding Fee)。
- **網格識別**：自動過濾手動交易，僅追蹤由網格機器人 (`aos_` 前綴) 產生的訂單。

## 🛠️ 技術棧

- **語言**: Python 3.11+
- **AI**: Google Gemini 3 Flash (google-genai)
- **API**: Binance FAPI (python-binance)
- **互動**: Telegram Bot API (python-telegram-bot v21)
- **資料庫**: SQLite (aiosqlite)
- **排程**: APScheduler

## 🚀 快速開始

### 1. 安裝依賴
```bash
pip install -r pyproject.toml
# 或者直接安裝主要套件
pip install python-binance google-genai python-telegram-bot aiosqlite pydantic-settings structlog apscheduler python-dotenv
```

### 2. 設定環境變數
複製 `.env.example` 並更名為 `.env`，填入相關資訊：
```env
BINANCE_API_KEY=你的幣安API金鑰
BINANCE_API_SECRET=你的幣安API私鑰
GEMINI_API_KEY=你的GoogleAI金鑰
TELEGRAM_BOT_TOKEN=你的BotToken
TELEGRAM_CHAT_ID=你的ChatID
TRADING_SYMBOLS=BTCUSDC,ETHUSDC,SOLUSDC
```

### 3. 執行程式
```bash
python main.py
```

## 🤖 Telegram 指令

- `/status` - 查看所有交易對即時狀態與損益
- `/analyze` - 立即觸發 Gemini AI 深度分析與策略建議
- `/risk` - 顯示風險儀表板（保證金與清算價）
- `/pnl` - 顯示累計損益報表（含手續費與 Funding）
- `/ask <問題>` - 向 Gemini AI 諮詢任何交易相關問題
- `/sessions` - 查看網格輪次 (Grid Sessions) 歷史紀錄
- `/help` - 顯示完整指令列表

## 📊 專案架構

```text
cry3/
├── config/             # 配置管理與策略邊界設定
├── src/gridbot/
│   ├── ai/            # Gemini AI 邏輯與 Prompt
│   ├── binance/       # 幣安 FAPI 資料抓取
│   ├── core/          # 應用程式主循環與排程器
│   ├── grid/          # 損益分析與指標計算
│   ├── storage/       # SQLite 資料庫與 Repository
│   └── telegram/      # Bot 處理器與訊息格式化
└── main.py             # 入口點
```

## ⚠️ 免責聲明
本系統僅供監控與分析建議使用，不具備自動下單功能。所有交易操作應由使用者自行在幣安平台評估後執行。加密貨幣合約交易具有高風險，請謹慎投資。
