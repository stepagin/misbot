"use strict";
const test = require("node:test"),
    assert = require("node:assert/strict"),
    fs = require("node:fs"),
    os = require("node:os"),
    path = require("node:path"),
    vm = require("node:vm"),
    { EventEmitter } = require("node:events");

// Execute the actual entrypoint and signing helper, replacing only external I/O.
function installation(directory, options = {}) {
    const files = new Proxy(fs, {
        get(target, name) {
            if (["existsSync", "readFileSync", "writeFileSync", "mkdirSync"].includes(name))
                return (file, ...args) => target[name](directory + file, ...args);
            if (name === "renameSync")
                return (from, to) => target.renameSync(directory + from, directory + to);
            return target[name];
        }
    });
    const signing = { exports: {} };
    vm.runInNewContext(
        fs.readFileSync(path.join(__dirname, "../../matrix-referral/signing.js"), "utf8"),
        {
            require: (name) => (name === "node:fs" ? files : require(name)),
            module: signing,
            Buffer
        }
    );
    const keys = signing.exports,
        clients = new Map(),
        rooms = new Map(),
        logs = [],
        notices = [],
        requests = [],
        queues = [];
    let uploaded = 0,
        created = 0;
    class MatrixClient {
        constructor(url, token) {
            assert.equal(url, "http://synapse:8008");
            if (clients.has(token)) return clients.get(token);
            this.user = "@" + token + ":example.org";
            const identity = keys.newKey(),
                device = token + "-device";
            this.device = {
                user_id: this.user,
                device_id: device,
                keys: { ["ed25519:" + device]: identity.public }
            };
            this.crypto = {
                isReady: true,
                clientDeviceId: device,
                clientDeviceEd25519: identity.public,
                decryptRoomEvent: async (event) => {
                    if (!options.canDecrypt) throw new Error("private crypto payload");
                    return {
                        raw: {
                            ...event.raw,
                            type: "m.room.message",
                            content: { msgtype: "m.text", body: "помощь" }
                        }
                    };
                },
                sign: async (object) => ({
                    [this.user]: {
                        ["ed25519:" + device]: keys.signed(object, identity, this.user).signatures[
                            this.user
                        ]["ed25519:" + identity.public]
                    }
                })
            };
            clients.set(token, this);
        }
        async getUserId() {
            return this.user;
        }
        async setDisplayName() {}
        on(name, handler) {
            this.handlers ??= new Map();
            this.handlers.set(name, handler);
        }
        emit(name, ...args) {
            this.handlers.get(name)?.(...args);
        }
        async getRoomState() {
            return [this.user, "@matrix-admin:example.org"]
                .map((state_key) => ({
                    type: "m.room.member",
                    state_key,
                    content: { membership: "join" }
                }))
                .concat({
                    type: "m.room.encryption",
                    content: { algorithm: "m.megolm.v1.aes-sha2" }
                });
        }
        async sendNotice(room, body) {
            notices.push({ room, body });
        }
        async doSync() {
            return {};
        }
        async start() {
            await this.doSync();
        }
        async getJoinedRooms() {
            return [];
        }
        async createRoom() {
            created++;
            return "!private:example.org";
        }
        async doRequest(method, url, query, body) {
            const user = this.user,
                device = this.crypto.clientDeviceId;
            if (url.endsWith("/keys/query"))
                return {
                    device_keys: { [user]: { [device]: this.device } },
                    ...(this.keyset
                        ? {
                              master_keys: { [user]: this.keyset.master_key },
                              self_signing_keys: { [user]: this.keyset.self_signing_key }
                          }
                        : {})
                };
            if (url.endsWith("/device_signing/upload")) {
                uploaded++;
                this.keyset = body;
                return {};
            }
            if (url.endsWith("/signatures/upload")) {
                this.device = body[user][device];
                this.keyset.master_key = body[user][Object.values(this.keyset.master_key.keys)[0]];
                return {};
            }
            throw new Error("Unexpected request");
        }
    }
    class Storage {
        constructor(file) {
            this.file = file;
        }
        readValue(key) {
            return rooms.get(this.file + key);
        }
        storeValue(key, value) {
            rooms.set(this.file + key, value);
        }
    }
    for (const name of ["admin-panel", "server-guide"]) {
        const file = name === "admin-panel" ? "federation" : "welcome";
        fs.mkdirSync(directory + "/config", { recursive: true });
        fs.mkdirSync(directory + "/data", { recursive: true });
        fs.writeFileSync(
            directory + "/config/" + file + ".json",
            JSON.stringify({
                server_name: "example.org",
                homeserver_url: "http://synapse:8008",
                user_id: "@" + name + ":example.org",
                access_token: name
            })
        );
    }
    async function boot() {
        let finish, handler;
        const done = new Promise((resolve) => {
            finish = resolve;
        });
        const modules = {
            "node:fs": files,
            "node:net": {
                createConnection() {
                    if (!options.response) throw new Error("Unexpected control request");
                    const socket = new EventEmitter();
                    socket.destroy = () => {};
                    socket.setTimeout = (timeout) => {
                        socket.timeout = timeout;
                    };
                    socket.write = (raw) => {
                        const request = JSON.parse(raw);
                        requests.push({ request, timeout: socket.timeout });
                        const response = options.response(request, requests.length);
                        if (response === "timeout") socket.emit("timeout");
                        else socket.emit("data", Buffer.from(JSON.stringify(response) + "\n"));
                    };
                    queueMicrotask(() => socket.emit("connect"));
                    return socket;
                }
            },
            "node:http": {
                createServer(callback) {
                    handler = callback;
                    return {
                        listen() {
                            finish(0);
                        }
                    };
                }
            },
            "matrix-bot-sdk": {
                MatrixClient,
                SimpleFsStorageProvider: Storage,
                RustSdkCryptoStorageProvider: class {},
                EncryptedRoomEvent: class {
                    constructor(raw) {
                        this.raw = raw;
                    }
                },
                LogService: { setLogger() {} }
            },
            "@matrix-org/matrix-sdk-crypto-nodejs": { StoreType: { Sqlite: "sqlite" } },
            "./signing": keys,
            "./runtime": {
                ...require("../runtime"),
                SerialQueue: class extends require("../runtime").SerialQueue {
                    constructor(...args) {
                        super(...args);
                        queues.push(this);
                    }
                }
            },
            "./logic": options.response ? require("../logic") : { FederationBot: class {} },
            "./welcome": {
                WelcomeBot: class {},
                DeliveryQueue: class {},
                privateRoom: async () => true
            }
        };
        vm.runInNewContext(fs.readFileSync(path.join(__dirname, "../main.js"), "utf8"), {
            require(name) {
                assert.ok(Object.hasOwn(modules, name), "Unexpected dependency");
                return modules[name];
            },
            process: { umask() {}, exit: finish },
            console: { log: (message) => logs.push(message) },
            setInterval: () => ({ unref() {} })
        });
        const code = await done;
        let status;
        if (handler)
            handler(
                { url: "/health" },
                {
                    writeHead(value) {
                        status = value;
                    },
                    end(body) {
                        assert.equal(JSON.parse(body).ok, true);
                    }
                }
            );
        return { code, status };
    }
    return {
        boot,
        logs,
        clients,
        notices,
        requests,
        idle: () => Promise.all(queues.map((queue) => queue.idle())),
        counts: () => ({ uploaded, created })
    };
}

