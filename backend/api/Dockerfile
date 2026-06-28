FROM python:3.12-slim

WORKDIR /workspace/backend/api

COPY backend/api/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/api/*.py ./
COPY backend/api/collectors/ ./collectors/
COPY backend/api/common/ ./common/
COPY backend/api/datahub/ ./datahub/
COPY backend/api/endpoints/ ./endpoints/
COPY backend/api/services/ ./services/
COPY backend/api/strategy/ ./strategy/
COPY backend/api/backtest/ ./backtest/
COPY config/ /workspace/config/
COPY src/ /workspace/src/

ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/workspace:/workspace/backend/api
EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]
