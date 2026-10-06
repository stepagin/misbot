"use strict";

function escapeHtml(value) {
    return String(value).replace(/[&<>"']/g, x => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[x]));
}
function notice(body, html) {
    return {msgtype: "m.notice", body, ...(html ? {format: "org.matrix.custom.html", formatted_body: html} : {})};
}
function expiry(value) {
    if (!value) return "без срока";
    const time = new Date(new Date(value).getTime() + 3 * 60 * 60 * 1000);
    const pad = n => String(n).padStart(2, "0");
    return `${pad(time.getUTCDate())}.${pad(time.getUTCMonth() + 1)}.${time.getUTCFullYear()} ${pad(time.getUTCHours())}:${pad(time.getUTCMinutes())} МСК`;
}
function remaining(item, now = Date.now()) {
    const a = item.attributes;
    if (a.revoked_at) return "отозван";
    if (a.times_used >= (a.usage_limit || 1)) return "использован";
    if (!a.expires_at) return "без срока";
    const minutes = Math.ceil((new Date(a.expires_at).getTime() - now) / 60000);
    if (minutes <= 0) return "истёк";
    const days = Math.floor(minutes / 1440), hours = Math.floor(minutes % 1440 / 60), mins = minutes % 60;
    return `осталось ${days ? `${days} д ${hours} ч` : hours ? `${hours} ч ${mins} мин` : `${mins} мин`}`;
}
function tokenMessage(item, server, bot = "@referral-bot:" + server) {
    const a = item.attributes;
    const uses = a.usage_limit === 1 ? "Один код — один человек." : `Регистраций по этому коду: ${a.usage_limit}.`;
    const body = `Приглашение на ${server}\n\n${a.token}\n\nДо: ${expiry(a.expires_at)}\n${uses}\n\nОткрой Element X, укажи сервер ${server} и введи код при регистрации.

Регистрация: https://web.${server}
Реферальный бот помогает управлять своими кодами: https://matrix.to/#/${encodeURIComponent(bot)}
Следующая команда боту: токены.`;
    const html = escapeHtml(body).replace(escapeHtml(a.token), `<code>${escapeHtml(a.token)}</code>`).replace(/\n/g, "<br>");
    return notice(body, html);
}

class ReferralBot {
    constructor(client, control, store, config) { Object.assign(this, {client, control, store, config}); }
    local(mxid) { return typeof mxid === "string" && mxid.startsWith("@") && mxid.endsWith(":" + this.config.server_name) && mxid !== this.config.user_id; }
    call(action, mxid, extras = {}) { return this.control({action: "matrix_" + action, server: this.config.server_name, mxid, ...extras}); }
    key(kind, room, sender) { return JSON.stringify([kind, room, sender]); }
    label(sender, id) { return this.store.getValue(JSON.stringify(["label", sender, id])) || ""; }
    async invitation(roomId, event) {
        if (!this.local(event.sender)) return;
        await this.call("identity", event.sender);
        await this.client.joinRoom(roomId);
    }
    async privateRoom(roomId, sender) {
        if (!this.local(sender)) return false;
        const state = await this.client.getRoomState(roomId);
        const members = state.filter(e => e.type === "m.room.member" && ["join", "invite"].includes(e.content?.membership)).map(e => e.state_key);
        return members.length === 2 && members.includes(sender) && members.includes(this.config.user_id);
    }
    async encryptedRoom(roomId) {
        const e = await this.client.getRoomStateEvent(roomId, "m.room.encryption", "");
        return e?.algorithm === "m.megolm.v1.aes-sha2";
    }
    async menu(roomId) {
        const seconds = this.config.invitation_lifetime_seconds || 604800;
        const usage = this.config.invitation_usage_limit || 1;
        const duration = seconds % 86400 === 0 ? `${seconds / 86400} д` : seconds % 3600 === 0 ? `${seconds / 3600} ч` : `${seconds} с`;
        return this.client.sendNotice(roomId, `Реферальный бот\n\nпригласить — создать приглашение\nпригласить Для Антона — создать с названием\nтокены [offset] — мои приглашения (например: токены 20)\nотозвать [ID] — выбрать и отозвать своё приглашение
помощь — эта справка
отмена — отменить выбор; ещё — следующая страница выбора\n\nРегистраций на код: ${usage}. Срок: ${duration}.`);
    }
    async list(room, sender, offset, revoke = false, previous = null) {
        const page = await this.call("tokens", sender, {offset, limit: 20});
        if (!page.tokens.length) {
            if (revoke && !previous) this.store.setValue(this.key("revoke", room, sender), null);
            return this.client.sendNotice(room, previous ? "Больше приглашений нет. Выбери номер из списка или отмена." : "Нет приглашений для показа.");
        }
        const first = revoke ? (previous?.ids.length || 0) : offset;
        const lines = page.tokens.map((item, i) => {
            const label = this.label(sender, item.id);
            return {text: `${first + i + 1}. ${label ? label + " · " : ""}${item.attributes.token} — ${remaining(item)}`,
                html: `${first + i + 1}. ${label ? escapeHtml(label) + " · " : ""}<code>${escapeHtml(item.attributes.token)}</code> — ${escapeHtml(remaining(item))}`};
        });
        const title = revoke ? "Какое приглашение отозвать?" : "Мои приглашения";
        const footer = revoke
            ? `Напиши номер. Отмена: отмена или «отмена». Выбор действует 15 минут.${page.has_more ? "\nЕщё приглашения: ещё" : ""}`
            : page.has_more ? `Ещё приглашения: токены ${offset + 20}` : "";
        if (revoke) this.store.setValue(this.key("revoke", room, sender), {
            ids: [...(previous?.ids || []), ...page.tokens.map(x => x.id)],
            offset: offset + page.tokens.length, hasMore: page.has_more, expires: Date.now() + 900000,
        });
        const body = [title, lines.map(x => x.text).join("\n"), footer].filter(Boolean).join("\n\n");
        const html = [escapeHtml(title), lines.map(x => x.html).join("<br>"), escapeHtml(footer).replace(/\n/g, "<br>")].filter(Boolean).join("<br><br>");
        return this.client.sendMessage(room, notice(body, html));
    }
    async handle(room, event, text) {
        const sender = event.sender;
        const key = this.key("revoke", room, sender);
        if (text.startsWith("/")) return this.menu(room);
        const match = text.match(/^(\S+)(?:\s+([\s\S]*))?$/);
        const command = match[1].toLowerCase(), arg = match[2]?.trim();
        let dialog = this.store.getValue(key);
        if (["отмена", "0"].includes(command)) {
            this.store.setValue(key, null);
            return this.client.sendNotice(room, "Отзыв отменён. Коды сохранены.");
        }
        if (/^\d+$/.test(text) || command === "ещё") {
            if (!dialog || dialog.expires <= Date.now()) {
                this.store.setValue(key, null);
                return this.client.sendNotice(room, "Нет активного выбора. Напиши отозвать, чтобы получить свежий список.");
            }
            if (command === "ещё") {
                if (!dialog.hasMore) return this.client.sendNotice(room, "Это весь список. Выбери номер или отмена.");
                return this.list(room, sender, dialog.offset, true, dialog);
            }
            const index = Number(text) - 1;
            if (!Number.isSafeInteger(index) || index < 0 || index >= dialog.ids.length)
                return this.client.sendNotice(room, "Такого номера нет. Выбери номер из списка или отмена.");
            // The saved ID is tied to this exact room/user and shown list, not
            // a refreshed position which could point to another invitation.
            await this.call("revoke", sender, {resource: dialog.ids[index]});
            this.store.setValue(key, null);
            return this.client.sendNotice(room, `Приглашение №${index + 1} отозвано.`);
        }
        this.store.setValue(key, null); // A new command cancels an old selection.
        if (["пригласить"].includes(command)) {
            if (arg && (arg.length > 80 || /[\r\n\x00-\x1f\x7f]/.test(arg)))
                return this.client.sendNotice(room, "Название: одна строка, до 80 символов. Например: пригласить Для Антона.");
            const item = await this.call("invite", sender, {request_id: "matrix:" + event.event_id});
            if (arg) this.store.setValue(JSON.stringify(["label", sender, item.id]), arg);
            await this.client.sendMessage(room, tokenMessage(item, this.config.server_name, this.config.user_id));
            if (arg) await this.client.sendNotice(room, `Название для тебя: ${arg}`);
        } else if (["токены"].includes(command)) {
            const offset = arg === undefined ? 0 : Number(arg);
            if (!Number.isSafeInteger(offset) || offset < 0) return this.client.sendNotice(room, "Просмотр: токены или токены 20.");
            await this.list(room, sender, offset);
        } else if (["отозвать"].includes(command)) {
            if (arg) {
                await this.call("revoke", sender, {resource: arg});
                await this.client.sendNotice(room, "Приглашение отозвано.");
            } else await this.list(room, sender, 0, true);
        } else await this.menu(room);
    }
    async message(roomId, event) {
        if (event.content?.["m.relates_to"]?.rel_type === "m.replace") return;
        if (!event?.event_id || event.sender === this.config.user_id || event.content?.msgtype !== "m.text") return;
        if (!await this.privateRoom(roomId, event.sender) || !await this.encryptedRoom(roomId)) return;
        if (this.store.getValue("processed:" + event.event_id)) return;
        const text = String(event.content.body || "").trim();
        if (!text || text.length > 256) return;
        await this.call("identity", event.sender);
        await this.handle(roomId, event, text);
        this.store.setValue("processed:" + event.event_id, Date.now());
    }
}
module.exports = {ReferralBot, tokenMessage, expiry, remaining};
