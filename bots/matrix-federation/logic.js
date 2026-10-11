"use strict";

const HELP = `Панель администратора Misbot
версия — установленная и последняя версия, список изменений
обновить — проверка версии и подтверждение обновления
статистика день / неделя / месяц / всего
рассылка — сообщение всем через @server-guide
приветствие — показать или изменить текст для новых пользователей
уведомления — включение и отключение уведомлений
серверы / заявки — федерация
ещё — редкие команды федерации
Обновление через SSH: sudo misbot update latest`;
const MORE = `Федерация: взаимное подключение серверов
проверить сервер example.org
добавить сервер example.org / удалить сервер example.org
пригласить сервер example.org
заявки / операции
подтвердить MB-0123456789abcdef0123456789abcdef
отклонить MB-… / отменить MB-… / повторить MB-… / статус MB-…
приглашения
принять комнату !room:example.org / отклонить комнату !room:example.org
покинуть комнату !room:example.org
пригласить бота example.org !room:example.org
автоприём включить / автоприём выключить
восстановить / откатить — локальные настройки федерации
Применение политики может временно остановить службы.`;

function parse(text) {
    const single = {"серверы":"servers", "заявки":"requests", "операции":"operations", "приглашения":"room-invitations", "восстановить":"recover", "откатить":"rollback"};
    const command=text.toLowerCase();
    if (Object.hasOwn(single,command)) return {action:single[command]};
    // Fold command words only. Matrix room IDs and request IDs are opaque.
    let m=text.match(/^(проверить|добавить|удалить|пригласить) сервер ([^\s]+)$/i);
    if(m)return {action:{"проверить":"check","добавить":"add","удалить":"remove","пригласить":"invite"}[m[1].toLowerCase()],argument:m[2]};
    const requests={"подтвердить":"approve","отклонить":"reject","отменить":"cancel","повторить":"retry","статус":"request-status"};
    m=text.match(/^([^\s]+) (MB-[0-9a-f]{32})$/);
    if(m && Object.hasOwn(requests,m[1].toLowerCase()))return {action:requests[m[1].toLowerCase()],argument:m[2]};
    m=text.match(/^(принять|отклонить|покинуть) комнату (![^\s]+)$/i);
    if(m)return {action:{"принять":"room-accept","отклонить":"room-reject","покинуть":"room-leave"}[m[1].toLowerCase()],argument:m[2]};
    m=text.match(/^пригласить бота ([^\s]+) (![^\s]+)$/i);
    if(m)return {action:"room-invite",argument:JSON.stringify({server:m[1],room:m[2]})};
    m=command.match(/^автоприём (включить|выключить)$/);
    if(m)return {action:"auto-accept",argument:m[1]==="включить"?"true":"false"};
    return null;
}

function statisticsText(result) {
    return `Сообщений: ${result.messages}\nРегистраций: ${result.registrations}\nЗвонки: недоступно\nСредняя длительность звонка: недоступно\nПериод: ${result.start_utc} — ${result.end_utc}\n${result.note}`;
}
function versionText(result) {
    return `Установлена: Misbot ${result.installed}\nПоследняя стабильная: ${result.latest||"релиз ещё не опубликован"}\n${result.update_available?"Доступно обновление.":"Обновление не требуется."}\n${result.notes||""}\n${result.url||""}\nКоманда: ${result.command}`;
}

