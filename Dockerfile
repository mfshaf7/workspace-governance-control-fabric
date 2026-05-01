FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY pyproject.toml README.md alembic.ini ./
COPY apps ./apps
COPY packages ./packages
COPY migrations ./migrations
COPY policies ./policies
COPY schemas ./schemas
COPY scripts ./scripts
COPY examples ./examples
COPY docs ./docs
COPY AGENTS.md ./

RUN python -m pip install --no-cache-dir --upgrade pip \
  && python -m pip install --no-cache-dir .

RUN useradd --create-home --uid 10001 wgcf
USER wgcf

EXPOSE 8080

CMD ["uvicorn", "wgcf_api.app:app", "--host", "0.0.0.0", "--port", "8080"]
