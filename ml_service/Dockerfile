FROM python:3.12-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 OMP_NUM_THREADS=2 OPENBLAS_NUM_THREADS=2
COPY requirements.txt ./
COPY ml_assets/catboost/ml_module/requirements.txt /tmp/ml-requirements.txt
RUN pip install --no-cache-dir -r requirements.txt -r /tmp/ml-requirements.txt
COPY ml_assets/catboost/ml_module ml_assets/catboost/ml_module
COPY ml_assets/catboost_classificator/risk_module ml_assets/catboost_classificator/risk_module
COPY ml_service ml_service
COPY shared shared
EXPOSE 8001
CMD ["uvicorn", "ml_service.app:app", "--host", "0.0.0.0", "--port", "8001", "--workers", "1"]