test(
    "both accounts start with separate keys; retry retains identities, devices and room",
    { timeout: 5000 },
    async () => {
        const folder = fs.mkdtempSync(path.join(os.tmpdir(), "misbot-startup-"));
        try {
            const fixture = installation(folder);
            assert.deepEqual(await fixture.boot(), { code: 0, status: 200 });
            const panel = fs.readFileSync(folder + "/data/cross-signing.json", "utf8");
            const welcome = fs.readFileSync(folder + "/data/welcome/cross-signing.json", "utf8");
            assert.equal(JSON.parse(panel).user, "@admin-panel:example.org");
            assert.equal(JSON.parse(welcome).user, "@server-guide:example.org");
            assert.notEqual(JSON.parse(panel).master.public, JSON.parse(welcome).master.public);
            const devices = [...fixture.clients.values()].map(
                (client) => client.crypto.clientDeviceId
            );
            assert.deepEqual(await fixture.boot(), { code: 0, status: 200 });
            assert.equal(fs.readFileSync(folder + "/data/cross-signing.json", "utf8"), panel);
            assert.equal(
                fs.readFileSync(folder + "/data/welcome/cross-signing.json", "utf8"),
                welcome
            );
            assert.deepEqual(
                [...fixture.clients.values()].map((client) => client.crypto.clientDeviceId),
                devices
            );
            assert.deepEqual(fixture.counts(), { uploaded: 2, created: 1 });
            assert.deepEqual(fixture.logs, []);
        } finally {
            fs.rmSync(folder, { recursive: true, force: true });
        }
    }
);

test("actual panel entrypoint reports transient busy only after reauthorizing and recovers", async () => {
    const folder = fs.mkdtempSync(path.join(os.tmpdir(), "misbot-entrypoint-"));
    try {
        let busy = true;
        const fixture = installation(folder, {
            response: (request) => {
                assert.equal(request.action, "authorize");
                if (busy) {
                    busy = false;
                    return { ok: false, code: "busy_pending_or_operation_failed" };
                }
                return { ok: true, result: { authorized: true } };
            }
        });
        await fixture.boot();
        const client = fixture.clients.get("admin-panel");
        const event = {
            event_id: "$request",
            sender: "@matrix-admin:example.org",
            content: { msgtype: "m.text", body: "помощь" }
        };
        client.emit("room.message", "!private", event);
        await fixture.idle();
        assert.equal(fixture.notices.length, 1);
        assert.match(fixture.notices[0].body, /Повторите команду позже/);
        assert.ok(fixture.requests.every((call) => call.timeout === 10000));
        client.emit("room.message", "!private", event);
        await fixture.idle();
        assert.equal(fixture.notices.length, 2);
        assert.match(fixture.notices[1].body, /Панель администратора Misbot/);
        assert.ok(fixture.logs.includes("Federation bot: command_unconfirmed"));
    } finally {
        fs.rmSync(folder, { recursive: true, force: true });
    }
});

