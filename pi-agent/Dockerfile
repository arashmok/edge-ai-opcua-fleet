FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY servo_driver.py pubsub_agent.py ua_pubsub.py ./
# Centralized topology: the agent runs as an MQTT client reporting to the Spark gateway.
# Do not use opcua_arm_server.py; the single-robot path is removed.
CMD ["python", "pubsub_agent.py"]
