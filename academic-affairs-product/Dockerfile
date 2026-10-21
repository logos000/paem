# Product image: React UI build stage + API.
FROM node:20-alpine AS web
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ ./
RUN npm run build

FROM python:3.12-slim
RUN pip install --no-cache-dir \
    "fastapi>=0.115,<1" "uvicorn>=0.34,<1" "pydantic>=2.8,<3" "httpx>=0.28,<1" python-dotenv

WORKDIR /app
COPY product/ product/
COPY --from=web /web/dist/ web/dist/

ENV PYTHONUNBUFFERED=1 PYTHONPATH=/app PYTHONIOENCODING=utf-8 \
    BUILDER_URL=http://corpus-runtime:8772 PRODUCT_ARM=light ASK_LOG=/data/logs/ask.jsonl
EXPOSE 8770
CMD ["python", "-m", "uvicorn", "product.app:app", "--host", "0.0.0.0", "--port", "8770"]
