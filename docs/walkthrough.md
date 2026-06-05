# Cry3 Project Handover & Maintenance Guide

This document provides a comprehensive overview of the **Cry3** project state, recent updates, system architecture, and VM deployment workflow to help the incoming developer/Codex take over maintenance seamlessly.

---

## 📌 Project Overview
**Cry3** is a Binance Futures trading bot designed to:
1. Run automated strategies on the Binance Futures Testnet in simulated/monitoring mode.
2. Broadcast real-time trading signals and detailed strategy diagnostics to Telegram.
3. Provide manual execution matching and "one-run" order management.

---

## ⚙️ Core Architecture & Key Files

To understand or debug the system, review the following components:

### 1. Telegram Bot & Command Handling
* **Bot Config & Startup**: [bot.py](file:///c:/Users/jack_shih/Desktop/cry3/src/gridbot/telegram/bot.py)
  * Uses `python-telegram-bot` v20+.
  * Sets the command menu dynamically on launch using the `post_init` hook (via `application.bot.set_my_commands`).
* **Command Handlers**: [handlers.py](file:///c:/Users/jack_shih/Desktop/cry3/src/gridbot/telegram/handlers.py)
  * Contains logic for `/signal`, `/mainnet`, `/testnet`, `/pnl`, and `/ai`.
  * **Crucial detail**: Message formatting uses `parse_mode="HTML"`. Any output text containing raw `<` or `>` characters must be escaped using `html.escape` or HTML entities (`&lt;` / `&gt;`) to prevent Telegram parse mode failures.

### 2. Trading Strategy Logic (Wildcat Strategy)
* **Strategy Execution & Live Adapter**: [wildcat_live.py](file:///c:/Users/jack_shih/Desktop/cry3/src/gridbot/strategy/wildcat_live.py)
  * Implements `generate_wildcat_v2_adverse_guard_live_decision`.
  * Includes the diagnostics generator `explain_wildcat_no_signal` which evaluates and formats indicators (RSI, Stochastic, Bollinger Bands, ATR, VWAP) to explain why sub-logics (S1 & S5) did not trigger.

### 3. Testnet Auto Trader & Isolation
* **Testnet Controller**: [auto_trader.py](file:///c:/Users/jack_shih/Desktop/cry3/src/gridbot/testnet/auto_trader.py)
  * Orchestrates testnet cycles and state monitoring.
  * In simulated mode (`TESTNET_TELEGRAM_SIGNAL_ONLY=true`), it performs scanning and logging without placing actual trades.

### 4. Configuration & Environment Files
* **Local Testnet Config**: [testnet/.env.testnet](file:///c:/Users/jack_shih/Desktop/cry3/testnet/.env.testnet)
  * Main configurations for the testnet system (strategy labels, leverages, limits).
* **GCP Credentials Configuration**:
  * Configured via `gcloud` locally to manage VM authorization.

---

## 🛠️ Recent Major Modifications

1. **Detailed Sub-Logic Diagnostics**:
   * Added `explain_wildcat_no_signal` to details why `S1_BB_RSI` or `S5_Stoch` did not satisfy entry conditions (e.g. low volatility bounds, Stochastic crossings, minimum volume/body ratio thresholds).
2. **HTML Entity Parsing Fix**:
   * Replaced raw `<` and `>` inequality signs in all diagnostics outputs with `&lt;` and `&gt;` to fix the `Can't parse entities: unsupported start tag` error in Telegram.
3. **Automatic Bot Command Menu Registration**:
   * Wired `post_init` in `bot.py` to automatically publish commands to the Telegram Server API.
   * Created [set_telegram_commands.py](file:///c:/Users/jack_shih/Desktop/cry3/scripts/set_telegram_commands.py) to forcefully set command lists for both the Mainnet and Testnet bots.

---

## 🚀 VM Deployment & Operation

The bot is hosted on a Google Cloud Platform VM instance.

* **VM Details**:
  * **Instance Name**: `cry3jack`
  * **Zone**: `asia-east1-a`
  * **Absolute Repository Path**: `/home/jack_shih/cry3`
* **Service Management (Systemd)**:
  * The bot runs as a systemd service: `cry3.service`
  * **Restart Service**: `sudo systemctl restart cry3`
  * **Check Status**: `sudo systemctl status cry3`
  * **View Live Logs**: `sudo tail -f /home/jack_shih/cry3/testnet/logs/service.log`
* **Local Deploy Script**: [deploy.sh](file:///c:/Users/jack_shih/Desktop/cry3/scripts/deploy.sh)
  * Synchronizes local changes to GitHub `main` and triggers a remote checkout/pull and restart of the systemd service on the VM.

---

## 💡 Quick Tips for the Next Codex
* **HTML Parsing Crashes**: If the bot crashes with `Can't parse entities` again, it is because some string formatting output inside `handlers.py` contains unescaped `<` or `>` symbols. Always wrap variables or strings in `html.escape()` or convert brackets to `&lt;`/`&gt;` when using `parse_mode="HTML"`.
* **Testing Changes**: You can run local integration/unit tests using `pytest` inside the root workspace.
