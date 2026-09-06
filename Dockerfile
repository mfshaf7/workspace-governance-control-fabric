FROM python:3.12-slim AS app-base

ARG WGCF_SOURCE_REVISION=unverified

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV WGCF_DELIVERY_ART_CONTRACT_ROOT=/app/contracts/delivery-art
ENV WGCF_WORKSPACE_INTAKE_CONTRACT_ROOT=/app/contracts/workspace-intake
ENV WGCF_WORKSPACE_ACTIVE_INVENTORY_CONTRACT_ROOT=/app/contracts/workspace-active-inventory

WORKDIR /app

COPY pyproject.toml README.md alembic.ini ./
COPY apps ./apps
COPY contracts ./contracts
COPY packages ./packages
COPY migrations ./migrations
COPY policies ./policies
COPY schemas ./schemas
COPY scripts ./scripts
COPY examples ./examples
COPY docs ./docs
COPY AGENTS.md ./

RUN python -m pip install --no-cache-dir --upgrade pip \
  && python -m pip install --no-cache-dir . \
  && install -d -m 0755 /opt/wgcf/build \
  && printf '%s\n' "${WGCF_SOURCE_REVISION}" > /opt/wgcf/build/source-revision \
  && chmod 0444 /opt/wgcf/build/source-revision

RUN useradd --create-home --uid 10001 wgcf

FROM app-base AS worker

RUN apt-get update \
  && apt-get install --no-install-recommends --yes ca-certificates git \
  && rm -rf /var/lib/apt/lists/* \
  && python -m pip install --no-cache-dir ".[worker]" \
  && install -d -o wgcf -g wgcf -m 0750 \
    /var/lib/wgcf/orchestration/controlled-proof \
    /var/lib/wgcf/orchestration/validation-readiness

USER wgcf

CMD ["wgcf-worker", "status", "--repo-root", "/app"]

FROM app-base AS api

RUN apt-get update \
  && apt-get install --no-install-recommends --yes git \
  && rm -rf /var/lib/apt/lists/*

USER wgcf

EXPOSE 8080
CMD ["uvicorn", "wgcf_api.app:app", "--host", "0.0.0.0", "--port", "8080"]
