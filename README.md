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

```text
+------------------+               +------------------------------------+
|   OT / Cloud     |               |       DGX Spark (role=edge)        |
| (MES, Dashboards |    OPC UA     |                                    |
|  UaExpert, etc.) +-------------->+  +------------------------------+  |
|                  |   (client)    |  | spark-gateway (gateway.py)   |  |
+------------------+               |  | - Single OPC UA endpoint     |  |
                                   |  |   opc.tcp://<spark>:4840/    |  |
                                   |  | - OPC UA<->MQTT bridge        |  |
                                   |  +------------------------------+  |
                                   +------------------------------------+
                                                  |
                                                  | MQTT
                                                  |
                                  +---------------+-----------------+
                                  |               |                 |
                          +-------v----+  +-------v----+  +-------v----+
                          |Pi arm1     |  |Pi arm2     |  |Pi armN     |
                          |role=arm    |  |role=arm    |  |role=arm    |
                          +------------+  +------------+  +------------+
                          |pubsub_agent|  |pubsub_agent|  |pubsub_agent|
                          |- state:    |  |- state:    |  |- state:    |
                          |  fleet/.../|  |  fleet/.../|  |  fleet/.../|
                          |  state     |  |  state     |  |  state     |
                          |- cmd:      |  |- cmd:      |  |- cmd:      |
                          |  fleet/.../|  |  fleet/.../|  |  fleet/.../|
                          |  cmd       |  |  cmd       |  |  cmd       |
                          |- SG90 GPIO |  |- SG90 GPIO |  |- SG90 GPIO |
                          |- local     |  |- local     |  |- local     |
                          |  safe-stop |  |  safe-stop |  |  safe-stop |
                          +------------+  +------------+  +------------+
```

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

## Running the full stack (k3s)

Deploy the centralized OPC UA fleet to real hardware using the k3s manifest.

### Prerequisites

- One DGX Spark (control plane) and one or more Raspberry Pis (agents).
- Install k3s:

  ```sh
  # On Spark (server):
  curl -sfL https://get.k3s.io | sh -

  # On each Pi (agent, replace <spark-ip>):
  curl -sfL https://get.k3s.io | K3S_URL=https://<spark-ip>:6443 K3S_TOKEN=<token> sh -
  ```

- Label the nodes so workloads land on the correct hardware:

  ```sh
  kubectl label node <pi-hostname>    role=arm
  kubectl label node <spark-hostname> role=edge
  ```

  See `deploy/k3s/k3s-opcua-stack.yaml` header for more details.

### Build and push container images

- Build the gateway image on the Spark:

  ```sh
  cd spark-gateway && docker build -t your-registry/spark-gateway:latest .
  docker push your-registry/spark-gateway:latest
  ```

- Build the Pi agent image on a Pi (or cross-build for arm64):

  ```sh
  cd pi-agent && docker build -t your-registry/pi-agent:latest .
  docker push your-registry/pi-agent:latest
  ```

- Update `deploy/k3s/k3s-opcua-stack.yaml` if your registry or image tags differ.

### Configure the fleet

- Edit the `opcua-config` ConfigMap in `deploy/k3s/k3s-opcua-stack.yaml`:

  - `ROBOTS`: comma-separated robot IDs (`arm1` for one, `arm1,arm2,arm3` for many).
  - Keep `MQTT_HOST=mqtt-broker`, `MQTT_PORT=1883`, `OPCUA_PORT=4840`.

- For each additional robot, deploy a separate `pi-agent` Deployment with a
  distinct `ROBOT_ID` (the manifest header explains the pattern).

### Apply the stack

```sh
kubectl apply -f deploy/k3s/k3s-opcua-stack.yaml
```

### Verify

```sh
kubectl -n edge-opcua get pods
```

Confirm `mqtt-broker`, `spark-gateway`, and `pi-agent` (plus optional `ros2-opcua-bridge`, `opcua-ai-results-server`) are Running.

- Use UaExpert (or any OPC UA client) to connect to the fleet endpoint:
  `opc.tcp://<spark-host>:4840/fleet/`
- You should see one node per robot (Arm1, Arm2, … ArmN), each with per-joint
  target and state nodes.

This single flow works for one robot or many; only the `ROBOTS` list and number
of `pi-agent` Deployments change—the topology stays centralized.

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
