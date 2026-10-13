"use strict";
const test = require("node:test"),
    assert = require("node:assert/strict");
const {
    SerialQueue,
    DecryptionRetry,
    controlTimeout,
    startNotificationPolling
} = require("../runtime");

test("stalled periodic work coalesces and input runs before waiting background work", async () => {
    const calls = [];
    const queue = new SerialQueue();
    let release;
    queue.add(
        "poll",
        async () => {
            calls.push("poll-start");
            await new Promise((resolve) => {
                release = resolve;
            });
            calls.push("poll-end");
        },
        true
    );
    await new Promise((resolve) => setImmediate(resolve));
    for (let index = 0; index < 1000; index++) {
        assert.equal(
            queue.add("poll", () => calls.push("duplicate"), true),
            false
        );
        queue.add("notifications", () => calls.push("notifications"), true);
    }
    queue.add("owner-event", () => calls.push("input"));
    assert.equal(queue.keys.size, 3);
    release();
    await queue.idle();
    assert.deepEqual(calls, ["poll-start", "poll-end", "input", "notifications"]);
    assert.equal(queue.keys.size, 0);
});

test("failed jobs release scheduling keys and never leak error values", async () => {
    const errors = [],
        calls = [],
        queue = new SerialQueue((code) => errors.push(code));
    queue.add(
        "poll",
        async () => {
            throw new Error("private-error-value");
        },
        true
    );
    await queue.idle();
    queue.add("poll", async () => calls.push("recovered"), true);
    await queue.idle();
    assert.deepEqual(errors, ["operation_unconfirmed"]);
    assert.deepEqual(calls, ["recovered"]);
});

test("the queue has one active crypto task and a fixed admission bound", async () => {
    const queue = new SerialQueue();
    let active = 0,
        maximum = 0;
    for (let index = 0; index < 256; index++)
        assert.equal(
            queue.add("message" + index, async () => {
                maximum = Math.max(maximum, ++active);
                await Promise.resolve();
                active--;
            }),
            true
        );
    assert.equal(
        queue.add("overflow", () => {}),
        false
    );
    await queue.idle();
    assert.equal(maximum, 1);
});

test("controller reads have short bounded timeouts without changing queue write arguments", () => {
    for (const action of ["authorize", "panel-outbox", "panel-ack", "notifications"])
        assert.equal(controlTimeout(action), 10000);
    assert.equal(controlTimeout("check"), 90000);
    assert.equal(controlTimeout("panel-server-read"), 90000);
    assert.equal(controlTimeout("invite"), 30000);
});

function fixture() {
    const values = new Map(),
        handled = [],
        attempts = [],
        owner = "@owner:example.org";
    let now = 100,
        ready = false,
        authorized = true;
    const store = {
        getValue: (key) => values.get(key),
        setValue: (key, value) => values.set(key, value)
    };
    const make = () =>
        new DecryptionRetry(
            store,
            async (room, event) => {
                attempts.push(event.event_id);
                if (!ready) throw new Error("private-decryption-details");
                return {
                    raw: {
                        ...event,
                        type: "m.room.message",
                        content: { msgtype: "m.text", body: "помощь\n<>&\"'" }
                    }
                };
            },
            async (room, event) => {
                if (!authorized) throw new Error("authorization_unavailable");
                handled.push({ room, event });
            },
            (sender) => sender === owner,
            () => now
        );
    const event = (id = "$event") => ({
        event_id: id,
        sender: owner,
        type: "m.room.encrypted",
        content: {
            algorithm: "m.megolm.v1.aes-sha2",
            ciphertext: "encrypted",
            session_id: "session",
            sender_key: "key"
        }
    });
    return {
        make,
        event,
        store,
        handled,
        attempts,
        ready: () => {
            ready = true;
        },
        revoke: () => {
            authorized = false;
        },
        advance: () => {
            now += 600001;
        }
    };
}

test("missing-key events survive restart and process once after a key arrives", async () => {
    const x = fixture();
    let retry = x.make();
    assert.equal(retry.remember("!private", x.event()), true);
    assert.equal(retry.remember("!private", x.event()), false);
    await retry.retry();
    assert.equal(x.handled.length, 0);
    retry = x.make();
    x.ready();
    await retry.retry();
    await retry.retry();
    assert.equal(x.handled.length, 1);
    assert.equal(x.handled[0].event.content.body, "помощь\n<>&\"'");
    assert.deepEqual(x.store.getValue("decryption-pending"), []);
});

test("retry retains encrypted input while authorization is unavailable and expires it", async () => {
    const x = fixture();
    const retry = x.make();
    retry.remember("!private", x.event());
    x.ready();
    x.revoke();
    await retry.retry();
    assert.equal(x.handled.length, 0);
    const saved = x.store.getValue("decryption-pending");
    assert.equal(saved.length, 1);
    assert.equal(saved[0].event.content.body, undefined);
    x.advance();
    await retry.retry();
    assert.deepEqual(x.store.getValue("decryption-pending"), []);
});

test("retry rejects unrelated, cleartext and oversized events and caps persisted ciphertext", () => {
    const x = fixture(),
        retry = x.make();
    assert.equal(retry.remember("!room", { ...x.event(), sender: "@other:example.org" }), false);
    assert.equal(retry.remember("!room", { ...x.event(), type: "m.room.message" }), false);
    const oversized = x.event();
    oversized.content.ciphertext = "x".repeat(65536);
    assert.equal(retry.remember("!room", oversized), false);
    for (let index = 0; index < 100; index++) retry.remember("!room", x.event("$event" + index));
    assert.equal(x.store.getValue("decryption-pending").length, 64);
});

test("a missing key in the first batch does not starve later encrypted events", async () => {
    const x = fixture(),
        retry = x.make();
    for (let index = 0; index < 9; index++) retry.remember("!room", x.event("$event" + index));
    await retry.retry();
    await retry.retry();
    assert.ok(x.attempts.includes("$event8"));
});

test("notification pagination yields to commands and retries errors on the normal interval", async () => {
    const timers = [],
        calls = [],
        errors = [];
    const queue = new SerialQueue((code) => errors.push(code));
    const schedule = (callback, delay) => {
        timers.push({ callback, delay });
        return { unref() {} };
    };
    let page = 0;
    startNotificationPolling(
        queue,
        async () => {
            calls.push("page" + ++page);
            if (page === 2) throw new Error("private RPC failure");
            return page === 1;
        },
        schedule
    );
    assert.equal(timers[0].delay, 30000);
    timers.shift().callback();
    await queue.idle();
    assert.equal(timers[0].delay, 1000);
    timers.shift().callback();
    queue.add("command", () => calls.push("command"));
    await queue.idle();
    assert.deepEqual(calls, ["page1", "command", "page2"]);
    assert.deepEqual(errors, ["operation_unconfirmed"]);
    assert.equal(timers[0].delay, 30000);
    timers.shift().callback();
    await queue.idle();
    assert.equal(calls.at(-1), "page3");
    assert.equal(timers.length, 1);
    assert.equal(timers[0].delay, 30000);
    assert.equal(queue.keys.size, 0);
});
