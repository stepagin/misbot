"use strict";
process.umask(0o077);
const fs=require("node:fs"),net=require("node:net"),http=require("node:http");
const {MatrixClient,SimpleFsStorageProvider,RustSdkCryptoStorageProvider,LogService}=require("matrix-bot-sdk");
const {StoreType}=require("@matrix-org/matrix-sdk-crypto-nodejs");
const {FederationBot}=require("./logic"),{ensureCrossSigning}=require("./signing");
let stage="configuration";
LogService.setLogger({trace(){},debug(){},info(){},warn(){},error(){}});
function control(request) {
    return new Promise((resolve,reject)=>{
        const socket=net.createConnection("/run/control/control.sock");let raw="",done=false;
        const finish=(error,value)=>{if(done)return;done=true;socket.destroy();error?reject(new Error("Управляющая служба недоступна или отказала. Проверьте misbot status; повторите через federation requests retry/recover.")):resolve(value);};
        socket.setTimeout(300000);
        socket.on("connect",()=>socket.write(JSON.stringify(request)+"\n"));
        socket.on("data",chunk=>{raw+=chunk.toString();if(raw.length>32768)return finish(true);if(!raw.includes("\n"))return;
            try{const value=JSON.parse(raw.split("\n")[0]);finish(!value.ok,value.result);}catch{finish(true);}});
        socket.on("timeout",()=>finish(true));socket.on("error",()=>finish(true));socket.on("end",()=>{if(!done)finish(true);});
    });
}
async function main() {
    const config=JSON.parse(fs.readFileSync("/config/federation.json","utf8"));
    const store=new SimpleFsStorageProvider("/data/state.json");
    const crypto=new RustSdkCryptoStorageProvider("/data/crypto",StoreType.Sqlite);
    const client=new MatrixClient(config.homeserver_url,config.access_token,store,crypto);
    stage="identity";if(await client.getUserId()!==config.user_id)throw new Error("identity");
    const bot=new FederationBot(client,control,{getValue:k=>store.readValue(k),setValue:(k,v)=>store.storeValue(k,v)},config);
    let lastSync=0,ready=false;const sync=client.doSync.bind(client);
    client.doSync=async token=>{const result=await sync(token);lastSync=Date.now();return result;};
    // One queue also serializes crypto/membership operations and duplicate deliveries.
    let queue=Promise.resolve();
    const enqueue=fn=>{queue=queue.then(fn).catch(()=>console.log("Federation operation not confirmed; use CLI status/retry"));};
    client.on("room.invite",(room,event)=>enqueue(()=>bot.invitation(room,event)));
    client.on("room.message",(room,event)=>enqueue(async()=>{
        try{await bot.message(room,event);}catch{if(bot.local(event.sender))await client.sendNotice(room,"Изменение не подтверждено. Проверьте sudo misbot status. Если показан operation ID, используйте sudo misbot federation operations и указанную recovery/retry команду. Для исправления ввода: помощь.").catch(()=>{});}
    }));
    stage="profile";await client.setDisplayName("Бот управления федерацией");stage="sync";await client.start();stage="cross-signing";await ensureCrossSigning(client);
    stage="admin-room";
    if(!store.readValue("admin-room")) {
        // Recover a lost createRoom response by finding our existing private room.
        let found=null;
        for(const room of await client.getJoinedRooms()) {
            const state=await client.getRoomState(room);
            const members=state.filter(e=>e.type==="m.room.member"&&["join","invite"].includes(e.content?.membership)).map(e=>e.state_key);
            if(members.length===2&&members.includes(config.user_id)&&members.includes("@matrix-admin:"+config.server_name)
                &&state.some(e=>e.type==="m.room.encryption"&&e.content?.algorithm==="m.megolm.v1.aes-sha2")){found=room;break;}
        }
        const room=found||await client.createRoom({preset:"private_chat",visibility:"private",is_direct:true,
            invite:["@matrix-admin:"+config.server_name],name:"Misbot — управление федерацией",
            initial_state:[{type:"m.room.encryption",state_key:"",content:{algorithm:"m.megolm.v1.aes-sha2"}}]});
        store.storeValue("admin-room",typeof room==="string"?room:room.room_id);
    }
    ready=true;
    setInterval(()=>enqueue(()=>bot.notifications()),30000).unref();
    http.createServer((req,res)=>{const ok=ready&&client.crypto.isReady&&Date.now()-lastSync<180000;
        res.writeHead(req.url==="/health"?(ok?200:503):404,{"Content-Type":"application/json"});res.end(JSON.stringify({ok}));}).listen(8080,"0.0.0.0");
}
main().catch(()=>{console.log("Federation bot startup failed stage="+stage+"; retain session/crypto state");process.exit(1);});
