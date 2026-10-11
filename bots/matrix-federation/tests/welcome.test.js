"use strict";
const test = require("node:test"),
    assert = require("node:assert/strict");
const { WelcomeBot, sendOnce, DeliveryQueue } = require("../welcome");
function setup() {
    const values = new Map(),
        calls = [],
        config = { user_id: "@server-guide:example.org", server_name: "example.org" };
    const state = [config.user_id, "@alice:example.org"].map((state_key) => ({
        type: "m.room.member",
        state_key,
        content: { membership: "invite" }
    }));
    state.push({ type: "m.room.encryption", content: { algorithm: "m.megolm.v1.aes-sha2" } });
    const client = {
        crypto: { encryptRoomEvent: async (r, t, c) => ({ ciphertext: c.body }) },
        getRoomState: async () => state,
        getJoinedRooms: async () => ["!dm"],
        createRoom: async () => "!dm",
        doRequest: async (...args) => calls.push(args)
    };
    const store = { getValue: (k) => values.get(k), setValue: (k, v) => values.set(k, v) };
    return {
        client,
        store,
        calls,
        state,
        bot: new WelcomeBot(client, store, config),
        item: {
            id: "a".repeat(64),
            recipient: "@alice:example.org",
            via: "welcome",
            body: "Привет"
        }
    };
}
test("welcome has no message handler and recovers its encrypted private room", async () => {
    const x = setup();
    assert.equal(x.bot.message, undefined);
    await x.bot.deliver(x.item);
    await x.bot.deliver(x.item);
    assert.equal(x.calls.length, 1);
    assert.match(x.calls[0][1], /m.room.encrypted\/a{64}$/);
});
test("ambiguous send uses the same ciphertext and transaction after retry", async () => {
    const x = setup();
    let once = true;
    x.client.doRequest = async (...args) => {
        x.calls.push(args);
        if (once) {
            once = false;
            throw new Error("timeout");
        }
    };
    await assert.rejects(x.bot.deliver(x.item));
    await x.bot.deliver(x.item);
    assert.deepEqual(x.calls[0], x.calls[1]);
});
test("no delivery to a group or unencrypted room", async () => {
    const x = setup();
    x.state.push({
        type: "m.room.member",
        state_key: "@eve:example.org",
        content: { membership: "join" }
    });
    x.client.createRoom = async () => "!unsafe";
    await assert.rejects(x.bot.deliver(x.item));
    assert.equal(x.calls.length, 0);
});
test("a declined welcome invitation is not recreated by delivery retries", async () => {
    const x = setup();
    await x.bot.deliver(x.item);
    let created = 0;
    x.client.createRoom = async () => {
        created++;
        return "!new";
    };
    x.state.find((e) => e.state_key === x.item.recipient).content.membership = "leave";
    const next = { ...x.item, id: "b".repeat(64) };
    await assert.rejects(x.bot.deliver(next));
    await assert.rejects(x.bot.deliver(next));
    assert.equal(created, 0);
    assert.equal(x.calls.length, 1);
});
