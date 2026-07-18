"""Verify the 'always-centralized' changes in the robotics repo.

Covers:
  1) Multi-document YAML validity + required resources in deploy/k3s/k3s-opcua-stack.yaml
  2) Dockerfile check for pi-agent (no opcua_arm_server.py, CMD runs pubsub_agent.py)
  3) Import/smoke test of the centralized entrypoints (pi-agent/pubsub_agent.py,
     spark-gateway/gateway.py) as modules (no main-loop side effects).
"""

import importlib
import os
import sys

import pytest

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
MANIFEST = os.path.join(REPO_ROOT, "deploy", "k3s", "k3s-opcua-stack.yaml")
DOCKERFILE = os.path.join(REPO_ROOT, "pi-agent", "Dockerfile")

yaml = pytest.importorskip("yaml")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _load_docs():
    with open(MANIFEST, "r", encoding="utf-8") as fh:
        return list(yaml.safe_load_all(fh))


def _find(docs, kind, name):
    for doc in docs:
        if not isinstance(doc, dict):
            continue
        meta = doc.get("metadata") or {}
        if doc.get("kind") == kind and meta.get("name") == name:
            return doc
    return None


def _all_of_kind(docs, kind):
    return [d for d in docs if isinstance(d, dict) and d.get("kind") == kind]


def _container_ports(doc):
    """Return the list of containerPort values for a Deployment doc."""
    ports = []
    containers = (((doc.get("spec") or {}).get("template") or {})
                  .get("spec", {}).get("containers", []))
    for c in containers:
        for p in c.get("ports", []) or []:
            if "containerPort" in p:
                ports.append(p["containerPort"])
    return ports


def _service_port(doc):
    """Return the list of `port` values for a Service doc."""
    ports = []
    for p in (doc.get("spec") or {}).get("ports", []) or []:
        if "port" in p:
            ports.append(p["port"])
    return ports


def _env_map(doc):
    """Return a dict of env name->value for the FIRST container of a Deployment."""
    containers = (((doc.get("spec") or {}).get("template") or {})
                  .get("spec", {}).get("containers", []))
    if not containers:
        return {}
    env = {}
    for e in containers[0].get("env", []) or []:
        if "name" in e and "value" in e:
            env[e["name"]] = e["value"]
    return env


def _volumes(doc):
    """Return the list of volume dicts for a Deployment."""
    return (((doc.get("spec") or {}).get("template") or {})
            .get("spec", {}).get("volumes", [])) or []


# --------------------------------------------------------------------------- #
# 1) YAML validity
# --------------------------------------------------------------------------- #
def test_manifest_parses_as_multi_doc():
    docs = _load_docs()
    assert docs, "expected at least one YAML document"
    # every doc must be a dict (no null/str fragments from bad separators)
    for d in docs:
        assert isinstance(d, dict), f"unexpected non-mapping document: {d!r}"


def test_configmap_opcua_config():
    docs = _load_docs()
    cm = _find(docs, "ConfigMap", "opcua-config")
    assert cm is not None, "ConfigMap 'opcua-config' not found"
    data = cm.get("data") or {}
    assert data.get("MQTT_HOST") == "mqtt-broker"
    assert data.get("MQTT_PORT") == "1883"
    assert data.get("OPCUA_PORT") == "4840"
    assert data.get("ROBOTS") == "arm1"
    assert "ARM_JOINTS" in data and data["ARM_JOINTS"], "ARM_JOINTS missing/empty"


def test_mqtt_broker_deployment_and_service():
    docs = _load_docs()
    dep = _find(docs, "Deployment", "mqtt-broker")
    assert dep is not None, "Deployment 'mqtt-broker' not found"
    assert 1883 in _container_ports(dep), "mqtt-broker Deployment must expose 1883"
    svc = _find(docs, "Service", "mqtt-broker")
    assert svc is not None, "Service 'mqtt-broker' not found"
    assert 1883 in _service_port(svc), "mqtt-broker Service must expose port 1883"


