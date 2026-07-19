#!/usr/bin/env python3
"""
Fleet mode: the Pi does NOT host an OPC UA server. It talks to the central
gateway on the DGX Spark over MQTT, exchanging OPC UA PubSub JSON
NetworkMessages (OPC UA Part 14) via the ua_pubsub codec. Each robot:

  - publishes joint state to      fleet/<robot_id>/state   (DSW_STATE, retained)
  - subscribes to commands on     fleet/<robot_id>/cmd     (DSW_COMMAND)
  - publishes an LWT status on    fleet/<robot_id>/status  (online/offline)
  - runs a LOCAL safe-stop that releases the servos on ANY of:
      * an explicit safe_stop command,
      * loss of the broker connection (on_disconnect),
      * no command within WATCHDOG_S (the gateway streams setpoints, so this
        only trips on a real loss, not on a stationary arm).

Robots never host an inbound socket, so the fleet scales by picking a new
ROBOT_ID.

Env (all optional):
  ROBOT_ID       default "arm1"
  MQTT_HOST      default "mqtt-broker"
  MQTT_PORT      default 1883
  ARM_CHANNELS   GPIO BCM pins,  default "17,27,22,23" (Pi 4 header pins 11,13,15,16)
  ARM_JOINTS     joint names,    default "base,pitch,reach,gripper"
  ANGLE_MIN / ANGLE_MAX   default 0 / 180
  WATCHDOG_S     stale-command timeout, default 2.0
  STATE_HZ       state publish rate,    default 5
  IDLE_RELEASE_S seconds a target must be stable before the servo pulse is cut
                 so it stops buzzing at rest; 0 = always hold. default 0.5
  HOLD_JOINTS    joints that must always hold (never idle-release) because they
                 bear load and would sag, e.g. "pitch,reach". default none
"""

import json
import logging
import os
import threading
import time

import paho.mqtt.client as mqtt

from servo_driver import ServoDriver
from ua_pubsub import encode, decode, DSW_STATE, DSW_COMMAND

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("pubsub-agent")

ROBOT_ID = os.getenv("ROBOT_ID", "arm1")
MQTT_HOST = os.getenv("MQTT_HOST", "mqtt-broker")
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883"))
ANGLE_MIN = float(os.getenv("ANGLE_MIN", "0"))
ANGLE_MAX = float(os.getenv("ANGLE_MAX", "180"))
WATCHDOG_S = float(os.getenv("WATCHDOG_S", "2.0"))
STATE_HZ = float(os.getenv("STATE_HZ", "5"))
# Software PWM (lgpio) jitters a few us, so a held servo keeps micro-correcting
# and buzzes. Once a target has been stable for IDLE_RELEASE_S, cut the pulse so
# the servo de-energizes and goes silent. Set to 0 to always hold (with buzz).
IDLE_RELEASE_S = float(os.getenv("IDLE_RELEASE_S", "0.5"))
# Joints that must HOLD position (never idle-release) because they bear load and
# would sag if de-energized (e.g. a shoulder/elbow). They keep a steady pulse
# (may buzz slightly); the rest still idle-release for quiet. Comma list.
HOLD_JOINTS = {j for j in os.getenv("HOLD_JOINTS", "").split(",") if j}
# BCM GPIO pins, one per joint. Default is a conflict-free set on the Pi 4
# 40-pin header (BCM 17,27,22,23 = physical pins 11,13,15,16); these avoid the
# I2C/SPI/UART/I2S buses. lgpio drives software PWM on any GPIO via /dev/gpiochip.
CHANNELS = [int(c) for c in os.getenv("ARM_CHANNELS", "17,27,22,23").split(",")]
JOINTS = os.getenv("ARM_JOINTS", "base,pitch,reach,gripper").split(",")

CMD_TOPIC = f"fleet/{ROBOT_ID}/cmd"
STATE_TOPIC = f"fleet/{ROBOT_ID}/state"
STATUS_TOPIC = f"fleet/{ROBOT_ID}/status"


