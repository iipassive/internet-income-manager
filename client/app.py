#!/usr/bin/env python3
"""Internet Income Manager v12"""
import os,json,subprocess,uuid,secrets,re,hashlib,time,shlex,urllib.request,socket,threading
from datetime import datetime
from flask import Flask,render_template,request,jsonify

app=Flask(__name__)
app.secret_key=secrets.token_hex(32)
BASE=os.path.dirname(os.path.abspath(__file__))
DATA=os.path.join(BASE,"data")
os.makedirs(DATA,exist_ok=True)
CFG=os.path.join(DATA,"config.json")
PXF=os.path.join(DATA,"proxies.json")
ACF=os.path.join(DATA,"accounts.json")
CIF=os.path.join(DATA,"containers.json")  # Track container metadata (UUIDs etc)
RLF=os.path.join(DATA,"ref_links.json")
PGF=os.path.join(DATA,"proxy_groups.json")
CAF=os.path.join(DATA,"custom_apps.json")
PXLIVE=os.path.join(DATA,"proxy_live.json")
CHECKER_IMAGE="curlimages/curl:latest"
CLIENT_LAST_REPORT=0
# Local client version — bumped each release. Compared against /api/version on server.
CLIENT_VERSION="2.4.3"
CLIENT_BUILD=15

# Stale-while-revalidate cache so slow ops (`docker ps -a` over hundreds of
# containers can take 20+s) never block UI: serve last value instantly,
# refresh in background. Only the FIRST request after a cold start pays the cost.
_CACHE={}
_CACHE_LOCKS={}
_CACHE_META_LOCK=threading.Lock()
def _key_lock(k):
    with _CACHE_META_LOCK:
        lk=_CACHE_LOCKS.get(k)
        if lk is None:lk=_CACHE_LOCKS[k]=threading.Lock()
        return lk

def _cached(key,ttl,fn):
    now=time.time();v=_CACHE.get(key)
    if v is not None:
        if v[0]>now:return v[1]            # fresh
        # Stale: spawn a single-flight background refresh, return stale data NOW
        lk=_key_lock(key)
        if lk.acquire(blocking=False):
            def _refresh():
                try:
                    val=fn();_CACHE[key]=(time.time()+ttl,val)
                except Exception:pass
                finally:lk.release()
            threading.Thread(target=_refresh,daemon=True).start()
        return v[1]
    # Cold (no entry): compute synchronously
    val=fn();_CACHE[key]=(now+ttl,val);return val

def _cache_drop(*keys):
    for k in keys:_CACHE.pop(k,None)

# Background deploy jobs — track progress so frontend can show progress bar
JOBS={}
JOBS_LOCK=threading.Lock()
def job_create(total,label=""):
    jid="job-"+secrets.token_hex(6)
    with JOBS_LOCK:
        JOBS[jid]={"id":jid,"total":total,"done":0,"ok_count":0,"results":[],"status":"running","label":label,"started":time.time()}
    # Garbage-collect old finished jobs (>10min)
    cutoff=time.time()-600
    with JOBS_LOCK:
        for old in [k for k,v in JOBS.items() if v.get("finished",0) and v["finished"]<cutoff]:
            JOBS.pop(old,None)
    return jid
def job_step(jid,result):
    with JOBS_LOCK:
        j=JOBS.get(jid)
        if not j:return
        j["done"]+=1
        if result.get("ok"):j["ok_count"]+=1
        # Track ii (real app containers) separately so UI can hide tun infrastructure
        name=result.get("name","")
        if name.startswith("ii-"):
            j["ii_done"]=j.get("ii_done",0)+1
            if result.get("ok"):j["ii_ok"]=j.get("ii_ok",0)+1
        # Keep only last 50 result objects to bound memory
        j["results"].append(result)
        if len(j["results"])>200:j["results"]=j["results"][-200:]
        if j["done"]>=j["total"]:
            j["status"]="completed"
            j["finished"]=time.time()
def job_get(jid):
    with JOBS_LOCK:return dict(JOBS.get(jid)) if jid in JOBS else None

@app.errorhandler(404)
def e404(e):return jsonify(error="Not found"),404
@app.errorhandler(500)
def e500(e):return jsonify(error=str(e)),500
@app.errorhandler(Exception)
def eall(e):return jsonify(error=str(e)),500
@app.after_request
def no_cache(r):
    r.headers["Cache-Control"]="no-store, no-cache, must-revalidate, max-age=0"
    r.headers["Pragma"]="no-cache"
    r.headers["Expires"]="0"
    return r

TUN_IMAGE="ghcr.io/tun2proxy/tun2proxy:v0.7.19"

DOCKER_CMDS={
"iproyal":"docker run -d --name {name} {net} --restart=no iproyal/pawns-cli:latest -email={email} -password={password} -device-name={device} -accept-tos",
"packetstream":"docker run -d --name {name} {net} --restart=no -e CID={cid} packetstream/psclient:latest",
"grass":"docker run -d --name {name} {net} --restart=no -e GRASS_USER={email} -e GRASS_PASS={password} mrcolorrain/grass",
"traffmonetizer":"docker run -d --name {name} {net} --restart=no traffmonetizer/cli_v2 start accept --token {token}",
"repocket":"docker run -d --name {name} {net} --restart=no -e RP_EMAIL={email} -e RP_API_KEY={api_key} repocket/repocket:latest",
"peer2profit":"docker run -d --name {name} {net} --restart=no -e P2P_EMAIL={email} enwaiax/peer2profit:latest",
"proxyrack":"docker run -d --name {name} {net} --restart=no --entrypoint /bin/sh -e UUID={uuid} -e API_KEY={api_key} -e DEVICE_NAME={device} -v {name}-data:/app/Roaming proxyrack/pop:latest -c \"mkdir -p /app/Roaming/PoP && :> /app/go_version.txt && exec /bin/bash run.sh\"",
"bitping":"docker run -d --name {name} {net} --restart=no --entrypoint /bin/bash bitping/bitpingd:latest -c \"/app/bitpingd login -e {email} -p {password} && exec /app/docker.sh\"",
"earnfm":"docker run -d --name {name} {net} --restart=no -e EARNFM_TOKEN={api_key} earnfm/earnfm-client:latest",
"proxylite":"docker run -d --name {name} {net} --restart=no -e USER_ID={user_id} proxylite/proxyservice:latest",
"wipter":"docker run -d --name {name} {net} --restart=no -e WIPTER_EMAIL={email} -e WIPTER_PASSWORD={password} techroy23/docker-wipter:latest",
"ebesucher":"docker run -d --name {name} {net} --restart=no -e EBESUCHER_USERNAME={username} ebesucher/ebesucher",
"antgain":"docker run -d --name {name} {net} --restart=no -e ANTGAIN_API_KEY={api_key} antgain/antgain",
"urnetwork":"docker run -d --name {name} {net} --restart=no -e UR_AUTH_TOKEN={auth_token} urnetwork/urnetwork",
}

APPS={
"iproyal":{"name":"IPRoyal Pawns","icon":"👑","color":"#8b5cf6",
  "fields":[{"key":"email","label":"Email","type":"email"},{"key":"password","label":"Password","type":"password"}],
  "desc":"Residential proxy","pay":"Crypto, PayPal","limit":0},
"packetstream":{"name":"PacketStream","icon":"📦","color":"#3b82f6",
  "fields":[{"key":"cid","label":"CID","type":"text"}],"desc":"Bandwidth marketplace","pay":"PayPal","limit":0},
"grass":{"name":"Grass","icon":"🌿","color":"#22c55e",
  "fields":[{"key":"email","label":"Email","type":"email"},{"key":"password","label":"Password","type":"password"}],
  "desc":"Web scraping","pay":"Crypto","limit":0},
"traffmonetizer":{"name":"TraffMonetizer","icon":"🚦","color":"#ef4444",
  "fields":[{"key":"token","label":"Token","type":"text"}],"desc":"Traffic monetization","pay":"Crypto","limit":0},
"repocket":{"name":"Repocket","icon":"🔄","color":"#06b6d4",
  "fields":[{"key":"email","label":"Email","type":"email"},{"key":"api_key","label":"API Key","type":"text"}],
  "desc":"Bandwidth + API","pay":"PayPal, Crypto","limit":0},
"peer2profit":{"name":"Peer2Profit","icon":"🤝","color":"#a855f7",
  "fields":[{"key":"email","label":"Email","type":"email"}],"desc":"P2P bandwidth","pay":"Crypto","limit":0},
"proxyrack":{"name":"ProxyRack","icon":"🗄️","color":"#f97316",
  "fields":[{"key":"api_key","label":"API Key","type":"text"}],"desc":"Proxy contributor","pay":"PayPal","limit":500},
"bitping":{"name":"BitPing","icon":"📡","color":"#14b8a6",
  "fields":[{"key":"email","label":"Email","type":"email"},{"key":"password","label":"Password","type":"password"}],
  "desc":"Network monitoring","pay":"Crypto","limit":0},
"earnfm":{"name":"EarnFM","icon":"📻","color":"#ec4899",
  "fields":[{"key":"api_key","label":"API Key","type":"text"}],"desc":"Bandwidth monetization","pay":"Crypto, PayPal","limit":0},
"proxylite":{"name":"ProxyLite","icon":"⚡","color":"#eab308",
  "fields":[{"key":"user_id","label":"User ID","type":"text"}],"desc":"Proxy sharing","pay":"Crypto","limit":0},
"wipter":{"name":"Wipter","icon":"🌐","color":"#0ea5e9",
  "fields":[{"key":"email","label":"Email","type":"email"},{"key":"password","label":"Password","type":"password"}],
  "desc":"Residential bandwidth","pay":"Crypto","limit":0},
"ebesucher":{"name":"Ebesucher","icon":"🖥️","color":"#64748b",
  "fields":[{"key":"username","label":"Username","type":"text"}],"desc":"Surf exchange","pay":"PayPal","limit":0},
"antgain":{"name":"AntGain","icon":"🐜","color":"#b45309",
  "fields":[{"key":"api_key","label":"API Key","type":"text"}],"desc":"Bandwidth sharing","pay":"Crypto","limit":0},
"urnetwork":{"name":"URnetwork","icon":"🔗","color":"#dc2626",
  "fields":[{"key":"auth_token","label":"Auth Token","type":"text"}],"desc":"Decentralized network","pay":"Crypto","limit":0},
}

def lj(p,d):
    try:
        if os.path.exists(p):
            with open(p) as f:return json.load(f)
    except:pass
    return d if isinstance(d,list) else(d.copy() if isinstance(d,dict) else d)
def sj(p,d):
    with open(p,"w") as f:json.dump(d,f,indent=2)
def cfg():
    c={**{"device_name":"ubuntu","server_url":"http://mainsite.vinaproxy.net:18881","license_key":"","client_id":"","hidden_apps":[],
         # Boot-time auto-start: "off" = stay stopped, user starts manually; "sequential" = ii-manager starts them with rate limit
         "auto_start_on_boot":"sequential","boot_start_batch":3,"boot_start_delay":2.0,"boot_start_grace":30},**lj(CFG,{})}
    if not c.get("client_id"):
        c["client_id"]="iim-"+hashlib.md5((socket.gethostname()+str(time.time())+secrets.token_hex(8)).encode()).hexdigest()[:12]
        scfg(c)
    return c
def scfg(c):sj(CFG,c)
def pxs():return lj(PXF,[])
def spx(p):sj(PXF,p)
def acs():
    data=lj(ACF,{})
    chg=False
    for k,al in data.items():
        for a in al:
            if "id" not in a:a["id"]=str(uuid.uuid4())[:8];chg=True
            pids=a.get("proxy_ids",[])
            clean=[p for p in pids if p and p!="undefined" and p!="null"]
            if len(clean)!=len(pids):a["proxy_ids"]=clean;chg=True
    if chg:sj(ACF,data)
    return data