class FederationBot {
    constructor(client, control, store, config) {Object.assign(this,{client,control,store,config});}
    local(sender) {return sender === (this.config.administrator_mxids?.[0] || "@matrix-admin:"+this.config.server_name);}
    async authorize(sender) {
        if(!this.local(sender))throw new Error("admin_unconfirmed");
        const result=await this.control({action:"authorize",actor:sender});
        if(!result.authorized)throw new Error("admin_unconfirmed");
    }
    async privateRoom(room) {
        const state=await this.client.getRoomState(room);
        const members=state.filter(e=>e.type==="m.room.member" && ["join","invite"].includes(e.content?.membership)).map(e=>e.state_key);
        return members.length===2 && members.includes(this.config.user_id) && members.some(x=>this.local(x)) && state.some(e=>e.type==="m.room.encryption" && e.content?.algorithm==="m.megolm.v1.aes-sha2");
    }
    async invitation(room,event) {
        if(event?.state_key!==this.config.user_id || event.content?.membership!=="invite" || event.sender===this.config.user_id)return;
        if(this.local(event.sender)) {await this.authorize(event.sender);await this.client.joinRoom(room);return;}
        const check=await this.control({action:"auto-invitation",argument:JSON.stringify({sender:event.sender,room})});
        if(check.allowed)await this.client.joinRoom(room);
    }
    async call(action, argument, event) {
        return this.control({action,argument,actor:event.sender,event_id:event.event_id});
    }
    async message(room,event) {
        if(!event?.event_id || !this.local(event.sender) || event.content?.msgtype!=="m.text" || event.content?.["m.relates_to"]?.rel_type==="m.replace")return;
        const original=String(event.content.body||"").trim(),text=original.toLowerCase();
        if(!text || Buffer.byteLength(original)>8000 || this.store.getValue("processed:"+event.event_id))return;
        try {await this.authorize(event.sender);if(!await this.privateRoom(room))return;} catch {return;}
        const reply=async body=>{
            await this.authorize(event.sender);
            if(!await this.privateRoom(room))throw new Error("private_room_required");
            return this.client.sendNotice(room,body);
        };
        const key="draft:"+room;
        let draft=this.store.getValue(key);
        if(draft && Date.now()-draft.created>24*3600000){this.store.setValue(key,null);draft=null;}
        // Authorization and room membership precede every draft mutation and response.
        this.store.setValue("processed:"+event.event_id,Date.now());
        if(text==="отмена" || (draft && text==="отменить")) {this.store.setValue(key,null);await reply("Действие отменено.");return;}
        if(draft?.kind==="broadcast") {
            if(text==="отправить" && draft.body) {
                // Keep the same event ID on retry after a lost RPC response.
                const result=await this.call("panel-broadcast",draft.body,{...event,event_id:draft.id});
                this.store.setValue(key,null);await reply(`Получателей: ${result.queued}. ${result.message}`);return;
            }
            if(text==="рассылка")return reply("Пришлите текст нового сообщения или «отмена».");
            draft={kind:"broadcast",body:original,id:event.event_id,created:Date.now()};this.store.setValue(key,draft);
            await reply(`Сообщение от администратора сервера ${this.config.server_name},\n\n${original}`);
            await reply("Подтверждение рассылки: «отправить» или «отмена». Для редактирования пришлите другой текст в этот чат.");return;
        }
        if(draft?.kind==="welcome") {
            await this.call("panel-welcome",original,event);this.store.setValue(key,null);await reply("Приветствие сохранено. {domain} заменяется доменом сервера.");return;
        }
        if(draft?.kind==="update" && text==="обновить подтверждаю") {
            const result=await this.call("panel-update",draft.version,{...event,event_id:draft.id});
            this.store.setValue(key,null);await reply(`Обновление ${result.version}: ${result.status}. ${result.message||""}`);return;
        }
        if(["помощь","меню","help","/start"].includes(text)) {await reply(HELP);return;}
        if(text==="ещё" || text==="еще") {await reply(MORE);return;}
        if(text==="версия") {await reply(versionText(await this.call("panel-version",null,event)));return;}
        if(text==="обновить") {
            const result=await this.call("panel-version",null,event);await reply(versionText(result));
            if(result.update_available && result.update_supported!==false){this.store.setValue(key,{kind:"update",version:result.latest,id:event.event_id,created:Date.now()});
                await reply("Для запуска напишите «обновить подтверждаю». Отмена: «отмена». Проверим свободный диск до применения; сервер временно остановится. Результат сохранится при перезапуске.");}return;
        }
        if(text==="рассылка") {this.store.setValue(key,{kind:"broadcast",id:event.event_id,created:Date.now()});await reply("Пришлите текст рассылки. Далее покажу итог и попрошу подтверждение. Отмена: «отмена».");return;}
        if(text==="приветствие") {const result=await this.call("panel-welcome",null,event);await reply(result.text);await reply("Изменить: «изменить приветствие».");return;}
        if(text==="изменить приветствие") {this.store.setValue(key,{kind:"welcome",created:Date.now()});await reply("Пришлите новое приветствие. Можно использовать {domain}. Отмена: «отмена».");return;}
        if(text==="уведомления") {
            const result=await this.call("panel-status",null,event),p=result.preferences;
            await reply(`Обновления: ${p.updates?"включены":"выключены"}\nРегистрации: ${p.registrations?"включены":"выключены"}\nЕжемесячная статистика: ${p.statistics?"включена":"выключена"}\nНастройка: «уведомления обновления / регистрации / статистика включить / выключить».`);return;
        }
        let m=text.match(/^уведомления (обновления|регистрации|статистика) (включить|выключить)$/);
        if(m){await this.call("panel-preferences",{key:{обновления:"updates",регистрации:"registrations",статистика:"statistics"}[m[1]],enabled:m[2]==="включить"},event);await reply("Настройка уведомлений сохранена.");return;}
        m=text.match(/^статистика (день|неделя|месяц|всего)$/);
        if(m){await reply(statisticsText(await this.call("panel-statistics",{день:"day",неделя:"week",месяц:"month",всего:"all"}[m[1]],event)));return;}
        const request=parse(original);
        if(!request){await reply(HELP);return;}
        const result=await this.control({...request,actor:event.sender,event_id:event.event_id});
        await reply(JSON.stringify(result,(key,value)=>/backup|migration|move|checkpoint/i.test(key)?undefined:value,2));
    }
    async notifications() {
        const snapshot=await this.control({action:"notifications"});
        for(const room of await this.client.getJoinedRooms()) {
            if(!await this.privateRoom(room))continue;
            const admin=this.config.administrator_mxids?.[0]||"@matrix-admin:"+this.config.server_name;
            try {await this.authorize(admin);} catch {continue;}
            for(const item of snapshot.requests||[]) {
                const key="notified:"+item.id+":"+item.state;
                if(this.store.getValue(key))continue;
                await this.client.sendNotice(room,`Заявка ${item.id}: ${item.server}; ${item.state}. Подтвердить: подтвердить ${item.id}; отказ: отклонить ${item.id}.`);
                this.store.setValue(key,true);
            }
            for(const item of snapshot.operations||[]) {
                const key="operation-notified:"+item.id+":"+item.state;
                if(this.store.getValue(key))continue;
                await this.client.sendNotice(room,"Операция "+item.id+": "+item.state+"\n"+JSON.stringify(item.result,(key,value)=>/backup|migration|move|checkpoint/i.test(key)?undefined:value,2));this.store.setValue(key,true);
            }
            break; // Exactly one private admin room, even if multiple such rooms exist.
        }
    }
}
module.exports={FederationBot,parse,HELP,MORE,statisticsText};
