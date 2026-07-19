#!/usr/bin/env python3
"""
Central gateway on the DGX Spark. Hosts ONE OPC UA server that models the whole
fleet, and bridges it to the robots over MQTT using OPC UA PubSub JSON
NetworkMessages (OPC UA Part 14) via the ua_pubsub codec.

This file supersedes the earlier gateway.py and gateway_new.py; delete those.

North side (OT facing), a single OPC UA endpoint opc.tcp://<spark>:4840/fleet/:
    <robot>/<joint>/target   writable    commanded angle
    <robot>/<joint>/state    read only   reported angle
    <robot>/safe_stop        writable    release this robot
    Fleet/safe_stop          writable    release the whole fleet
  OT apps browse every robot in one session.

South side (robot facing) over MQTT:
  subscribes  fleet/+/state   decodes DSW_STATE NetworkMessages into state nodes
  publishes   fleet/<id>/cmd  streams DSW_COMMAND setpoints at CMD_HZ

Why stream setpoints instead of sending only on change: the robot's safety
watchdog releases the servos if no command arrives within its timeout. Edge
triggered commands would make a stationary arm droop. Streaming the full
setpoint set at a steady rate keeps the arm held and doubles as a heartbeat, so
the watchdog trips only on a real loss of the gateway or broker.

Env (all optional):
  OPCUA_PORT   default 4840
  MQTT_HOST    default "mqtt-broker"
  MQTT_PORT    default 1883
  ROBOTS       comma list of robot ids,  default "arm1,arm2,arm3"
  ARM_JOINTS   joint names,              default "base,pitch,reach,gripper"
  CMD_HZ       setpoint stream rate,     default 10
"""

import asyncio
import logging
import os
import time

import paho.mqtt.client as mqtt
from asyncua import Server

from ua_pubsub import encode, decode, DSW_STATE, DSW_COMMAND

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("gateway")
# asyncua logs every Read/Write request at INFO, which floods the log with the
# OT client's polling. Keep its protocol chatter at WARNING so our own semantic
# lines (target changes, safe_stop, liveness) are what you actually see.
logging.getLogger("asyncua").setLevel(logging.WARNING)

PORT = int(os.getenv("OPCUA_PORT", "4840"))
MQTT_HOST = os.getenv("MQTT_HOST", "mqtt-broker")
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883"))
ROBOTS = os.getenv("ROBOTS", "arm1,arm2,arm3").split(",")
JOINTS = os.getenv("ARM_JOINTS", "base,pitch,reach,gripper").split(",")
CMD_HZ = float(os.getenv("CMD_HZ", "10"))
# A robot is "online" only while its reported state keeps arriving. If no
# DSW_STATE message is decoded within STALE_S seconds (agent unplugged, crashed,
# or link lost) the gateway marks it offline so OT clients can react.
STALE_S = float(os.getenv("STALE_S", "3.0"))


