FROM python:3.12-slim

WORKDIR /app

# Зависимости отдельным слоем — лучше кэшируется.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY *.py ./

# Реестр, сессии MAX, маршрутизация и бэкапы живут здесь (примонтируй томом).
VOLUME ["/app/data"]

CMD ["python", "-u", "bridge.py"]
