FROM python:3.11-slim

WORKDIR /srv

# Runtime dependencies only — test/eval tooling lives in requirements-dev.txt
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Never run as root; port 8080 needs no privileges.
RUN useradd --create-home --shell /usr/sbin/nologin app && chown -R app:app /srv
USER app

EXPOSE 8080
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
