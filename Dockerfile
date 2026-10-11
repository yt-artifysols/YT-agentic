# One image for every Python service; the command (set in Terraform) picks the role:
#   hub      uvicorn ytcore.approvals.service.main:app
#   gateway  uvicorn ytcore.gateway.main:app
#   worker   python -m ytcore.workflow.worker run
#   ops jobs python -m ytcore.<job> ...
# Build with a digest-pinned base in CI; the image holds no secrets (they are resolved at run
# time through the SecretsProvider) and runs as a non-root user.
ARG PYTHON_IMAGE=python:3.12-slim
FROM ${PYTHON_IMAGE} AS build
WORKDIR /src
COPY pyproject.toml README.md ./
COPY ytcore ./ytcore
ARG EXTRAS=gcp,youtube,agents,hub,gateway,otel
RUN pip install --no-cache-dir --prefix=/install ".[${EXTRAS}]"

FROM ${PYTHON_IMAGE}
COPY --from=build /install /usr/local
RUN useradd --system --uid 10001 --no-create-home app
USER 10001
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
EXPOSE 8080
CMD ["python", "-c", "raise SystemExit('set a command: hub, gateway or worker')"]
