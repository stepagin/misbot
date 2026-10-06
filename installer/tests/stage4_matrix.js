"use strict";
// Disposable native-MAS admin client. Never log token codes or decrypted payloads.
process.umask(0o077);
const fs=require("node:fs");
const {MatrixClient,SimpleFsStorageProvider,RustSdkCryptoStorageProvider,LogService}=require("matrix-bot-sdk");
const {StoreType}=require("@matrix-org/matrix-sdk-crypto-nodejs");
LogService.setLogger({trace(){},debug(){},info(){},warn(){},error(){}});
const sleep=ms=>new Promise(r=>setTimeout(r,ms));
let stage="startup";
async function main(){
 const cfg=JSON.parse(fs.readFileSync("/test/config.json","utf8"));
 async function refresh(){
  const response=await fetch(cfg.token_endpoint,{method:"POST",headers:{"Content-Type":"application/x-www-form-urlencoded"},
   body:new URLSearchParams({grant_type:"refresh_token",client_id:cfg.client_id,refresh_token:cfg.refresh_token})});
  if(!response.ok)throw new Error("Fixture token refresh failed");
  const value=await response.json();cfg.access_token=value.access_token;cfg.refresh_token=value.refresh_token||cfg.refresh_token;
  fs.writeFileSync("/test/config.json",JSON.stringify(cfg),{mode:0o600});
 }
 await refresh();
 const client=new MatrixClient(cfg.homeserver_url,cfg.access_token,new SimpleFsStorageProvider("/test/state.json"),new RustSdkCryptoStorageProvider("/test/crypto",StoreType.Sqlite));
 let refreshing=Promise.resolve();
 const refreshTimer=setInterval(()=>{refreshing=refreshing.then(async()=>{await refresh();client.accessToken=cfg.access_token;}).catch(()=>{});},45000);refreshTimer.unref();
 const replies=[];client.on("room.message",(room,event)=>{if(event.content?.body)replies.push({room,event});});
 await client.start();
 try{
  const restored=process.argv[2]==="restored";
  const resume=process.argv[2]==="resume";
  const rooms=(await client.doRequest("GET","/_matrix/client/v3/sync",{timeout:0})).rooms?.invite||{};
  const room=(restored||resume)?JSON.parse(fs.readFileSync('/test/admin-room.json','utf8')).room:Object.keys(rooms).find(r=>rooms[r].invite_state.events.some(e=>e.sender==="@federation-bot:"+cfg.server_name));
  if(!room)throw new Error("Admin notification room absent");
  await client.joinRoom(room);await sleep(4000);
  async function request(command,predicate,bot="@federation-bot:"+cfg.server_name,target=room){
   const start=replies.length;await client.sendText(target,command);
   for(let i=0;i<70;i++){const item=replies.slice(start).find(x=>x.room===target&&x.event.sender===bot&&predicate(x.event.content.body));if(item)return item.event;await sleep(1000);}
   throw new Error("Encrypted result not received");
  }
  const help=await request("помощь",body=>body.includes("Бот федерации"));
  const raw=await client.doRequest("GET","/_matrix/client/v3/rooms/"+encodeURIComponent(room)+"/event/"+encodeURIComponent(help.event_id));
  if(raw.type!=="m.room.encrypted"||raw.content.body)throw new Error("Plaintext admin reply");
  await request("серверы",body=>body.includes('"servers"'));
  fs.writeFileSync("/test/admin-room.json",JSON.stringify({room}));
  console.log("PASS admin encrypted help/status; server stores encrypted event; ordinary bot identity");
  if(restored){console.log("PASS restored federation crypto/session receives and encrypts replies in original room");return;}
  if(!resume){
  stage="manual-invitation";
  let manual=await client.createRoom({preset:"private_chat",visibility:"private",invite:["@federation-bot:"+cfg.server_name],
   initial_state:[{type:"m.room.encryption",state_key:"",content:{algorithm:"m.megolm.v1.aes-sha2"}}]});
  if(typeof manual!=="string")manual=manual.room_id;
  await sleep(3000);
  if((await client.getRoomStateEvent(manual,"m.room.member","@federation-bot:"+cfg.server_name)).membership!=="invite")throw new Error("Default accepted invitation automatically");
  await request("принять комнату "+manual,body=>body.includes('"operation_id"'));
  for(let i=0;i<60;i++){if((await client.getRoomStateEvent(manual,"m.room.member","@federation-bot:"+cfg.server_name)).membership==="join")break;if(i===59)throw new Error("Manual room acceptance failed");await sleep(1000);}
  stage="joined-room-encryption";
  await request("помощь",body=>body.includes("Бот федерации"),"@federation-bot:"+cfg.server_name,manual);
  stage="room-leave";
  await request("покинуть комнату "+manual,body=>body.includes('"operation_id"'));
  for(let i=0;i<60;i++){if((await client.getRoomStateEvent(manual,"m.room.member","@federation-bot:"+cfg.server_name)).membership==="leave")break;if(i===59)throw new Error("Room leave failed");await sleep(1000);}
  stage="room-reject";
  let declined=await client.createRoom({preset:"private_chat",visibility:"private",invite:["@federation-bot:"+cfg.server_name],
   initial_state:[{type:"m.room.encryption",state_key:"",content:{algorithm:"m.megolm.v1.aes-sha2"}}]});if(typeof declined!=="string")declined=declined.room_id;
  await sleep(3000);
  await request("отклонить комнату "+declined,body=>body.includes('"operation_id"'));
  for(let i=0;i<60;i++){if((await client.getRoomStateEvent(declined,"m.room.member","@federation-bot:"+cfg.server_name)).membership==="leave")break;if(i===59)throw new Error("Room rejection failed");await sleep(1000);}
  console.log("PASS real manual invitation acceptance/rejection/leave, default manual, encrypted joined-room command");
  stage="referral";
  const referral="@referral-bot:"+cfg.server_name;
  let direct=await client.createRoom({preset:"private_chat",visibility:"private",invite:[referral],is_direct:true,
   initial_state:[{type:"m.room.encryption",state_key:"",content:{algorithm:"m.megolm.v1.aes-sha2"}}]});
  if(typeof direct!=="string")direct=direct.room_id;
  for(let i=0;i<60;i++){const state=await client.getRoomState(direct);if(state.some(e=>e.type==="m.room.member"&&e.state_key===referral&&e.content.membership==="join"))break;if(i===59)throw new Error("Referral did not join");await sleep(1000);}
  await sleep(4000);
  await request("помощь",body=>body.includes("Реферальный бот"),referral,direct);
  const invitation=await request("пригласить Вымышленный друг",body=>body.includes("Приглашение на"),referral,direct);
  const code=invitation.content.body.match(/MIS-[A-Za-z0-9_-]+/)?.[0];if(!code)throw new Error("Invitation absent");
  const links=invitation.content.body.match(/https:\/\/\S+/g)||[];if(links.length!==2||links.some(x=>x.includes(code)))throw new Error("Unsafe link");
  if(!invitation.content.body.includes("Следующая команда боту: токены"))throw new Error("Next command missing");
  await request("токены",body=>body.includes(code),referral,direct);
  const list=await request("отозвать",body=>body.includes("Какое приглашение"),referral,direct);
  const number=list.content.body.split("\n").find(x=>x.includes(code))?.match(/^(\d+)\./)?.[1];if(!number)throw new Error("Selection absent");
  await request(number,body=>body.includes("отозвано"),referral,direct);
  await request("/invite",body=>body.includes("пригласить")&&body.includes("помощь"),referral,direct);
  console.log("PASS referral Russian create/list/revoke and old-slash help over real E2EE; token-free links");
  }
  stage="durable-policy";
  const ack=await request("добавить сервер "+(resume?"resumed-peer.invalid":"fixture-peer.invalid"),body=>body.includes('"operation_id"'));
  const id=JSON.parse(ack.content.body).operation_id;
  for(let i=0;i<900;i++){
   if(replies.some(x=>x.room===room&&x.event.sender==="@federation-bot:"+cfg.server_name&&x.event.content.body.includes(id)&&x.event.content.body.includes('complete'))){
    console.log("PASS asynchronous bot policy change survives its own checked-backup stop/restart and delivers encrypted result");return;
   }
   await sleep(1000);
  }
  throw new Error("Durable policy result missing");
 }finally{clearInterval(refreshTimer);await refreshing;client.stop();}
}
main().then(()=>process.exit(0)).catch(error=>{
 const code=typeof error?.errcode==="string"&&/^M_[A-Z_]{1,50}$/.test(error.errcode)?error.errcode:"unconfirmed";
 console.log("Matrix fixture failed stage="+stage+" code="+code+"; inspect protected evidence");process.exit(1);
});
