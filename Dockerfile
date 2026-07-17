FROM python:3.12-slim

# Never write .pyc files; never buffer stdout. Unbuffered matters in containers:
# a buffered crash log is a lost crash log.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Dependencies before source. Docker caches layers, and this layer only rebuilds
# when pyproject.toml changes — so editing a .py file doesn't reinstall FastAPI.
COPY pyproject.toml ./
COPY app ./app
RUN pip install --no-cache-dir -e .

COPY sql ./sql
COPY scripts ./scripts
COPY sample_events.json ./

# Don't run as root. If the process is ever compromised, this is the difference
# between an attacker owning a python process and owning the container.
RUN useradd --create-home --shell /bin/bash appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
