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
            return {"state": state, "target": target,
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
</style>
</head>
<body>
<div class="wrap">
  <h1>Fleet OT Client — <span style="text-transform:capitalize">__ROBOT__</span></h1>
  <div class="sub">OPC UA endpoint: __ENDPOINT__</div>
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
let joints=[], dragging={}, stopOn=false;
const $=(id)=>document.getElementById(id);
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
    sl.addEventListener('input',()=>{dragging[j]=true; $(`v-${j}`).textContent=sl.value+'°';});
    sl.addEventListener('change',async()=>{dragging[j]=false; try{await setJoint(j,sl.value);}catch(e){}});
  }
}
async function poll(){
  try{
    const s=await api('/api/state');
    for(const j of joints){
      $(`st-${j}`).textContent='↺ '+s.state[j].toFixed(0)+'°';
      if(!dragging[j]){ $(`sl-${j}`).value=s.target[j]; $(`v-${j}`).textContent=s.target[j].toFixed(0)+'°'; }
    }
    stopOn=s.safe_stop;
    const b=$('stopBtn'); b.classList.toggle('armed',stopOn);
    b.textContent=stopOn?'SAFE-STOP is ON — click to clear':'SAFE-STOP';
    $('status').textContent='connected · updated '+new Date().toLocaleTimeString();
  }catch(e){ $('status').textContent='gateway unreachable: '+e.message; }
}
async function centerAll(){ for(const j of joints){ try{await setJoint(j,90);}catch(e){} } }
async function toggleStop(){ try{ await api('/api/safe_stop',{method:'POST',
  headers:{'Content-Type':'application/json'},body:JSON.stringify({on:!stopOn})}); }catch(e){} poll(); }
(async()=>{ try{await build();}catch(e){ $('joints').textContent='cannot reach gateway: '+e.message; }
  setInterval(poll,800); })();
</script>
</body>
</html>
"""
