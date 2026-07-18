#!/usr/bin/env python3
"""
Fleet mode: the Pi does NOT host an OPC UA server. Instead it talks to the
central gateway on the DGX Spark over MQTT, which is the transport used by
OPC UA PubSub. Each robot:

  - publishes its joint state to   fleet/<robot_id>/state   (retained, JSON)
  - subscribes to commands on      fleet/<robot_id>/cmd     (JSON)
  - runs a LOCAL safe-stop that releases the servos if commands go stale or a
    safe_stop flag arrives, independent of the network and the gateway.

This keeps robots socket-free (they never listen for inbound connections) and
lets the fleet scale: a new robot just picks a new robot_id.

State/command messages are OPC UA PubSub JSON NetworkMessages (OPC UA Part 14).

Env (all optional):
  ROBOT_ID       default "arm1"
  MQTT_HOST      default "mqtt-broker"
  MQTT_PORT      default 1883
  ARM_CHANNELS   GPIO BCM pins,  default "13,12,18,19"
  ARM_JOINTS     joint names,    default "base,pitch,reach,gripper"
  ANGLE_MIN / ANGLE_MAX   default 0 / 180
  WATCHDOG_S     stale-command timeout, default 2.0
  STATE_HZ       state publish rate,    default 5
"""

import json
import logging
import os
import threading
import time

import paho.mqtt.client as mqtt

from servo_driver import ServoDriver

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("pubsub-agent")

ROBOT_ID = os.getenv("ROBOT_ID", "arm1")
MQTT_HOST = os.getenv("MQTT_HOST", "mqtt-broker")
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883"))
ANGLE_MIN = float(os.getenv("ANGLE_MIN", "0"))
ANGLE_MAX = float(os.getenv("ANGLE_MAX", "180"))
WATCHDOG_S = float(os.getenv("WATCHDOG_S", "2.0"))
STATE_HZ = float(os.getenv("STATE_HZ", "5"))
CHANNELS = [int(c) for c in os.getenv("ARM_CHANNELS", "13,12,18,19").split(",")]
JOINTS = os.getenv("ARM_JOINTS", "base,pitch,reach,gripper").split(",")

CMD_TOPIC = f"fleet/{ROBOT_ID}/cmd"
STATE_TOPIC = f"fleet/{ROBOT_ID}/state"


class Agent:
    def __init__(self):
        self.driver = ServoDriver(CHANNELS, ANGLE_MIN, ANGLE_MAX)
        self.channel_of = dict(zip(JOINTS, CHANNELS))
        center = (ANGLE_MIN + ANGLE_MAX) / 2.0
        self.state = {j: center for j in JOINTS}
        self.lock = threading.Lock()
        self.last_cmd_time = time.monotonic()
        self.safe_stop = False
        for j in JOINTS:
            self.driver.set_angle(self.channel_of[j], center)

        self.client = mqtt.Client(client_id=f"agent-{ROBOT_ID}")
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message

    def _on_connect(self, client, userdata, flags, rc):
        log.info("connected to broker %s:%s rc=%s", MQTT_HOST, MQTT_PORT, rc)
        client.subscribe(CMD_TOPIC)

    def _on_message(self, client, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode())
        except ValueError:
            log.warning("bad command payload on %s", msg.topic)
            return
        with self.lock:
            self.safe_stop = bool(payload.get("safe_stop", False))
            for j in JOINTS:
                if j in payload:
                    self.state[j] = float(payload[j])
            self.last_cmd_time = time.monotonic()

    def control_loop(self):
        """Drive servos and enforce the local safe-stop. Runs even if the
        broker or gateway is unreachable."""
        while True:
            with self.lock:
                stopped = self.safe_stop
                stale = (time.monotonic() - self.last_cmd_time) > WATCHDOG_S
                targets = dict(self.state)
            if stopped or stale:
                self.driver.release_all()
            else:
                for j in JOINTS:
                    self.driver.set_angle(self.channel_of[j], targets[j])
            time.sleep(0.05)

    def publish_loop(self):
        period = 1.0 / STATE_HZ
        while True:
            with self.lock:
                joints = dict(self.state)
            payload = json.dumps({"robot_id": ROBOT_ID, "joints": joints, "ts": time.time()})
            self.client.publish(STATE_TOPIC, payload, retain=True)
            time.sleep(period)

    def run(self):
        self.client.connect(MQTT_HOST, MQTT_PORT, keepalive=10)
        self.client.loop_start()
        threading.Thread(target=self.control_loop, daemon=True).start()
        log.info("agent %s up. cmd=%s state=%s", ROBOT_ID, CMD_TOPIC, STATE_TOPIC)
        try:
            self.publish_loop()
        except KeyboardInterrupt:
            log.info("shutting down")
            self.driver.release_all()


if __name__ == "__main__":
    Agent().run()
TOPIC, STATE_TOPIC)
        try:
            self.publish_loop()
        except KeyboardInterrupt:
            log.info("shutting down")
            self.driver.release_all()


if __name__ == "__main__":
    Agent().run()
errupt:
            log.info("shutting down")
            self.driver.release_all()


if __name__ == "__main__":
    Agent().run()
