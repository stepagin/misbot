"use strict";
process.umask(0o077);
const fs = require("node:fs");
const net = require("node:net");
const http = require("node:http");
const {
    MatrixClient,
    SimpleFsStorageProvider,
    RustSdkCryptoStorageProvider,
    LogService
} = require("matrix-bot-sdk");
const { StoreType } = require("@matrix-org/matrix-sdk-crypto-nodejs");
const { ReferralBot } = require("./logic");
const { ensureCrossSigning } = require("./signing");

// SDK exceptions can contain decrypted messages and HTTP bodies; keep logs metadata-only.
LogService.setLogger({ trace() {}, debug() {}, info() {}, warn() {}, error() {} });
function log(message) {
    console.log(new Date().toISOString() + " " + message);
}
function control(request, timeout = 90000) {
    return new Promise((resolve, reject) => {
        let body = "";
        const socket = net.createConnection("/run/control/control.sock");
        socket.setTimeout(timeout);
        socket.on("connect", () => socket.write(JSON.stringify(request) + "\n"));
        socket.on("data", (chunk) => {
            body += chunk.toString();
            if (body.length > 32768) {
                socket.destroy();
                return reject(new Error("Invalid control response"));
            }
            if (!body.includes("\n")) return;
            socket.end();
            try {
                const result = JSON.parse(body.split("\n")[0]);
                if (!result.ok) {
                    const error = new Error("Control operation denied");
                    error.userMessage = result.error;
                    return reject(error);
                }
                resolve(result.result);
            } catch (error) {
                reject(error);
            }
        });
        socket.on("timeout", () => {
            socket.destroy();
            reject(new Error("Control timeout"));
        });
        socket.on("error", () => reject(new Error("Control unavailable")));
    });
}

async function main() {
    const config = JSON.parse(fs.readFileSync("/config/referral.json", "utf8"));
    const store = new SimpleFsStorageProvider("/data/state.json");
    const crypto = new RustSdkCryptoStorageProvider("/data/crypto", StoreType.Sqlite);
    const client = new MatrixClient(config.homeserver_url, config.access_token, store, crypto);
    if ((await client.getUserId()) !== config.user_id) throw new Error("Bot identity mismatch");
    const processed = {
        getValue: (key) => store.readValue(key),
        setValue: (key, value) => store.storeValue(key, value)
    };
    const bot = new ReferralBot(client, control, processed, config);
    let ready = false;
    let lastSync = Date.now();
    // This SDK does not emit a sync event; update readiness on successful /sync.
    const originalSync = client.doSync.bind(client);
    client.doSync = async (token) => {
        const result = await originalSync(token);
        lastSync = Date.now();
        return result;
    };
    client.on("room.invite", async (roomId, event) => {
        try {
            await bot.invitation(roomId, event);
            log("Invitation processed");
        } catch {
            log("Invitation declined or unavailable");
        }
    });
    const queues = new Map();
    client.on("room.message", (roomId, event) => {
        const pending = (queues.get(roomId) || Promise.resolve()).then(async () => {
            try {
                await bot.message(roomId, event);
            } catch (error) {
                log("Command failed");
                if (
                    (await bot.privateRoom(roomId, event.sender).catch(() => false)) &&
                    (await bot.encryptedRoom(roomId).catch(() => false))
                ) {
                    await client
                        .sendNotice(
                            roomId,
                            error.userMessage || "Не удалось выполнить команду. Попробуй ещё раз."
                        )
                        .catch(() => {});
                }
            }
        });
        queues.set(roomId, pending);
        pending.finally(() => {
            if (queues.get(roomId) === pending) queues.delete(roomId);
        });
    });
    await client.setDisplayName("Реферальный бот");
    await client.start();
    await ensureCrossSigning(client);
    ready = true;
    log("Referral bot started; encryption ready; device cross-signed");
    http.createServer(async (req, res) => {
        if (req.url !== "/health") {
            res.writeHead(404);
            return res.end();
        }
        let ok = ready && client.crypto.isReady && Date.now() - lastSync < 180000;
        try {
            await control({ action: "servers" }, 2000);
        } catch {
            ok = false;
        }
        res.writeHead(ok ? 200 : 503, { "Content-Type": "application/json" });
        res.end(JSON.stringify({ ok }));
    }).listen(8080, "0.0.0.0");
}

main().catch((error) => {
    const kind = String(error.errcode || error.code || error.name || "unknown")
        .replace(/[^A-Za-z0-9_]/g, "")
        .slice(0, 64);
    log("Referral bot startup failed type=" + kind);
    process.exit(1);
});
