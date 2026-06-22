FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    POETRY_VERSION=1.8.3 \
    POETRY_NO_INTERACTION=1 \
    POETRY_VIRTUALENVS_CREATE=false

WORKDIR /app

# Install Poetry, then project dependencies first for better layer caching.
RUN pip install --upgrade pip "poetry==${POETRY_VERSION}"

# poetry.lock is optional (copied if present) for reproducible installs.
COPY pyproject.toml poetry.lock* ./
RUN poetry install --no-root --only main

# Copy application code.
COPY app ./app
COPY alembic ./alembic
COPY alembic.ini ./
COPY entrypoint.sh ./
RUN chmod +x entrypoint.sh

EXPOSE 8100

ENTRYPOINT ["./entrypoint.sh"]
