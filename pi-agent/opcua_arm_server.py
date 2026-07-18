#!/usr/bin/env python3
"""
Single-robot mode: the Pi hosts its own OPC UA server and exposes the arm as a
field device. Good for Phase 0 (mock, no hardware) through Phase 2 (one real
arm). For a fleet, use pubsub_agent.py instead, which reports to the central
gateway over MQTT rather than hosting a socket per robot.

Exposes per joint:
  <joint>.target   writable   degrees, commanded
  <joint>.state    read only  degrees, last value sent (open loop, no feedback)
  Arm.safe_stop    writable   bool, release all servos when true

Env (all optional):
  OPCUA_BIND_PORT   default 4840
  ARM_CHANNELS      GPIO BCM pins,          default "17,27,22,23" (Pi 4 pins 11,13,15,16)
  ARM_JOINTS        joint names,            default "base,pitch,reach,gripper"
  ANGLE_MIN         default 0
  ANGLE_MAX         default 180
  WATCHDOG_S        stale-command timeout,  default 2.0
"""

import asyncio
import logging
import os
import time

from asyncua import Server

from servo_driver import ServoDriver

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("opcua-arm")

ANGLE_MIN = float(os.getenv("ANGLE_MIN", "0"))
ANGLE_MAX = float(os.getenv("ANGLE_MAX", "180"))
WATCHDOG_S = float(os.getenv("WATCHDOG_S", "2.0"))
PORT = int(os.getenv("OPCUA_BIND_PORT", "4840"))
# BCM GPIO pins, one per joint. Default BCM 17,27,22,23 = Pi 4 physical header
# pins 11,13,15,16; a conflict-free set clear of the I2C/SPI/UART/I2S buses.
CHANNELS = [int(c) for c in os.getenv("ARM_CHANNELS", "17,27,22,23").split(",")]
JOINTS = os.getenv("ARM_JOINTS", "base,pitch,reach,gripper").split(",")


class ArmServer:
    def __init__(self):
        self.driver = ServoDriver(CHANNELS, ANGLE_MIN, ANGLE_MAX)
        self.targets, self.states = {}, {}
        self.channel_of = dict(zip(JOINTS, CHANNELS))
        self.last_cmd_time = time.monotonic()
        self.safe_stop_node = None

    async def build(self):
        self.server = Server()
        await self.server.init()
        self.server.set_endpoint(f"opc.tcp://0.0.0.0:{PORT}/arm/")
        self.server.set_server_name("Pi Servo Arm")
        idx = await self.server.register_namespace("http://arm.local")

        arm = await self.server.nodes.objects.add_object(idx, "Arm")
        self.safe_stop_node = await arm.add_variable(idx, "safe_stop", False)
        await self.safe_stop_node.set_writable()

        center = (ANGLE_MIN + ANGLE_MAX) / 2.0
        for name in JOINTS:
            joint = await arm.add_object(idx, name)
            tnode = await joint.add_variable(idx, "target", center)
            snode = await joint.add_variable(idx, "state", center)
            await tnode.set_writable()
            self.targets[name], self.states[name] = tnode, snode
            actual = self.driver.set_angle(self.channel_of[name], center)
            await snode.write_value(actual)

    async def control_loop(self):
        while True:
            stopped = await self.safe_stop_node.read_value()
            now = time.monotonic()
            if stopped or (now - self.last_cmd_time) > WATCHDOG_S:
                self.driver.release_all()  # local safe-stop, survives network loss
                await asyncio.sleep(0.1)
                continue
            for name in JOINTS:
                target = await self.targets[name].read_value()
                current = await self.states[name].read_value()
                if abs(target - current) > 0.5:
                    actual = self.driver.set_angle(self.channel_of[name], target)
                    await self.states[name].write_value(actual)
                    self.last_cmd_time = now
            await asyncio.sleep(0.05)

    async def run(self):
        await self.build()
        async with self.server:
            log.info("OPC UA arm server on port %s, joints=%s", PORT, JOINTS)
            await self.control_loop()


if __name__ == "__main__":
    try:
        asyncio.run(ArmServer().run())
    except KeyboardInterrupt:
        log.info("shutting down")
