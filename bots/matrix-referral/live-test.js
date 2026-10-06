"use strict";
// A temporary device for the existing bootstrap account talks only to the bot.
// Never print decrypted replies, token codes, credentials, room IDs or key material.
const fs = require("node:fs");
const {MatrixClient, SimpleFsStorageProvider, RustSdkCryptoStorageProvider, LogService} = require("matrix-bot-sdk");
const {StoreType} = require("@matrix-org/matrix-sdk-crypto-nodejs");
const {validSignature} = require("./signing");
LogService.setLogger({trace() {}, debug() {}, info() {}, warn() {}, error() {}});
const sleep = ms => new Promise(r => setTimeout(r, ms));

async function main() {
    const cfg = JSON.parse(fs.readFileSync("/test/config.json", "utf8"));
    const domain = cfg.server_name || "misbot.ru";
    const botDevice = cfg.bot_device_id || "MISBOT-REFERRAL-01";
    const store = new SimpleFsStorageProvider("/test/state.json");
    const crypto = new RustSdkCryptoStorageProvider("/test/crypto", StoreType.Sqlite);
    const client = new MatrixClient(cfg.homeserver_url, cfg.access_token, store, crypto);
    const replies = [];
    client.on("room.message", (room, event) => {
        if (event.sender === cfg.bot_user_id && event.content?.body) replies.push({room, event});
    });
    await client.start();
    let roomId;
    try {
        roomId = await client.createRoom({visibility: "private", preset: "trusted_private_chat",
            room_version: "11", invite: [cfg.bot_user_id], is_direct: true,
            initial_state: [{type: "m.room.encryption", state_key: "", content: {algorithm: "m.megolm.v1.aes-sha2"}}]});
        if (typeof roomId !== "string") roomId = roomId.room_id;
        for (let i = 0; i < 35; i++) {
            const state = await client.getRoomState(roomId);
            if (state.some(m => m.type === "m.room.member" && m.state_key === cfg.bot_user_id && m.content.membership === "join")) break;
            if (i === 34) throw new Error("Bot did not join");
            await sleep(1000);
        }
        await sleep(2500);
        const sendAndWait = async (command, predicate) => {
            const start = replies.length;
            await client.sendText(roomId, command);
            for (let i = 0; i < 45; i++) {
                const match = replies.slice(start).find(x => x.room === roomId && predicate(x.event.content));
                if (match) return match.event;
                await sleep(1000);
            }
            throw new Error("Expected encrypted reply not received");
        };
        await sendAndWait("помощь", c => c.body.includes("Реферальный бот"));
        console.log("Encrypted command and decrypted help reply received");
        const label = "Проверка диалога";
        const invitation = await sendAndWait("пригласить " + label, c => c.body.includes("Приглашение на " + domain));
        if (!invitation.content.formatted_body.includes("<code>")) throw new Error("Code formatting missing");
        if (!invitation.content.body.includes("МСК")) throw new Error("Moscow time missing");
        if (invitation.content.body.includes("отозвать") || invitation.content.body.includes(label)) throw new Error("Forwarded invitation contains creator metadata");
        const code = invitation.content.body.match(/MIS-[A-Za-z0-9_-]+/)?.[0];
        if (!code) throw new Error("Code missing");
        const raw = await client.doRequest("GET", "/_matrix/client/v3/rooms/" + encodeURIComponent(roomId) + "/event/" + encodeURIComponent(invitation.event_id));
        if (raw.type !== "m.room.encrypted" || raw.content.body) throw new Error("Invitation reply was stored in plaintext");
        console.log("Invitation issued over E2EE; server stores m.room.encrypted; monospace and MSK confirmed");
        const listed = await sendAndWait("токены", c => c.body.includes("Мои приглашения"));
        if (!listed.content.body.includes(label) || !listed.content.body.includes(code)) throw new Error("Creator name missing from list");
        await sendAndWait("отозвать", c => c.body.includes("Какое приглашение отозвать"));
        await sendAndWait("отмена", c => c.body.includes("Отзыв отменён"));
        const afterCancel = await sendAndWait("токены", c => c.body.includes("Мои приглашения"));
        if (!afterCancel.content.body.includes(code)) throw new Error("Cancellation revoked the token");
        const selection = await sendAndWait("отозвать", c => c.body.includes("Какое приглашение отозвать"));
        const line = selection.content.body.split("\n").find(x => x.includes(code));
        const number = line?.match(/^(\d+)\./)?.[1];
        if (!number) throw new Error("Token number missing");
        await sendAndWait(number, c => c.body.includes("отозвано"));
        console.log("Named invitation listed; cancel preserved code; numbered revoke succeeded over E2EE");
        const after = await sendAndWait("токены", c => c.body.includes("Мои приглашения") || c.body.includes("Нет приглашений"));
        if (after.content.body.includes(code)) throw new Error("Revoked token remained in list");
        console.log("Revoked token hidden; forwardable invitation contains no name or revoke command");
        const keys = await client.doRequest("POST", "/_matrix/client/v3/keys/query", null, {device_keys: {[cfg.bot_user_id]: [botDevice]}});
        const master = Object.values(keys.master_keys[cfg.bot_user_id].keys)[0];
        const self = keys.self_signing_keys[cfg.bot_user_id], signing = Object.values(self.keys)[0];
        if (!validSignature(self, cfg.bot_user_id, master) || !validSignature(keys.device_keys[cfg.bot_user_id][botDevice], cfg.bot_user_id, signing)) throw new Error("Remote bot cross-signing verification failed");
        console.log("Independent recipient verified master -> self-signing -> bot device signature chain");
    } finally {
        if (roomId) await client.leaveRoom(roomId).catch(() => {});
        client.stop();
    }
}

main().then(() => process.exit(0)).catch(error => {
    // Our explicit assertions are safe; SDK exception text is not.
    const allowed = ["Bot did not join", "Expected encrypted reply not received", "Code formatting missing", "Moscow time missing", "Revocation command missing", "Invitation reply was stored in plaintext"];
    console.error(allowed.includes(error.message) ? error.message : "Encrypted live test failed");
    process.exit(1);
});
