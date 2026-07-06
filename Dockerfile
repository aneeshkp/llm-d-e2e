FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends curl && \
    curl -LO "https://dl.k8s.io/release/$(curl -Ls https://dl.k8s.io/release/stable.txt)/bin/linux/amd64/kubectl" && \
    install kubectl /usr/local/bin/kubectl && rm kubectl && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir uv

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen

COPY . .

ENTRYPOINT ["uv", "run", "llm-d-e2e"]
