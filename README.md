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
                                   |  | - OPC UA PubSub / MQTT       |  |
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
| `servo_driver.py` | Pi | Drives SG90 servos via lgpio (gpiochip PWM), mock fallback |
| `pubsub_agent.py` | Pi | Publishes state and subscribes to commands over MQTT, local safe-stop |
| `gateway.py` | DGX Spark | One OPC UA endpoint for the fleet, OPC UA PubSub over MQTT |
| `k3s-opcua-stack.yaml` | cluster | Deploys and heals the containers, pins them by node label |

## Quick start (no hardware needed)

Everything runs in mock mode when lgpio and the broker are absent, so you can
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

This walkthrough takes you from bare hardware to a live OPC UA fleet endpoint.
Everything is deployed by a single manifest, `deploy/k3s/k3s-opcua-stack.yaml`,
which creates the `edge-opcua` namespace, an MQTT broker, the OPC UA gateway,
and one agent per robot. Do the steps in order; each one builds on the last.

### Step 0 — What you need

- **One DGX Spark** — becomes the k3s server (control plane), and also runs the
  MQTT broker and the OPC UA fleet gateway.
- **One or more Raspberry Pis** — each becomes a k3s agent wired to one arm.
- A workstation with an OPC UA client (e.g. [UaExpert](https://www.unified-automation.com/products/development-tools/uaexpert.html))
  to browse the fleet once it is up.
- The arms wired to their Pis (see [On hardware](#on-hardware)). You can also
  bring the stack up first in mock mode and wire the arms later.

### Step 1 — Install k3s and join the Pis

On the **Spark** (installs the k3s server):

```sh
curl -sfL https://get.k3s.io | sh -
```

Grab the node token the agents need to join:

```sh
sudo cat /var/lib/rancher/k3s/server/node-token
```

On **each Pi** (joins as an agent — replace `<spark-ip>` and `<token>`):

```sh
curl -sfL https://get.k3s.io | K3S_URL=https://<spark-ip>:6443 K3S_TOKEN=<token> sh -
```

Back on the Spark, confirm every node has joined (`kubectl` on k3s needs sudo,
or copy `/etc/rancher/k3s/k3s.yaml` to `~/.kube/config`):

```sh
sudo kubectl get nodes -o wide
```

### Step 2 — Label the nodes

Workloads are pinned by node label: the gateway and broker land on the Spark
(`role=edge`), and each agent lands on a Pi (`role=arm`). Use the hostnames from
`kubectl get nodes`:

```sh
sudo kubectl label node <spark-hostname> role=edge
sudo kubectl label node <pi-hostname>    role=arm
```

Without these labels the pods stay `Pending` (see [Troubleshooting](#troubleshooting-k3s)).

### Step 3 — Provide the container images

k3s runs images from its own containerd. The manifest sets
`imagePullPolicy: IfNotPresent`, so either push to a registry both boxes can
reach, or import locally-built images straight into containerd.

**Option A — local import (simplest for a bench).** Build each image on the box
that will run it, then import it into k3s:

```sh
# On the Spark:
cd spark-gateway && docker build -t spark-gateway:latest .
docker save spark-gateway:latest | sudo k3s ctr images import -

# On each Pi (native arm64 build):
cd pi-agent && docker build -t pi-agent:latest .
docker save pi-agent:latest | sudo k3s ctr images import -
```

Then set the image names in the manifest to the plain tags (`spark-gateway:latest`,
`pi-agent:latest`).

**Option B — registry.** Build, tag, and push to a registry, then leave the
`your-registry/...` image names in the manifest (or point them at your registry):

```sh
cd spark-gateway && docker build -t your-registry/spark-gateway:latest . && docker push your-registry/spark-gateway:latest
cd pi-agent      && docker build -t your-registry/pi-agent:latest .      && docker push your-registry/pi-agent:latest
```

The optional `ros2-opcua-bridge` and `opcua-ai-results-server` images are only
needed if you keep those Deployments (Step 5).

### Step 4 — Configure the fleet

Edit the `opcua-config` ConfigMap in `deploy/k3s/k3s-opcua-stack.yaml`. These
values are injected into both the gateway and the agents:

| Key | Meaning | Default |
|---|---|---|
| `ROBOTS` | Robot IDs the **gateway** models, comma-separated | `arm1` |
| `ARM_JOINTS` | Joint names per arm (order matters) | `base,pitch,reach,gripper` |
| `MQTT_HOST` / `MQTT_PORT` | Broker the gateway and agents connect to | `mqtt-broker` / `1883` |
| `OPCUA_PORT` | Port for the single OPC UA fleet endpoint | `4840` |
| `SAMPLE_INTERVAL_MS` | State sampling interval | `100` |
| `ROS_DOMAIN_ID` | Only used by the optional ROS 2 bridge | `42` |

Two rules keep the gateway and agents in sync:

- The gateway's `ROBOTS` list must contain every robot ID you deploy.
- Each `pi-agent` Deployment sets its own `ROBOT_ID` (in the Deployment `env`,
  not the ConfigMap) and must match one entry in `ROBOTS`.

The shipped manifest is preconfigured for a **single robot** (`ROBOTS: "arm1"`
and one `pi-agent` with `ROBOT_ID=arm1`). To run more than one arm, see
[Step 7](#step-7--scale-to-more-robots).

Also check the `pi-agent` Deployment's serial device: it mounts `/dev/ttyUSB0`
via `hostPath`. If your arm enumerates elsewhere (`ls /dev/ttyUSB*` on the Pi),
update both the `volumeMounts` path and the `hostPath.path`.

### Step 5 — Deploy the stack

```sh
sudo kubectl apply -f deploy/k3s/k3s-opcua-stack.yaml
```

This creates, in the `edge-opcua` namespace: the `mqtt-broker`, the
`spark-gateway`, one `pi-agent`, and the two optional edge-AI Deployments
(`ros2-opcua-bridge`, `opcua-ai-results-server`). If you don't want the optional
components, delete their blocks from the manifest before applying, or remove
them afterward with `kubectl delete deployment -n edge-opcua ros2-opcua-bridge opcua-ai-results-server`.

### Step 6 — Verify

Watch the pods come up:

```sh
sudo kubectl -n edge-opcua get pods -o wide
```

`mqtt-broker`, `spark-gateway`, and `pi-agent` should reach `Running`. Tail the
logs to confirm the bridge is flowing:

```sh
sudo kubectl -n edge-opcua logs deploy/spark-gateway   # "OPC UA fleet endpoint on port 4840, robots=['arm1']"
sudo kubectl -n edge-opcua logs deploy/pi-agent        # connects to broker, publishes state
```

Then connect an OPC UA client to the fleet endpoint on the Spark:

```
opc.tcp://<spark-host>:4840/fleet/
```

You should see one object per robot (`Arm1`, `Arm2`, … `ArmN`), each exposing a
`state` node and a `target` node per joint. Write a value to a joint's `target`
node and watch the matching `state` node follow it — that round trip proves the
OPC UA → MQTT → agent → MQTT → OPC UA path end to end.

### Step 7 — Scale to more robots

Adding an arm never changes the topology — you extend the `ROBOTS` list and add
one more agent:

1. Add the ID to the ConfigMap: `ROBOTS: "arm1,arm2"`.
2. Copy the `pi-agent` Deployment block in the manifest and, in the copy, change
   the `metadata.name` (e.g. `pi-agent-arm2`), the `ROBOT_ID` env value
   (`arm2`), and — if it runs on a different Pi — the `nodeSelector`/label so it
   lands on the right board.
3. Re-apply: `sudo kubectl apply -f deploy/k3s/k3s-opcua-stack.yaml`.

The gateway picks up the new robot in its model and a new `Arm2` object appears
under the same OPC UA endpoint.

### Troubleshooting (k3s)

| Symptom | Likely cause | Fix |
|---|---|---|
| Pod stuck `Pending` | Node labels missing | Re-run the `kubectl label node ...` commands (Step 2) |
| `ImagePullBackOff` | Image not on the node / registry unreachable | Import locally (Step 3, Option A) or fix the registry path |
| `pi-agent` `CrashLoopBackOff` | Wrong serial device path | Correct `/dev/ttyUSB0` in the Deployment, or run mock (remove the device mount) |
| OPC UA client can't connect | Firewall or wrong host | Ensure the Spark's `4840/tcp` is reachable; the gateway uses `hostNetwork`, so use the Spark's real IP |
| `state` never moves | Agent not matched to a `ROBOTS` entry | Make each `ROBOT_ID` match one ID in `ROBOTS` |

### Teardown

```sh
sudo kubectl delete -f deploy/k3s/k3s-opcua-stack.yaml
```

This removes the whole `edge-opcua` namespace and everything in it; k3s itself
stays installed.

## On hardware

- Servo signal wires go to Pi GPIO pins. The default is BCM `17,27,22,23`
  (Raspberry Pi 4 physical header pins 11, 13, 15, 16) — a conflict-free set
  clear of the I2C/SPI/UART/I2S buses. Override with `ARM_CHANNELS` if needed.
  lgpio drives software PWM on any GPIO via `/dev/gpiochip`, so hardware-PWM
  pins are not required.
- Servos get a SEPARATE 5V supply, never the Pi 5V pin. Tie all grounds together.
- lgpio needs no daemon (unlike pigpio); just make sure the agent's user can
  reach `/dev/gpiochip*` (the `gpio` group on Raspberry Pi OS, or a privileged
  container). Works on Raspberry Pi OS Bookworm/Trixie and on the Pi 5.
- Note: SG90 servos are open loop, so reported state is the commanded angle, not
  measured. The local safe-stop on each Pi runs independently of the network.

See `docs/getting-started.md` for the full phased build and `docs/architecture.md`
for the design rationale.

## Standards note

The gateway and agents exchange OPC UA PubSub JSON NetworkMessages conforming to
OPC UA Part 14 (JSON message mapping) over the MQTT transport—one of the standard
OPC UA PubSub transports. The encoding is the official OPC UA PubSub JSON format:
a NetworkMessage with MessageType `ua-data` carrying DataSetMessages
(`ua-keyframe`) whose Payload fields use the Part 6 (v1.05) JSON Data Encoding.
Under VerboseEncoding a concrete scalar collapses to its bare JSON value
(e.g. a Double joint angle is simply `"base": 90.0`), and the DataSetMessage
`Status` is omitted when the value is Good. asyncua provides the OPC UA
client/server (north/OT-facing) endpoint; paho-mqtt provides the broker
transport; the on-the-wire PubSub message format itself—the JSON
NetworkMessage—is the standardized OPC UA encoding, implemented in the shared
`ua_pubsub.py` codec. open62541 is an alternative full-stack option, but this
repo is fully spec-compliant for PubSub over MQTT.
