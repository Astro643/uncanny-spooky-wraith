# pip install discord.py fastapi uvicorn upstash-redis google-genai cohere
# env: DISCORD_TOKEN UPSTASH_REDIS_REST_URL UPSTASH_REDIS_REST_TOKEN GEMINI_API_KEY COHERE_API_KEY DASH_TOKEN WEBHOOK_SECRET [PORT]
import os,json,zlib,base64,asyncio,threading,time
import discord,uvicorn,cohere
from discord import AuditLogAction as A
from fastapi import FastAPI,Header,HTTPException
from pydantic import BaseModel
from google import genai
from upstash_redis import Redis
from upstash_redis.asyncio import Redis as ARedis

E=os.environ
U,T=E["UPSTASH_REDIS_REST_URL"],E["UPSTASH_REDIS_REST_TOKEN"]
R,AR=Redis(url=U,token=T),ARedis(url=U,token=T)
G=genai.Client(api_key=E["GEMINI_API_KEY"])
CO=cohere.AsyncClientV2(api_key=E["COHERE_API_KEY"])
PANIC,LIM,WIN="server:config:panic_mode","server:config:limit","server:config:window"
BP="server:blueprint:%d"
MODELS=(E.get("GEMINI_PRIMARY","gemini-3.8-flash"),E.get("GEMINI_FALLBACK","gemini-3.5-flash-lite"))
MK={0:"create_text_channel",5:"create_text_channel",2:"create_voice_channel",13:"create_stage_channel",15:"create_forum"}

# ---------- blueprint (single overwritten key per guild) ----------
def _ow(c):
    return [["r",t.name,*(x.value for x in p.pair())] if isinstance(t,discord.Role) else ["m",t.id,*(x.value for x in p.pair())] for t,p in c.overwrites.items()]

def _ch(c):
    d={"n":c.name,"t":c.type.value,"p":c.position,"o":_ow(c)}
    if d["t"]==0: d.update(tp=c.topic,nsfw=c.nsfw)
    elif d["t"]==2: d["ul"]=c.user_limit
    return d

def pack(g):
    d={"r":[{"n":r.name,"c":r.color.value,"pm":r.permissions.value,"h":r.hoist,"m":r.mentionable} for r in g.roles if not r.managed and not r.is_default()],
       "c":[{**_ch(c),"ch":[_ch(x) for x in c.channels]} for c in g.categories],
       "x":[_ch(c) for c in g.channels if c.category is None and not isinstance(c,discord.CategoryChannel)]}
    return base64.b64encode(zlib.compress(json.dumps(d,separators=(",",":")).encode(),9)).decode()

def _perm(g,o):
    r={}
    for k,n,a,d in o:
        t=discord.utils.get(g.roles,name=n) if k=="r" else g.get_member(n)
        if t: r[t]=discord.PermissionOverwrite.from_pair(discord.Permissions(a),discord.Permissions(d))
    return r

async def _mk(g,d,cat=None):
    m=MK.get(d["t"])
    if not m or any(c.name==d["n"] and c.category==cat and MK.get(c.type.value)==m for c in g.channels): return
    kw=dict(name=d["n"],overwrites=_perm(g,d["o"]),position=d["p"])
    if cat: kw["category"]=cat
    if d["t"]==0: kw.update(topic=d.get("tp"),nsfw=d.get("nsfw",False))
    if d["t"]==2: kw["user_limit"]=d.get("ul",0)
    try: await getattr(g,m)(**kw)
    except discord.HTTPException: pass

async def restore(g):
    raw=await AR.get(BP%g.id)
    if not raw: return
    d=json.loads(zlib.decompress(base64.b64decode(raw)))
    try:
        for r in d["r"]:
            if not discord.utils.get(g.roles,name=r["n"]):
                await g.create_role(name=r["n"],color=discord.Color(r["c"]),permissions=discord.Permissions(r["pm"]),hoist=r["h"],mentionable=r["m"])
        for c in d["c"]:
            cat=discord.utils.get(g.categories,name=c["n"]) or await g.create_category(c["n"],overwrites=_perm(g,c["o"]),position=c["p"])
            for x in c["ch"]: await _mk(g,x,cat)
        for x in d["x"]: await _mk(g,x)
    except discord.HTTPException: pass

async def snapshot_all():
    for g in bot.guilds:
        if not await AR.get(f"server:hold:{g.id}"): await AR.set(BP%g.id,pack(g))

