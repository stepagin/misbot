"use strict";
const test=require("node:test"),assert=require("node:assert/strict");
const {FederationBot,parse,HELP,MORE}=require("../logic");
function setup(){
 const cfg={user_id:"@federation-bot:example.org",server_name:"example.org",administrator_mxids:["@admin:example.org"]},sent=[],calls=[],saved=new Map();let allowed=true;
 const client={getRoomState:async()=>[{type:"m.room.member",state_key:cfg.user_id,content:{membership:"join"}},{type:"m.room.member",state_key:"@admin:example.org",content:{membership:"join"}},{type:"m.room.encryption",content:{algorithm:"m.megolm.v1.aes-sha2"}}],sendNotice:async(r,b)=>sent.push(b),joinRoom:async r=>sent.push("join "+r),getRoomStateEvent:async()=>({membership:"invite"})};
 const control=async r=>{calls.push(r);if(r.action==="authorize"&& !allowed)throw new Error();return r.action==="authorize"?{authorized:true}:r.action==="auto-invitation"?{allowed:false}:{status:"applied"};};
 const bot=new FederationBot(client,control,{getValue:k=>saved.get(k),setValue:(k,v)=>saved.set(k,v)},cfg);
 const event={event_id:"$a",sender:"@admin:example.org",content:{msgtype:"m.text",body:"добавить сервер other.example"}};
 return {bot,event,calls,sent,client,cfg,revoke:()=>{allowed=false;}};
}
test("Russian grammar and opaque IDs",()=>{
 assert.equal(parse("подтвердить MB-"+"a".repeat(32)).action,"approve");assert.equal(parse("подтвердить 1"),null);
 assert.equal(parse("принять комнату !Case:example.org").argument,"!Case:example.org");assert.match(MORE,/взаимное подключение/);assert.doesNotMatch(HELP+MORE,/копи|backup|перенос/i);
});
test("current rights and event replay/self/edit",async()=>{
 const x=setup();await x.bot.message("!room",x.event);await x.bot.message("!room",x.event);
 assert.equal(x.calls.filter(r=>r.action==="add").length,1);
 for(const sender of [x.cfg.user_id,"@admin:foreign.example"]){await x.bot.message("!room",{...x.event,event_id:"$foreign",sender});}
 await x.bot.message("!room",{...x.event,event_id:"$edit",content:{...x.event.content,"m.relates_to":{rel_type:"m.replace"}}});
 x.revoke();await x.bot.message("!room",{...x.event,event_id:"$revoked"});assert.equal(x.calls.filter(r=>r.action==="add").length,1);
});
test("manual default and explicit trusted auto invitation",async()=>{
 const x=setup(),event={state_key:x.cfg.user_id,sender:"@federation-bot:other.example",content:{membership:"invite"}};
 await x.bot.invitation("!room",event);assert.equal(x.sent.length,0);
 x.bot.control=async()=>({allowed:true});await x.bot.invitation("!room",event);assert.equal(x.sent[0],"join !room");
 x.bot.control=async()=>({allowed:false});await x.bot.invitation("!revoked",event);assert.equal(x.sent.length,1);
});
test("only the configured owner receives replies; group commands are ignored",async()=>{
 const x=setup();await x.bot.message("!room",{...x.event,sender:"@another-admin:example.org"});assert.equal(x.sent.length,0);assert.equal(x.calls.length,0);
 x.client.getRoomState=async()=>[x.cfg.user_id,"@admin:example.org","@guest:example.org"].map(state_key=>({type:"m.room.member",state_key,content:{membership:"join"}}));
 await x.bot.message("!group",x.event);assert.equal(x.sent.length,0);assert.equal(x.calls.filter(c=>c.action==="add").length,0);
});
test("broadcast preview is separate from confirmation, edit and cancel require no delivery",async()=>{
 const x=setup();let seq=0;const send=body=>x.bot.message("!room",{...x.event,event_id:"$"+(++seq),content:{msgtype:"m.text",body}});
 await send("рассылка");await send("Первые новости");assert.equal(x.sent.at(-2),"Сообщение от администратора сервера example.org,\n\nПервые новости");assert.match(x.sent.at(-1),/Подтверждение/);
 await send("Новые новости");assert.match(x.sent.at(-2),/Новые новости/);assert.equal(x.calls.filter(c=>c.action==="panel-broadcast").length,0);
 x.bot.control=async r=>{x.calls.push(r);return r.action==="authorize"?{authorized:true}:{queued:2,message:"очередь"};};
 await send("отправить");const calls=x.calls.filter(c=>c.action==="panel-broadcast");assert.equal(calls.length,1);assert.equal(calls[0].argument,"Новые новости");assert.equal(calls[0].event_id,"$3");
 await send("рассылка");await send("Не отправлять");await send("отмена");assert.equal(x.calls.filter(c=>c.action==="panel-broadcast").length,1);
});
test("confirmed update is pinned and retries use the original event",async()=>{
 const x=setup();x.bot.control=async r=>{x.calls.push(r);return r.action==="authorize"?{authorized:true}:r.action==="panel-version"?{installed:"1.1.0",latest:"1.2.0",update_available:true,command:"sudo misbot update latest"}:{version:r.argument,status:"queued"};};
 await x.bot.message("!room",{...x.event,event_id:"$version",content:{msgtype:"m.text",body:"обновить"}});
 assert.equal(x.calls.filter(r=>r.action==="panel-update").length,0);
 await x.bot.message("!room",{...x.event,event_id:"$confirm",content:{msgtype:"m.text",body:"обновить подтверждаю"}});
 const update=x.calls.find(r=>r.action==="panel-update");assert.equal(update.argument,"1.2.0");assert.equal(update.event_id,"$version");
});
test("owner revocation during an operation prevents its reply",async()=>{
 const x=setup(),original=x.bot.control;
 x.bot.control=async request=>{const result=await original(request);if(request.action==="add")x.revoke();return result;};
 await assert.rejects(x.bot.message("!room",x.event));assert.equal(x.sent.length,0);
});
