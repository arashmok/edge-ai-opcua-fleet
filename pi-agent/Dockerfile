FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY servo_driver.py opcua_arm_server.py pubsub_agent.py ./
# Fleet mode by default. Override to opcua_arm_server.py for single-robot mode.
CMD ["python", "pubsub_agent.py"]
