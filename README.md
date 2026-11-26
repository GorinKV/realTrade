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
| `autotrade.py` | Правило автоторговли: до 50 сделок в день, нагрузка <95% → сделки на 10% депозита |
| `trailing.py` | Трейлинг-стоп: подтягивает SL за лучшей ценой с отступом в пунктах (0.3 = 30 пп) и шагом сдвига, вниз не двигает |
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

## Правило автоторговли

После каждого часового анализа (`generate_analysis_report.py`):

1. считаются **исполненные сегодня** сделки (по MSK) и **нагрузка на депозит**
   `max(позиции / equity, маржа / equity)`;
2. если сделок **меньше 50** и нагрузка **меньше 95%** — выбирается самый
   надёжный и прибыльный кандидат: score скринера + подтверждение потоком
   T-Invest − штраф за спред; поток «против» и широкий спред исключаются;
3. покупается **10% депозита** (целые лоты, не больше кэша и лимита заявки),
   лимитом по лучшему аску после обязательной pretrade-проверки;
4. после исполнения автоматически выставляются SL/TP из плана (стоп 1.2 ATR,
   цель 2.5 ATR);
5. не более **1 сделки на тикер в день** и не более **50 сделок в день**;
   журнал — `trades_journal.jsonl` (в git не попадает).

Отключить: `--no-autotrade` у отчёта или `"autotrade": {"enabled": false}`
в `trade_config.json`.

## Трейлинг-стоп

`trailing.py` держит стоп на заданном отступе от максимума цены:

```bash
python trailing.py BRZ6 --qty 1 --trail 0.3 --step 0.01 --min-move 0.1 --once
python trailing.py BRZ6 --qty 1 --trail 0.3 --min-move 0.1 --interval 15
```

- отступ `--trail` — в пунктах цены: 0.3 при шаге 0.01 = 30 пп (цена 90 → стоп 89.70);
- `--min-move` — двигать стоп только после прохода N пунктов от текущего уровня:
  0.1 = 10 пп, стоп идёт ступенями, а не на каждом шаге цены;
- `--step` — шаг цены инструмента (Brent 0.01, золото 0.1, акции 0.01);
- стоп двигается только вверх; перед постановкой нового старый снимается,
  чтобы два SL не продали позицию дважды;
- состояние (`best`, текущий стоп, id заявки) — в `trailing_state.json`;
- при закрытой позиции трейлинг сам останавливается.

Для BRZ6 запускается автоматизацией раз в минуту (`BRZ6 трейлинг 0.3 (шаг 10 пп)`).

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
