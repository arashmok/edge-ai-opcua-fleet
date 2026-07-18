# Architecture

## The layers

From the OT world down to the metal:

1. **OT and cloud apps** (OT platform, MES, dashboards, an OPC UA browser like
   UaExpert). They consume data and issue commands through OPC UA.
2. **OPC UA backbone.** A vendor-neutral, secure (certificate-based, encrypted)
   industrial data bus. This is the interface OT systems speak.
3. **DGX Spark edge node.** Runs the AI inference and hosts the single OPC UA
   endpoint that models every robot. Also the k3s server.
4. **Raspberry Pi robots.** Each drives its servos, runs a local safe-stop, and
   reports to the gateway. k3s agents.
5. **Arm hardware.** SG90 servos driven from GPIO via pigpio.

## Why the arm is an OPC UA field device

In OPC UA a field device is modeled as an address space of typed nodes. The arm
is exposed as joint objects, each with a writable `target` and a read-only
`state`. OT apps read and write those nodes without knowing anything about
servos, GPIO, or Python. That abstraction is the whole value of putting OPC UA
in the loop.

## Centralized architecture

The DGX Spark hosts one OPC UA endpoint that models the entire fleet (Arm1..ArmN).
OT apps browse everyone in a single session, and adding a robot is just a new id
in the ROBOTS list; no topology change is required. Whether you run one robot or
many, the setup is identical: the Spark hosts the OPC UA endpoint, and each robot
is a lightweight MQTT client that publishes state and subscribes to commands.
No robot hosts an inbound socket.

A single robot is simply the centralized setup with `ROBOTS=arm1`. The same flow
and components are used; only the robot list changes.

## Why MQTT is still OPC UA

OPC UA PubSub uses the MQTT broker transport as one of its standard delivery
mechanisms (OPC UA Part 14). This repo implements the complete standard: MQTT
messages are genuine OPC UA PubSub JSON NetworkMessages (MessageType "ua-data")
carrying DataSetMessages with fields encoded as DataValue/Variant per the OPC UA
JSON NetworkMessage mapping. The broker is not a separate bridge protocol—it is
the official PubSub transport and the messages themselves are OPC UA-compliant.
UDP multicast is an alternative PubSub transport but is fragile across VLANs and
WiFi; for a fleet-on-a-plant-network, MQTT is the practical and standard choice.

## The safe-stop is deliberately outside orchestration

k3s can restart a crashed container, but during the seconds it takes to
reschedule, the arm cannot be waiting on a container that is not running. So each
Pi holds a local loop that releases the servos if commands go stale or a
safe_stop flag arrives. That loop survives a network drop, a broker outage, and a
pod restart. Everything else can be cloud-native and self-healing; the thing that
keeps the physical arm safe cannot depend on the cluster or the network.

## k3s topology

One cluster spans the Spark (server) and the Pis (agents). Node labels pin
workloads to the right machine: the arm-facing containers land on the Pis where
the hardware is (role=arm), and the AI and gateway containers land on the Spark
(role=edge). k3s handles deployment, health checks, and rolling updates so the
same workload definitions run everywhere.

## Known tradeoffs

- The gateway adds a hop between the arm and its OPC UA representation, so the
  model is slightly behind the physical arm. Fine for supervisory data; the
  safe-stop stays local precisely so nothing safety-critical depends on the hop.
- The gateway and broker are a single point of failure for fleet visibility, not
  for robot safety. Replicate them (k3s makes this straightforward) for
  resilience.
- SG90 servos are open loop, so reported state is commanded, not measured. Use
  servos with encoders if you need true feedback.
- The repo carries its own Part 14-conformant JSON NetworkMessage codec
  (`ua_pubsub.py`) for PubSub message encoding over MQTT, so it does not depend
  on a particular library's built-in PubSub support; asyncua is used only for the
  client/server (OT-facing) endpoint.
