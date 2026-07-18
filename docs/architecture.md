# Architecture

## The layers

From the OT world down to the metal:

1. **OT and cloud apps** (OT platform, MES, dashboards, an OPC UA browser like
   UaExpert). They consume data and issue commands through OPC UA.
2. **OPC UA backbone.** A vendor-neutral, secure (certificate-based, encrypted)
   industrial data bus. This is the interface OT systems speak.
3. **DGX Spark edge node.** Runs the AI inference and, in the fleet design, hosts
   the single OPC UA endpoint that models every robot. Also the k3s server.
4. **Raspberry Pi robots.** Each drives its servos, runs a local safe-stop, and
   reports to the gateway. k3s agents.
5. **Arm hardware.** SG90 servos driven from GPIO via pigpio.

## Why the arm is an OPC UA field device

In OPC UA a field device is modeled as an address space of typed nodes. The arm
is exposed as joint objects, each with a writable `target` and a read-only
`state`. OT apps read and write those nodes without knowing anything about
servos, GPIO, or Python. That abstraction is the whole value of putting OPC UA
in the loop.

## Single robot vs centralized fleet

**Single robot.** Each Pi hosts its own OPC UA server. Simple, but every robot
exposes a socket, every OT client holds a session per robot, and certificates
multiply per device. Fine for one or two arms.

**Centralized fleet.** The Spark hosts one OPC UA endpoint that models the whole
fleet (Arm1..ArmN). OT apps browse everyone in a single session, and adding a
robot is just a new id. The robots become lightweight clients that publish state
and subscribe to commands over MQTT. No robot hosts an inbound socket.

## Why MQTT is still OPC UA

OPC UA has two communication models: client/server (sockets and sessions) and
PubSub (publish/subscribe). PubSub runs over either UDP multicast or a broker
(MQTT or AMQP). So a broker is not a foreign protocol bolted on; it is one of
OPC UA PubSub's own sanctioned transports. UDP multicast is fragile across VLANs
and WiFi, which is exactly a fleet-on-a-plant-network situation, so the broker
transport is the practical choice. The result is broker-based and inside the
OPC UA standard at the same time.

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
- PubSub support varies by OPC UA library. asyncua is strong on client/server;
  open62541 has mature PubSub. Verify your stack before committing to full
  PubSub.
