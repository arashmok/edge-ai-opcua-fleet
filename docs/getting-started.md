# Mimicking the OPC UA edge AI architecture: robotic arm + Pi + DGX Spark

A hands-on build guide for a miniature version of the industrial reference
architecture. One robotic arm stands in for the field devices, a Raspberry Pi
is the thin edge client wired to the arm, a DGX Spark is the edge AI node, OPC
UA is the secure backbone, and k3s orchestrates the containers.

---

## 1. How the mini setup maps to the real architecture

| Reference architecture | Your mini rig | Role |
|---|---|---|
| Field devices (PLCs, I/O, instrumentation) | Robotic arm + pubsub_agent on the Pi | Source of process data, receiver of commands via MQTT; publishes fleet/<id>/state, subscribes to fleet/<id>/cmd |
| Thin edge client on the robot | Raspberry Pi (k3s agent) | Real-time control loop, safe-stop watchdog, MQTT pubsub client; uses pigpio (mock fallback) |
| Edge AI node | DGX Spark (k3s server) | Inference, MQTT broker, OPC UA fleet gateway, ROS 2 bridge, optional AI-results OPC UA server |
| Secure comms via OPC UA | OPC UA over your LAN, port 4840 (to gateway); MQTT over port 1883 (to broker) | OT apps browse one fleet endpoint; robot link uses OPC UA PubSub JSON over MQTT |
| OT platform / MES / cloud apps | Your laptop dashboard or an OPC UA browser | Consumer of AI results and arm state via the gateway |
| Orchestration | k3s spanning the Pi and the Spark | Deploy, self-heal, update the containers (mqtt-broker, spark-gateway, pi-agent, ...) |

The key idea: the arm appears on the backbone as a "field device" via a central
OPC UA fleet gateway running on the Spark (spark-gateway/gateway.py), which
models the entire fleet (Arm1/ArmN per robot). The Pi runs a lightweight MQTT
pubsub agent (pi-agent/pubsub_agent.py) that publishes state and subscribes to
commands using standard OPC UA PubSub JSON NetworkMessages (Part 14). Your ROS 2
world stays internal to the robot and the Spark, joined to the OPC UA world by a
bridge container.

---

## 2. Bill of materials

### Robotic arm (pick one with a controllable interface and, ideally, ROS 2 support)
- Elephant Robotics myCobot 280 (6-DOF, Python API, ROS 2 packages). A strong
  default for this project.
- Interbotix PincherX 150 (ROS 2 native via Interbotix packages).
- Any arm with a documented serial or Python API works. The requirement is that
  you can read joint states and send joint or pose commands from code.

### Compute
- Raspberry Pi 5 (8GB recommended) with an active cooler and a quality power
  supply. This runs the k3s agent, the servo driver, and the MQTT pubsub agent (it does not host an OPC UA server).
- NVIDIA DGX Spark as the edge AI node and k3s server. If you do not have one
  yet, you can substitute any x86 machine with an NVIDIA GPU for early phases,
  then swap in the Spark later. The architecture does not change.

### Network
- A dedicated switch or a spare router. Put the Pi and the Spark on the same
  subnet to start.
- Optional but recommended for realism: a managed switch so you can place the
  robot traffic on its own VLAN, mimicking OT network segmentation.

---

## 3. Build it in phases

Do not wire everything at once. Each phase gives you a working checkpoint.

### Phase 0: OPC UA with no hardware
Prove the backbone with pure software before the arm arrives.

1. On any machine, `pip install asyncua`.
2. Write a tiny OPC UA server that exposes six float variables (joint angles)
   and updates them with fake sine-wave values.
3. Write an OPC UA client that connects and prints the values.
4. Browse the server with a free tool like UaExpert to see the address space.