def test_spark_gateway_deployment_and_service():
    docs = _load_docs()
    dep = _find(docs, "Deployment", "spark-gateway")
    assert dep is not None, "Deployment 'spark-gateway' not found"
    assert 4840 in _container_ports(dep), "spark-gateway Deployment must expose 4840"
    svc = _find(docs, "Service", "spark-gateway")
    assert svc is not None, "Service 'spark-gateway' not found"
    assert 4840 in _service_port(svc), "spark-gateway Service must expose port 4840"


def test_pi_agent_deployment():
    docs = _load_docs()
    dep = _find(docs, "Deployment", "pi-agent")
    assert dep is not None, "Deployment 'pi-agent' not found"

    env = _env_map(dep)
    assert env.get("ROBOT_ID") == "arm1", f"ROBOT_ID env not arm1: {env!r}"

    vols = _volumes(dep)
    hostpaths = []
    for v in vols:
        hp = v.get("hostPath") or {}
        if "path" in hp:
            hostpaths.append(hp["path"])
    assert "/dev/ttyUSB0" in hostpaths, f"no hostPath for /dev/ttyUSB0 in {vols!r}"


def test_no_opcua_arm_server_deployment():
    docs = _load_docs()
    names = [((d.get("metadata") or {}).get("name")) for d in _all_of_kind(docs, "Deployment")]
    assert "opcua-arm-server" not in names
    assert "opcua_arm_server" not in names
    assert not any("opcua-arm-server" in (n or "") for n in names)


# --------------------------------------------------------------------------- #
# 2) Dockerfile check
# --------------------------------------------------------------------------- #
def test_dockerfile_no_opcua_arm_server():
    with open(DOCKERFILE, "r", encoding="utf-8") as fh:
        text = fh.read()
    assert "opcua_arm_server.py" not in text, "Dockerfile must not reference opcua_arm_server.py"
    assert "opcua-arm-server" not in text


def test_dockerfile_cmd_runs_pubsub_agent():
    with open(DOCKERFILE, "r", encoding="utf-8") as fh:
        text = fh.read()
    assert "pubsub_agent.py" in text, "Dockerfile must reference pubsub_agent.py"
    # CMD line must contain pubsub_agent.py
    cmd_lines = [ln for ln in text.splitlines() if ln.strip().startswith("CMD")]
    assert cmd_lines, "no CMD instruction found"
    assert any("pubsub_agent.py" in ln for ln in cmd_lines), \
        f"CMD does not run pubsub_agent.py: {cmd_lines!r}"


def test_dockerfile_does_not_copy_opcua_arm_server():
    with open(DOCKERFILE, "r", encoding="utf-8") as fh:
        text = fh.read()
    copy_lines = [ln for ln in text.splitlines() if ln.strip().startswith("COPY")]
    for ln in copy_lines:
        assert "opcua_arm_server.py" not in ln, \
            f"Dockerfile COPYs opcua_arm_server.py: {ln!r}"


# --------------------------------------------------------------------------- #
# 3) Import/smoke test of centralized entrypoints
# --------------------------------------------------------------------------- #
def test_import_pubsub_agent(tmp_path):
    pi_dir = os.path.join(REPO_ROOT, "pi-agent")
    sys.path.insert(0, pi_dir)
    try:
        mod = importlib.import_module("pubsub_agent")
        assert hasattr(mod, "Agent"), "pubsub_agent.Agent missing"
        # __main__ guard: importing must not have started the run loop.
        # (Agent() construction touches hardware via ServoDriver mock; do NOT
        # instantiate it here to avoid side effects.)
    finally:
        sys.path.remove(pi_dir)


def test_import_gateway(tmp_path):
    gw_dir = os.path.join(REPO_ROOT, "spark-gateway")
    sys.path.insert(0, gw_dir)
    try:
        mod = importlib.import_module("gateway")
        assert hasattr(mod, "Gateway"), "gateway.Gateway missing"
    finally:
        sys.path.remove(gw_dir)
