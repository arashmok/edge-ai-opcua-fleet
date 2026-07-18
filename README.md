# edge-ai-opcua-fleet

A starter for an edge AI robotics platform where a fleet of servo arms is driven
by Raspberry Pis, aggregated by a DGX Spark edge gateway, exposed to OT systems
over OPC UA, and orchestrated by k3s.

It scales from a single arm on a bench to a centralized fleet without changing the
overall shape: the arm is always an OPC UA field device, OPC UA is always the
industrial backbone, and the transport underneath remains MQTT for all cases.

## Centralized architecture

The DGX Spark hosts ONE OPC UA endpoint (the fleet model), and all robots are
lightweight MQTT clients that publish state and subscribe to commands. Adding a
robot is just a new id in the ROBOTS list; no topology change is required.
Whether you have one robot or many, the setup is identical: run the gateway on
the Spark and one pubsub_agent per robot on its Pi.

## Layout

```
deploy/k3s/        k3s manifests for the containerized stack
pi-agent/          runs on each Raspberry Pi (servo driver + MQTT client)
spark-gateway/     runs on the DGX Spark (single OPC UA endpoint + MQTT bridge)
docs/              architecture notes, getting-started guide, design sketches
```

## How the pieces map

| Component | Runs on | Role |
|---|---|---|
| `servo_driver.py` | Pi | Drives SG90 servos via pigpio (DMA PWM), mock fallback |
| `pubsub_agent.py` | Pi | Publishes state and subscribes to commands over MQTT, local safe-stop |
| `gateway.py` | DGX Spark | One OPC UA endpoint for the fleet, bridged to MQTT |
| `k3s-opcua-stack.yaml` | cluster | Deploys and heals the containers, pins them by node label |

## Quick start (no hardware needed)

Everything runs in mock mode when pigpio and the broker are absent, so you can
try the flow on a laptop first.

```
# terminal 1: gateway (hosts the single OPC UA endpoint)
cd spark-gateway && pip install -r requirements.txt && python gateway.py

# terminal 2: one robot agent (set ROBOT_ID to arm1 for a single robot)
cd pi-agent && pip install -r requirements.txt && ROBOT_ID=arm1 python pubsub_agent.py
```

You need an MQTT broker (e.g. local Mosquitto). The same flow scales to many
robots: add more `ROBOT_ID` instances and set `ROBOTS` on the gateway.

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