def sacs(a):sj(ACF,a)

# Container metadata store (tracks earnapp UUIDs, proxy mapping etc)
# Use a lock to prevent race conditions when multiple deploy threads modify in parallel
_CT_META_LOCK=threading.Lock()
def ct_meta():return lj(CIF,{})
def sct_meta(d):sj(CIF,d)
def update_ct_meta(name,**kwargs):
    """Atomic update of a single container's metadata. Safe for concurrent callers."""
    with _CT_META_LOCK:
        d=lj(CIF,{})
        if name not in d:d[name]={}
        d[name].update(kwargs)
        sj(CIF,d)
def get_ct_meta(name):
    with _CT_META_LOCK:
        return lj(CIF,{}).get(name,{})

# Proxy groups: store list of {id,name,proxy_ids,created}
def pgs():
    d=lj(PGF,{"groups":[]})
    if isinstance(d,list):d={"groups":d}
    return d.get("groups",[])
def spg(g):sj(PGF,{"groups":g})

# Custom Docker apps (user-defined). Each entry:
# {key, name, icon, color, desc, pay, limit, fields:[{key,label,type}], cmd_template, _custom:true}
def custom_apps_list():
    d=lj(CAF,{"apps":[]})
    return d.get("apps",[])
def save_custom_apps(arr):sj(CAF,{"apps":arr})

def reload_custom_apps():
    """Merge persisted custom apps into APPS + DOCKER_CMDS at runtime."""
    # Drop previously loaded custom entries
    for k in [k for k,v in APPS.items() if v.get("_custom")]:
        APPS.pop(k,None);DOCKER_CMDS.pop(k,None)
    for c in custom_apps_list():
        k=c.get("key");
        if not k or not c.get("cmd_template"):continue
        if k in APPS and not APPS[k].get("_custom"):continue  # don't overwrite built-in
        APPS[k]={
            "name":c.get("name") or k,
            "icon":c.get("icon") or "🛠️",
            "color":c.get("color") or "#64748b",
            "fields":c.get("fields") or [],
            "desc":c.get("desc") or "Custom Docker app",
            "pay":c.get("pay") or "-",
            "limit":int(c.get("limit") or 0),
            "_custom":True,
        }
        DOCKER_CMDS[k]=c["cmd_template"]

reload_custom_apps()

def gen_uuid():
    return str(uuid.uuid4())

def image_for_app(k):
    if k not in DOCKER_CMDS:return ""
    vals={"name":"x","net":"","device":"x","email":"x","password":"x","token":"x","cid":"x",
          "api_key":"x","user_id":"x","account_id":"x","auth_token":"x","username":"x"}
    try:parts=shlex.split(DOCKER_CMDS[k].format(**vals))
    except Exception:return ""
    opts_with_val={"--name","--network","--restart","--device","--cap-add","--sysctl","--dns","-e","--entrypoint","-v"}
    skip=0
    for p in parts[2:]:
        if skip:skip-=1;continue
        if p in opts_with_val:skip=1;continue
        if p.startswith("--name=") or p.startswith("--network=") or p.startswith("--restart=") or p.startswith("--device=") or p.startswith("--cap-add=") or p.startswith("--sysctl=") or p.startswith("--dns="):
            continue
        if p=="-d" or p.startswith("-e"):
            continue
        if not p.startswith("-"):return p
    return ""

def _image_info_raw(img):
    try:
        r=subprocess.run(["docker","image","inspect",img,"--format","{{.Id}}|{{.Created}}"],
            capture_output=True,text=True,timeout=10)
        if r.returncode!=0:return {"image":img,"pulled":False}
        iid,created=(r.stdout.strip().split("|",1)+[""])[:2]
        return {"image":img,"pulled":True,"id":iid.replace("sha256:","")[:12],"created":created[:19]}
    except Exception as e:return {"image":img,"pulled":False,"error":str(e)}
def image_info(img):
    if not img:return {"image":"","pulled":False}
    return _cached(f"img:{img}",60,lambda:_image_info_raw(img))

# ==== Shared tun: 1 tun per proxy, reused across apps ====
def proxy_tun_name(proxy_id):
    """Stable container name for a proxy's shared tun."""
    return f"tun-px-{proxy_id}"

def ensure_proxy_tun(proxy_id,proxy_url):
    """Idempotent: ensure tun-px-{proxy_id} exists and running with given proxy URL.
    Returns (ok, tun_name, msg)."""
    if not proxy_id or not proxy_url:return False,"","missing proxy id/url"
    name=proxy_tun_name(proxy_id)
    # Check existing
    r=subprocess.run(["docker","inspect","--format","{{.State.Status}}|{{index .Config.Cmd 1}}",name],
        capture_output=True,text=True,timeout=10)
    if r.returncode==0:
        parts=(r.stdout.strip().split("|")+["",""])[:2]
        status=parts[0];current_url=parts[1] if len(parts)>1 else ""
        # If URL changed → recreate
        if proxy_url and current_url and current_url!=proxy_url:
            subprocess.run(["docker","rm","-f",name],capture_output=True,text=True,timeout=15)
        elif status!="running":
            subprocess.run(["docker","start",name],capture_output=True,text=True,timeout=15)
            return True,name,"started"
        else:return True,name,"already running"
    # Create new
    cmd=f"docker run -d --name {name} --restart=no --device=/dev/net/tun --cap-add=NET_ADMIN --sysctl net.ipv6.conf.all.disable_ipv6=1 --dns 1.1.1.1 --dns 8.8.8.8 {TUN_IMAGE} --proxy {proxy_url} --dns over-tcp --exit-on-fatal-error"
    r=subprocess.run(cmd,shell=True,capture_output=True,text=True,timeout=60)
    _cache_drop("allct")
    if r.returncode!=0:return False,name,(r.stdout+r.stderr).strip()[-200:]
    time.sleep(1)
    return True,name,"created"

def remove_proxy_tun(proxy_id):
    """Remove tun for a proxy (called when proxy deleted/disabled)."""
    name=proxy_tun_name(proxy_id)
    subprocess.run(["docker","rm","-f",name],capture_output=True,text=True,timeout=15)
    _cache_drop("allct")

def _startup_tun_sync():
    """On service start, ensure every enabled proxy has its shared tun running.
    Existing tuns are detected idempotently; only missing ones get spawned."""
    time.sleep(5)  # let Flask + Docker settle
    enabled=[(p["id"],p["url"]) for p in pxs() if p.get("enabled",True) and p.get("url")]
    if not enabled:return
    spawned=0
    for pid,url in enabled:
        try:
            tn=proxy_tun_name(pid)
            r=subprocess.run(["docker","inspect","--format","{{.State.Status}}",tn],capture_output=True,text=True,timeout=8)
            if r.returncode==0 and "running" in r.stdout:continue
            ok,_,_=ensure_proxy_tun(pid,url)
            if ok:spawned+=1
            time.sleep(0.2)
        except Exception:continue
    if spawned:
        print(f"[startup-sync] spawned {spawned}/{len(enabled)} missing proxy tuns",flush=True)
threading.Thread(target=_startup_tun_sync,daemon=True).start()

# Pre-warm the docker-ps cache at startup so the very first page load is instant
# (avoids the 20s+ cold call when there are hundreds of containers).
def _warm_caches():
    try:allct()
    except Exception:pass
threading.Thread(target=_warm_caches,daemon=True).start()

# ==== Boot-time auto-start orchestrator ====
# Docker's RestartPolicy is "no" — so on host reboot, all containers stay stopped.
# We sequentially start them here, throttled, so docker daemon isn't crushed by
# 800+ parallel container starts (which caused load avg >100 in earlier sessions).
BOOT_ORCH={"status":"idle","total":0,"done":0,"ok":0,"started_at":None}
BOOT_LOCK=threading.Lock()

def _seed_intended_state():
    """One-time migration: if ct_meta entries lack intended_state, infer from
    current docker status (running → 'running', stopped → 'stopped')."""
    try:
        meta=ct_meta();ct=allct()
        running={c["name"] for c in ct if c.get("running")}
        chg=False
        for n,m in meta.items():
            if "intended_state" not in m:
                m["intended_state"]="running" if n in running else "stopped"
                chg=True
        if chg:sct_meta(meta)
    except Exception:pass

def _boot_auto_start():
    """Sequentially start ii-* and tun-* containers whose intended_state=running but
    are currently stopped. Respects cfg.auto_start_on_boot setting."""
    c=cfg()
    mode=c.get("auto_start_on_boot","sequential")
    if mode=="off":
        BOOT_ORCH["status"]="disabled";return
    grace=float(c.get("boot_start_grace",30))
    time.sleep(grace)  # let docker daemon settle
    _seed_intended_state()
    meta=ct_meta()
    # List containers that should be running (intended_state=running) AND are currently stopped
    try:cur={c["name"]:c.get("running",False) for c in allct()}
    except Exception:cur={}
    want=[n for n,m in meta.items() if m.get("intended_state")=="running" and not cur.get(n,False)]
    # Also include shared tuns referenced by want list (their intended state isn't tracked separately)
    tuns_needed=set()
    for n in want:
        if n.startswith("ii-"):
            # Lookup proxy_url → derive tun name via existing helper
            pu=meta.get(n,{}).get("proxy_url","")
            if pu and pu!="direct":
                pid=_proxy_id_from_url(pu)
                if pid:tuns_needed.add(proxy_tun_name(pid))
    # Add stopped tuns that we need to start first
    for tn in tuns_needed:
        if tn not in want and not cur.get(tn,False):want.insert(0,tn)
    # Order: tuns first, then ii-*
    want=sorted(set(want),key=lambda n:(0 if n.startswith("tun-") else 1,n))
    if not want:
        BOOT_ORCH["status"]="idle";return
    batch=max(1,int(c.get("boot_start_batch",3)))
    delay=float(c.get("boot_start_delay",2.0))
    with BOOT_LOCK:
        BOOT_ORCH.update({"status":"running","total":len(want),"done":0,"ok":0,
            "started_at":datetime.utcnow().isoformat()+"Z","batch":batch,"delay":delay})
    print(f"[boot-start] {len(want)} containers to start (batch={batch}, delay={delay}s)",flush=True)
    i=0
    while i<len(want):
        chunk=want[i:i+batch]
        threads=[]
        for n in chunk:
            t=threading.Thread(target=_boot_start_one,args=(n,),daemon=True)
            t.start();threads.append(t)
        for t in threads:t.join(timeout=30)
        i+=batch
        with BOOT_LOCK:BOOT_ORCH["done"]=min(i,len(want))
        if i<len(want):time.sleep(delay)
    with BOOT_LOCK:BOOT_ORCH["status"]="done"
    _cache_drop("allct")
    print(f"[boot-start] complete: {BOOT_ORCH['ok']}/{BOOT_ORCH['total']} started",flush=True)

def _boot_start_one(name):
    try:
        r=subprocess.run(["docker","start",name],capture_output=True,text=True,timeout=30)
        if r.returncode==0:
            with BOOT_LOCK:BOOT_ORCH["ok"]+=1
    except Exception:pass

def _proxy_id_from_url(url):
    """Resolve proxy_id from URL by looking it up in proxies.json."""
    try:
        for p in pxs():
            if p.get("url")==url:return p.get("id")
    except Exception:pass
    return None

threading.Thread(target=_boot_auto_start,daemon=True).start()

# ==== Proxy live check (curl ifconfig.me through the shared tun namespace) ====
def load_live_results():return lj(PXLIVE,{})
def save_live_results(d):sj(PXLIVE,d)

