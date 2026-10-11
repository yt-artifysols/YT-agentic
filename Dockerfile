# One image for every Python service; the command (set in Terraform) picks the role:
#   hub      uvicorn ytcore.approvals.service.main:app
#   gateway  uvicorn ytcore.gateway.main:app
#   worker   python -m ytcore.workflow.worker run
#   ops jobs python -m ytcore.<job> ...
# Build with `--build-arg PYTHON_IMAGE=python:3.12-slim@sha256:<digest>`: the base image has no
# default on purpose, so an unpinned build fails. Dependencies install only from the hash-locked
# requirements-runtime.lock (`make lock`); the image holds no secrets (they are resolved at run
# time through the SecretsProvider) and runs as a non-root user.
ARG PYTHON_IMAGE
FROM ${PYTHON_IMAGE} AS build
WORKDIR /src
COPY requirements-runtime.lock ./
RUN pip install --no-cache-dir --require-hashes --prefix=/install -r requirements-runtime.lock
COPY pyproject.toml README.md ./
COPY ytcore ./ytcore
RUN pip install --no-cache-dir --no-deps --prefix=/install .

ARG PYTHON_IMAGE
FROM ${PYTHON_IMAGE}
COPY --from=build /install /usr/local
RUN useradd --system --uid 10001 --no-create-home app
USER 10001
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
EXPOSE 8080
CMD ["python", "-c", "raise SystemExit('set a command: hub, gateway or worker')"]
