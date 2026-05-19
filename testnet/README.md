# Cry3 Testnet Runtime

This folder is the isolated Binance Futures testnet runtime for Cry3.

The code still runs from the parent repo, but all mutable testnet state lives here:

- `testnet/.env.testnet`
- `testnet/data/gridbot_testnet.db`
- `testnet/logs/service.log`
- `testnet/systemd/cry3-testnet.service`

## Setup On The VM

```bash
cd /home/jack_shih/cry3
cp testnet/.env.testnet.example testnet/.env.testnet
nano testnet/.env.testnet
bash testnet/setup_testnet_venv.sh
bash testnet/check_testnet_env.sh
bash testnet/run_testnet.sh
```

## Systemd

```bash
sudo cp /home/jack_shih/cry3/testnet/systemd/cry3-testnet.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now cry3-testnet.service
sudo systemctl status cry3-testnet.service
tail -f /home/jack_shih/cry3/testnet/logs/service.log
```

## Safety Rules

- `BINANCE_TESTNET` must be `true`.
- `DB_PATH` must stay under `testnet/`.
- Use separate testnet API keys.
- Start with `TRADING_MODE=signal_only`.
- Keep mainnet `.env` and testnet `.env.testnet` separate.

## Candidate Strategy Backtests

Current ETHUSDC research candidate:

```bash
bash testnet/run_candidate_v6_backtests.sh
```

The script reruns both calendar-month and 30-day rolling-window backtests for the
latest signal-journal router candidate. It is for validation only; keep testnet
execution in `signal_only` until the rolling windows and live paper signals are
reviewed.
