FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# ВАЖНО: раньше здесь был `CMD ["python", "main.py"]` — main.py выполняет
# один анализ и завершается немедленно. В облачном окружении (Railway)
# это означало бы мгновенное завершение контейнера сразу после старта
# (crash-loop), а не непрерывный paper-forward сервис. Правильная точка
# входа — веб-сервер api/server.py через uvicorn, который держит процесс
# живым и обслуживает /health, /paper/tick, /observability/* и т.д.
#
# Startup safety gate (config/startup_safety.py) выполняется при импорте
# api/server.py: при LIVE_TRADING=true или нераспознанном
# TRADING_ENVIRONMENT процесс завершится с ошибкой ДО того, как uvicorn
# начнёт слушать порт — это ожидаемое и безопасное поведение.
#
# PORT задаётся Railway автоматически; 8000 — безопасный локальный fallback.
CMD ["sh", "-c", "uvicorn api.server:app --host 0.0.0.0 --port ${PORT:-8000}"]
