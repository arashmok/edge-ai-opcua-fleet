FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
# gcc/make are needed to build the lgpio C extension from its sdist; they are
# removed afterwards to keep the image small.
RUN apt-get update \
 && apt-get install -y --no-install-recommends gcc make \
 && pip install --no-cache-dir -r requirements.txt \
 && apt-get purge -y gcc make && apt-get autoremove -y \
 && rm -rf /var/lib/apt/lists/*
COPY servo_driver.py ua_pubsub.py opcua_arm_server.py pubsub_agent.py ./
# Fleet mode by default. Override to opcua_arm_server.py for single-robot mode.
CMD ["python", "pubsub_agent.py"]
