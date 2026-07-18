#!/usr/bin/env python3
"""
Central gateway on the DGX Spark. Hosts ONE OPC UA server that models the whole
fleet, and bridges it to the robots over MQTT using OPC UA PubSub JSON
NetworkMessages (OPC UA Part 14, JSON Message Mapping) over the MQTT transport.

North side (OT facing):
  A single OPC UA endpoint opc.tcp://<spark>:4840 with an address space:
    Arm1/<joint>/target  (writable)   Arm1/<joint>/state  (read only)
    Arm2/...                          ArmN/...
  OT apps, MES, and dashboards browse every robot in one session.

South side (robot facing) over MQTT:
  subscribes  fleet/+/state   and copies values into the OPC UA state nodes
  publishes   fleet/<id>/cmd  whenever a target node is written

This gateway emits and consumes OPC UA PubSub JSON NetworkMessages conforming
to OPC UA Part 14, using standard topics:
  - State: fleet/<robot_id>/state
  - Commands: fleet/<robot_id>/cmd

Env (all optional):
  OPCUA_PORT   default 4840
  MQTT_HOST    default "mqtt-broker"
  MQTT_PORT    default 1883
  ROBOTS       comma list of robot ids,  default "arm1,arm2,arm3"
  ARM_JOINTS   joint names,              default "base,pitch,reach,gripper"
"""

import asyncio
import logging
import os

import paho.mqtt.client as mqtt
from asyncua import Server

from ua_pubsub import DSW_STATE, DSW_COMMAND, decode, encode

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("gateway")

PORT = int(os.getenv("OPCUA_PORT", "4840"))
MQTT_HOST = os.getenv("MQTT_HOST", "mqtt-broker")
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883"))
ROBOTS = os.getenv("ROBOTS", "arm1,arm2,arm3").split(",")
JOINTS = os.getenv("ARM_JOINTS", "base,pitch,reach,gripper").split(",")


class Gateway:
    def __init__(self, loop):
        self.loop = loop
        self.target_nodes = {}   # (robot, joint) -> writable OPC UA node
        self.state_nodes = {}    # (robot, joint) -> read-only OPC UA node
        self.last_target = {}    # (robot, joint) -> last published value
        self.client = mqtt.Client(client_id="fleet-gateway")
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message
        # Per-robot command sequence number counters
        self.cmd_seq = {robot: 0 for robot in ROBOTS}

    # ---- OPC UA address space ----
    async def build(self):
        self.server = Server()
        await self.server.init()
        self.server.set_endpoint(f"opc.tcp://0.0.0.0:{PORT}/fleet/")
        self.server.set_server_name("OPC UA Fleet Gateway")
        idx = await self.server.register_namespace("http://fleet.local")
        for robot in ROBOTS:
            robot_obj = await self.server.nodes.objects.add_object(idx, robot)
            for joint in JOINTS:
                jnode = await robot_obj.add_object(idx, joint)
                tnode = await jnode.add_variable(idx, "target", 90.0)
                snode = await jnode.add_variable(idx, "state", 90.0)
                await tnode.set_writable()
                self.target_nodes[(robot, joint)] = tnode
                self.state_nodes[(robot, joint)] = snode
                self.last_target[(robot, joint)] = 90.0

    # ---- MQTT (runs in paho's own thread) ----
    def _on_connect(self, client, userdata, flags, rc):
        log.info("gateway connected to broker %s:%s rc=%s", MQTT_HOST, MQTT_PORT, rc)
        client.subscribe("fleet/+/state")

    def _on_message(self, client, userdata, msg):
        try:
            decoded = decode(msg.payload)
        except ValueError as exc:
            log.warning("Failed to decode message on %s: %s", msg.topic, exc)
            return
        
        robot = decoded.get("publisher_id")
        # Look for datasets with writer_id == DSW_STATE (1)
        for ds in decoded.get("datasets", []):
            if ds.get("writer_id") == DSW_STATE:
                joints = ds.get("fields", {})
                # Hand the update to the asyncio loop, which owns the OPC UA nodes.
                asyncio.run_coroutine_threadsafe(self._apply_state(robot, joints), self.loop)

    async def _apply_state(self, robot, joints):
        for joint, val in joints.items():
            node = self.state_nodes.get((robot, joint))
            if node is not None:
                await node.write_value(float(val))

    # ---- North to south: publish target writes down to the robots ----
    async def publish_loop(self):
        while True:
            # Build batched command messages per robot
            robot_commands = {robot: {} for robot in ROBOTS}
            
            for (robot, joint), tnode in self.target_nodes.items():
                val = await tnode.read_value()
                if abs(val - self.last_target[(robot, joint)]) > 0.5:
                    self.last_target[(robot, joint)] = val
                    robot_commands[robot][joint] = val
            
            # Publish one NetworkMessage per robot with changes
            for robot, joints in robot_commands.items():
                if joints:
                    self.cmd_seq[robot] += 1
                    self.client.publish(
                        f"fleet/{robot}/cmd",
                        encode(
                            publisher_id="fleet-gateway",
                            dataset_writer_id=DSW_COMMAND,
                            sequence_number=self.cmd_seq[robot],
                            payload_fields=joints
                        )
                    )
            
            await asyncio.sleep(0.05)

    async def run(self):
        await self.build()
        self.client.connect(MQTT_HOST, MQTT_PORT, keepalive=10)
        self.client.loop_start()
        async with self.server:
            log.info("OPC UA fleet endpoint on port %s, robots=%s", PORT, ROBOTS)
            await self.publish_loop()


if __name__ == "__main__":
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(Gateway(loop).run())
    except KeyboardInterrupt:
        log.info("shutting down")