def check_proxy_live(pid):
    """Return {alive,ip,checked_at,err}. Times out at 12s."""
    tun=proxy_tun_name(pid)
    out={"alive":False,"ip":None,"checked_at":int(time.time()),"err":""}
    # Skip if tun not running
    r=subprocess.run(["docker","inspect","--format","{{.State.Status}}",tun],capture_output=True,text=True,timeout=5)
    if r.returncode!=0 or "running" not in r.stdout:
        out["err"]="tun not running";return out
    try:
        r=subprocess.run(["docker","run","--rm","--network",f"container:{tun}",CHECKER_IMAGE,
                          "-s","--max-time","6","--connect-timeout","4","https://ifconfig.me/ip"],
                         capture_output=True,text=True,timeout=15)
        if r.returncode==0:
            ip=(r.stdout or "").strip()
            if ip and 7<=len(ip)<=45 and ip.count(".")>=3 and not any(c.isspace() for c in ip):
                out["alive"]=True;out["ip"]=ip
            else:out["err"]="bad ip: "+ip[:40]
        else:
            out["err"]=(r.stderr or r.stdout or "").strip()[-120:] or "curl failed"
    except subprocess.TimeoutExpired:out["err"]="timeout"
    except Exception as e:out["err"]=str(e)[:120]
    return out

def check_all_proxies_live():
    """Check all enabled proxies sequentially. Stores result to PXLIVE file."""
    enabled=[p["id"] for p in pxs() if p.get("enabled",True)]
    results=load_live_results()
    # Drop entries for proxies that no longer exist
    valid_ids=set(p["id"] for p in pxs())
    for stale in [k for k in list(results.keys()) if k not in valid_ids]:results.pop(stale,None)
    for i,pid in enumerate(enabled):
        results[pid]=check_proxy_live(pid)
        # Save every 20 proxies so frontend sees partial progress
        if (i+1)%20==0:save_live_results(results)
        time.sleep(0.3)  # gentle rate
    save_live_results(results)

def _live_check_loop():
    time.sleep(90)  # let startup-sync finish first
    try:subprocess.run(["docker","pull",CHECKER_IMAGE],capture_output=True,timeout=180)
    except Exception:pass
    while True:
        try:check_all_proxies_live()
        except Exception as e:print(f"[live-check] error: {e}",flush=True)
        time.sleep(15*60)  # 15 minutes between full sweeps
threading.Thread(target=_live_check_loop,daemon=True).start()

def _ref_links_fetch():
    c=cfg();cached=lj(RLF,{})
    url=(c.get("server_url") or "").rstrip("/")
    if not url:return cached
    try:
        req=urllib.request.Request(url+"/api/ref-links",headers={"Accept":"application/json"})
        with urllib.request.urlopen(req,timeout=5) as r:data=json.loads(r.read().decode())
        links=data.get("links",data) if isinstance(data,dict) else {}
        if isinstance(links,dict):
            sj(RLF,links);return links
    except Exception:pass
    return cached
# refresh=True forces a network call; otherwise hits the 60s cache.
def ref_links(refresh=False):
    if refresh:_cache_drop("ref_links")
    return _cached("ref_links",60,_ref_links_fetch)

def _wallets_fetch():
    c=cfg();url=(c.get("server_url") or "").rstrip("/")
    if not url:return []
    try:
        req=urllib.request.Request(url+"/api/wallets",headers={"Accept":"application/json"})
        with urllib.request.urlopen(req,timeout=5) as r:data=json.loads(r.read().decode())
        if isinstance(data,list):return data
    except Exception:pass
    return []
def wallets():return _cached("wallets",120,_wallets_fetch)

def proxy_usage():
    a=acs();meta=ct_meta();used={}
    pmap={x.get("id"):x for x in pxs()}
    for app_key,arr in a.items():
        app_name=APPS.get(app_key,{}).get("name",app_key)
        for acc in arr:
            for pid in acc.get("proxy_ids",[]):
                if pid in pmap:
                    used.setdefault(pid,[]).append({"app":app_key,"app_name":app_name,"account":acc.get("label","Account"),"deployed":False})
    # Only show "deployed" entries for containers that actually exist on the host
    existing_cts={c["name"] for c in allct()}
    url_to_pid={x.get("url"):x.get("id") for x in pmap.values()}
    stale=[]
    for name,m in meta.items():
        if name not in existing_cts:stale.append(name);continue
        pid=url_to_pid.get(m.get("proxy_url"))
        if pid:
            used.setdefault(pid,[]).append({"app":m.get("app",""),"app_name":APPS.get(m.get("app",""),{}).get("name",m.get("app","")),"account":m.get("acc_id",""),"container":name,"deployed":True})
    # Lazy cleanup: prune metadata of containers that no longer exist
    if stale:
        for s in stale:meta.pop(s,None)
        sct_meta(meta)
    return used

def os_info():
    try:
        data={}
        with open("/etc/os-release") as f:
            for l in f:
                if "=" in l:
                    k,v=l.strip().split("=",1);data[k]=v.strip('"')
        return data.get("PRETTY_NAME") or data.get("NAME") or "Linux"
    except Exception:return "Linux"

def report_client(stats=None):
    global CLIENT_LAST_REPORT
    now=time.time()
    if now-CLIENT_LAST_REPORT<60:return
    CLIENT_LAST_REPORT=now
    c=cfg();url=(c.get("server_url") or "").rstrip("/")
    if not url:return
    try:
        p=pxs();a=acs();s=stats or sysstats()
        custom=[{"key":x.get("key"),"name":x.get("name"),"icon":x.get("icon"),
                 "image":(x.get("cmd_template") or "").split(" ")[-1] if x.get("cmd_template") else "",
                 "fields":x.get("fields") or [],"accounts":len(a.get(x.get("key"),[]))}
                for x in custom_apps_list()]
        # Send proxies list (id, url, label, enabled) — control server can view + copy
        px_list=[{"id":x.get("id"),"url":x.get("url"),"label":x.get("label",""),"enabled":x.get("enabled",True)} for x in p]
        payload={"client_id":c.get("client_id"),"device_name":c.get("device_name","ubuntu"),
            "hostname":socket.gethostname(),"os":os_info(),"version":CLIENT_VERSION,"build":CLIENT_BUILD,
            "docker":dkok(),"stats":s,"accounts":sum(len(v) for v in a.values()),
            "proxies":len(p),"proxies_enabled":sum(1 for x in p if x.get("enabled",True)),
            "apps_enabled":sum(1 for arr in a.values() for x in arr if x.get("enabled",True)),
            "custom_apps":custom,"proxies_list":px_list}
        req=urllib.request.Request(url+"/api/clients/report",data=json.dumps(payload).encode(),
            headers={"Content-Type":"application/json","Accept":"application/json"},method="POST")
        urllib.request.urlopen(req,timeout=4).read()
    except Exception:pass

def _allct_raw():
    try:
        # 60s timeout — large container counts on stressed daemons take >10s to list
        r=subprocess.run(["docker","ps","-a","--format","{{.ID}}\\t{{.Names}}\\t{{.Status}}\\t{{.Image}}"],
            capture_output=True,text=True,timeout=60)
        out=[]
        if r.returncode==0:
            for l in r.stdout.strip().split("\n"):
                if not l:continue
                p=l.split("\t")
                if len(p)>=4:
                    out.append({"id":p[0][:12],"name":p[1],"status":p[2],
                        "image":p[3],"running":"Up" in p[2],"is_tun":p[1].startswith("tun-")})
        return out
    except Exception:return []
# Cached: when daemon is healthy ~3s TTL; bumped to 8s when many containers to reduce docker-daemon load.
def allct():return _cached("allct",30.0,_allct_raw)  # 30s TTL — SWR keeps it instant after first warm

def app_containers(k):return[c for c in allct() if c["name"].startswith(f"ii-{k}-") and not c["is_tun"]]
def dkrm(n):subprocess.run(["docker","rm","-f",n],capture_output=True,text=True,timeout=15)
def dkact(a,n):
    try:
        cmd=["docker","rm","-f",n] if a=="rm" else ["docker",a,n]
        r=subprocess.run(cmd,capture_output=True,text=True,timeout=30)
        return r.returncode==0,(r.stdout+r.stderr).strip()
    except Exception as e:return False,str(e)
def dkstop(n):
    try:
        r=subprocess.run(["docker","stop",n],capture_output=True,text=True,timeout=30)
        return r.returncode==0,(r.stdout+r.stderr).strip()
    except Exception as e:return False,str(e)

def dkok():
    try:return subprocess.run(["docker","info"],capture_output=True,text=True,timeout=10).returncode==0
    except:return False

def sysstats():
    s={}
    try:
        with open("/proc/loadavg") as f:l=f.read().split();s["cpu"]=l[0]
        with open("/proc/uptime") as f:
            sec=int(float(f.read().split()[0]))
            d,rem=divmod(sec,86400);h,rem=divmod(rem,3600);m,_=divmod(rem,60)
            s["uptime"]=f"{d}d {h}h {m}m"
        r=subprocess.run(["free","-m"],capture_output=True,text=True,timeout=5)
        if r.returncode==0:
            m2=r.stdout.strip().split("\n")[1].split()
            s["mem_used"]=int(m2[2]);s["mem_total"]=int(m2[1]);s["mem_pct"]=round(int(m2[2])/int(m2[1])*100,1)
        r=subprocess.run(["df","-h","/"],capture_output=True,text=True,timeout=5)
        if r.returncode==0:d2=r.stdout.strip().split("\n")[1].split();s["disk"]=f"{d2[2]}/{d2[1]}";s["disk_pct"]=d2[4]
        try:
            with open("/sys/class/thermal/thermal_zone0/temp") as f:s["temp"]=round(int(f.read().strip())/1000,1)
        except:s["temp"]=None
        ct=[c for c in allct() if not c["is_tun"]]
        s["ct_run"]=sum(1 for c in ct if c["running"]);s["ct_tot"]=len(ct)
    except:pass
    return s

# ==================== LICENSE / TIER ENFORCEMENT ====================
# Tier limits enforced locally. Server is source of truth for which tier the
# client is in (cached on disk in data/license_cache.json + refreshed every 10min).
DEFAULT_TIERS={
    "trial":{"name":"Free Trial","max_containers_per_app":20,    "max_proxies":999999,"color":"#6b7280"},
    "basic":{"name":"Basic",     "max_containers_per_app":100,   "max_proxies":999999,"color":"#3b82f6"},
    "pro":  {"name":"Pro",       "max_containers_per_app":999999,"max_proxies":999999,"color":"#a855f7"},
}
LIC_CACHE=os.path.join(DATA,"license_cache.json")
def lic_cache():
    return lj(LIC_CACHE,{"tier":"trial","license_key":"","expires_at":"","checked_at":""})
def lic_save(d):sj(LIC_CACHE,d)

def cur_tier():
    """Return current tier dict (with name, max_containers, max_proxies, color)."""
    cache=lic_cache()
    tier_key=cache.get("tier","trial")
    # Honor expiry — fall back to trial if expired
    exp=cache.get("expires_at","")
    if exp:
        try:
            from datetime import timezone
            if datetime.fromisoformat(exp.replace("Z","+00:00"))<datetime.now(timezone.utc):
                tier_key="trial"
        except Exception:pass
    base=DEFAULT_TIERS.get(tier_key,DEFAULT_TIERS["trial"]).copy()
    base["tier"]=tier_key
    base["license_key"]=cache.get("license_key","")
    base["expires_at"]=exp
    return base

def lic_usage_per_app():
    """Return {app_key: container_count} — for tier enforcement (limit is per-app)."""
    counts={}
    try:
        for c in allct():
            if c.get("is_tun"):continue
            n=c.get("name","")
            if not n.startswith("ii-"):continue
            # Container naming: ii-<app>-<acc_id>-<idx>  → app is segment between first 2 hyphens
            parts=n.split("-",2)
            if len(parts)>=3:counts[parts[1]]=counts.get(parts[1],0)+1
    except Exception:pass
    return counts

