FROM python:3.13-slim
WORKDIR /app
COPY connector.py ./
RUN pip install --no-cache-dir "aiohttp>=3.11,<4"
ENV PYTHONUNBUFFERED=1
CMD ["python", "connector.py"]
