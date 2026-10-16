"use strict";
const { noticeContent } = require("./logic");

function deliveryError(code) {
    const error = new Error(code);
    error.code = code;
    return error;
}

async function privateRoom(client, room, bot, recipient, requireJoined = false) {
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
        (!requireJoined ||
            [bot, recipient].every((user) =>
                state.some(
                    (e) =>
                        e.type === "m.room.member" &&
                        e.state_key === user &&
                        e.content?.membership === "join"
                )
            )) &&
        state.some(
            (e) => e.type === "m.room.encryption" && e.content?.algorithm === "m.megolm.v1.aes-sha2"
        )
    );
}

// The SDK generates a new transaction ID for sendEvent(). Persist ciphertext and
// use the outbox ID as Matrix transaction ID to recover an ambiguous HTTP reply.
async function sendOnce(client, store, room, item) {
    if (
        typeof item.id !== "string" ||
        !/^[a-f0-9]{64}$/.test(item.id) ||
        typeof item.body !== "string" ||
        !item.body ||
        Buffer.byteLength(item.body) > 16384
    )
        throw deliveryError("invalid_delivery");
    const key = "delivery:" + item.id;
    if (store.getValue(key) === "sent") return;
    let saved = store.getValue(key);
    const retry = !!saved;
    if (!saved) {
        const content = noticeContent(item.body, { literal: item.via !== "admin" });
        const encrypted = await client.crypto.encryptRoomEvent(room, "m.room.message", content);
        saved = { room, type: "m.room.encrypted", content: encrypted };
        store.setValue(key, saved);
    }
    if (saved.room !== room) throw deliveryError("delivery_room_changed");
    // Re-run public SDK preparation to share the current session key after a
    // restart/join. Keep the original ciphertext and transaction for the PUT.
    // A rotated session cannot decrypt the saved event: do not confirm delivery.
    if (retry) {
        const prepared = await client.crypto.encryptRoomEvent(room, "m.room.message", {
            msgtype: "m.notice",
            body: item.body
        });
        if (prepared.session_id !== saved.content.session_id)
            throw deliveryError("delivery_key_session_changed");
    }
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
            throw deliveryError("guide_room_unavailable");
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
            // An ambiguous create response is not permission to invite again.
            // Keep the intent and retry discovery, including after a restart.
            throw deliveryError("guide_room_creation_unconfirmed");
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
        if (!(await privateRoom(this.client, room, this.config.user_id, item.recipient, true)))
            throw deliveryError("recipient_not_joined");
        await sendOnce(this.client, this.store, room, item);
    }
    async shareCurrentKeys(room) {
        const state = await this.client.getRoomState(room);
        const recipient = state.find(
            (e) =>
                e.type === "m.room.member" &&
                e.state_key !== this.config.user_id &&
                e.content?.membership === "join"
        )?.state_key;
        if (
            !recipient ||
            this.store.getValue("guide-room:" + recipient) !== room ||
            !(await privateRoom(this.client, room, this.config.user_id, recipient, true))
        )
            return;
        // Sharing produces no room message. It can recover the still-current
        // session of a legacy sent welcome, never a discarded/rotated session.
        await this.client.crypto.encryptRoomEvent(room, "m.room.message", {
            msgtype: "m.notice",
            body: ""
        });
        return true;
    }
}

class DeliveryQueue {
    constructor(admin, welcome, control, store, config, adminBot, diagnostic = () => {}) {
        Object.assign(this, { admin, welcome, control, store, config, adminBot, diagnostic });
    }
    async poll() {
        const actor = this.config.administrator_mxids[0];
        const result = await this.control({ action: "panel-outbox", actor });
        for (const item of result.items) {
            let delivered = false,
                waitingForJoin = false;
            try {
                if (item.via === "welcome") await this.welcome.deliver(item);
                else {
                    const recipient = item.recipient;
                    await this.adminBot.authorize(recipient);
                    let room =
                        this.store.getValue("admin-room:" + recipient) ||
                        (recipient === actor ? this.store.getValue("admin-room") : null);
                    if (
                        !room ||
                        !(await privateRoom(this.admin, room, this.config.user_id, recipient))
                    ) {
                        room = null;
                        for (const candidate of await this.admin.getJoinedRooms()) {
                            if (
                                await privateRoom(
                                    this.admin,
                                    candidate,
                                    this.config.user_id,
                                    recipient
                                )
                            ) {
                                room = candidate;
                                break;
                            }
                        }
                    }
                    if (!room) throw new Error("admin_room_required");
                    if (
                        !(await privateRoom(this.admin, room, this.config.user_id, recipient, true))
                    )
                        throw deliveryError("recipient_not_joined");
                    await sendOnce(this.admin, this.store, room, item);
                }
                delivered = true;
            } catch (error) {
                /* SDK errors may contain credentials or decrypted messages. */
                waitingForJoin = error?.code === "recipient_not_joined";
                if (!waitingForJoin)
                    this.diagnostic(
                        error?.code === "delivery_key_session_changed"
                            ? "delivery_key_session_changed"
                            : "delivery_unconfirmed"
                    );
            }
            await this.control({
                action: "panel-ack",
                actor,
                argument: {
                    id: item.id,
                    delivered,
                    ...(waitingForJoin ? { waiting_for_join: true } : {})
                }
            });
        }
    }
}
module.exports = { WelcomeBot, DeliveryQueue, privateRoom, sendOnce };