def lic_usage_per_app_running():
    """Like lic_usage_per_app but only counts RUNNING containers (used by start-check)."""
    counts={}
    try:
        for c in allct():
            if c.get("is_tun") or not c.get("running"):continue
            n=c.get("name","")
            if not n.startswith("ii-"):continue
            parts=n.split("-",2)
            if len(parts)>=3:counts[parts[1]]=counts.get(parts[1],0)+1
    except Exception:pass
    return counts

def lic_usage():
    """Aggregate usage for the License card. Returns total + per-app breakdown."""
    per_app=lic_usage_per_app()
    return {"containers":sum(per_app.values()),"per_app":per_app,"proxies":len(pxs())}

def lic_check_can_deploy(app_key,n_new=1):
    """Check whether deploying n_new MORE <app_key> containers fits the tier per-app limit."""
    t=cur_tier()
    limit=t.get("max_containers_per_app",20)
    if limit>=999999:return True,""
    cur=lic_usage_per_app().get(app_key,0)
    if cur+n_new>limit:
        return False,f"Tier {t['name']} allows {limit} {app_key} containers max per app. You have {cur}. Upgrade your plan to deploy more."
    return True,""

def lic_check_can_start(app_key,n_new=1):
    """Check whether starting n_new MORE <app_key> containers (which already exist) fits the tier."""
    t=cur_tier()
    limit=t.get("max_containers_per_app",20)
    if limit>=999999:return True,""
    cur=lic_usage_per_app_running().get(app_key,0)
    if cur+n_new>limit:
        return False,f"Tier {t['name']} allows {limit} running {app_key} containers max per app. You currently have {cur} running. Upgrade to start more, or stop some first."
    return True,""

def lic_check_can_add_proxy(n_new=1):
    """No proxy cap on any tier — kept as a stub so call-sites don't break."""
    return True,""

def _license_refresh_loop():
    """Background: every 10 min ask control server if our license tier changed."""
    while True:
        try:
            c=cfg();url=(c.get("server_url") or "").rstrip("/")
            if url and c.get("client_id"):
                req=urllib.request.Request(f"{url}/api/license/by-client/{c['client_id']}",headers={"Accept":"application/json"})
                with urllib.request.urlopen(req,timeout=5) as r:data=json.loads(r.read().decode())
                if data.get("ok"):
                    lic_save({
                        "tier":data.get("tier","trial"),
                        "license_key":data.get("license_key",""),
                        "plan_type":data.get("plan_type",""),
                        "expires_at":data.get("expires_at",""),
                        "checked_at":datetime.utcnow().isoformat()+"Z",
                    })
        except Exception:pass
        time.sleep(600)
threading.Thread(target=_license_refresh_loop,daemon=True).start()

def deploy_one(app_key,acc,proxy_url,idx,device,proxy_id=None):
    aid=acc.get("id","x")
    app_name=f"ii-{app_key}-{aid}-{idx}"
    dkrm(app_name)
    # Cleanup legacy dedicated tun left over from pre-shared-tun architecture
    legacy_tun=f"tun-{app_key}-{aid}-{idx}"
    subprocess.run(["docker","rm","-f",legacy_tun],capture_output=True,text=True,timeout=15)

    # Setup shared tun (1 tun per proxy, reused by all apps) if proxy supplied
    if proxy_url:
        # Resolve proxy_id from URL if not provided (fallback for callers that only pass URL)
        if not proxy_id:
            for px in pxs():
                if px.get("url")==proxy_url:proxy_id=px.get("id");break
        if not proxy_id:
            # No proxy_id resolvable → fall back to dedicated tun for this deploy
            tun_name=f"tun-{app_key}-{aid}-{idx}"
            dkrm(tun_name)
            tun_cmd=f"docker run -d --name {tun_name} --restart=no --device=/dev/net/tun --cap-add=NET_ADMIN --sysctl net.ipv6.conf.all.disable_ipv6=1 --dns 1.1.1.1 --dns 8.8.8.8 {TUN_IMAGE} --proxy {proxy_url} --dns over-tcp --exit-on-fatal-error"
            r=subprocess.run(tun_cmd,shell=True,capture_output=True,text=True,timeout=120)
            if r.returncode!=0:
                return{"ok":False,"name":app_name,"proxy":proxy_url[:45],"err":"tun: "+(r.stdout+r.stderr).strip()[-150:]}
            time.sleep(2)
            net=f"--network container:{tun_name}"
        else:
            ok,tun_name,msg=ensure_proxy_tun(proxy_id,proxy_url)
            if not ok:
                return{"ok":False,"name":app_name,"proxy":proxy_url[:45],"err":"tun: "+msg}
            net=f"--network container:{tun_name}"
    else:
        net=""

    if app_key not in DOCKER_CMDS:return{"ok":False,"name":app_name,"err":"No template"}

    creds=acc.get("credentials",{})
    # For ProxyRack: auto-generate UUID (persisted per-container)
    pr_uuid=""
    if app_key=="proxyrack":
        existing=get_ct_meta(app_name)
        pr_uuid=existing.get("uuid") or gen_uuid()
        update_ct_meta(app_name,uuid=pr_uuid,proxy_url=proxy_url or"direct")

    vals={"name":app_name,"net":net,"device":f"{device}-{aid[:4]}-{idx}",
          "email":"","password":"","token":"","cid":"","api_key":"",
          "user_id":"","account_id":"","auth_token":"","username":""}
    vals["uuid"]=pr_uuid
    for k2,v2 in creds.items():vals[k2]=v2

    try:cmd=DOCKER_CMDS[app_key].format(**vals)
    except KeyError as e:return{"ok":False,"name":app_name,"err":f"Missing: {e}"}
    cmd=re.sub(r'\s+',' ',cmd).strip()

    # Save proxy mapping in metadata — atomic to survive parallel deploys
    update_ct_meta(app_name,proxy_url=proxy_url or"direct",app=app_key,acc_id=aid,intended_state="running")

    r=subprocess.run(cmd,shell=True,capture_output=True,text=True,timeout=120)
    return{"ok":r.returncode==0,"name":app_name,"proxy":(proxy_url or"direct")[:45],
           "out":(r.stdout+r.stderr).strip()[-200:]}

# ==================== ROUTES ====================
@app.route("/")
def index():return render_template("index.html")

@app.route("/api/status")
def api_status():
    try:
        a=acs();p=pxs();s=sysstats()
        report_client(s)
        return jsonify(docker=dkok(),stats=s,
            accs=sum(len(v) for v in a.values()),
            px=len(p),px_on=sum(1 for x in p if x.get("enabled",True)))
    except Exception as e:return jsonify(error=str(e)),500

@app.route("/api/apps")
def api_apps():
    try:
        a=acs();c=cfg();hidden=set(c.get("hidden_apps",[]) or [])
        # One docker ps for all apps; bucket by app key once.
        ct_buckets={}
        for ct in allct():
            if ct["is_tun"] or not ct["name"].startswith("ii-"):continue
            parts=ct["name"].split("-",2)
            if len(parts)>=3:ct_buckets.setdefault(parts[1],[]).append(ct)
        out=[]
        for k,v in APPS.items():
            al=a.get(k,[])
            fc=al[0].get("credentials",{}) if al else {}
            cts=ct_buckets.get(k,[])
            out.append(dict(key=k,**v,accs=len(al),
                en=sum(1 for x in al if x.get("enabled",True)),fc=fc,
                run=sum(1 for c in cts if c["running"]),
                err=sum(1 for c in cts if not c["running"]),tot=len(cts),
                hidden=(k in hidden)))
        return jsonify(out)
    except Exception as e:return jsonify(error=str(e)),500

@app.route("/api/apps/<k>/visibility",methods=["POST"])
def api_app_visibility(k):
    try:
        if k not in APPS:return jsonify(error="Unknown"),404
        v=bool(request.json.get("hidden",False))
        c=cfg();h=set(c.get("hidden_apps",[]) or [])
        if v:h.add(k)
        else:h.discard(k)
        c["hidden_apps"]=sorted(h)
        scfg(c)
        return jsonify(ok=True,hidden=list(h))
    except Exception as e:return jsonify(error=str(e)),500

@app.route("/api/apps/<k>/detail")
def api_detail(k):
    try:
        if k not in APPS:return jsonify(error="Unknown"),404
        d=APPS[k];al=acs().get(k,[]);p=pxs()
        pm={x.get("id",""):x for x in p if x.get("id")}
        cts_raw=[c for c in allct() if c["name"].startswith(f"ii-{k}-")]
        meta=ct_meta()
        # Enrich containers
        cts=[]
        for c in cts_raw:
            m=meta.get(c["name"],{})
            c["proxy_url"]=m.get("proxy_url")
            cts.append(c)
        ea=[]
        for a in al:
            ap=[pm[i] for i in a.get("proxy_ids",[]) if i in pm]
            acc_cts=[c for c in cts if f"-{a['id']}-" in c["name"]]
            ea.append({**a,"proxy_count":len(ap),"containers":acc_cts,
                "run":sum(1 for c in acc_cts if c["running"]),
                "errs":sum(1 for c in acc_cts if not c["running"])})
        return jsonify(d=d,accounts=ea,containers=cts,proxies=p)
    except Exception as e:return jsonify(error=str(e)),500

@app.route("/api/apps/<k>/info")
def api_app_info(k):
    try:
        if k not in APPS:return jsonify(error="Unknown"),404
        links=ref_links(refresh=True)
        img=image_for_app(k)
        cts=app_containers(k)
        return jsonify(key=k,app=APPS[k],ref_url=links.get(k,""),docker=image_info(img),
            accounts=len(acs().get(k,[])),devices=len(cts),running=sum(1 for c in cts if c["running"]))
    except Exception as e:return jsonify(error=str(e)),500

@app.route("/api/apps/<k>/pull-image",methods=["POST"])
def api_pull_image(k):
    try:
        img=image_for_app(k)
        if not img:return jsonify(error="No image"),404
        r=subprocess.run(["docker","pull",img],capture_output=True,text=True,timeout=300)
        return jsonify(ok=r.returncode==0,image=img,output=(r.stdout+r.stderr).strip()[-1200:],docker=image_info(img))
    except Exception as e:return jsonify(error=str(e)),500

@app.route("/api/apps/<k>/qsave",methods=["POST"])
def api_qsave(k):
    try:
        if k not in APPS:return jsonify(error="Unknown"),404
        cr=request.json.get("credentials",{});a=acs()
        if k not in a:a[k]=[]
        if a[k]:
            if "id" not in a[k][0]:a[k][0]["id"]=str(uuid.uuid4())[:8]
            a[k][0]["credentials"]=cr
        else:
            a[k].append({"id":str(uuid.uuid4())[:8],"label":"Account 1",
                "credentials":cr,"proxy_ids":[],"enabled":True,"created":datetime.now().isoformat()})
        sacs(a);return jsonify(ok=True)
    except Exception as e:return jsonify(error=str(e)),500

@app.route("/api/acc/<k>",methods=["POST"])
def api_add_acc(k):
    try:
        if k not in APPS:return jsonify(error="Unknown"),404
        d=request.json;a=acs()
        if k not in a:a[k]=[]
        ac={"id":str(uuid.uuid4())[:8],"label":d.get("label",f"Account {len(a[k])+1}"),
            "credentials":d.get("credentials",{}),"proxy_ids":d.get("proxy_ids",[]),
            "enabled":True,"created":datetime.now().isoformat()}
        a[k].append(ac);sacs(a);return jsonify(ok=True,account=ac)
    except Exception as e:return jsonify(error=str(e)),500

