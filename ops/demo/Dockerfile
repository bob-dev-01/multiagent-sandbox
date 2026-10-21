# The demo backend: FastAPI + the real Agent Factory, for Azure Container Apps.
# Build from the repository root:  docker build -f ops/demo/Dockerfile .
FROM python:3.13-slim

ARG KUBECTL_VERSION=v1.35.0
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates git \
 && curl -fsSLo /usr/local/bin/kubectl \
      "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/amd64/kubectl" \
 && chmod +x /usr/local/bin/kubectl \
 && apt-get purge -y curl && rm -rf /var/lib/apt/lists/*

WORKDIR /app
ENV PIP_NO_CACHE_DIR=1 PYTHONUNBUFFERED=1 HF_HOME=/opt/hf

# CPU-only torch first, so sentence-transformers does not pull the CUDA build.
RUN pip install torch --index-url https://download.pytorch.org/whl/cpu
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install -e ".[embeddings,dashboard]" "azure-identity>=1.19" "azure-keyvault-secrets>=4.9" "httpx>=0.27" "anthropic>=0.40" "autogen-core>=0.7.5,<0.8" \
 && python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('all-MiniLM-L6-v2')"  && python -c "import httpx, anthropic, fastapi, uvicorn, azure.identity, azure.keyvault.secrets, autogen_core"

COPY corpus ./corpus
COPY research/calibration ./research/calibration

RUN useradd --create-home --uid 10001 demo && chown -R demo /opt/hf
USER demo
EXPOSE 8080
CMD ["uvicorn", "agentfactory.demo.app:app", "--host", "0.0.0.0", "--port", "8080", "--app-dir", "src"]
