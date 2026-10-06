"use strict";

const HELP = `Бот федерации — только для серверных администраторов этого сервера.
помощь
серверы
проверить сервер example.org
добавить сервер example.org
удалить сервер example.org
пригласить сервер example.org — заявка на взаимное подключение, не приглашение в комнату
заявки
операции — результаты изменений после перезапуска бота
подтвердить MB-0123456789abcdef0123456789abcdef
отклонить MB-0123456789abcdef0123456789abcdef
отменить MB-0123456789abcdef0123456789abcdef
повторить MB-0123456789abcdef0123456789abcdef
статус MB-0123456789abcdef0123456789abcdef
приглашения — комнаты, адресованные самому боту
принять комнату !room:example.org
отклонить комнату !room:example.org
покинуть комнату !room:example.org
пригласить бота example.org !room:example.org
автоприём включить / автоприём выключить (default выключено)
восстановить — завершить локальное применение из checkpoint
откатить — вернуть прежнюю локальную конфигурацию
Добавление в allowlist, согласование серверов и членство в комнате — отдельные операции.
Применение требует проверенной копии и перезапуска Synapse/Web; HTTPS не доказывает работу федеративных комнат.`;

function parse(text) {
    const single = {"серверы":"servers", "заявки":"requests", "операции":"operations", "приглашения":"room-invitations", "восстановить":"recover", "откатить":"rollback"};
    if (single[text]) return {action:single[text]};
    let m=text.match(/^(проверить|добавить|удалить|пригласить) сервер ([^\s]+)$/);
    if(m)return {action:{"проверить":"check","добавить":"add","удалить":"remove","пригласить":"invite"}[m[1]],argument:m[2]};
    m=text.match(/^(подтвердить|отклонить|отменить|повторить|статус) (MB-[0-9a-f]{32})$/);
    if(m)return {action:{"подтвердить":"approve","отклонить":"reject","отменить":"cancel","повторить":"retry","статус":"request-status"}[m[1]],argument:m[2]};
    m=text.match(/^(принять|отклонить|покинуть) комнату (![^\s]+)$/);
    if(m)return {action:{"принять":"room-accept","отклонить":"room-reject","покинуть":"room-leave"}[m[1]],argument:m[2]};
    m=text.match(/^пригласить бота ([^\s]+) (![^\s]+)$/);
    if(m)return {action:"room-invite",argument:JSON.stringify({server:m[1],room:m[2]})};
    m=text.match(/^автоприём (включить|выключить)$/);
    if(m)return {action:"auto-accept",argument:m[1]==="включить"?"true":"false"};
    return null;
}

class FederationBot {
    constructor(client, control, store, config) {Object.assign(this,{client,control,store,config});}
    local(sender) {return typeof sender==="string" && sender.startsWith("@") && sender.endsWith(":"+this.config.server_name) && sender!==this.config.user_id;}
    async authorize(sender) {
        if(!this.local(sender))throw new Error("admin_unconfirmed");
        const result=await this.control({action:"authorize",actor:sender});
        if(!result.authorized)throw new Error("admin_unconfirmed");
    }
    async invitation(room,event) {
        if(event?.state_key!==this.config.user_id || event.content?.membership!=="invite" || event.sender===this.config.user_id)return;
        // Default is manual. A trusted peer is authenticated by the persisted protocol identity.
        const check=await this.control({action:"auto-invitation",argument:JSON.stringify({sender:event.sender,room})});
        if(check.allowed) {
            await this.client.joinRoom(room);
        }
    }
    async message(room,event) {
        if(!event?.event_id || event.sender===this.config.user_id || event.content?.msgtype!=="m.text" || event.content?.["m.relates_to"]?.rel_type==="m.replace")return;
        const text=String(event.content.body||"").trim().toLowerCase();
        if(!text || text.length>512)return;
        // Preserve case of opaque room/event identifiers; only command words are lowercase.
        const original=String(event.content.body||"").trim();
        const request=parse(original) || (original===text ? parse(text):null);
        if(this.store.getValue("processed:"+event.event_id))return;
        try {await this.authorize(event.sender);}
        catch {return;} // No private command output to unconfirmed accounts.
        if(text==="помощь" || !request) {await this.client.sendNotice(room,HELP);return;}
        this.store.setValue("processed:"+event.event_id,Date.now());
        const result=await this.control({...request,actor:event.sender,event_id:event.event_id});
        await this.client.sendNotice(room,JSON.stringify(result,null,2));
    }
    async notifications() {
        const snapshot=await this.control({action:"notifications"});const requests=snapshot.requests;
        for(const room of await this.client.getJoinedRooms()) {
            const state=await this.client.getRoomState(room);
            // Notify only local admins, in a private bot/admin room; never leak requests to peers.
            const members=state.filter(e=>e.type==="m.room.member" && ["join","invite"].includes(e.content?.membership)).map(e=>e.state_key);
            if(members.length!==2 || !members.includes(this.config.user_id))continue;
            const admin=members.find(x=>x!==this.config.user_id);
            try {await this.authorize(admin);} catch {continue;}
            for(const item of requests) {
                const key="notified:"+room+":"+item.id+":"+item.state;
                if(this.store.getValue(key))continue;
                await this.client.sendNotice(room,`Заявка ${item.id}: ${item.server}; ${item.state}. Согласуется только взаимный allowlist. Применение: копия и перезапуск Synapse/Web. Срок: ${new Date(item.expires_at*1000).toISOString()}. Подтвердить: подтвердить ${item.id}; отказ: отклонить ${item.id}.`);
                this.store.setValue(key,true);
            }
            for(const item of snapshot.operations||[]) {
                const key="operation-notified:"+room+":"+item.id+":"+item.state;
                if(this.store.getValue(key))continue;
                await this.client.sendNotice(room,"Операция "+item.id+": "+item.state+"\n"+JSON.stringify(item.result,null,2));
                this.store.setValue(key,true);
            }
        }
    }
}
module.exports={FederationBot,parse,HELP};