You now understand nodes, namespaces, and subscriptions, which is 80 percent of
OPC UA. In this project the OPC UA server is the central gateway on the Spark
(spark-gateway/gateway.py), not on the Pi. The server exposes a single fleet
endpoint `opc.tcp://<spark>:4840/fleet/` with Arm1/<joint>/target (writable) and
Arm1/<joint>/state (read-only) per robot.

### Phase 1: Two-node k3s cluster
1. On the Spark, install the k3s server:
   `curl -sfL https://get.k3s.io | sh -`
2. Grab the join token from the Spark:
   `sudo cat /var/lib/rancher/k3s/server/node-token`
3. On the Pi, join as an agent:
   `curl -sfL https://get.k3s.io | K3S_URL=https://<spark-ip>:6443 K3S_TOKEN=<token> sh -`
4. From the Spark, confirm both nodes:
   `kubectl get nodes -o wide`
5. Label the nodes so workloads land on the right box:
   ```
   kubectl label node <pi-hostname>    role=arm
   kubectl label node <spark-hostname> role=edge
   ```

### Phase 2: Pi-side MQTT pubsub agent (1 to 2 days)
1. Wire the arm to the Pi over USB. Find the device with `ls /dev/ttyUSB*` or
   `dmesg`.
2. Run `pi-agent/pubsub_agent.py` (see the codebase for environment variables:
   ROBOT_ID, ARM_JOINTS, ARM_CHANNELS, WATCHDOG_S, STATE_HZ, ANGLE_MIN/MAX). It
   drives the servos via `servo_driver.py` (pigpio, with mock fallback), runs a
   local safe-stop watchdog, PUBLISHES joint state to `fleet/<robot_id>/state`
   and SUBSCRIBES to commands on `fleet/<robot_id>/cmd` over MQTT. Messages use
   standard OPC UA PubSub JSON NetworkMessages (Part 14, JSON mapping) via the
   shared `ua_pubsub.py` codec (present in both `pi-agent/` and `spark-gateway/`).
3. Containerize the agent using `pi-agent/Dockerfile` and push the image to a
   registry the cluster can reach (a local registry is fine).

The Pi does NOT host an OPC UA server; it only exchanges OPC UA PubSub JSON
messages over MQTT with the central gateway on the Spark.

### Phase 3: AI on the Spark (2 to 3 days)
1. Run `spark-gateway/gateway.py` on the Spark (ROBOTS env var: e.g. "arm1" for
   one robot, "arm1,arm2,arm3" for many). It hosts the OPC UA fleet endpoint at
   `opc.tcp://<spark>:4840/fleet/`, bridges OPC UA <-> MQTT using the same
   `ua_pubsub.py` codec, and maintains state nodes (Arm1/<joint>/state) and
   target nodes (Arm1/<joint>/target) per robot.
2. Write an OPC UA client that subscribes to the arm state via the gateway
   (`opc.tcp://<spark>:4840/fleet/`).
3. Feed the state (plus a camera stream if you add one) into a model. Good
   starter tasks: predict a collision, classify a grasp, or run a small
   vision-language model that decides the next pose.
4. Write commands back to the arm via the gateway's target nodes; the gateway
   publishes a command NetworkMessage on MQTT that the Pi agent applies.
5. Optionally add a second OPC UA server that publishes the AI results, so a
   dashboard or MES stand-in can consume them through the same backbone.

The AI never connects directly to the Pi; it uses the central gateway.

### Phase 4: ROS 2 bridge and orchestration (2 to 3 days)
1. Add ROS 2 nodes for the control side and bridge them to OPC UA (via the
   spark-gateway or the optional opcua-ros2-bridge container).
2. Apply `deploy/k3s/k3s-opcua-stack.yaml` so k3s runs `mqtt-broker`, `spark-gateway`,
   `pi-agent` (with `ROBOT_ID` per robot), plus optional `ros2-opcua-bridge` and
   `opcua-ai-results-server`. Nodes are labeled `role=arm` (Pi) and `role=edge` (Spark).
