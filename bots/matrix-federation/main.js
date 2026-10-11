"use strict";
process.umask(0o077);
const fs = require("node:fs"),
    net = require("node:net"),
    http = require("node:http");
const {
    MatrixClient,
    SimpleFsStorageProvider,
    RustSdkCryptoStorageProvider,
    LogService
} = require("matrix-bot-sdk");
const { StoreType } = require("@matrix-org/matrix-sdk-crypto-nodejs");
const { FederationBot } = require("./logic"),
    { ensureCrossSigning } = require("./signing");
const { WelcomeBot, DeliveryQueue, privateRoom } = require("./welcome");
let stage = "configuration";
LogService.setLogger({ trace() {}, debug() {}, info() {}, warn() {}, error() {} });
function control(request) {
    return new Promise((resolve, reject) => {
        const socket = net.createConnection("/run/control/control.sock");
        let raw = "",
            done = false;
        const finish = (error, value) => {
            if (done) return;
            done = true;
            socket.destroy();
            error
                ? reject(
                      new Error(
                          "Управляющая служба не подтвердила запрос. Проверьте misbot status. Для заявки используйте federation requests retry; для восстановления — federation recover."
                      )
                  )
                : resolve(value);
        };
        socket.setTimeout(300000);
        socket.on("connect", () => socket.write(JSON.stringify(request) + "\n"));
        socket.on("data", (chunk) => {
            raw += chunk.toString();
            if (raw.length > 32768) return finish(true);
            if (!raw.includes("\n")) return;
            try {
                const value = JSON.parse(raw.split("\n")[0]);
                finish(!value.ok, value.result);
            } catch {
                finish(true);
            }
        });
        socket.on("timeout", () => finish(true));
        socket.on("error", () => finish(true));
        socket.on("end", () => {
            if (!done) finish(true);
        });
    });
}
async function main() {
    const config = JSON.parse(fs.readFileSync("/config/federation.json", "utf8"));
    const administrators = config.administrator_mxids || ["@matrix-admin:" + config.server_name];
    if (
        !Array.isArray(administrators) ||
        administrators.length !== 1 ||
        administrators.some(
            (mxid) =>
                typeof mxid !== "string" ||
                !mxid.startsWith("@") ||
                !mxid.endsWith(":" + config.server_name)
        )
    )
        throw new Error("administrators");
    config.administrator_mxids = administrators;
    const store = new SimpleFsStorageProvider("/data/state.json");
    const crypto = new RustSdkCryptoStorageProvider("/data/crypto", StoreType.Sqlite);
    const client = new MatrixClient(config.homeserver_url, config.access_token, store, crypto);
    stage = "identity";
    if ((await client.getUserId()) !== config.user_id) throw new Error("identity");
    const bot = new FederationBot(
        client,
        control,
        { getValue: (k) => store.readValue(k), setValue: (k, v) => store.storeValue(k, v) },
        config
    );
    let lastSync = 0,
        ready = false;
    const sync = client.doSync.bind(client);
    client.doSync = async (token) => {
        const result = await sync(token);
        lastSync = Date.now();
        return result;
    };
    // One queue also serializes crypto/membership operations and duplicate deliveries.
    let queue = Promise.resolve();
    const enqueue = (fn) => {
        queue = queue
            .then(fn)
            .catch(() => console.log("Federation operation not confirmed; use CLI status/retry"));
    };
    client.on("room.invite", (room, event) => enqueue(() => bot.invitation(room, event)));
    client.on("room.message", (room, event) =>
        enqueue(async () => {
            try {
                await bot.message(room, event);
            } catch {
                if (
                    bot.local(event.sender) &&
                    (await bot.authorize(event.sender).then(
                        () => true,
                        () => false
                    )) &&
                    (await bot.privateRoom(room).catch(() => false))
                )
                    await client
                        .sendNotice(
                            room,
                            "Результат команды не подтверждён. Выполните sudo misbot status. Если есть идентификатор операции, проверьте sudo misbot federation operations и выполните указанную команду recovery/retry. Справка по вводу: помощь."
                        )
                        .catch(() => {});
            }
        })
    );
    stage = "profile";
    await client.setDisplayName("Панель администратора Misbot");
    stage = "sync";
    await client.start();
    stage = "cross-signing";
    await ensureCrossSigning(client);
    stage = "admin-room";
    if (
        !store.readValue("admin-room") ||
        !(await privateRoom(
            client,
            store.readValue("admin-room"),
            config.user_id,
            administrators[0]
        ).catch(() => false))
    ) {
        // Recover a lost createRoom response by finding our existing private room.
        let found = null;
        for (const room of await client.getJoinedRooms()) {
            const state = await client.getRoomState(room);
            const members = state
                .filter(
                    (e) =>
                        e.type === "m.room.member" &&
                        ["join", "invite"].includes(e.content?.membership)
                )
                .map((e) => e.state_key);
            if (
                members.length === administrators.length + 1 &&
                members.includes(config.user_id) &&
                administrators.every((mxid) => members.includes(mxid)) &&
                state.some(
                    (e) =>
                        e.type === "m.room.encryption" &&
                        e.content?.algorithm === "m.megolm.v1.aes-sha2"
                )
            ) {
                found = room;
                break;
            }
        }
        const room =
            found ||
            (await client.createRoom({
                preset: "private_chat",
                visibility: "private",
                is_direct: true,
                invite: administrators,
                name: "Misbot — панель администратора",
                initial_state: [
                    {
                        type: "m.room.encryption",
                        state_key: "",
                        content: { algorithm: "m.megolm.v1.aes-sha2" }
                    }
                ]
            }));
        store.storeValue("admin-room", typeof room === "string" ? room : room.room_id);
    }
    stage = "welcome";
    const welcomeConfig = JSON.parse(fs.readFileSync("/config/welcome.json", "utf8"));
    fs.mkdirSync("/data/welcome", { recursive: true, mode: 0o700 });
    const welcomeStore = new SimpleFsStorageProvider("/data/welcome/state.json");
    const welcomeCrypto = new RustSdkCryptoStorageProvider(
        "/data/welcome/crypto",
        StoreType.Sqlite
    );
    const welcomeClient = new MatrixClient(
        welcomeConfig.homeserver_url,
        welcomeConfig.access_token,
        welcomeStore,
        welcomeCrypto
    );
    if ((await welcomeClient.getUserId()) !== welcomeConfig.user_id)
        throw new Error("welcome_identity");
    let welcomeSync = 0;
    const guideSync = welcomeClient.doSync.bind(welcomeClient);
    welcomeClient.doSync = async (token) => {
        const result = await guideSync(token);
        welcomeSync = Date.now();
        return result;
    };
    await welcomeClient.setDisplayName("Серверный помощник");
    await welcomeClient.start();
    await ensureCrossSigning(welcomeClient, "/data/welcome/cross-signing.json");
    const welcome = new WelcomeBot(
        welcomeClient,
        {
            getValue: (k) => welcomeStore.readValue(k),
            setValue: (k, v) => welcomeStore.storeValue(k, v)
        },
        welcomeConfig
    );
    const deliveries = new DeliveryQueue(
        client,
        welcome,
        control,
        { getValue: (k) => store.readValue(k), setValue: (k, v) => store.storeValue(k, v) },
        config,
        bot
    );
    // Same queue serializes encryption and room state; at most two messages / 5s.
    setInterval(() => enqueue(() => deliveries.poll()), 5000).unref();
    ready = true;
    setInterval(() => enqueue(() => bot.notifications()), 30000).unref();
    http.createServer((req, res) => {
        const ok =
            ready &&
            client.crypto.isReady &&
            welcomeClient.crypto.isReady &&
            Date.now() - lastSync < 180000 &&
            Date.now() - welcomeSync < 180000;
        res.writeHead(req.url === "/health" ? (ok ? 200 : 503) : 404, {
            "Content-Type": "application/json"
        });
        res.end(JSON.stringify({ ok }));
    }).listen(8080, "0.0.0.0");
}
main().catch(() => {
    console.log("Federation bot startup failed stage=" + stage + "; retain session/crypto state");
    process.exit(1);
});
