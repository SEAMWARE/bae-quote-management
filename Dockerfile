FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml ./
COPY quote_management ./quote_management
COPY main.py ./
RUN pip install --no-cache-dir . && useradd --system --uid 10001 app
USER app
EXPOSE 8080
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
