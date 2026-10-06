"use strict";
const test=require("node:test"),assert=require("node:assert/strict");
const {FederationBot,parse,HELP}=require("../logic");
function setup(){
 const cfg={user_id:"@federation-bot:example.org",server_name:"example.org"},sent=[],calls=[],saved=new Map();let allowed=true;
 const client={sendNotice:async(r,b)=>sent.push(b),joinRoom:async r=>sent.push("join "+r),getRoomStateEvent:async()=>({membership:"invite"})};
 const control=async r=>{calls.push(r);if(r.action==="authorize"&& !allowed)throw new Error();return r.action==="authorize"?{authorized:true}:r.action==="auto-invitation"?{allowed:false}:{status:"applied"};};
 const bot=new FederationBot(client,control,{getValue:k=>saved.get(k),setValue:(k,v)=>saved.set(k,v)},cfg);
 const event={event_id:"$a",sender:"@admin:example.org",content:{msgtype:"m.text",body:"добавить сервер other.example"}};
 return {bot,event,calls,sent,client,cfg,revoke:()=>{allowed=false;}};
}
test("Russian grammar and opaque IDs",()=>{
 assert.equal(parse("подтвердить MB-"+"a".repeat(32)).action,"approve");assert.equal(parse("подтвердить 1"),null);
 assert.equal(parse("принять комнату !Case:example.org").argument,"!Case:example.org");assert.match(HELP,/взаимное подключение/);
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
