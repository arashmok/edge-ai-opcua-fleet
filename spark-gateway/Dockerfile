FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY ua_pubsub.py gateway.py ./
CMD ["python", "gateway.py"]
