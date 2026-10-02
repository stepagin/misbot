"use strict";
// Disposable acceptance client. Output assertions only, never content or keys.
const fs = require("node:fs");
const {MatrixClient, SimpleFsStorageProvider, RustSdkCryptoStorageProvider, LogService} = require("matrix-bot-sdk");
const {OlmMachine, UserId, DeviceId, RoomId, StoreType} = require("@matrix-org/matrix-sdk-crypto-nodejs");
const {validSignature} = require("./signing");
LogService.setLogger({trace() {}, debug() {}, info() {}, warn() {}, error() {}});
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
const helpFragment = "Реферальный бот";
const emit = (check, values = {}) => console.log(JSON.stringify({check, passed: true, ...values}));

async function verifySignatures(client, cfg) {
    const user = cfg.bot_user_id, device = cfg.bot_device_id;
    const keys = await client.doRequest("POST", "/_matrix/client/v3/keys/query", null,
        {device_keys: {[user]: [device]}});
    const master = keys.master_keys?.[user], self = keys.self_signing_keys?.[user];
    const deviceKeys = keys.device_keys?.[user]?.[device];
    const masterPublic = Object.values(master?.keys || {})[0];
    const selfPublic = Object.values(self?.keys || {})[0];
    const devicePublic = deviceKeys?.keys?.["ed25519:" + device];
    if (!masterPublic || !selfPublic || !devicePublic ||
        !validSignature(master, user, devicePublic, "ed25519:" + device) ||
        !validSignature(self, user, masterPublic) ||
        !validSignature(deviceKeys, user, selfPublic)) throw new Error("Restored public signature chain failed");
    const tampered = {...deviceKeys, device_id: "fictional-tampered-device"};
    if (validSignature(tampered, user, selfPublic) ||
        validSignature(self, user, Buffer.alloc(32, 70).toString("base64")))
        throw new Error("Invalid public signature unexpectedly accepted");
    emit("restored_bot_public_signatures", {device_master_self_chain: true,
        tampered_device_refused: true, wrong_public_key_refused: true});
}

async function verify() {
    const cfg = JSON.parse(fs.readFileSync("/test/config.json", "utf8"));
    await verifySignatures(new MatrixClient(cfg.homeserver_url, cfg.access_token), cfg);
}

async function produce() {
    const cfg = JSON.parse(fs.readFileSync("/test/config.json", "utf8"));
    const client = new MatrixClient(cfg.homeserver_url, cfg.access_token,
        new SimpleFsStorageProvider("/test/state.json"),
        new RustSdkCryptoStorageProvider("/test/crypto", StoreType.Sqlite));
    const replies = [];
    client.on("room.message", (room, event) => {
        if (event.sender === cfg.bot_user_id && typeof event.content?.body === "string") replies.push({room, event});
    });
    let room;
    let saved = false;
    await client.start();
    try {
        await verifySignatures(client, cfg);
        const who = await client.getWhoAmI();
        room = await client.createRoom({visibility: "private", preset: "trusted_private_chat",
            room_version: "11", invite: [cfg.bot_user_id], is_direct: true,
            initial_state: [{type: "m.room.encryption", state_key: "", content: {algorithm: "m.megolm.v1.aes-sha2"}}]});
        if (typeof room !== "string") room = room.room_id;
        for (let attempt = 0; attempt < 35; attempt++) {
            const state = await client.getRoomState(room);
            if (state.some(event => event.type === "m.room.member" && event.state_key === cfg.bot_user_id && event.content.membership === "join")) break;
            if (attempt === 34) throw new Error("Controlled bot join timeout");
            await sleep(1000);
        }
        await sleep(2500);
        await client.sendText(room, "/start");
        let reply;
        for (let attempt = 0; attempt < 45; attempt++) {
            reply = replies.find(item => item.room === room && item.event.content.body.includes(helpFragment));
            if (reply) break;
            await sleep(1000);
        }
        if (!reply) throw new Error("Encrypted help reply timeout");
        const raw = await client.doRequest("GET", "/_matrix/client/v3/rooms/" + encodeURIComponent(room) + "/event/" + encodeURIComponent(reply.event.event_id));
        if (raw.type !== "m.room.encrypted" || raw.content.body) throw new Error("Reply was not stored encrypted");
        fs.writeFileSync("/test/history.json", JSON.stringify({room_id: room, raw,
            user_id: who.user_id, device_id: who.device_id}), {mode: 0o600});
        saved = true;
        emit("restored_bot_actual_e2ee_reply", {raw_encrypted: true, decrypted_help: true,
            scope: "disposable SDK client; no owner browser/device acceptance"});
    } finally {
        if (room && !saved) await client.leaveRoom(room).catch(() => {});
        client.stop();
    }
}

async function recover(missing) {
    const history = JSON.parse(fs.readFileSync("/test/history.json", "utf8"));
    const machine = await OlmMachine.initialize(new UserId(history.user_id), new DeviceId(history.device_id),
        missing ? "/test/empty-crypto" : "/test/crypto", "", StoreType.Sqlite);
    let event;
    try {
        event = JSON.parse((await machine.decryptRoomEvent(JSON.stringify(history.raw), new RoomId(history.room_id))).event);
    } catch (error) {
        if (!missing) throw error;
        emit("client_history_missing_keys_refused", {scope: "same user/device ID with empty local room-key store; no network"});
        return;
    }
    if (missing) throw new Error("Empty client store unexpectedly decrypted historical event");
    if (event.type !== "m.room.message" || !event.content.body.includes(helpFragment)) throw new Error("Historical decrypted help was not confirmed");
    emit("client_history_saved_keys_decrypted", {scope: "normal Rust SDK consumer; ciphertext and client keys, no network"});
}

const mode = process.argv[2];
Promise.resolve().then(() => {
    if (mode === "produce") return produce();
    if (mode === "verify") return verify();
    if (mode === "recover" || mode === "missing") return recover(mode === "missing");
    throw new Error("Unknown disposable test mode");
}).then(() => process.exit(0)).catch(error => {
    const allowed = ["Controlled bot join timeout", "Encrypted help reply timeout", "Reply was not stored encrypted",
        "Empty client store unexpectedly decrypted historical event", "Historical decrypted help was not confirmed",
        "Restored public signature chain failed", "Invalid public signature unexpectedly accepted"];
    console.error(JSON.stringify({check: "restored_e2ee_client", passed: false,
        message: allowed.includes(error.message) ? error.message : "Disposable E2EE consumer failed; retain its private stores"}));
    process.exit(1);
});
