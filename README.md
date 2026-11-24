# realTrade

Реальная торговля: **данные — T-Инвест и MOEX ISS, исполнение — Финам**.

Т-Банк для сделок не используем: дорого (0.3%/сторона) и нестабильный gRPC
(`UNAVAILABLE ... tcp handshaker shutdown`). T-Инвест оставлен как источник
данных: стакан с плотностями и лента сделок. Заявки идут только в Финам,
перед каждой — обязательная предторговая проверка.

## Структура

| Файл | Назначение |
|---|---|
| `finam_client.py` | Клиент Finam Trade API (REST, stdlib): счёт, позиции, заявки, SL/TP, котировки, стакан, свечи. CLI с `--yes`/`--force` |
| `pretrade.py` | Предторговая проверка Финама: инструмент, лот, свежесть котировки, спред, глубина, отклонение цены, кэш/позиция, лимит стоимости, дубли, сессия |
| `screener.py` | Скринер «in play» по всему TQBR (MOEX ISS): RVOL, расширение диапазона, ATR, гэп, тренд + план входа/стопа/цели |
| `marketdata.py` | Поток T-Инвест: стакан (`imbalance`), лента (`delta`, `vwap`, крупные принты) — только анализ |
| `generate_analysis_report.py` | Часовой отчёт: портфель Финама, защита SL/TP, кандидаты, поток; Markdown + Telegram |
| `trading_schedule.py` / `.json` | Расписание сессий/окон торговли |
| `trade_config.json` | Лимиты предторговой проверки |
| `tests/` | Тесты: скринер, pretrade, клиент Финама, устойчивость MCP |

## Разделение ролей

```
MOEX ISS  ──►  screener (весь рынок, дневная история)
T-Invest  ──►  marketdata (стакан/лента по финалистам)   ──►  анализ
Finam     ──►  pretrade (лимиты) ──► заявка / SL-TP      ──►  исполнение
```

## Доступ

Токены — в локальных `.env` (chmod 600), в репозиторий не попадают:

- `finam.env` — `FINAM_SECRET_TOKEN` (обмен на JWT через `POST /v1/sessions`)
- `tinvest.env` — токены T-Invest (только данные)
- MOEX ISS — публичный, без токена

## Быстрые команды

```bash
# Портфель / котировка / стакан
python finam_client.py account
python finam_client.py quote SBER
python finam_client.py book SBER

# Предторговая проверка (без отправки)
python pretrade.py SBER --side BUY --qty 10 --limit 288.50

# Заявка: сначала проверка, затем карточка, затем --yes
python finam_client.py buy  SBER --qty 10 --limit 288.50 --yes
python finam_client.py sell SBER --qty 10 --market --yes

# Защита позиции
python finam_client.py sltp SBER --qty 10 --sl 280 --tp 300 --yes

# Скринер и отчёт
python screener.py --top 8
python generate_analysis_report.py            # + Telegram
python generate_analysis_report.py --no-telegram
```

## Лимиты (trade_config.json)

- спред ≤ 0.4%, отклонение лимитной цены ≤ 1% от последней;
- стоимость заявки ≤ 50 000 ₽ и ≤ 25% equity;
- риск на сделку 0.5% equity (размер позиции в скринере);
- комиссия 0.3%/сторона — цель должна перекрывать 0.6% на круг;
- дубли: активная заявка по тому же символу в ту же сторону блокирует новую.

## Проверено вживую (24.11.2026, счёт 950034)

- `POST /orders` LIMIT → `ORDER_STATUS_NEW` → `DELETE` → `ORDER_STATUS_CANCELED`;
- `POST /sltp-orders` → `ORDER_STATUS_WATCHING` → `DELETE` → `ORDER_STATUS_DISABLED`;
- `GET /instruments/{symbol}/quotes/latest|orderbook|trades/latest`, `bars` (TIME_FRAME_D);
- `GET /accounts/{id}` — equity, кэш, позиции; `GET /accounts/{id}/orders` — история.

GitHub: [GorinKV/realTrade](https://github.com/GorinKV/realTrade)