class Agent:
    def __init__(self):
        self.driver = ServoDriver(CHANNELS, ANGLE_MIN, ANGLE_MAX)
        self.channel_of = dict(zip(JOINTS, CHANNELS))
        center = (ANGLE_MIN + ANGLE_MAX) / 2.0
        self.state = {j: center for j in JOINTS}
        self.lock = threading.Lock()
        self.last_cmd_time = time.monotonic()
        self.safe_stop = False
        self.connected = False
        self.state_seq = 0
        for j in JOINTS:
            self.driver.set_angle(self.channel_of[j], center)

        # paho-mqtt 2.x callback API
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                                  client_id=f"agent-{ROBOT_ID}")
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message
        self.client.on_disconnect = self._on_disconnect
        # Last Will: if the robot drops off, the broker publishes "offline".
        self.client.will_set(STATUS_TOPIC, json.dumps({"status": "offline"}),
                             qos=1, retain=True)

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        log.info("connected to broker %s:%s rc=%s", MQTT_HOST, MQTT_PORT, reason_code)
        with self.lock:
            self.connected = True
            # Fresh connection: treat as a command boundary so we do not
            # instantly trip the watchdog before the first setpoint arrives.
            self.last_cmd_time = time.monotonic()
        client.subscribe(CMD_TOPIC, qos=1)
        client.publish(STATUS_TOPIC, json.dumps({"status": "online"}), qos=1, retain=True)

    def _on_disconnect(self, client, userdata, disconnect_flags, reason_code, properties):
        log.warning("disconnected from broker rc=%s, releasing servos", reason_code)
        with self.lock:
            self.connected = False

    def _on_message(self, client, userdata, msg):
        try:
            decoded = decode(msg.payload)
        except ValueError as exc:
            log.warning("bad command on %s: %s", msg.topic, exc)
            return
        for ds in decoded.get("datasets", []):
            if ds.get("writer_id") != DSW_COMMAND:
                continue
            fields = ds.get("fields", {})
            with self.lock:
                if "safe_stop" in fields:
                    self.safe_stop = bool(fields["safe_stop"])
                for j in JOINTS:
                    if j in fields:
                        self.state[j] = float(fields[j])
                self.last_cmd_time = time.monotonic()

    def control_loop(self):
        """Drive servos and enforce the local safe-stop. Runs even if the
        broker or gateway is unreachable.

        Each joint is handled INDEPENDENTLY so moving one does not disturb the
        others. A joint whose target has been stable for IDLE_RELEASE_S is
        released (pulse cut) so it goes silent, and re-driven only when ITS OWN
        target changes. Joints in HOLD_JOINTS are never released (they hold
        against load, accepting some software-PWM buzz).
        """
        last = {j: None for j in JOINTS}      # last driven angle per joint
        settled = {j: 0.0 for j in JOINTS}    # when this joint reached target
        while True:
            now = time.monotonic()
            with self.lock:
                stopped = self.safe_stop
                connected = self.connected
                stale = (now - self.last_cmd_time) > WATCHDOG_S
                targets = dict(self.state)
            if stopped or stale or not connected:
                self.driver.release_all()
                for j in JOINTS:
                    last[j] = None
                time.sleep(0.05)
                continue
            for j in JOINTS:
                ch = self.channel_of[j]
                tgt = targets[j]
                if tgt != last[j]:
                    # This joint's target moved: drive it, restart its timer.
                    last[j] = tgt
                    settled[j] = now
                    self.driver.set_angle(ch, tgt)
                elif j in HOLD_JOINTS:
                    self.driver.set_angle(ch, tgt)          # always hold
                elif IDLE_RELEASE_S > 0 and (now - settled[j]) > IDLE_RELEASE_S:
                    self.driver.release(ch)                 # settled: go quiet
                else:
                    self.driver.set_angle(ch, tgt)          # hold during settle
            time.sleep(0.05)

    def publish_loop(self):
        period = 1.0 / STATE_HZ
        while True:
            with self.lock:
                joints = dict(self.state)
                self.state_seq += 1
                seq = self.state_seq
            # State is a proper OPC UA PubSub NetworkMessage (DSW_STATE).
            payload = encode(publisher_id=ROBOT_ID, dataset_writer_id=DSW_STATE,
                             sequence_number=seq, payload_fields=joints)
            self.client.publish(STATE_TOPIC, payload, qos=0, retain=True)
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
        finally:
            self.driver.release_all()
            self.client.publish(STATUS_TOPIC, json.dumps({"status": "offline"}),
                                qos=1, retain=True)
            self.client.loop_stop()


if __name__ == "__main__":
    Agent().run()