@app.route("/api/acc/<k>/<aid>",methods=["PUT"])
def api_upd_acc(k,aid):
    try:
        a=acs()
        for ac in a.get(k,[]):
            if ac["id"]==aid:
                d=request.json
                for x in["label","credentials","proxy_ids","enabled"]:
                    if x in d:ac[x]=d[x]
                sacs(a);return jsonify(ok=True)
        return jsonify(error="Not found"),404
    except Exception as e:return jsonify(error=str(e)),500

@app.route("/api/acc/<k>/<aid>",methods=["DELETE"])
def api_del_acc(k,aid):
    try:a=acs();a[k]=[x for x in a.get(k,[]) if x["id"]!=aid];sacs(a);return jsonify(ok=True)
    except Exception as e:return jsonify(error=str(e)),500

@app.route("/api/apps/<k>/deploy",methods=["POST"])
def api_deploy_app(k):
    try:
        if k not in APPS:return jsonify(error="Unknown"),404
        c=cfg();al=acs().get(k,[]);p=pxs()
        pm={x.get("id",""):x for x in p if x.get("id")}
        dev=c.get("device_name","ubuntu")
        # Build task list of (acc, proxy_url, idx)
        tasks=[]
        for acc in al:
            if not acc.get("enabled",True):continue
            pids=[pid for pid in acc.get("proxy_ids",[]) if pid in pm and pm[pid].get("enabled",True)]
            if pids:
                for i,pid in enumerate(pids):tasks.append((acc,pm[pid]["url"],i,pid))
            else:tasks.append((acc,None,0,None))
        if not tasks:return jsonify(ok=False,error="No enabled accounts/proxies"),400
        jid=job_create(len(tasks),label=f"deploy {k}")
        with JOBS_LOCK:JOBS[jid]["ii_total"]=len(tasks);JOBS[jid]["ii_done"]=0;JOBS[jid]["ii_ok"]=0
        def runner():
            for acc,proxy,i,pxid in tasks:
                try:r=deploy_one(k,acc,proxy,i,dev,proxy_id=pxid)
                except Exception as e:r={"ok":False,"err":str(e)}
                # deploy_one returns name = ii-...; force ii tracking
                if r.get("name","").startswith("ii-"):pass
                job_step(jid,r)
            _cache_drop("allct")
        threading.Thread(target=runner,daemon=True).start()
        return jsonify(ok=True,job_id=jid,total=len(tasks),ii_total=len(tasks))
    except Exception as e:return jsonify(error=str(e)),500

@app.route("/api/apps/<k>/stop",methods=["POST"])
def api_stop_app(k):
    try:
        ct=allct();n=0
        targets=[c["name"] for c in ct if c["name"].startswith(f"ii-{k}-") or c["name"].startswith(f"tun-{k}-")]
        targets.sort(key=lambda x:0 if x.startswith("ii-") else 1)
        for name in targets:
            ok,_=dkstop(name)
            if ok:n+=1
        _cache_drop("allct")
        return jsonify(ok=True,stopped=n,total=len(targets))
    except Exception as e:return jsonify(error=str(e)),500

@app.route("/api/apps/<k>/delete",methods=["POST"])
def api_delete_app(k):
    try:
        ct=allct();n=0
        for c in ct:
            if c["name"].startswith(f"ii-{k}-") or c["name"].startswith(f"tun-{k}-"):
                dkrm(c["name"]);n+=1
        _cache_drop("allct")
        return jsonify(ok=True,removed=n)
    except Exception as e:return jsonify(error=str(e)),500

@app.route("/api/apps/<k>/restart",methods=["POST"])
def api_restart_app(k):
    try:
        cts=app_containers(k)
        for c in cts:subprocess.run(["docker","restart",c["name"]],capture_output=True,text=True,timeout=30)
        return jsonify(ok=True,restarted=len(cts))
    except Exception as e:return jsonify(error=str(e)),500

# ==== Per-account actions ====
@app.route("/api/acc/<k>/<aid>/deploy",methods=["POST"])
def api_acc_deploy(k,aid):
    try:
        if k not in APPS:return jsonify(error="Unknown"),404
        c=cfg();al=acs().get(k,[]);p=pxs()
        pm={x.get("id",""):x for x in p if x.get("id")}
        dev=c.get("device_name","ubuntu")
        acc=next((a for a in al if a.get("id")==aid),None)
        if not acc:return jsonify(error="Account not found"),404
        if not acc.get("enabled",True):return jsonify(ok=False,error="Account disabled"),400
        pids=[pid for pid in acc.get("proxy_ids",[]) if pid in pm and pm[pid].get("enabled",True)]
        tasks=[(acc,pm[pid]["url"],i,pid) for i,pid in enumerate(pids)] if pids else [(acc,None,0,None)]
        ok,err=lic_check_can_deploy(k,len(tasks))
        if not ok:return jsonify(ok=False,error=err,license_blocked=True),402
        jid=job_create(len(tasks),label=f"deploy {k}/{aid}")
        with JOBS_LOCK:JOBS[jid]["ii_total"]=len(tasks);JOBS[jid]["ii_done"]=0;JOBS[jid]["ii_ok"]=0
        def runner():
            for acc_,proxy,i,pxid in tasks:
                try:r=deploy_one(k,acc_,proxy,i,dev,proxy_id=pxid)
                except Exception as e:r={"ok":False,"err":str(e)}
                job_step(jid,r)
            _cache_drop("allct")
        threading.Thread(target=runner,daemon=True).start()
        return jsonify(ok=True,job_id=jid,total=len(tasks),ii_total=len(tasks))
    except Exception as e:return jsonify(error=str(e)),500

@app.route("/api/jobs/<jid>")
def api_job_status(jid):
    j=job_get(jid)
    if not j:return jsonify(error="Not found"),404
    # Don't send full results list to frontend on every poll (only last few for debug)
    j["recent_results"]=j.get("results",[])[-3:]
    j.pop("results",None)
    # Default ii fields if missing (older jobs)
    j.setdefault("ii_done",j.get("done",0))
    j.setdefault("ii_total",j.get("total",0))
    j.setdefault("ii_ok",j.get("ok_count",0))
    return jsonify(j)

@app.route("/api/acc/<k>/<aid>/stop",methods=["POST"])
def api_acc_stop(k,aid):
    try:
        ct=allct();n=0
        pii=f"ii-{k}-{aid}-";ptun=f"tun-{k}-{aid}-"
        targets=[c["name"] for c in ct if c["name"].startswith(pii) or c["name"].startswith(ptun)]
        targets.sort(key=lambda x:0 if x.startswith("ii-") else 1)
        for name in targets:
            ok,_=dkstop(name)
            if ok:
                n+=1
                if name.startswith("ii-"):update_ct_meta(name,intended_state="stopped")
        _cache_drop("allct")
        return jsonify(ok=True,stopped=n,total=len(targets))
    except Exception as e:return jsonify(error=str(e)),500

@app.route("/api/acc/<k>/<aid>/restart",methods=["POST"])
def api_acc_restart(k,aid):
    try:
        cts=[c for c in allct() if c["name"].startswith(f"ii-{k}-{aid}-") and not c["is_tun"]]
        for c in cts:subprocess.run(["docker","restart",c["name"]],capture_output=True,text=True,timeout=30)
        _cache_drop("allct")
        return jsonify(ok=True,restarted=len(cts))
    except Exception as e:return jsonify(error=str(e)),500

# Start all stopped containers of an account, SEQUENTIALLY (one at a time, with small delay)
# Tun starts before ii (network namespace dep). Useful after server reboot or after stop.
@app.route("/api/acc/<k>/<aid>/start-all",methods=["POST"])
def api_acc_start_all(k,aid):
    try:
        ct=allct()
        ptun=f"tun-{k}-{aid}-";pii=f"ii-{k}-{aid}-"
        tuns=[c["name"] for c in ct if not c.get("running") and c["name"].startswith(ptun)]
        iis=[c["name"] for c in ct if not c.get("running") and c["name"].startswith(pii)]
        targets=tuns+iis
        if not targets:return jsonify(ok=False,error="No stopped containers for this account"),400
        # Cap how many we can actually start, given tier limit (per-app, counting already-running)
        t=cur_tier();limit=t.get("max_containers_per_app",20)
        if limit<999999:
            running_now=lic_usage_per_app_running().get(k,0)
            allowed=max(0,limit-running_now)
            if allowed<len(iis):
                if allowed<=0:
                    return jsonify(ok=False,error=f"Tier {t['name']} already at limit ({running_now}/{limit} running for {k}). Upgrade or stop some.",license_blocked=True),402
                # Trim ii list — keep tuns (they need to run first regardless)
                iis=iis[:allowed]
                targets=tuns+iis
        delay=float((request.json or {}).get("delay",0.3))
        jid=job_create(len(targets),label=f"start {k}/{aid}")
        with JOBS_LOCK:
            JOBS[jid]["tun_total"]=len(tuns);JOBS[jid]["ii_total"]=len(iis)
            JOBS[jid]["ii_done"]=0;JOBS[jid]["ii_ok"]=0
        def runner():
            for name in targets:
                try:
                    r=subprocess.run(["docker","start",name],capture_output=True,text=True,timeout=60)
                    if r.returncode==0 and name.startswith("ii-"):
                        update_ct_meta(name,intended_state="running")
                    job_step(jid,{"ok":r.returncode==0,"name":name,"out":(r.stdout+r.stderr).strip()[-160:]})
                except Exception as e:
                    job_step(jid,{"ok":False,"name":name,"err":str(e)})
                if delay>0:time.sleep(delay)
            _cache_drop("allct")
        threading.Thread(target=runner,daemon=True).start()
        return jsonify(ok=True,job_id=jid,total=len(targets),tun_total=len(tuns),ii_total=len(iis))
    except Exception as e:return jsonify(error=str(e)),500

# Bulk start specific container names (sequential, smart: tun-* started before ii-*)
@app.route("/api/ct/bulk-start",methods=["POST"])
def api_ct_bulk_start():
    try:
        d=request.json or {}
        names=d.get("names") or []
        if not names:return jsonify(error="No names"),400
        # Smart ordering: ensure tun-* started before its ii-* sibling
        tuns=set();iis=set()
        for n in names:
            if n.startswith("tun-"):tuns.add(n)
            elif n.startswith("ii-"):
                iis.add(n)
                tuns.add(n.replace("ii-","tun-",1))  # auto-add tun dep
        # Per-app license cap: trim ii list if starting these would exceed limit
        t=cur_tier();limit=t.get("max_containers_per_app",20)
        if limit<999999:
            running_per_app=lic_usage_per_app_running()
            # Group requested ii by app, keep only first N per app
            by_app={}
            for n in sorted(iis):
                parts=n.split("-",2);app=parts[1] if len(parts)>=3 else ""
                by_app.setdefault(app,[]).append(n)
            allowed_iis=set()
            dropped=0
            for app,lst in by_app.items():
                room=max(0,limit-running_per_app.get(app,0))
                if room>=len(lst):allowed_iis.update(lst)
                else:
                    allowed_iis.update(lst[:room])
                    dropped+=len(lst)-room
            iis=allowed_iis
            if dropped>0 and not iis:
                return jsonify(ok=False,error=f"Tier {t['name']} cap reached for all selected containers ({limit}/app). Stop some first or upgrade.",license_blocked=True),402
        ordered=sorted(tuns)+sorted(iis)
        # Dedupe preserving order
        seen=set();final=[]
        for n in ordered:
            if n not in seen:final.append(n);seen.add(n)
        delay=float(d.get("delay",0.3))
        jid=job_create(len(final),label="bulk start")
        with JOBS_LOCK:
            JOBS[jid]["tun_total"]=len(tuns);JOBS[jid]["ii_total"]=len(iis)
            JOBS[jid]["ii_done"]=0;JOBS[jid]["ii_ok"]=0
        def runner():
            for name in final:
                try:
                    r=subprocess.run(["docker","start",name],capture_output=True,text=True,timeout=60)
                    if r.returncode==0 and name.startswith("ii-"):
                        update_ct_meta(name,intended_state="running")
                    job_step(jid,{"ok":r.returncode==0,"name":name})
                except Exception as e:
                    job_step(jid,{"ok":False,"name":name,"err":str(e)})
                if delay>0:time.sleep(delay)
            _cache_drop("allct")
        threading.Thread(target=runner,daemon=True).start()
        return jsonify(ok=True,job_id=jid,total=len(final),tun_total=len(tuns),ii_total=len(iis))
    except Exception as e:return jsonify(error=str(e)),500