test("actual panel entrypoint never sends fallback text after authorization is denied", async () => {
    const folder = fs.mkdtempSync(path.join(os.tmpdir(), "misbot-entrypoint-"));
    try {
        const fixture = installation(folder, {
            response: () => ({ ok: false, code: "admin_unconfirmed" })
        });
        await fixture.boot();
        fixture.clients.get("admin-panel").emit("room.message", "!private", {
            event_id: "$denied",
            sender: "@matrix-admin:example.org",
            content: { msgtype: "m.text", body: "помощь" }
        });
        await fixture.idle();
        assert.deepEqual(fixture.notices, []);
    } finally {
        fs.rmSync(folder, { recursive: true, force: true });
    }
});

test("actual panel entrypoint uses a short timeout and gives safe feedback after authorization recovers", async () => {
    const folder = fs.mkdtempSync(path.join(os.tmpdir(), "misbot-entrypoint-"));
    try {
        let timeout = true;
        const fixture = installation(folder, {
            response: () => {
                if (timeout) {
                    timeout = false;
                    return "timeout";
                }
                return { ok: true, result: { authorized: true } };
            }
        });
        await fixture.boot();
        fixture.clients.get("admin-panel").emit("room.message", "!private", {
            event_id: "$timeout",
            sender: "@matrix-admin:example.org",
            content: { msgtype: "m.text", body: "помощь" }
        });
        await fixture.idle();
        assert.equal(fixture.requests[0].timeout, 10000);
        assert.equal(fixture.notices.length, 1);
        assert.match(fixture.notices[0].body, /Ответ не получен/);
        assert.ok(fixture.logs.every((line) => !line.includes("access_token")));
    } finally {
        fs.rmSync(folder, { recursive: true, force: true });
    }
});

test("actual panel entrypoint retries encrypted commands after key arrival without duplicate replies", async () => {
    const folder = fs.mkdtempSync(path.join(os.tmpdir(), "misbot-entrypoint-"));
    try {
        const options = {
            canDecrypt: false,
            response: () => ({ ok: true, result: { authorized: true } })
        };
        const fixture = installation(folder, options);
        await fixture.boot();
        const client = fixture.clients.get("admin-panel");
        client.emit("room.failed_decryption", "!private", {
            event_id: "$encrypted",
            sender: "@matrix-admin:example.org",
            type: "m.room.encrypted",
            content: {
                algorithm: "m.megolm.v1.aes-sha2",
                ciphertext: "encrypted-payload",
                session_id: "session"
            }
        });
        options.canDecrypt = true;
        client.emit("to_device.decrypted", {});
        await fixture.idle();
        client.emit("to_device.decrypted", {});
        await fixture.idle();
        assert.equal(fixture.notices.length, 1);
        assert.match(fixture.notices[0].body, /Панель администратора Misbot/);
        assert.ok(fixture.logs.includes("Federation bot: decryption_pending"));
        assert.doesNotMatch(fixture.logs.join("\n"), /encrypted-payload|private crypto payload/);
    } finally {
        fs.rmSync(folder, { recursive: true, force: true });
    }
});

test(
    "welcome owner mismatch fails closed and preserves both accounts' keys",
    { timeout: 5000 },
    async () => {
        const folder = fs.mkdtempSync(path.join(os.tmpdir(), "misbot-startup-"));
        try {
            const fixture = installation(folder);
            await fixture.boot();
            const panel = fs.readFileSync(folder + "/data/cross-signing.json", "utf8");
            const welcomeFile = folder + "/data/welcome/cross-signing.json";
            const damaged = JSON.parse(fs.readFileSync(welcomeFile, "utf8"));
            damaged.user = "@different:example.org";
            const saved = JSON.stringify(damaged);
            fs.writeFileSync(welcomeFile, saved);
            assert.deepEqual(await fixture.boot(), { code: 1, status: undefined });
            assert.equal(fs.readFileSync(folder + "/data/cross-signing.json", "utf8"), panel);
            assert.equal(fs.readFileSync(welcomeFile, "utf8"), saved);
            assert.deepEqual(fixture.counts(), { uploaded: 2, created: 1 });
            assert.match(fixture.logs[0], /stage=welcome; retain session\/crypto state/);
            assert.equal(fixture.logs.length, 1);
        } finally {
            fs.rmSync(folder, { recursive: true, force: true });
        }
    }
);
