FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py .
EXPOSE 8080
# OPCUA_URL points at the fleet gateway (override for your host).
ENV OPCUA_URL="opc.tcp://192.168.50.230:4840/fleet/"
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080"]