3. Test failure recovery: kill a pod, unplug the network, and watch what
   restarts and what safely stops (e.g., the Pi's local watchdog halts the arm
   if commands go stale).

---

## 4. The OPC UA stack choices

- **Server and client library:** `asyncua` (Python) for fast prototyping. Move
  to `open62541` (C) later if you need a smaller footprint on the Pi.
- **Security:** start with `None` so you can see traffic, then switch to
  `Basic256Sha256` with certificates. In production the certificate exchange is
  the whole point of "secure comms via OPC UA," so do not skip it permanently.
- **Client/server vs PubSub:** this project uses OPC UA PubSub over MQTT from
  the start, for one robot or many (one robot = ROBOTS=arm1). The OT-facing
  access to the gateway is OPC UA client/server (`opc.tcp://<spark>:4840/fleet/`),
  while the robot-to-gateway link uses OPC UA PubSub JSON NetworkMessages (Part 14)
  over an MQTT broker (Mosquitto on the Spark). This decouples publishers and
  subscribers and scales transparently as you add robots.
- **Information model:** model the arm with meaningful node names and types
  (joints, poses, status) rather than raw floats. This is what lets an OT app
  consume the data without custom parsing.

---

## 5. Two things that will trip you up in k3s

### ROS 2 discovery inside Kubernetes
ROS 2 uses DDS multicast discovery, which does not traverse pod networking by
default. The two working options:
- Run the ROS 2 pods with `hostNetwork: true` (used in the manifests). Simplest.
- Or run a DDS Discovery Server and point nodes at it. Cleaner at scale.

### GPU access on the Spark
For pods to use the GPU you need two pieces in place on the Spark:
- The NVIDIA container runtime wired into k3s (containerd config), so
  `runtimeClassName: nvidia` works.
- The NVIDIA device plugin installed, so `nvidia.com/gpu: 1` resource requests
  are schedulable.
Verify with a quick `nvidia-smi` job before deploying the real AI container.

---

## 6. Verifying each layer

- **OPC UA up:** browse `opc.tcp://<spark-ip>:4840/fleet/` with UaExpert and see
  Arm1..ArmN nodes (per robot) with per-joint target/state nodes updating.
- **Cluster healthy:** `kubectl get pods -n edge-opcua -o wide` shows
  `mqtt-broker` and `spark-gateway` on the Spark (role=edge) and `pi-agent` on
  the Pi (role=arm), plus optional `ros2-opcua-bridge` and `opcua-ai-results-server`
  on the Spark.
- **Data flowing:** the gateway logs incoming joint state from MQTT and writes it
  into OPC UA state nodes; writing a target node causes the gateway to publish a
  command NetworkMessage on MQTT that the Pi agent applies to the servos.
- **MQTT broker check:** watch OPC UA PubSub JSON NetworkMessages with an MQTT
  client, e.g. `mosquitto_sub -h <spark> -t 'fleet/#' -v`, and see ua-data
  messages on `fleet/<id>/state` and `fleet/<id>/cmd`.
- **Resilience:** `kubectl delete pod <ai-pod>` and confirm k3s recreates it,
  while the Pi's local safe-stop watchdog keeps the arm safe during the gap.
- **Network path:** when OPC UA sessions will not open, `tcpdump -i any port 4840`
  on the Spark shows whether the client is reaching the gateway; `tcpdump -i any
  port 1883` on the Spark shows MQTT traffic for the robot link. Wireshark decodes
  both OPC UA and OPC UA PubSub JSON (Part 14).

---

## 7. Where to go next

Once the loop works end to end, the interesting extensions are: add a camera and
a real perception model on the Spark, put the robot traffic on a separate VLAN
to practice OT segmentation, turn on OPC UA certificate security, and add a
second robot — which is just adding its id to the gateway's `ROBOTS` list and deploying another `pi-agent`
with a distinct `ROBOT_ID` (the centralized topology does not change). Each of
those maps directly to a line in the job description you were reading.