# App-level: start all stopped containers across all accounts of an app
@app.route("/api/apps/<k>/start-all",methods=["POST"])
def api_app_start_all(k):
    try:
        ct=allct()
        ptun=f"tun-{k}-";pii=f"ii-{k}-"
        tuns=[c["name"] for c in ct if not c.get("running") and c["name"].startswith(ptun)]
        iis=[c["name"] for c in ct if not c.get("running") and c["name"].startswith(pii)]
        targets=tuns+iis
        if not targets:return jsonify(ok=False,error="No stopped containers"),400
        delay=float((request.json or {}).get("delay",0.3))
        jid=job_create(len(targets),label=f"start {k}")
        def runner():
            for name in targets:
                try:
                    r=subprocess.run(["docker","start",name],capture_output=True,text=True,timeout=60)
                    job_step(jid,{"ok":r.returncode==0,"name":name})
                except Exception as e:
                    job_step(jid,{"ok":False,"name":name,"err":str(e)})
                if delay>0:time.sleep(delay)
            _cache_drop("allct")
        threading.Thread(target=runner,daemon=True).start()
        return jsonify(ok=True,job_id=jid,total=len(targets))
    except Exception as e:return jsonify(error=str(e)),500

@app.route("/api/acc/<k>/<aid>/containers/delete",methods=["POST"])
def api_acc_ctsdel(k,aid):
    try:
        ct=allct();n=0
        pii=f"ii-{k}-{aid}-";ptun=f"tun-{k}-{aid}-"
        for c in ct:
            if c["name"].startswith(pii) or c["name"].startswith(ptun):
                dkrm(c["name"]);n+=1
        _cache_drop("allct")
        return jsonify(ok=True,removed=n)
    except Exception as e:return jsonify(error=str(e)),500

@app.route("/api/ct/<name>/<action>",methods=["POST"])
def api_ct_action(name,action):
    try:
        if action not in("start","stop","restart","rm"):return jsonify(error="Invalid"),400
        if action=="rm":
            if name.startswith("ii-"):dkrm(name.replace("ii-","tun-",1))
            dkrm(name);_cache_drop("allct");return jsonify(ok=True)
        # Enforce tier on START — counts currently-running of same app, blocks if would exceed cap
        if action=="start" and name.startswith("ii-"):
            parts=name.split("-",2)
            app_key=parts[1] if len(parts)>=3 else ""
            if app_key:
                ok_lic,err=lic_check_can_start(app_key,1)
                if not ok_lic:return jsonify(ok=False,error=err,license_blocked=True),402
        ok,msg=dkact(action,name);_cache_drop("allct")
        # Record user intent so boot-orchestrator does the right thing on next reboot
        if name.startswith("ii-") and ok:
            intent="running" if action in("start","restart") else "stopped"
            update_ct_meta(name,intended_state=intent)
        return jsonify(ok=ok,msg=msg)
    except Exception as e:return jsonify(error=str(e)),500

@app.route("/api/ct/<name>/swap-proxy",methods=["POST"])
def api_swap_proxy(name):
    """Swap proxy of a single ii container to a new proxy.
    Resolves new proxy_id from URL → ensures shared tun for it → recreates the ii to attach.
    Legacy fallback: if old tun was dedicated (tun-<app>-<accId>-<idx>), updates that tun in place."""
    try:
        new_url=request.json.get("proxy_url","")
        if not new_url:return jsonify(error="No URL"),400
        app_name=name if name.startswith("ii-") else name.replace("tun-","ii-",1)
        # Find new proxy_id by URL
        new_pid=None
        for px in pxs():
            if px.get("url")==new_url:new_pid=px.get("id");break
        if new_pid:
            ok,tun_name,msg=ensure_proxy_tun(new_pid,new_url)
            if not ok:return jsonify(ok=False,output=msg),500
            # Restart ii container — Docker can't change --network live, but if container is already in a tun
            # we just restart it (it stays attached to its existing network). For real proxy migration we'd
            # need to recreate; that's a bigger op. For now just update metadata so deploy paths are right.
            subprocess.run(["docker","restart",app_name],capture_output=True,text=True,timeout=30)
            meta=ct_meta()
            if app_name not in meta:meta[app_name]={}
            meta[app_name]["proxy_url"]=new_url
            meta[app_name]["proxy_id"]=new_pid
            sct_meta(meta)
            return jsonify(ok=True,output=f"Note: container still attached to its original tun namespace. For full migration, redeploy the container with the new proxy.")
        else:
            return jsonify(ok=False,output="New proxy URL not found in proxies.json — add it first"),400
    except Exception as e:return jsonify(error=str(e)),500

# Rebuild lost container metadata by inspecting docker network mode → tun-px-{pid} → proxy URL
@app.route("/api/system/rebuild-meta",methods=["POST"])
def api_rebuild_meta():
    try:
        # Map proxy_id → url
        pid_to_url={p["id"]:p["url"] for p in pxs()}
        # For each running ii- container, inspect its network mode (container:CONTAINER_ID)
        out={"rebuilt":0,"skipped":0,"errors":0}
        for c in allct():
            n=c["name"]
            if not n.startswith("ii-"):continue
            parts=n.split("-")
            if len(parts)<3:out["skipped"]+=1;continue
            app_key=parts[1]
            # Reconstruct acc_id (everything between app and -idx)
            # name format: ii-<app>-<accId>-<idx>  but app may have dash (vd: wipter-custom)
            # so split last 2 as accId-idx, app is between
            # Easier: use rsplit
            rest=n[3:]  # strip "ii-"
            parts2=rest.rsplit("-",2)
            if len(parts2)!=3:out["skipped"]+=1;continue
            app_key_full=parts2[0];aid=parts2[1]
            try:
                r=subprocess.run(["docker","inspect","--format","{{.HostConfig.NetworkMode}}",n],
                                 capture_output=True,text=True,timeout=8)
                if r.returncode!=0:out["errors"]+=1;continue
                netmode=r.stdout.strip()
                # netmode looks like: container:abcdef123 OR container:tun-px-XXXX
                proxy_url=""
                if netmode.startswith("container:"):
                    ref=netmode.split(":",1)[1]
                    # Resolve container name
                    r2=subprocess.run(["docker","inspect","--format","{{.Name}}",ref],
                                      capture_output=True,text=True,timeout=8)
                    if r2.returncode==0:
                        tun_name=r2.stdout.strip().lstrip("/")
                        if tun_name.startswith("tun-px-"):
                            pid=tun_name[7:]
                            proxy_url=pid_to_url.get(pid,"")
                update_ct_meta(n,app=app_key_full,acc_id=aid,proxy_url=proxy_url or "direct")
                out["rebuilt"]+=1
            except Exception:out["errors"]+=1
        return jsonify(ok=True,**out)
    except Exception as e:return jsonify(error=str(e)),500

# One-time helper: ensure tun exists for every enabled proxy. Useful after refactor migration.
@app.route("/api/proxies/sync-tuns",methods=["POST"])
def api_sync_tuns():
    try:
        targets=[(p["id"],p["url"]) for p in pxs() if p.get("enabled",True) and p.get("url")]
        if not targets:return jsonify(ok=False,error="No enabled proxies"),400
        jid=job_create(len(targets),label="sync-tuns")
        with JOBS_LOCK:JOBS[jid]["ii_total"]=len(targets);JOBS[jid]["ii_done"]=0;JOBS[jid]["ii_ok"]=0
        def runner():
            for pid,url in targets:
                ok,nm,msg=ensure_proxy_tun(pid,url)
                # Treat tun completion as ii_done so progress bar shows real count
                job_step(jid,{"ok":ok,"name":"ii-"+nm,"msg":msg})
                time.sleep(0.1)
            _cache_drop("allct")
        threading.Thread(target=runner,daemon=True).start()
        return jsonify(ok=True,job_id=jid,total=len(targets),ii_total=len(targets))
    except Exception as e:return jsonify(error=str(e)),500

@app.route("/api/ct/<name>/logs")
def api_ct_logs(name):
    try:
        r=subprocess.run(["docker","logs","--tail","200",name],capture_output=True,text=True,timeout=10)
        return jsonify(logs=r.stdout+r.stderr)
    except:return jsonify(logs="Error / logging disabled")

@app.route("/api/containers")
def api_all_cts():return jsonify([c for c in allct() if not c["is_tun"]])

@app.route("/api/teardown",methods=["POST"])
def api_teardown():
    try:
        ct=allct();n=0
        for c in ct:
            if c["name"].startswith("ii-") or c["name"].startswith("tun-"):dkrm(c["name"]);n+=1
        _cache_drop("allct")
        return jsonify(ok=True,removed=n)
    except Exception as e:return jsonify(error=str(e)),500

# Proxies
@app.route("/api/proxies")
def api_pxs():return jsonify(pxs())
@app.route("/api/proxies/usage")
def api_pxs_usage():
    try:
        usage=proxy_usage()
        # Build map of tun-px-* status: running / stopped / missing
        ct=allct()
        tun_status={}
        for c in ct:
            n=c["name"]
            if n.startswith("tun-px-"):
                tun_status[n]="running" if c.get("running") else "stopped"
        live=load_live_results()
        out=[]
        for p in pxs():
            u=usage.get(p.get("id"),[])
            tname=proxy_tun_name(p["id"])
            tstate=tun_status.get(tname,"missing")
            out.append({**p,"used":bool(u),"usage":u,"tun":tstate,"live":live.get(p["id"])})
        return jsonify(out)
    except Exception as e:return jsonify(error=str(e)),500

# Manual live check for a single proxy
@app.route("/api/proxies/<pid>/check",methods=["POST"])
def api_proxy_check(pid):
    try:
        if not any(p["id"]==pid for p in pxs()):return jsonify(error="Not found"),404
        r=check_proxy_live(pid)
        results=load_live_results();results[pid]=r;save_live_results(results)
        return jsonify(ok=True,**r)
    except Exception as e:return jsonify(error=str(e)),500

# Trigger background check of all proxies — returns immediately
@app.route("/api/proxies/check-all",methods=["POST"])
def api_proxies_check_all():
    try:
        threading.Thread(target=check_all_proxies_live,daemon=True).start()
        return jsonify(ok=True,message="Live check started in background. Refresh in ~60s to see results.")
    except Exception as e:return jsonify(error=str(e)),500
@app.route("/api/proxies/add",methods=["POST"])
def api_addpx():
    try:
        d=request.json;p=pxs()
        px={"id":str(uuid.uuid4())[:8],"url":d.get("url",""),"label":d.get("label",""),"enabled":True,"added":datetime.now().isoformat()}
        p.append(px);spx(p)
        # Auto-spawn shared tun in background (don't block API)
        if px["url"]:threading.Thread(target=ensure_proxy_tun,args=(px["id"],px["url"]),daemon=True).start()
        return jsonify(ok=True,proxy=px)
    except Exception as e:return jsonify(error=str(e)),500