# ---------- anti-nuke ----------
async def _actor(g,act,tid):
    for _ in range(3):
        try:
            async for e in g.audit_logs(limit=5,action=act):
                if getattr(e.target,"id",None)==tid and time.time()-e.created_at.timestamp()<15: return e.user
        except discord.HTTPException: return
        await asyncio.sleep(.4)

async def punish(g,u):
    try:
        m=g.get_member(u.id)
        if m: await m.edit(roles=[],reason="anti-nuke")
    except discord.HTTPException: pass
    await AR.incr("server:stats:neutralized")
    await AR.set(f"server:hold:{g.id}",1,ex=3600)
    if await AR.set(f"server:restore:{g.id}",1,ex=30,nx=True): await restore(g)

async def guard(g,act,tid):
    p,l,w=await AR.mget(PANIC,LIM,WIN)
    u=await _actor(g,act,tid)
    if not u or u.id in(g.owner_id,bot.user.id): return
    k=f"server:count:{g.id}:{u.id}"
    n=await AR.incr(k)
    if n==1: await AR.expire(k,int(w or 10))
    s=f"server:session:{g.id}:{u.id}"
    await AR.hincrby(s,act.name,1); await AR.expire(s,3600); await AR.sadd("server:sessions",s)
    await AR.incr("server:stats:events")
    if p=="1" or n>int(l or 3): await punish(g,u)

# ---------- AI failover ----------
async def ai(p):
    for m in MODELS:
        try: return (await G.aio.models.generate_content(model=m,contents=p)).text
        except Exception: pass
    try: return (await CO.chat(model="command-r",messages=[{"role":"user","content":p}])).message.content[0].text
    except Exception: return "UNAVAILABLE"

async def agent():
    while True:
        await asyncio.sleep(300)
        for k in await AR.smembers("server:sessions"):
            d=await AR.hgetall(k)
            if not d: await AR.srem("server:sessions",k); continue
            await AR.set("server:ai:"+k,await ai("Admin session action counts (last hour): %s. Reply one line: RISK=LOW|MED|HIGH + reason."%json.dumps(d)),ex=3600)

# ---------- discord ----------
class W(discord.Client):
    def __init__(s,**k): super().__init__(**k); s.tree=discord.app_commands.CommandTree(s)
    async def setup_hook(s): asyncio.create_task(agent()); await s.tree.sync()

I=discord.Intents.default(); I.members=True
bot=W(intents=I)

@bot.event
async def on_ready():
    for g in bot.guilds:
        if not await AR.exists(BP%g.id): await AR.set(BP%g.id,pack(g))

@bot.event
async def on_guild_channel_delete(c): await guard(c.guild,A.channel_delete,c.id)
@bot.event
async def on_guild_role_delete(r): await guard(r.guild,A.role_delete,r.id)
@bot.event
async def on_member_ban(g,m): await guard(g,A.ban,m.id)

# ---------- api ----------
app=FastAPI()

def auth(t):
    if t!=E["DASH_TOKEN"]: raise HTTPException(401)

class Cfg(BaseModel):
    panic_mode:bool|None=None
    limit:int|None=None
    window:int|None=None

@app.api_route("/",methods=["GET","HEAD"])
def health(): return {"ok":True}

@app.get("/api/dashboard/stats")
def stats(x_token:str=Header()):
    auth(x_token)
    p,l,w,ev,nu=R.mget(PANIC,LIM,WIN,"server:stats:events","server:stats:neutralized")
    return {"panic_mode":p=="1","limit":int(l or 3),"window":int(w or 10),"events":int(ev or 0),"neutralized":int(nu or 0),"guilds":len(bot.guilds)}

@app.post("/api/dashboard/config")
def config(c:Cfg,x_token:str=Header()):
    auth(x_token)
    for k,v in((PANIC,None if c.panic_mode is None else int(c.panic_mode)),(LIM,c.limit),(WIN,c.window)):
        if v is not None: R.set(k,str(v))
    return {"ok":True}

@app.post("/api/webhook/snapshot")
def snapshot(x_secret:str=Header()):
    if x_secret!=E["WEBHOOK_SECRET"]: raise HTTPException(401)
    asyncio.run_coroutine_threadsafe(snapshot_all(),bot.loop)
    return {"queued":True}

import cmds; cmds.setup(bot,globals())

if __name__=="__main__":
    threading.Thread(target=uvicorn.run,args=(app,),kwargs={"host":"0.0.0.0","port":int(E.get("PORT",10000)),"log_level":"warning"},daemon=True).start()
    bot.run(E["DISCORD_TOKEN"])
