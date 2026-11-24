# realTrade
<<<<<<< HEAD
Реальная торговля (Finam LQDT, T-Invest ROSN)
=======

Проект реальной торговли.

- `confirm_trade.py` / `confirm_trade_rest.py` — execution-контроллеры (MCP / REST) для лимитных заявок.
- `trading_schedule.py` / `trading_schedule.json` — расписание сессий/окнов торговли.
- `generate_analysis_report.py` — часовой анализ портфеля (сейчас запускается автоматически).

Доступ (файлы конфигов, chmod 600):
- `tinvest.env` — токены T-Invest (было в `~/.config/tinvest-mcp/`)
- `finam.env` — токены Финам (было в `~/.config/finam-mcp/`)
- MOEX: локальный сервис `https://127.0.0.1:9443` (токенов не требовалось)
>>>>>>> 62aabcd (realTrade: начальный проект — скрипты покупки, расписание, доступ (T-Invest, Финам))
