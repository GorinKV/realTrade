# realTrade

Проект реальной торговли.

## Структура

| Файл | Описание |
|---|---|
| `confirm_trade.py` | Execution-контроллер заявок через MCP (когда gRPC Т-Инвестиций жив) |
| `confirm_trade_rest.py` | Execution-контроллер заявок через REST (активный путь — менее зависимый от gRPC) |
| `trading_schedule.json` / `trading_schedule.py` | Расписание сессий/окон торговли |
| `generate_analysis_report.py` | Часовой анализ портфеля (результат: `analysis_latest.md`) |

## Текущие исполнители

- **Finam:** счёт `950034` — LQDT@MISX (обновление через `POST /v1/accounts/950034/orders`)
- **T-Invest (ранее):** счёт `2188481036`

## Доступ

Токены в файлах типа `.env` (локальные), в `.gitignore` и не попадают в GitHub:

- `tinvest.env` — токены T-Invest
- `finam.env` — токены Финама (секрет `tapi_sk_...`, JWT `tapi_ak_...`)

MOEX: локальный сервис `https://127.0.0.1:9443` (токенов не требуется).

## Быстрые команды

```bash
# Разовая покупка (пример — подгоните под себя)
/opt/app/venv/bin/python3 confirm_trade_rest.py --uid <uid> --lots 2 --limit 329.70 --yes

# Разовый анализ
/opt/app/venv/bin/python3 generate_analysis_report.py
```

## Контакт

GitHub: [GorinKV/realTrade](https://github.com/GorinKV/realTrade)