@app.route("/api/proxies/<pid>",methods=["DELETE"])
def api_delpx(pid):
    try:
        spx([p for p in pxs() if p["id"]!=pid])
        threading.Thread(target=remove_proxy_tun,args=(pid,),daemon=True).start()
        return jsonify(ok=True)
    except Exception as e:return jsonify(error=str(e)),500
@app.route("/api/proxies/bulk-rm",methods=["POST"])
def api_bulk_rm_px():
    try:
        ids=set(request.json.get("ids",[]))
        spx([x for x in pxs() if x["id"] not in ids])
        def cleanup():
            for pid in ids:remove_proxy_tun(pid)
        threading.Thread(target=cleanup,daemon=True).start()
        return jsonify(ok=True,removed=len(ids))
    except Exception as e:return jsonify(error=str(e)),500
@app.route("/api/proxies/<pid>/toggle",methods=["POST"])
def api_tglpx(pid):
    try:
        p=pxs()
        for x in p:
            if x["id"]==pid:
                x["enabled"]=not x.get("enabled",True);spx(p)
                # stop tun if disabled, start if enabled
                def manage():
                    if x["enabled"]:ensure_proxy_tun(pid,x.get("url",""))
                    else:subprocess.run(["docker","stop",proxy_tun_name(pid)],capture_output=True,text=True,timeout=15)
                    _cache_drop("allct")
                threading.Thread(target=manage,daemon=True).start()
                return jsonify(ok=True)
        return jsonify(error="Not found"),404
    except Exception as e:return jsonify(error=str(e)),500
@app.route("/api/proxies/import",methods=["POST"])
def api_imppx():
    try:
        t=request.json.get("text","");p=pxs();new_pxs=[]
        lines=[l.strip() for l in t.strip().split("\n") if l.strip() and not l.strip().startswith("#")]
        for l in lines:
            pid=str(uuid.uuid4())[:8]
            np={"id":pid,"url":l,"label":"","enabled":True,"added":datetime.now().isoformat()}
            p.append(np);new_pxs.append(np)
        spx(p)
        # Spawn tuns sequentially in background — avoid stressing Docker
        def spawn():
            for px in new_pxs:
                ensure_proxy_tun(px["id"],px["url"]);time.sleep(0.2)
        if new_pxs:threading.Thread(target=spawn,daemon=True).start()
        return jsonify(ok=True,count=len(new_pxs),ids=[x["id"] for x in new_pxs])
    except Exception as e:return jsonify(error=str(e)),500
@app.route("/api/proxies/clear",methods=["POST"])
def api_clrpx():
    try:
        old_ids=[p["id"] for p in pxs()]
        spx([])
        def cleanup():
            for pid in old_ids:remove_proxy_tun(pid)
        threading.Thread(target=cleanup,daemon=True).start()
        return jsonify(ok=True)
    except Exception as e:return jsonify(error=str(e)),500

# Proxy Groups — gom proxies thành nhóm để chọn nhanh khi add account
@app.route("/api/proxy-groups")
def api_pg_list():
    try:
        pmap={x.get("id"):x for x in pxs()}
        out=[]
        for g in pgs():
            valid=[pid for pid in g.get("proxy_ids",[]) if pid in pmap]
            on=sum(1 for pid in valid if pmap[pid].get("enabled",True))
            out.append({**g,"proxy_ids":valid,"count":len(valid),"enabled_count":on})
        return jsonify(out)
    except Exception as e:return jsonify(error=str(e)),500
@app.route("/api/proxy-groups",methods=["POST"])
def api_pg_create():
    try:
        d=request.json or {};name=(d.get("name") or "").strip()
        if not name:return jsonify(error="Missing name"),400
        pids=[p for p in (d.get("proxy_ids") or []) if p]
        g=pgs();new={"id":str(uuid.uuid4())[:8],"name":name,"proxy_ids":pids,"created":datetime.now().isoformat()}
        g.append(new);spg(g);return jsonify(ok=True,group=new)
    except Exception as e:return jsonify(error=str(e)),500
@app.route("/api/proxy-groups/<gid>",methods=["PUT"])
def api_pg_update(gid):
    try:
        d=request.json or {};g=pgs()
        for x in g:
            if x["id"]==gid:
                if "name" in d:x["name"]=str(d["name"]).strip()
                if "proxy_ids" in d:x["proxy_ids"]=[p for p in d["proxy_ids"] if p]
                spg(g);return jsonify(ok=True,group=x)
        return jsonify(error="Not found"),404
    except Exception as e:return jsonify(error=str(e)),500
@app.route("/api/proxy-groups/<gid>",methods=["DELETE"])
def api_pg_delete(gid):
    try:spg([g for g in pgs() if g["id"]!=gid]);return jsonify(ok=True)
    except Exception as e:return jsonify(error=str(e)),500

# Custom Docker apps — user-defined images, env vars, credentials
@app.route("/api/custom-apps")
def api_ca_list():
    try:return jsonify(custom_apps_list())
    except Exception as e:return jsonify(error=str(e)),500

@app.route("/api/custom-apps",methods=["POST"])
def api_ca_create():
    try:
        d=request.json or {}
        key=re.sub(r'[^a-z0-9_-]','',(d.get("key") or "").lower().strip())
        if not key:return jsonify(error="Key phải có (chữ thường, không dấu, không space)"),400
        if key in APPS and not APPS[key].get("_custom"):return jsonify(error=f"Key '{key}' trùng built-in app"),400
        cmd=(d.get("cmd_template") or "").strip()
        if not cmd:return jsonify(error="cmd_template phải có"),400
        if "{name}" not in cmd:return jsonify(error="cmd_template phải chứa placeholder {name}"),400
        if "{net}" not in cmd:return jsonify(error="cmd_template phải chứa placeholder {net}"),400
        arr=[c for c in custom_apps_list() if c.get("key")!=key]
        arr.append({
            "key":key,"name":d.get("name") or key,"icon":d.get("icon") or "🛠️",
            "color":d.get("color") or "#64748b","desc":d.get("desc") or "Custom Docker app",
            "pay":d.get("pay") or "-","limit":int(d.get("limit") or 0),
            "fields":[f for f in (d.get("fields") or []) if f.get("key") and f.get("label")],
            "cmd_template":cmd,"created":datetime.now().isoformat(),
        })
        save_custom_apps(arr);reload_custom_apps()
        return jsonify(ok=True,key=key)
    except Exception as e:return jsonify(error=str(e)),500

@app.route("/api/custom-apps/<k>",methods=["PUT"])
def api_ca_update(k):
    try:
        d=request.json or {}
        arr=custom_apps_list()
        for c in arr:
            if c.get("key")==k:
                for fld in("name","icon","color","desc","pay","cmd_template"):
                    if fld in d:c[fld]=d[fld]
                if "limit" in d:c["limit"]=int(d["limit"] or 0)
                if "fields" in d:c["fields"]=[f for f in d["fields"] if f.get("key") and f.get("label")]
                save_custom_apps(arr);reload_custom_apps()
                return jsonify(ok=True)
        return jsonify(error="Not found"),404
    except Exception as e:return jsonify(error=str(e)),500

@app.route("/api/custom-apps/<k>",methods=["DELETE"])
def api_ca_delete(k):
    try:
        # Also clean associated accounts data
        save_custom_apps([c for c in custom_apps_list() if c.get("key")!=k])
        reload_custom_apps()
        a=acs()
        if k in a:del a[k];sacs(a)
        return jsonify(ok=True)
    except Exception as e:return jsonify(error=str(e)),500

@app.route("/api/settings")
def api_getset():return jsonify(cfg())
@app.route("/api/settings",methods=["POST"])
def api_savset():
    try:
        c=cfg()
        for x in["device_name","server_url","license_key","auto_start_on_boot","boot_start_batch","boot_start_delay","boot_start_grace"]:
            if x in request.json:c[x]=request.json[x]
        scfg(c);return jsonify(ok=True)
    except Exception as e:return jsonify(error=str(e)),500

@app.route("/api/system/boot-status")
def api_boot_status():
    """Return current state of the boot orchestrator (status, progress)."""
    with BOOT_LOCK:return jsonify(dict(BOOT_ORCH))

@app.route("/api/system/boot-start-now",methods=["POST"])
def api_boot_start_now():
    """Manually trigger the boot orchestrator (e.g. user clicks Resume after server reboot)."""
    if BOOT_ORCH.get("status")=="running":
        return jsonify(ok=False,error="Already running")
    # Run with 0 grace
    def runner():
        c=cfg();orig=c.get("boot_start_grace",30);c["boot_start_grace"]=0;scfg(c)
        try:_boot_auto_start()
        finally:c["boot_start_grace"]=orig;scfg(c)
    threading.Thread(target=runner,daemon=True).start()
    return jsonify(ok=True,message="Boot start orchestrator triggered")

@app.route("/api/ref-links")
def api_ref_links():return jsonify(ref_links(refresh=True))

@app.route("/api/wallets")
def api_wallets():return jsonify(wallets())

def _ver_tuple(v):
    """'2.0.10' -> (2,0,10). Used for safe semver-ish comparison."""
    try:return tuple(int(x) for x in str(v or "0").split(".") if x.isdigit() or x.lstrip("-").isdigit())
    except Exception:return (0,)

# ==================== LICENSE ENDPOINTS ====================
@app.route("/api/license")
def api_license():
    """Current tier + limits + usage. Frontend uses this to gate UI."""
    t=cur_tier()
    return jsonify(tier=t,usage=lic_usage())

@app.route("/api/license/refresh",methods=["POST"])
def api_license_refresh():
    """Force-poll control server for license state (used after payment approval)."""
    try:
        c=cfg();url=(c.get("server_url") or "").rstrip("/")
        if not url:return jsonify(ok=False,error="No server URL")
        req=urllib.request.Request(f"{url}/api/license/by-client/{c['client_id']}",headers={"Accept":"application/json"})
        with urllib.request.urlopen(req,timeout=8) as r:data=json.loads(r.read().decode())
        if not data.get("ok"):return jsonify(ok=False,error="Server rejected the request")
        lic_save({
            "tier":data.get("tier","trial"),
            "license_key":data.get("license_key",""),
            "plan_type":data.get("plan_type",""),
            "expires_at":data.get("expires_at",""),
            "checked_at":datetime.utcnow().isoformat()+"Z",
        })
        return jsonify(ok=True,tier=cur_tier())
    except Exception as e:return jsonify(ok=False,error=str(e))

@app.route("/api/license/pricing")
def api_license_pricing():
    """Proxy pricing from control server so frontend has 1 source."""
    try:
        c=cfg();url=(c.get("server_url") or "").rstrip("/")
        if not url:return jsonify(ok=False,error="No server URL")
        req=urllib.request.Request(url+"/api/pricing",headers={"Accept":"application/json"})
        with urllib.request.urlopen(req,timeout=5) as r:return jsonify(ok=True,**json.loads(r.read().decode()))
    except Exception as e:return jsonify(ok=False,error=str(e))

@app.route("/api/license/payments")
def api_license_payments():
    """Return this client's recent payment history from the control server."""
    try:
        c=cfg();url=(c.get("server_url") or "").rstrip("/")
        if not url:return jsonify(ok=False,error="No server URL")
        cid=c.get("client_id","")
        if not cid:return jsonify(ok=True,payments=[])
        req=urllib.request.Request(f"{url}/api/payments/by-client/{cid}",headers={"Accept":"application/json"})
        with urllib.request.urlopen(req,timeout=6) as r:return jsonify(json.loads(r.read().decode()))
    except Exception as e:return jsonify(ok=False,error=str(e))

