import os,json,time,asyncio
from types import SimpleNamespace as NS
from fastapi import Header,HTTPException
from fastapi.responses import FileResponse,StreamingResponse
from pydantic import BaseModel

class Act(BaseModel):
    action:str
    guild_id:str|None=None
    on:bool|None=None
    actions:int|None=None
    seconds:int|None=None

class Chat(BaseModel):
    message:str
    history:list[dict]=[]
    guild_id:str|None=None

def setup(app,g):
    x=NS(**g)
    run=lambda c,t=90:asyncio.run_coroutine_threadsafe(c,x.bot.loop).result(t)

    async def act(d):
        a,gid=d.get("action"),d.get("guild_id")
        gu=x.bot.get_guild(int(gid)) if gid else (x.bot.guilds[0] if len(x.bot.guilds)==1 else None)
        if a=="panic":
            on=bool(d.get("on")); await x.AR.set(x.PANIC,str(int(on))); m=f"Panic mode {'ON' if on else 'OFF'}"
        elif a=="limit":
            n,s=max(1,min(50,int(d.get("actions") or 3))),max(1,min(300,int(d.get("seconds") or 10)))
            await x.AR.set(x.LIM,str(n)); await x.AR.set(x.WIN,str(s)); m=f"Threshold set to {n} actions / {s}s"
        elif a in("snapshot","rebuild","release") and gu:
            if a=="release": await x.AR.delete(f"server:hold:{gu.id}"); m=f"Hold cleared for {gu.name}"
            elif a=="rebuild": await x.restore(gu); m=f"Rebuild finished for {gu.name}"
            elif await x.AR.get(f"server:hold:{gu.id}"): raise ValueError("Snapshots are on hold. Release the hold first.")
            else: await x.AR.set(x.BP%gu.id,x.pack(gu)); m=f"Layout saved for {gu.name}"
        else: raise ValueError("Unknown action or server")
        await x.note("Dashboard: "+m); return m

    def state():
        gs=list(x.bot.guilds)
        v=x.R.mget(x.PANIC,x.LIM,x.WIN,"server:stats:events","server:stats:neutralized",*[f"server:hold:{k.id}" for k in gs])
        return {"panic":v[0]=="1","limit":int(v[1] or 3),"window":int(v[2] or 10),"events":int(v[3] or 0),"neutralized":int(v[4] or 0),
                "guilds":[{"id":str(k.id),"name":k.name,"hold":bool(h)} for k,h in zip(gs,v[5:])],"log":x.R.lrange("server:log",0,29)}

    @app.api_route("/",methods=["GET","HEAD"])
    def home(): return FileResponse(os.path.join(os.path.dirname(os.path.abspath(__file__)),"dashboard.html"))

    @app.get("/api/dashboard/state")
    def st(x_token:str=Header()): x.auth(x_token); return state()

    @app.get("/api/dashboard/stream")
    def stream(x_token:str=Header()):
        x.auth(x_token)
        def gen():
            seen=-1
            while True:
                with x._cv:
                    if x._v[0]==seen: x._cv.wait(25)
                    cur=x._v[0]
                if cur!=seen:
                    seen=cur
                    try: yield "data: "+json.dumps(state())+"\n\n"
                    except Exception: pass
                    time.sleep(.3)
                else: yield ": ka\n\n"
        return StreamingResponse(gen(),media_type="text/event-stream",headers={"Cache-Control":"no-cache","X-Accel-Buffering":"no"})

    @app.post("/api/dashboard/action")
    def action(d:Act,x_token:str=Header()):
        x.auth(x_token)
        try: return {"message":run(act(d.model_dump()))}
        except Exception as e: raise HTTPException(400,str(e)[:120])

    @app.post("/api/dashboard/chat")
    def chat(c:Chat,x_token:str=Header()):
        x.auth(x_token)
        s=state()
        p=("You are the admin assistant of Worker, a Discord security bot. Current state: "+json.dumps({k:s[k] for k in("panic","limit","window","events","neutralized","guilds")})
           +'. Available actions: {"action":"panic","on":bool}, {"action":"limit","actions":int,"seconds":int}, {"action":"snapshot","guild_id":str}, {"action":"rebuild","guild_id":str}, {"action":"release","guild_id":str}. '
           +'Reply ONLY with JSON: {"reply":"short friendly text","actions":[...]}. Use an empty actions list for questions. Never invent actions.\n'
           +"\n".join(f"{m.get('role')}: {str(m.get('content'))[:500]}" for m in c.history[-8:])+"\nuser: "+c.message[:1000])
        t=run(x.ai(p)) or "UNAVAILABLE"
        if t=="UNAVAILABLE": raise HTTPException(503,"AI unavailable")
        try: j=json.loads(t[t.index("{"):t.rindex("}")+1])
        except Exception: return {"reply":t,"results":[]}
        r=[]
        for a in j.get("actions",[])[:5]:
            try:
                if not a.get("guild_id"): a["guild_id"]=c.guild_id
                r.append("✓ "+run(act(a)))
            except Exception as e: r.append("✗ "+str(e)[:80])
        return {"reply":j.get("reply",""),"results":r}
