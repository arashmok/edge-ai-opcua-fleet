#!/usr/bin/env python3
"""
Web OT client for the OPC UA fleet gateway.

Serves a single-page UI with one slider per joint. Slider changes write the
joint's OPC UA `target` node on the gateway; the page polls the `state` nodes to
show the reported angle. A safe-stop button toggles the robot's safe_stop node.

A browser cannot speak the OPC UA binary protocol, so this small FastAPI backend
holds the asyncua client and exposes a thin JSON API the page calls.

Env (all optional):
  OPCUA_URL   gateway endpoint, default opc.tcp://192.168.50.230:4840/fleet/
  ROBOT_ID    robot object name, default "arm1"
  ARM_JOINTS  joint names,       default "base,pitch,reach,gripper"
  OPCUA_NS    namespace URI,     default "http://fleet.local"
  ANGLE_MIN / ANGLE_MAX          default 0 / 180
"""

import os
import asyncio
import contextlib

from asyncua import Client, ua
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

OPCUA_URL = os.getenv("OPCUA_URL", "opc.tcp://192.168.50.230:4840/fleet/")
ROBOT = os.getenv("ROBOT_ID", "arm1")
JOINTS = [j for j in os.getenv("ARM_JOINTS", "base,pitch,reach,gripper").split(",") if j]
NS_URI = os.getenv("OPCUA_NS", "http://fleet.local")
ANGLE_MIN = float(os.getenv("ANGLE_MIN", "0"))
ANGLE_MAX = float(os.getenv("ANGLE_MAX", "180"))


class Fleet:
    """A single, lazily-connected asyncua client with auto-reconnect. All access
    is serialized by a lock because an asyncua Client is not concurrency-safe."""

    def __init__(self):
        self._client = None
        self._targets = {}
        self._states = {}
        self._online = None
        self._stop = None
        self._lock = asyncio.Lock()

    async def _connect(self):
        client = Client(OPCUA_URL, timeout=5)
        await client.connect()
        idx = await client.get_namespace_index(NS_URI)
        for j in JOINTS:
            self._targets[j] = await client.nodes.objects.get_child(
                [f"{idx}:{ROBOT}", f"{idx}:{j}", f"{idx}:target"])
            self._states[j] = await client.nodes.objects.get_child(
                [f"{idx}:{ROBOT}", f"{idx}:{j}", f"{idx}:state"])
        self._stop = await client.nodes.objects.get_child(
            [f"{idx}:{ROBOT}", f"{idx}:safe_stop"])
        try:
            self._online = await client.nodes.objects.get_child(
                [f"{idx}:{ROBOT}", f"{idx}:online"])
        except Exception:  # noqa: BLE001 - older gateway without liveness node
            self._online = None
        self._client = client

    async def _reset(self):
        client, self._client = self._client, None
        if client is not None:
            with contextlib.suppress(Exception):
                await client.disconnect()

    async def _run(self, op):
        """Run op(), reconnecting once on any failure (stale session, etc.)."""
        async with self._lock:
            if self._client is None:
                await self._connect()
            try:
                return await op()
            except Exception:
                await self._reset()
                await self._connect()
                return await op()

    async def set_target(self, joint, angle):
        angle = max(ANGLE_MIN, min(ANGLE_MAX, float(angle)))

        async def op():
            await self._targets[joint].write_value(
                ua.Variant(angle, ua.VariantType.Double))
        await self._run(op)
        return angle

    async def set_stop(self, on):
        async def op():
            await self._stop.write_value(
                ua.Variant(bool(on), ua.VariantType.Boolean))
        await self._run(op)

    async def snapshot(self):
        async def op():
            state = {j: round(float(await self._states[j].read_value()), 1)
                     for j in JOINTS}
            target = {j: round(float(await self._targets[j].read_value()), 1)
                      for j in JOINTS}
            online = True
            if self._online is not None:
                online = bool(await self._online.read_value())
            return {"state": state, "target": target, "online": online,
                    "safe_stop": bool(await self._stop.read_value())}
        return await self._run(op)


fleet = Fleet()
app = FastAPI(title="OPC UA Fleet OT Client")


class SetReq(BaseModel):
    joint: str
    angle: float


class StopReq(BaseModel):
    on: bool


@app.get("/api/state")
async def api_state():
    try:
        return await fleet.snapshot()
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"error": str(exc)}, status_code=503)


@app.post("/api/set")
async def api_set(req: SetReq):
    if req.joint not in JOINTS:
        return JSONResponse({"error": "unknown joint"}, status_code=400)
    try:
        angle = await fleet.set_target(req.joint, req.angle)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"error": str(exc)}, status_code=503)
    return {"joint": req.joint, "angle": angle}


