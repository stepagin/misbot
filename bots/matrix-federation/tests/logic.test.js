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
test("all federation command words ignore case without changing their arguments",()=>{
 const id="MB-"+"0123456789abcdef".repeat(2),room="!MiXeD:example.org",server="Peer.Example";
 for(const [command,action] of Object.entries({серверы:"servers",заявки:"requests",операции:"operations",приглашения:"room-invitations",восстановить:"recover",откатить:"rollback"})){
  assert.deepEqual(parse(command.toUpperCase()),{action},command);
 }
 for(const [command,action] of Object.entries({проверить:"check",добавить:"add",удалить:"remove",пригласить:"invite"})){
  assert.deepEqual(parse(command.toUpperCase()+" СеРвЕр "+server),{action,argument:server},command);
 }
 for(const [command,action] of Object.entries({подтвердить:"approve",отклонить:"reject",отменить:"cancel",повторить:"retry",статус:"request-status"})){
  assert.deepEqual(parse(command.toUpperCase()+" "+id),{action,argument:id},command);
 }
 for(const [command,action] of Object.entries({принять:"room-accept",отклонить:"room-reject",покинуть:"room-leave"})){
  assert.deepEqual(parse(command.toUpperCase()+" КоМнАтУ "+room),{action,argument:room},command);
 }
 assert.deepEqual(parse("ПриГлаСить БОТА "+server+" "+room),{action:"room-invite",argument:JSON.stringify({server,room})});
 assert.deepEqual(parse("АвТоПрИёМ ВКЛЮЧИТЬ"),{action:"auto-accept",argument:"true"});
 assert.deepEqual(parse("АВТОПРИЁМ ВыКлЮчИтЬ"),{action:"auto-accept",argument:"false"});
});
test("request IDs require their exact case and unknown commands stay unknown",()=>{
 const id="MB-"+"a".repeat(32);
 for(const changed of [id.toLowerCase(),id.toUpperCase(),"Mb-"+"a".repeat(32)]){
  assert.equal(parse("ПОДТВЕРДИТЬ "+changed),null,changed);
 }
 for(const unknown of ["constructor","toString","__proto__","НЕИЗВЕСТНО"]){assert.equal(parse(unknown),null);}
});
test("mixed-case federation commands reach the backend with untouched IDs",async()=>{
 const x=setup(),id="MB-"+"a".repeat(32),room="!MiXeD:example.org";
 for(const [body,action,argument] of [
  ["ПОДТВЕРДИТЬ "+id,"approve",id],
  ["ПрИнЯтЬ КОМНАТУ "+room,"room-accept",room],
  ["ПРИГЛАСИТЬ бота Peer.Example "+room,"room-invite",JSON.stringify({server:"Peer.Example",room})],
 ]){
  await x.bot.message("!room",{...x.event,event_id:"$"+action,content:{msgtype:"m.text",body}});
  assert.equal(x.calls.find(c=>c.action===action)?.argument,argument);
 }
 for(const changed of [id.toLowerCase(),id.toUpperCase()]){
  await x.bot.message("!room",{...x.event,event_id:"$"+changed,content:{msgtype:"m.text",body:"ПОДТВЕРДИТЬ "+changed}});
 }
 assert.equal(x.calls.filter(c=>c.action==="approve").length,1);
});
test("everyday commands and settings work in either case",async()=>{
 const commands=[
  ["помощь"], ["меню"], ["help"], ["/start"], ["ещё"], ["еще"],
  ["версия","panel-version"], ["уведомления","panel-status"], ["приветствие","panel-welcome"],
  ...Object.entries({день:"day",неделя:"week",месяц:"month",всего:"all"}).map(([period,value])=>["статистика "+period,"panel-statistics",value]),
  ...Object.entries({обновления:"updates",регистрации:"registrations",статистика:"statistics"}).flatMap(([name,key])=>[
   ["уведомления "+name+" включить","panel-preferences",{key,enabled:true}],
   ["уведомления "+name+" выключить","panel-preferences",{key,enabled:false}],
  ]),
 ];
 for(const [command,action,argument] of commands){
  for(const body of [command,command.toUpperCase()]){
   const x=setup();x.bot.control=async request=>{
    x.calls.push(request);
    return request.action==="authorize"?{authorized:true}:{installed:"1.1.0",latest:"1.1.0",preferences:{updates:true,registrations:true,statistics:true},text:"Добро пожаловать!",messages:3,registrations:1};
   };
   await x.bot.message("!room",{...x.event,content:{msgtype:"m.text",body}});
   assert.ok(x.sent.length,body);
   const operations=x.calls.filter(c=>c.action!=="authorize");
   assert.equal(operations.length,action?1:0,body);
   if(action){assert.equal(operations[0].action,action,body);if(argument!==undefined)assert.deepEqual(operations[0].argument,argument,body);}
  }
 }
});
test("welcome editing preserves text case and recognizes mixed-case cancellation",async()=>{
 const x=setup();let seq=0;const send=body=>x.bot.message("!room",{...x.event,event_id:"$welcome"+(++seq),content:{msgtype:"m.text",body}});
 await send("ИзМеНиТь ПрИвЕтСтВиЕ");await send("Добро пожаловать на {domain}! Matrix ID: @Alice:example.org");
 const welcome=x.calls.find(c=>c.action==="panel-welcome");assert.equal(welcome.argument,"Добро пожаловать на {domain}! Matrix ID: @Alice:example.org");
 for(const cancel of ["ОтМеНа","ОТМЕНИТЬ"]){
  await send("ИЗМЕНИТЬ ПРИВЕТСТВИЕ");await send(cancel);assert.equal(x.sent.at(-1),"Действие отменено.");
 }
 assert.equal(x.calls.filter(c=>c.action==="panel-welcome").length,1);
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
 await send("РаСсЫлКа");await send("Первые Новости");assert.equal(x.sent.at(-2),"Сообщение от администратора сервера example.org,\n\nПервые Новости");assert.match(x.sent.at(-1),/Подтверждение/);
 await send("Новые НОВОСТИ");assert.equal(x.sent.at(-2),"Сообщение от администратора сервера example.org,\n\nНовые НОВОСТИ");assert.equal(x.calls.filter(c=>c.action==="panel-broadcast").length,0);
 x.bot.control=async r=>{x.calls.push(r);return r.action==="authorize"?{authorized:true}:{queued:2,message:"очередь"};};
 await send("ОТПРАВИТЬ");const calls=x.calls.filter(c=>c.action==="panel-broadcast");assert.equal(calls.length,1);assert.equal(calls[0].argument,"Новые НОВОСТИ");assert.equal(calls[0].event_id,"$3");
 await send("Рассылка");await send("Не отправлять");await send("ОтМеНа");assert.equal(x.calls.filter(c=>c.action==="panel-broadcast").length,1);
});
test("confirmed update is pinned and retries use the original event",async()=>{
 const x=setup();x.bot.control=async r=>{x.calls.push(r);return r.action==="authorize"?{authorized:true}:r.action==="panel-version"?{installed:"1.1.0",latest:"1.2.0",update_available:true,command:"sudo misbot update latest"}:{version:r.argument,status:"queued"};};
 await x.bot.message("!room",{...x.event,event_id:"$version",content:{msgtype:"m.text",body:"ОбНоВиТь"}});
 assert.equal(x.calls.filter(r=>r.action==="panel-update").length,0);
 await x.bot.message("!room",{...x.event,event_id:"$confirm",content:{msgtype:"m.text",body:"ОБНОВИТЬ ПодТверЖдаЮ"}});
 const update=x.calls.find(r=>r.action==="panel-update");assert.equal(update.argument,"1.2.0");assert.equal(update.event_id,"$version");
});
test("owner revocation during an operation prevents its reply",async()=>{
 const x=setup(),original=x.bot.control;
 x.bot.control=async request=>{const result=await original(request);if(request.action==="add")x.revoke();return result;};
 await assert.rejects(x.bot.message("!room",x.event));assert.equal(x.sent.length,0);
});
