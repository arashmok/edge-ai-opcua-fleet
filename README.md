# edge-ai-opcua-fleet

A starter for an edge AI robotics platform where a fleet of servo arms is driven
by Raspberry Pis, aggregated by a DGX Spark edge gateway, exposed to OT systems
over OPC UA, and orchestrated by k3s.

It grows from a single arm on a bench to a centralized fleet without changing the
overall shape: the arm is always an OPC UA field device, OPC UA is always the
industrial backbone, and only the transport underneath changes as you scale.

## Two architectures in this repo

**Single robot** (simple, good for learning). Each Pi hosts its own OPC UA server
and exposes the arm directly. See `pi-agent/opcua_arm_server.py`. This does not
scale well past a few robots, because every robot exposes its own socket.

**Centralized fleet** (the target design). The DGX Spark hosts ONE OPC UA endpoint
that models the whole fleet, and the robots report to it over MQTT, which is the
transport used by OPC UA PubSub. Robots never host an inbound socket, and adding a
robot is just a new id. See `pi-agent/pubsub_agent.py` and `spark-gateway/gateway.py`.

Design sketches for both are in `docs/` as editable PowerPoint files.

## Layout

```
deploy/k3s/        k3s manifests for the containerized stack
pi-agent/          runs on each Raspberry Pi (servo driver + OPC UA or MQTT)
spark-gateway/     runs on the DGX Spark (single OPC UA endpoint + MQTT bridge)
docs/              architecture notes, getting-started guide, design sketches
```

## How the pieces map

| Component | Runs on | Role |
|---|---|---|
| `servo_driver.py` | Pi | Drives SG90 servos via pigpio (DMA PWM), mock fallback |
| `opcua_arm_server.py` | Pi | Single-robot: hosts an OPC UA server for one arm |
| `pubsub_agent.py` | Pi | Fleet: publishes state and subscribes to commands over MQTT, local safe-stop |
| `gateway.py` | DGX Spark | Fleet: one OPC UA endpoint for all robots, bridged to MQTT |
| `k3s-opcua-stack.yaml` | cluster | Deploys and heals the containers, pins them by node label |

## Quick start (no hardware needed)

Everything runs in mock mode when pigpio and the broker are absent, so you can
try the flow on a laptop first.

Single robot:

```
cd pi-agent
pip install -r requirements.txt
python opcua_arm_server.py          # browse opc.tcp://localhost:4840/arm/ with UaExpert
```

Fleet (needs an MQTT broker, for example a local Mosquitto):

```
# terminal 1: gateway (hosts the single OPC UA endpoint)
cd spark-gateway && pip install -r requirements.txt && python gateway.py

# terminal 2: one robot agent
cd pi-agent && pip install -r requirements.txt && ROBOT_ID=arm1 python pubsub_agent.py
```

Then browse `opc.tcp://localhost:4840/fleet/` and you will see Arm1..ArmN, each
with target and state nodes per joint.

## On hardware

- Servo signal wires go to Pi GPIO pins (BCM numbering, default 13,12,18,19).
- Servos get a SEPARATE 5V supply, never the Pi 5V pin. Tie all grounds together.
- Enable pigpio on each Pi: `sudo systemctl enable --now pigpiod`.
- Note: SG90 servos are open loop, so reported state is the commanded angle, not
  measured. The local safe-stop on each Pi runs independently of the network.

See `docs/getting-started.md` for the full phased build and `docs/architecture.md`
for the design rationale.

## Standards note

The fleet gateway implements the OPC UA PubSub pattern pragmatically: an asyncua
OPC UA server for the OT-facing side plus paho MQTT as the broker transport. A
fully spec-compliant OPC UA PubSub deployment would use a PubSub-capable stack
such as open62541 end to end. The topology and address space are the same either
way.