@app.route("/api/license/payment/<pid>")
def api_license_payment_status(pid):
    """Forward to control server so frontend can poll auto-verify state."""
    try:
        c=cfg();url=(c.get("server_url") or "").rstrip("/")
        if not url:return jsonify(ok=False,error="No server URL")
        req=urllib.request.Request(f"{url}/api/payments/{pid}",headers={"Accept":"application/json"})
        with urllib.request.urlopen(req,timeout=6) as r:return jsonify(json.loads(r.read().decode()))
    except urllib.error.HTTPError as e:
        try:return jsonify(json.loads(e.read().decode()))
        except Exception:return jsonify(ok=False,error=str(e))
    except Exception as e:return jsonify(ok=False,error=str(e))

@app.route("/api/license/payment/<pid>/verify-now",methods=["POST"])
def api_license_payment_verify_now(pid):
    """Forward verify-now to control server (triggers immediate on-chain check)."""
    try:
        c=cfg();url=(c.get("server_url") or "").rstrip("/")
        if not url:return jsonify(ok=False,error="No server URL")
        req=urllib.request.Request(f"{url}/api/payments/{pid}/verify-now",data=b"",method="POST")
        with urllib.request.urlopen(req,timeout=6) as r:return jsonify(json.loads(r.read().decode()))
    except Exception as e:return jsonify(ok=False,error=str(e))

@app.route("/api/license/quote",methods=["POST"])
def api_license_quote():
    """Forward to control server: create payment intent with unique deposit address."""
    try:
        d=request.json or {};c=cfg();url=(c.get("server_url") or "").rstrip("/")
        if not url:return jsonify(ok=False,error="No server URL configured")
        payload={
            "client_id":c.get("client_id",""),
            "device_name":c.get("device_name",""),
            "tier":d.get("tier","basic"),
            "plan_type":d.get("plan_type","lifetime"),
        }
        req=urllib.request.Request(url+"/api/license/quote",
            data=json.dumps(payload).encode(),
            headers={"Content-Type":"application/json","Accept":"application/json"},method="POST")
        with urllib.request.urlopen(req,timeout=8) as r:return jsonify(json.loads(r.read().decode()))
    except urllib.error.HTTPError as e:
        try:return jsonify(json.loads(e.read().decode()))
        except Exception:return jsonify(ok=False,error=str(e))
    except Exception as e:return jsonify(ok=False,error=str(e))

@app.route("/api/license/pay",methods=["POST"])
def api_license_pay():
    """Forward a payment submission to control server."""
    try:
        d=request.json or {};c=cfg();url=(c.get("server_url") or "").rstrip("/")
        if not url:return jsonify(ok=False,error="No server URL configured")
        payload={
            "client_id":c.get("client_id",""),
            "device_name":c.get("device_name",""),
            "tier":d.get("tier","basic"),
            "plan_type":d.get("plan_type","lifetime"),
            "txid":(d.get("txid") or "").strip(),
            "note":d.get("note",""),
        }
        req=urllib.request.Request(url+"/api/payments/submit",
            data=json.dumps(payload).encode(),
            headers={"Content-Type":"application/json","Accept":"application/json"},method="POST")
        with urllib.request.urlopen(req,timeout=8) as r:data=json.loads(r.read().decode())
        return jsonify(data)
    except urllib.error.HTTPError as e:
        try:body=json.loads(e.read().decode())
        except Exception:body={"error":str(e)}
        return jsonify(ok=False,**body)
    except Exception as e:return jsonify(ok=False,error=str(e))

@app.route("/api/update/check")
def api_update_check():
    try:
        c=cfg();url=(c.get("server_url") or "").rstrip("/")
        if not url:return jsonify(ok=False,error="No server URL")
        req=urllib.request.Request(url+"/api/version",headers={"Accept":"application/json"})
        with urllib.request.urlopen(req,timeout=5) as r:data=json.loads(r.read().decode())
        cur={"version":CLIENT_VERSION,"build":CLIENT_BUILD}
        sv=data.get("version") or "0";sb=int(data.get("build") or 0)
        # Update is available when server version > current version, or same version but higher build
        sv_t=_ver_tuple(sv);cv_t=_ver_tuple(CLIENT_VERSION)
        available=(sv_t>cv_t) or (sv_t==cv_t and sb>CLIENT_BUILD)
        return jsonify(ok=True,server=data,current=cur,available=available)
    except Exception as e:return jsonify(ok=False,error=str(e))

@app.route("/api/update/apply",methods=["POST"])
def api_update_apply():
    """Download latest tarball from control server, replace app.py + index.html, restart service."""
    import tempfile,tarfile,shutil
    try:
        c=cfg();url=(c.get("server_url") or "").rstrip("/")
        if not url:return jsonify(ok=False,error="No server URL configured")
        tmp=tempfile.mkdtemp(prefix="iim-update-")
        tar_path=os.path.join(tmp,"client.tar.gz")
        # Download
        req=urllib.request.Request(url+"/download/client")
        with urllib.request.urlopen(req,timeout=120) as r,open(tar_path,"wb") as f:
            shutil.copyfileobj(r,f)
        size=os.path.getsize(tar_path)
        if size<2000:
            shutil.rmtree(tmp,ignore_errors=True)
            return jsonify(ok=False,error=f"Downloaded archive too small ({size}B)")
        # Extract
        ext=os.path.join(tmp,"x");os.makedirs(ext,exist_ok=True)
        with tarfile.open(tar_path,"r:gz") as t:t.extractall(ext)
        # The tarball contains iim-client-dist/{app.py,templates/index.html,...}
        src_root=None
        for d in os.listdir(ext):
            p=os.path.join(ext,d)
            if os.path.isdir(p) and os.path.exists(os.path.join(p,"app.py")):
                src_root=p;break
        if not src_root:
            shutil.rmtree(tmp,ignore_errors=True)
            return jsonify(ok=False,error="Archive missing app.py")
        new_app=os.path.join(src_root,"app.py")
        new_html=os.path.join(src_root,"templates","index.html")
        if not os.path.exists(new_app) or not os.path.exists(new_html):
            shutil.rmtree(tmp,ignore_errors=True)
            return jsonify(ok=False,error="Archive missing required files")
        if os.path.getsize(new_app)<1000 or os.path.getsize(new_html)<1000:
            shutil.rmtree(tmp,ignore_errors=True)
            return jsonify(ok=False,error="Archive files look corrupted (too small)")
        # Backup current
        ts=datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        bak=os.path.join(DATA,"backups",ts);os.makedirs(bak,exist_ok=True)
        try:
            shutil.copy(os.path.join(BASE,"app.py"),os.path.join(bak,"app.py"))
            shutil.copy(os.path.join(BASE,"templates","index.html"),os.path.join(bak,"index.html"))
        except Exception:pass
        # Replace
        shutil.copy(new_app,os.path.join(BASE,"app.py"))
        shutil.copy(new_html,os.path.join(BASE,"templates","index.html"))
        shutil.rmtree(tmp,ignore_errors=True)
        # Schedule restart in a detached subprocess so this response can return first
        subprocess.Popen(["bash","-c","sleep 2 && systemctl restart ii-manager"],
            start_new_session=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        return jsonify(ok=True,message="Update applied. Service will restart in 2s.",backup=ts,size=size)
    except Exception as e:
        return jsonify(ok=False,error=str(e))

@app.route("/api/debug/accounts")
def api_dbg():return jsonify(raw=acs(),meta=ct_meta())
@app.route("/api/reset",methods=["POST"])
def api_reset():sacs({});spx([]);sct_meta({});return jsonify(ok=True)

# Clean disabled (stopped/exited) ii- and legacy tun-{app}- containers.
# Shared tuns (tun-px-*) are NOT cleaned — they live with the proxy lifecycle.
@app.route("/api/system/clean-disabled",methods=["POST"])
def api_clean_disabled():
    try:
        ct=allct();n=0
        for c in ct:
            if c.get("running"):continue
            name=c["name"]
            # Skip shared tuns
            if name.startswith("tun-px-"):continue
            if name.startswith("ii-") or name.startswith("tun-"):
                dkrm(name);n+=1
        _cache_drop("allct")
        return jsonify(ok=True,removed=n)
    except Exception as e:return jsonify(error=str(e)),500

# Full system reset: remove containers/proxies/accounts/images. Requires confirm="yes".
@app.route("/api/system/reset",methods=["POST"])
def api_system_reset():
    try:
        d=request.json or {}
        if (d.get("confirm") or "").strip().lower()!="yes":
            return jsonify(error='Confirm string must be "yes"'),400
        out={"containers":0,"proxies":0,"groups":0,"accounts":0,"images":0,"pruned_space":""}
        # 1) Remove all ii-* and tun-* containers
        for c in allct():
            if c["name"].startswith("ii-") or c["name"].startswith("tun-"):
                dkrm(c["name"]);out["containers"]+=1
        # 2) Snapshot counts then wipe app data
        out["accounts"]=sum(len(v) for v in acs().values())
        out["proxies"]=len(pxs());out["groups"]=len(pgs())
        sacs({});spx([]);spg([]);sct_meta({})
        # 3) Remove images used by app templates + tun image
        images=set()
        for k in DOCKER_CMDS:
            img=image_for_app(k)
            if img:images.add(img)
        images.add(TUN_IMAGE)
        for img in images:
            r=subprocess.run(["docker","rmi","-f",img],capture_output=True,text=True,timeout=60)
            if r.returncode==0:out["images"]+=1
        # 4) docker system prune to reclaim dangling layers + volumes + networks
        try:
            r=subprocess.run(["docker","system","prune","-af","--volumes"],capture_output=True,text=True,timeout=180)
            # Extract "Total reclaimed space" line
            for line in (r.stdout or "").splitlines():
                if "reclaimed" in line.lower():out["pruned_space"]=line.strip();break
        except Exception:pass
        _cache_drop("allct")
        for img_key in list(_CACHE.keys()):
            if img_key.startswith("img:"):_CACHE.pop(img_key,None)
        return jsonify(ok=True,**out)
    except Exception as e:return jsonify(error=str(e)),500

# System power: shutdown / reboot host
@app.route("/api/system/shutdown",methods=["POST"])
def api_system_shutdown():
    try:
        if ((request.json or {}).get("confirm") or "").strip().lower()!="yes":
            return jsonify(error='Confirm must be "yes"'),400
        def delayed():
            time.sleep(2)
            subprocess.Popen(["shutdown","-h","now"])
        threading.Thread(target=delayed,daemon=True).start()
        return jsonify(ok=True,message="Shutdown initiated (powers off in ~5s)")
    except Exception as e:return jsonify(error=str(e)),500

@app.route("/api/system/reboot",methods=["POST"])
def api_system_reboot():
    try:
        if ((request.json or {}).get("confirm") or "").strip().lower()!="yes":
            return jsonify(error='Confirm must be "yes"'),400
        def delayed():
            time.sleep(2)
            subprocess.Popen(["reboot"])
        threading.Thread(target=delayed,daemon=True).start()
        return jsonify(ok=True,message="Reboot initiated")
    except Exception as e:return jsonify(error=str(e)),500

# Disk usage report
@app.route("/api/system/disk")
def api_disk():
    try:
        r=subprocess.run(["docker","system","df","--format","{{.Type}}\t{{.TotalCount}}\t{{.Active}}\t{{.Size}}\t{{.Reclaimable}}"],capture_output=True,text=True,timeout=10)
        items=[]
        if r.returncode==0:
            for l in r.stdout.strip().split("\n"):
                p=l.split("\t")
                if len(p)>=5:items.append({"type":p[0],"total":p[1],"active":p[2],"size":p[3],"reclaimable":p[4]})
        return jsonify(ok=True,items=items)
    except Exception as e:return jsonify(error=str(e)),500

if __name__=="__main__":
    app.run(host="0.0.0.0",port=18880,debug=False)
