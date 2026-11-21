FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Europe/Istanbul

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot ./bot
COPY universes ./universes
COPY settings.yaml ./settings.yaml

# Root olmayan kullanıcı; bot verisi /app/bots/<ad>/ altına bağlanan klasörde durur.
RUN useradd --create-home --uid 1000 botuser && mkdir -p /app/bots && chown -R botuser /app
USER botuser

# Kalp atışı dosyası 30 dakikadan eskiyse konteyner "unhealthy" görünür.
HEALTHCHECK --interval=5m --timeout=10s --start-period=3m CMD ["python", "-m", "bot.healthcheck"]

CMD ["python", "-m", "bot", "run"]