@app.post("/api/safe_stop")
async def api_safe_stop(req: StopReq):
    try:
        await fleet.set_stop(req.on)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"error": str(exc)}, status_code=503)
    return {"safe_stop": req.on}


@app.get("/", response_class=HTMLResponse)
async def index():
    return PAGE.replace("__ENDPOINT__", OPCUA_URL).replace("__ROBOT__", ROBOT)


PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Fleet OT Client</title>
<style>
  :root{--bg:#0f1420;--card:#1a2233;--fg:#e6ebf5;--mut:#8a97b1;--acc:#4f8cff;--ok:#28c76f;--bad:#ff4d4f;}
  *{box-sizing:border-box;}
  body{margin:0;font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;background:var(--bg);color:var(--fg);}
  .wrap{max-width:680px;margin:0 auto;padding:24px 16px 48px;}
  h1{font-size:20px;margin:0 0 4px;}
  .sub{color:var(--mut);font-size:13px;margin-bottom:20px;word-break:break-all;}
  .card{background:var(--card);border-radius:14px;padding:16px 18px;margin-bottom:14px;}
  .joint{display:flex;align-items:center;gap:14px;margin:10px 0;}
  .joint .name{width:84px;font-weight:600;text-transform:capitalize;}
  .joint input[type=range]{flex:1;accent-color:var(--acc);}
  .joint .val{width:52px;text-align:right;font-variant-numeric:tabular-nums;}
  .joint .st{width:70px;text-align:right;color:var(--mut);font-size:12px;font-variant-numeric:tabular-nums;}
  .row{display:flex;gap:10px;margin-top:6px;}
  button{border:0;border-radius:10px;padding:12px 16px;font-size:14px;font-weight:600;cursor:pointer;color:#fff;}
  .btn-center{background:#33405c;}
  .btn-stop{background:var(--bad);flex:1;}
  .btn-stop.armed{background:#7a2426;}
  .status{font-size:12px;color:var(--mut);margin-top:12px;}
  .viz{position:relative;}
  .viz .cap{display:flex;justify-content:space-between;color:var(--mut);font-size:12px;margin-bottom:8px;}
  .scene{width:100%;height:340px;border-radius:12px;overflow:hidden;background:#0c1524;}
  .scene canvas{display:block;width:100%!important;height:100%!important;}
  .viz.off .scene{opacity:.3;filter:grayscale(1);}
  .viz .offbadge{display:none;position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);
    background:var(--bad);color:#fff;font-weight:700;letter-spacing:.05em;padding:8px 16px;border-radius:10px;
    box-shadow:0 6px 20px rgba(0,0,0,.4);z-index:5;}
  .viz.off .offbadge{display:block;}
  .hint{color:var(--mut);font-size:11px;margin-top:6px;text-align:center;}
</style>
</head>
<body>
<div class="wrap">
  <h1>Fleet OT Client — <span style="text-transform:capitalize">__ROBOT__</span></h1>
  <div class="sub">OPC UA endpoint: __ENDPOINT__</div>
  <div class="card viz">
    <div class="cap"><span>Live arm (3D · mirrors reported state)</span><span id="vizHealth"></span></div>
    <div class="scene" id="scene"></div>
    <div class="offbadge">ARM OFFLINE</div>
    <div class="hint">drag to orbit · scroll to zoom</div>
  </div>
  <div class="card" id="joints">connecting…</div>
  <div class="card">
    <div class="row">
      <button class="btn-center" onclick="centerAll()">Center all (90°)</button>
      <button class="btn-stop" id="stopBtn" onclick="toggleStop()">SAFE-STOP</button>
    </div>
    <div class="status" id="status">—</div>
  </div>
</div>
<script>
let joints=[], dragging={}, stopOn=false, latest={};
const $=(id)=>document.getElementById(id);
const num=(v,d)=>(v===undefined||v===null||isNaN(v))?d:Number(v);
function renderArm(a){ if(window.updateArm3D) window.updateArm3D(a); }
async function api(path,opts){
  const r=await fetch(path,opts);
  const j=await r.json().catch(()=>({}));
  if(!r.ok) throw new Error(j.error||r.statusText);
  return j;
}
async function setJoint(j,angle){
  await api('/api/set',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({joint:j,angle:Number(angle)})});
}
async function build(){
  const s=await api('/api/state');
  joints=Object.keys(s.target);
  const c=$('joints'); c.innerHTML='';
  for(const j of joints){
    const row=document.createElement('div'); row.className='joint';
    row.innerHTML=`<div class="name">${j}</div>
      <input type="range" min="0" max="180" step="1" id="sl-${j}" value="${s.target[j]}">
      <div class="val" id="v-${j}">${s.target[j].toFixed(0)}°</div>
      <div class="st" id="st-${j}">↺ ${s.state[j].toFixed(0)}°</div>`;
    c.appendChild(row);
    const sl=$(`sl-${j}`);
    sl.addEventListener('input',()=>{dragging[j]=true; $(`v-${j}`).textContent=sl.value+'°'; latest[j]=Number(sl.value); renderArm(latest);});
    sl.addEventListener('change',async()=>{dragging[j]=false; try{await setJoint(j,sl.value);}catch(e){}});
  }
  latest=Object.assign({},s.state);
  renderArm(latest);
}
async function poll(){
  try{
    const s=await api('/api/state');
    for(const j of joints){
      $(`st-${j}`).textContent='↺ '+s.state[j].toFixed(0)+'°';
      if(!dragging[j]){ $(`sl-${j}`).value=s.target[j]; $(`v-${j}`).textContent=s.target[j].toFixed(0)+'°'; }
      if(!dragging[j]) latest[j]=s.state[j];
    }
    renderArm(latest);
    stopOn=s.safe_stop;
    const online=s.online!==false;
    const viz=document.querySelector('.viz');
    viz.classList.toggle('off',!online);
    for(const j of joints){ $(`sl-${j}`).disabled=!online; }
    const h=$('vizHealth');
    h.textContent = !online ? 'OFFLINE' : (stopOn?'SAFE-STOP ON':'online');
    h.style.color = (!online||stopOn) ? 'var(--bad)' : 'var(--ok)';
    const b=$('stopBtn'); b.classList.toggle('armed',stopOn);
    b.textContent=stopOn?'SAFE-STOP is ON — click to clear':'SAFE-STOP';
    $('status').textContent=(online?'connected':'ARM OFFLINE — gateway up, agent not reporting')+' · updated '+new Date().toLocaleTimeString();
  }catch(e){ $('status').textContent='gateway unreachable: '+e.message; }
}
async function centerAll(){ for(const j of joints){ try{await setJoint(j,90);}catch(e){} } }
async function toggleStop(){ try{ await api('/api/safe_stop',{method:'POST',
  headers:{'Content-Type':'application/json'},body:JSON.stringify({on:!stopOn})}); }catch(e){} poll(); }
(async()=>{ try{await build();}catch(e){ $('joints').textContent='cannot reach gateway: '+e.message; }
  setInterval(poll,800); })();
</script>
<script type="importmap">
{ "imports": {
  "three": "https://cdn.jsdelivr.net/npm/three@0.160.0/build/three.module.js",
  "three/addons/": "https://cdn.jsdelivr.net/npm/three@0.160.0/examples/jsm/"
} }
</script>
<script type="module">
import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';

const host = document.getElementById('scene');
const d2r = THREE.MathUtils.degToRad;

const scene = new THREE.Scene();
scene.background = new THREE.Color(0x0c1524);

const camera = new THREE.PerspectiveCamera(45, host.clientWidth/host.clientHeight, 0.1, 100);
camera.position.set(3.2, 2.6, 3.4);

const renderer = new THREE.WebGLRenderer({ antialias:true });
renderer.setPixelRatio(window.devicePixelRatio);
renderer.setSize(host.clientWidth, host.clientHeight);
host.appendChild(renderer.domElement);

const controls = new OrbitControls(camera, renderer.domElement);
controls.enablePan = false;
controls.minDistance = 2.5;
controls.maxDistance = 9;
controls.maxPolarAngle = Math.PI * 0.49;
controls.target.set(0, 1.1, 0);
controls.update();

// lights
scene.add(new THREE.HemisphereLight(0xbfd4ff, 0x1a2233, 1.0));
const key = new THREE.DirectionalLight(0xffffff, 1.1);
key.position.set(4, 6, 3);
scene.add(key);

// ground grid + floor pad
const grid = new THREE.GridHelper(10, 20, 0x2a3b57, 0x1c273c);
grid.material.opacity = 0.5; grid.material.transparent = true;
scene.add(grid);

const mat = (c) => new THREE.MeshStandardMaterial({ color:c, metalness:0.25, roughness:0.55 });
const COL = { base:0x33405c, post:0x3a4a6a, upper:0x4f8cff, fore:0x6ba0ff, jaw:0x9ec2ff, joint:0xe6ebf5 };
const joint = (r) => new THREE.Mesh(new THREE.SphereGeometry(r, 20, 16), mat(COL.joint));

// --- kinematic chain (Y up) ---
// baseGroup: yaw about Y carries the whole arm around
const baseGroup = new THREE.Group();
scene.add(baseGroup);

const basePad = new THREE.Mesh(new THREE.CylinderGeometry(0.85, 0.95, 0.18, 40), mat(COL.base));
basePad.position.y = 0.09; baseGroup.add(basePad);
const turntable = new THREE.Mesh(new THREE.CylinderGeometry(0.6, 0.6, 0.12, 40), mat(COL.post));
turntable.position.y = 0.24; baseGroup.add(turntable);
const post = new THREE.Mesh(new THREE.CylinderGeometry(0.18, 0.2, 0.7, 24), mat(COL.post));
post.position.y = 0.6; baseGroup.add(post);
// a marker so base yaw is obvious even when the arm is centered
const marker = new THREE.Mesh(new THREE.BoxGeometry(0.12, 0.06, 0.5), mat(COL.upper));
marker.position.set(0, 0.33, 0.42); baseGroup.add(marker);

// shoulder pivot on top of the post
const L1 = 1.15, L2 = 0.95;
const shoulder = new THREE.Group();
shoulder.position.set(0, 0.95, 0);
baseGroup.add(shoulder);
shoulder.add(joint(0.15));
const upper = new THREE.Mesh(new THREE.BoxGeometry(0.2, L1, 0.2), mat(COL.upper));
upper.position.y = L1/2; shoulder.add(upper);

// elbow at the end of the upper arm
const elbow = new THREE.Group();
elbow.position.y = L1;
shoulder.add(elbow);
elbow.add(joint(0.12));
const fore = new THREE.Mesh(new THREE.BoxGeometry(0.16, L2, 0.16), mat(COL.fore));
fore.position.y = L2/2; elbow.add(fore);

// gripper at the end of the forearm: two jaws that open/close
const wrist = new THREE.Group();
wrist.position.y = L2;
elbow.add(wrist);
wrist.add(joint(0.09));
const jawA = new THREE.Mesh(new THREE.BoxGeometry(0.05, 0.26, 0.08), mat(COL.jaw));
const jawB = new THREE.Mesh(new THREE.BoxGeometry(0.05, 0.26, 0.08), mat(COL.jaw));
jawA.position.set(0, 0.13, 0); jawB.position.set(0, 0.13, 0);
wrist.add(jawA); wrist.add(jawB);

// smoothed joint state (radians / metres)
const cur = { yaw:0, pitch:d2r(90), elbow:0, jaw:0.06 };
const tgt = { ...cur };

window.updateArm3D = (a) => {
  if(!a) return;
  const g = (v,d)=> (v===undefined||v===null||isNaN(v)) ? d : Number(v);
  // Shoulder joint limit: below PITCH_MIN the upper arm would swing down into
  // the base/turntable, which no real shoulder can do. Clamp the rendered pose.
  const PITCH_MIN = 56;
  tgt.yaw   = d2r(g(a.base,90) - 90);        // base -> yaw about Y
  tgt.pitch = d2r(180 - Math.max(g(a.pitch,90), PITCH_MIN)); // higher pitch -> more upright
  tgt.elbow = d2r(180 - g(a.reach,90));      // reach -> forearm bend (relative)
  tgt.jaw   = 0.03 + (g(a.gripper,90)/180) * 0.22; // gripper -> jaw gap
};

function onResize(){
  const w = host.clientWidth, h = host.clientHeight;
  camera.aspect = w/h; camera.updateProjectionMatrix();
  renderer.setSize(w, h);
}
window.addEventListener('resize', onResize);

function animate(){
  requestAnimationFrame(animate);
  const k = 0.15; // critically-ish damped lerp toward target
  cur.yaw   += (tgt.yaw   - cur.yaw)   * k;
  cur.pitch += (tgt.pitch - cur.pitch) * k;
  cur.elbow += (tgt.elbow - cur.elbow) * k;
  cur.jaw   += (tgt.jaw   - cur.jaw)   * k;
  baseGroup.rotation.y = cur.yaw;
  shoulder.rotation.x  = cur.pitch;
  elbow.rotation.x     = cur.elbow;
  jawA.position.z =  cur.jaw/2;
  jawB.position.z = -cur.jaw/2;
  controls.update();
  renderer.render(scene, camera);
}
animate();
</script>
</body>
</html>
"""
