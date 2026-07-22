FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p /app/data_seed \
    && if [ -d /app/data ]; then \
        cp -a /app/data/. /app/data_seed/; \
    fi

CMD ["sh", "-c", "mkdir -p /app/data && if [ -z \"$(ls -A /app/data 2>/dev/null)\" ] && [ -d /app/data_seed ]; then cp -a /app/data_seed/. /app/data/; fi && exec python -u /app/paper_watchdog.py"]