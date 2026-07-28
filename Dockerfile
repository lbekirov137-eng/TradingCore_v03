FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# ЕДИНЫЙ облачный entrypoint: uvicorn api.server:app.
#
# Ранее здесь запускался `python -u /app/paper_watchdog.py`. Согласовано
# на один entrypoint, потому что:
#   - api/server.py содержит startup safety gate, который отказывается
#     стартовать при LIVE_TRADING=true, TRADING_ENVIRONMENT=LIVE или
#     любом нераспознанном значении режима (fail-closed);
#   - он обслуживает /health и /ready, которые нужны Railway для
#     healthcheck и для внешнего контроля состояния;
#   - /ready активно перепроверяет конфигурацию в рантайме и отдаёт
#     HTTP 503 FAILED_SAFELY при попытке live-режима, плече > 1x или
#     риске > 0.1%.
#
# paper_watchdog.py / paper_live_loop.py НЕ удалены — они остаются
# доступны для локального и фонового использования; в облаке процесс
# держит живым именно веб-сервер.
#
# PORT задаётся Railway автоматически; 8000 — локальный fallback.
# `exec` обязателен: без него PID 1 остаётся у sh, который НЕ пересылает
# SIGTERM дочернему uvicorn. Проверено эмпирически: без exec контейнер не
# завершался по `docker stop`, Docker ждал 10 с и убивал его SIGKILL
# (ExitCode=137). Для paper-контура это означало бы обрыв процесса во
# время записи журнала/состояния при каждом рестарте или редеплое.
# С `exec` uvicorn становится PID 1, получает SIGTERM напрямую и
# завершается штатно (ExitCode=0).
CMD ["sh", "-c", "exec uvicorn api.server:app --host 0.0.0.0 --port ${PORT:-8000}"]
