FROM python:3.11-slim

ARG MANIFEST_REF=main
ARG MANIFEST_REPO=https://github.com/opendatahub-io/llm-d-conformance-manifests.git

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl git && \
    curl -LO "https://dl.k8s.io/release/$(curl -Ls https://dl.k8s.io/release/stable.txt)/bin/linux/amd64/kubectl" && \
    install kubectl /usr/local/bin/kubectl && rm kubectl && \
    curl -LsSf https://astral.sh/uv/install.sh | env UV_UNMANAGED_INSTALL=/usr/local/bin sh && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen

COPY . .
RUN rm -rf .venv && uv sync --frozen

RUN git clone --depth 1 --branch "${MANIFEST_REF}" "${MANIFEST_REPO}" /tmp/manifests && \
    mkdir -p deploy/manifests && \
    cp /tmp/manifests/*.yaml deploy/manifests/ && \
    printf 'branch: %s\nrepo: %s\n' "${MANIFEST_REF}" "${MANIFEST_REPO}" > deploy/manifests/.manifest-ref && \
    rm -rf /tmp/manifests

# Set up directories and permissions so non-root users can invoke the CLI
# with default runtime locations.
RUN mkdir -p reports .pytest_cache && \
    chmod -R a+rX /app && \
    chmod -R a+rwX deploy/manifests reports .pytest_cache

# UV_NO_SYNC prevents updates to the root-owned virtual environment, avoiding
# permission errors when non-root users invoke the image. This is acceptable
# because all dependencies are installed at image build time.
# Set the runtime cache after build steps so it is created by the runtime UID.
ENV UV_NO_SYNC=1 \
    UV_CACHE_DIR=/tmp/uv-cache

ENTRYPOINT ["uv", "run", "llm-d-e2e"]
