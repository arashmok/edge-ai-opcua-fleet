# Mimicking the OPC UA edge AI architecture: robotic arm + Pi + DGX Spark

A hands-on build guide for a miniature version of the industrial reference
architecture. One robotic arm stands in for the field devices, a Raspberry Pi
is the thin edge client wired to the arm, a DGX Spark is the edge AI node, OPC
UA is the secure backbone, and k3s orchestrates the containers.

---

## 1. How the mini setup maps to the real architecture

| Reference architecture | Your mini rig | Role |
|---|---|---|
| Field devices (PLCs, I/O, instrumentation) | Robotic arm + OPC UA server on the Pi | Source of process data, receiver of commands |
| Thin edge client on the robot | Raspberry Pi (k3s agent) | Real-time control loop, safe-stop, arm driver |
| Edge AI node | DGX Spark (k3s server) | Inference, OPC UA client, ROS 2 bridge |
| Secure comms via OPC UA | OPC UA over your LAN, port 4840 | Vendor-neutral, encrypted data backbone |
| OT platform / MES / cloud apps | Your laptop dashboard or an OPC UA browser | Consumer of AI results |
| Orchestration | k3s spanning the Pi and the Spark | Deploy, self-heal, update the containers |

The key idea: the arm appears on the backbone as a "field device" via an OPC UA
server running on the Pi, and the Spark reaches it as an OPC UA client. Your
ROS 2 world stays internal to the robot and the Spark, joined to the OPC UA
world by a bridge container.

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
  supply. This runs the k3s agent, the arm driver, and the OPC UA server.
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

### Phase 0: OPC UA with no hardware (1 evening)
Prove the backbone with pure software before the arm arrives.

1. On any machine, `pip install asyncua`.
2. Write a tiny OPC UA server that exposes six float variables (joint angles)
   and updates them with fake sine-wave values.
3. Write an OPC UA client that connects and prints the values.
4. Browse the server with a free tool like UaExpert to see the address space.

You now understand nodes, namespaces, and subscriptions, which is 80 percent of
OPC UA.

### Phase 1: Two-node k3s cluster (half a day)
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

### Phase 2: Real arm behind the OPC UA server (1 to 2 days)
1. Wire the arm to the Pi over USB. Find the device with `ls /dev/ttyUSB*` or
   `dmesg`.
2. Extend your Phase 0 server: instead of fake values, read the real joint
   states from the arm's API and publish them, and add a writable "target pose"
   node that drives the arm.
3. Add a safe-stop: a local loop on the Pi that halts the arm if the target
   node goes stale or out of range. This must run even if the network drops.
4. Containerize the server and push the image to a registry the cluster can
   reach (a local registry is fine).

### Phase 3: AI on the Spark (2 to 3 days)
1. Write the OPC UA client that subscribes to the arm state on the Spark.
2. Feed the state (plus a camera stream if you add one) into a model. Good
   starter tasks: predict a collision, classify a grasp, or run a small
   vision-language model that decides the next pose.
3. Write commands back to the arm's OPC UA target node.
4. Optionally add a second OPC UA server that publishes the AI results, so a
   dashboard or MES stand-in can consume them through the same backbone.

### Phase 4: ROS 2 bridge and orchestration (2 to 3 days)
1. Add ROS 2 nodes for the control side and bridge them to OPC UA.
2. Apply the manifests (see the companion file `k3s-opcua-stack.yaml`) so k3s
   runs and heals every container.
3. Test failure recovery: kill a pod, unplug the network, and watch what
   restarts and what safely stops.

---

## 4. The OPC UA stack choices

- **Server and client library:** `asyncua` (Python) for fast prototyping. Move
  to `open62541` (C) later if you need a smaller footprint on the Pi.
- **Security:** start with `None` so you can see traffic, then switch to
  `Basic256Sha256` with certificates. In production the certificate exchange is
  the whole point of "secure comms via OPC UA," so do not skip it permanently.
- **Client/server vs PubSub:** client/server is simplest for one arm. If you
  scale to several arms or high-rate telemetry, move to OPC UA PubSub over MQTT
  so publishers and subscribers decouple through a broker.
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

- **OPC UA up:** browse `opc.tcp://<pi-ip>:4840` with UaExpert and see joint
  nodes updating.
- **Cluster healthy:** `kubectl get pods -n edge-opcua -o wide` shows the arm
  server on the Pi and the AI, bridge, and results server on the Spark.
- **Data flowing:** the AI client logs incoming joint values; the arm reacts to
  a value written to its target node.
- **Resilience:** `kubectl delete pod <ai-pod>` and confirm k3s recreates it,
  while the Pi's safe-stop keeps the arm safe during the gap.
- **Network path:** when a session will not open, `tcpdump -i any port 4840` on
  the Pi shows whether the client is even reaching it, and Wireshark decodes the
  OPC UA handshake.

---

## 7. Where to go next

Once the loop works end to end, the interesting extensions are: add a camera and
a real perception model on the Spark, put the robot traffic on a separate VLAN
to practice OT segmentation, turn on OPC UA certificate security, and add a
second Pi with a second arm to see k3s schedule across a small fleet. Each of
those maps directly to a line in the job description you were reading.