class Gateway:
    def __init__(self, loop):
        self.loop = loop
        self.target_nodes = {}       # (robot, joint) -> writable node
        self.state_nodes = {}        # (robot, joint) -> read-only node
        self.robot_stop_nodes = {}   # robot -> writable safe_stop node
        self.online_nodes = {}       # robot -> read-only online (liveness) node
        self.fleet_stop_node = None  # fleet-wide writable safe_stop node
        self.cmd_seq = {robot: 0 for robot in ROBOTS}
        # monotonic timestamp of the last decoded state per robot; 0 => never
        self.last_state = {robot: 0.0 for robot in ROBOTS}
        # last commanded target / safe_stop seen, so we log only real changes
        self.last_targets = {}
        self.last_stop = {}
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                                  client_id="fleet-gateway")
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message

    # ---- OPC UA address space ----
    async def build(self):
        self.server = Server()
        await self.server.init()
        self.server.set_endpoint(f"opc.tcp://0.0.0.0:{PORT}/fleet/")
        self.server.set_server_name("OPC UA Fleet Gateway")
        idx = await self.server.register_namespace("http://fleet.local")

        fleet_obj = await self.server.nodes.objects.add_object(idx, "Fleet")
        self.fleet_stop_node = await fleet_obj.add_variable(idx, "safe_stop", False)
        await self.fleet_stop_node.set_writable()

        for robot in ROBOTS:
            robot_obj = await self.server.nodes.objects.add_object(idx, robot)
            stop_node = await robot_obj.add_variable(idx, "safe_stop", False)
            await stop_node.set_writable()
            self.robot_stop_nodes[robot] = stop_node
            online_node = await robot_obj.add_variable(idx, "online", False)
            self.online_nodes[robot] = online_node
            for joint in JOINTS:
                jnode = await robot_obj.add_object(idx, joint)
                tnode = await jnode.add_variable(idx, "target", 90.0)
                snode = await jnode.add_variable(idx, "state", 90.0)
                await tnode.set_writable()
                self.target_nodes[(robot, joint)] = tnode
                self.state_nodes[(robot, joint)] = snode

    # ---- MQTT (runs in paho's own thread) ----
    def _on_connect(self, client, userdata, flags, reason_code, properties):
        log.info("gateway connected to broker %s:%s rc=%s", MQTT_HOST, MQTT_PORT, reason_code)
        client.subscribe("fleet/+/state", qos=0)

    def _on_message(self, client, userdata, msg):
        try:
            decoded = decode(msg.payload)
        except ValueError as exc:
            log.warning("failed to decode on %s: %s", msg.topic, exc)
            return
        robot = decoded.get("publisher_id")
        for ds in decoded.get("datasets", []):
            if ds.get("writer_id") == DSW_STATE:
                if robot in self.last_state:
                    self.last_state[robot] = time.monotonic()
                joints = ds.get("fields", {})
                asyncio.run_coroutine_threadsafe(
                    self._apply_state(robot, joints), self.loop)

    async def _apply_state(self, robot, joints):
        for joint, val in joints.items():
            node = self.state_nodes.get((robot, joint))
            if node is not None:
                await node.write_value(float(val))

    # ---- Liveness: flip each robot's online node on state freshness ----
    async def health_loop(self):
        period = min(1.0, STALE_S / 2.0)
        while True:
            now = time.monotonic()
            for robot in ROBOTS:
                fresh = (now - self.last_state[robot]) < STALE_S
                node = self.online_nodes[robot]
                if bool(await node.read_value()) != fresh:
                    await node.write_value(fresh)
                    log.info("robot %s online=%s", robot, fresh)
            await asyncio.sleep(period)

    # ---- North to south: stream setpoints + safe_stop to each robot ----
    async def publish_loop(self):
        period = 1.0 / CMD_HZ
        while True:
            fleet_stop = await self.fleet_stop_node.read_value()
            for robot in ROBOTS:
                robot_stop = await self.robot_stop_nodes[robot].read_value()
                stop = bool(fleet_stop or robot_stop)
                fields = {"safe_stop": stop}
                if self.last_stop.get(robot) != stop:
                    log.info("OPC UA safe_stop  %s  ->  %s", robot, stop)
                    self.last_stop[robot] = stop
                for joint in JOINTS:
                    val = float(await self.target_nodes[(robot, joint)].read_value())
                    fields[joint] = val
                    prev = self.last_targets.get((robot, joint))
                    if prev is None or abs(prev - val) > 1e-9:
                        if prev is not None:
                            log.info("OPC UA target  %s/%s  %.1f -> %.1f",
                                     robot, joint, prev, val)
                        self.last_targets[(robot, joint)] = val
                self.cmd_seq[robot] += 1
                payload = encode(publisher_id="fleet-gateway",
                                 dataset_writer_id=DSW_COMMAND,
                                 sequence_number=self.cmd_seq[robot],
                                 payload_fields=fields)
                # QoS 1 so a setpoint is not silently dropped. Never retained:
                # a retained command would replay a stale setpoint on reconnect.
                self.client.publish(f"fleet/{robot}/cmd", payload, qos=1, retain=False)
            await asyncio.sleep(period)

    async def run(self):
        await self.build()
        self.client.connect(MQTT_HOST, MQTT_PORT, keepalive=10)
        self.client.loop_start()
        async with self.server:
            log.info("OPC UA fleet endpoint on port %s, robots=%s", PORT, ROBOTS)
            await asyncio.gather(self.publish_loop(), self.health_loop())


if __name__ == "__main__":
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(Gateway(loop).run())
    except KeyboardInterrupt:
        log.info("shutting down")
