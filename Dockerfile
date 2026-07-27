FROM python:3.12-slim

WORKDIR /app

COPY backend/requirements.txt backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt

# Pre-download the embedding model at build time so it's baked into the
# image - the container never needs runtime internet access to
# Hugging Face.
RUN python -c "from fastembed import TextEmbedding; TextEmbedding(model_name='sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2')"

# The reranker is opt-in (see RERANKER_ENABLED) and adds ~1.1GB to the
# image, plus a memory spike while the build instantiates it - enough to
# matter on a small VM building in place. Only bake it in when it will
# actually be used:
#   docker compose build --build-arg INCLUDE_RERANKER=1
ARG INCLUDE_RERANKER=0
RUN if [ "$INCLUDE_RERANKER" = "1" ]; then \
      python -c "from fastembed.rerank.cross_encoder import TextCrossEncoder; TextCrossEncoder(model_name='jinaai/jina-reranker-v2-base-multilingual')"; \
    fi

COPY backend backend
COPY frontend frontend

ENV APP_DATA_DIR=/app/data
VOLUME ["/app/data"]

EXPOSE 8000

WORKDIR /app/backend
# Cloud Run injects the port to listen on; default to 8000 elsewhere.
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
