"use strict";

async function privateRoom(client, room, bot, recipient) {
    const state = await client.getRoomState(room);
    const members = state
        .filter(
            (e) => e.type === "m.room.member" && ["join", "invite"].includes(e.content?.membership)
        )
        .map((e) => e.state_key);
    return (
        members.length === 2 &&
        members.includes(bot) &&
        members.includes(recipient) &&
        state.some(
            (e) => e.type === "m.room.encryption" && e.content?.algorithm === "m.megolm.v1.aes-sha2"
        )
    );
}

// The SDK generates a new transaction ID for sendEvent(). Persist ciphertext and
// use the outbox ID as Matrix transaction ID to recover an ambiguous HTTP reply.
async function sendOnce(client, store, room, item) {
    const key = "delivery:" + item.id;
    if (store.getValue(key) === "sent") return;
    let saved = store.getValue(key);
    if (!saved) {
        const content = { msgtype: "m.notice", body: item.body };
        const encrypted = await client.crypto.encryptRoomEvent(room, "m.room.message", content);
        saved = { room, type: "m.room.encrypted", content: encrypted };
        store.setValue(key, saved);
    }
    if (saved.room !== room) throw new Error("delivery_room_changed");
    await client.doRequest(
        "PUT",
        `/_matrix/client/v3/rooms/${encodeURIComponent(room)}/send/${saved.type}/${item.id}`,
        null,
        saved.content
    );
    store.setValue(key, "sent");
}

class WelcomeBot {
    constructor(client, store, config) {
        Object.assign(this, { client, store, config });
    }
    // User messages have no listener and generate no reply.
    async room(recipient) {
        const key = "guide-room:" + recipient;
        const saved = this.store.getValue(key);
        if (saved) {
            if (await privateRoom(this.client, saved, this.config.user_id, recipient)) return saved;
            // Respect a declined/closed DM and never recreate invitations on retries.
            throw new Error("guide_room_unavailable");
        }
        const pending = "guide-creating:" + recipient;
        if (this.store.getValue(pending)) {
            for (const room of await this.client.getJoinedRooms()) {
                if (await privateRoom(this.client, room, this.config.user_id, recipient)) {
                    this.store.setValue(key, room);
                    this.store.setValue(pending, false);
                    return room;
                }
            }
        }
        this.store.setValue(pending, true);
        const created = await this.client.createRoom({
            preset: "private_chat",
            visibility: "private",
            is_direct: true,
            invite: [recipient],
            name: "Новости сервера " + this.config.server_name,
            initial_state: [
                {
                    type: "m.room.encryption",
                    state_key: "",
                    content: { algorithm: "m.megolm.v1.aes-sha2" }
                }
            ]
        });
        const room = typeof created === "string" ? created : created.room_id;
        this.store.setValue(key, room);
        this.store.setValue(pending, false);
        return room;
    }
    async deliver(item) {
        const room = await this.room(item.recipient);
        if (!(await privateRoom(this.client, room, this.config.user_id, item.recipient)))
            throw new Error("private_room_required");
        await sendOnce(this.client, this.store, room, item);
    }
}

class DeliveryQueue {
    constructor(admin, welcome, control, store, config, adminBot) {
        Object.assign(this, { admin, welcome, control, store, config, adminBot });
    }
    async poll() {
        const actor = this.config.administrator_mxids[0];
        const result = await this.control({ action: "panel-outbox", actor });
        for (const item of result.items) {
            let delivered = false;
            try {
                if (item.via === "welcome") await this.welcome.deliver(item);
                else {
                    await this.adminBot.authorize(actor);
                    let room = this.store.getValue("admin-room");
                    if (
                        !room ||
                        !(await privateRoom(this.admin, room, this.config.user_id, actor))
                    ) {
                        room = null;
                        for (const candidate of await this.admin.getJoinedRooms()) {
                            if (
                                await privateRoom(this.admin, candidate, this.config.user_id, actor)
                            ) {
                                room = candidate;
                                break;
                            }
                        }
                    }
                    if (!room || item.recipient !== actor) throw new Error("admin_room_required");
                    await sendOnce(this.admin, this.store, room, item);
                }
                delivered = true;
            } catch {
                /* SDK errors may contain credentials or decrypted messages. */
            }
            await this.control({
                action: "panel-ack",
                actor,
                argument: { id: item.id, delivered }
            });
        }
    }
}
module.exports = { WelcomeBot, DeliveryQueue, privateRoom, sendOnce };
