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
        content: { membership: "join" }
    }));
    state.push({ type: "m.room.encryption", content: { algorithm: "m.megolm.v1.aes-sha2" } });
    const client = {
        crypto: {
            encryptRoomEvent: async (r, t, c) => ({ ciphertext: c.body, session_id: "session" })
        },
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
test("lost room creation never creates another room when discovery is empty or declined", async () => {
    const x = setup();
    let created = 0;
    x.client.createRoom = async () => {
        created++;
        throw new Error("lost_response");
    };
    x.client.getJoinedRooms = async () => [];
    await assert.rejects(x.bot.deliver(x.item), /lost_response/);
    x.bot = new WelcomeBot(x.client, x.store, x.bot.config);
    await assert.rejects(x.bot.deliver(x.item), /guide_room_creation_unconfirmed/);
    const recipient = x.state.find((event) => event.state_key === x.item.recipient);
    recipient.content.membership = "leave";
    x.client.getJoinedRooms = async () => ["!dm"];
    await assert.rejects(x.bot.deliver(x.item), /guide_room_creation_unconfirmed/);
    assert.equal(created, 1);
    assert.equal(x.calls.length, 0);
    recipient.content.membership = "join";
    await x.bot.deliver(x.item);
    await x.bot.deliver(x.item);
    assert.equal(created, 1);
    assert.equal(x.calls.length, 1);
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

test("welcome waits for acceptance, including restart, without creating another invitation", async () => {
    const x = setup();
    const recipient = x.state.find((event) => event.state_key === x.item.recipient);
    recipient.content.membership = "invite";
    let created = 0,
        encrypted = 0;
    x.client.createRoom = async () => {
        created++;
        return "!dm";
    };
    x.client.crypto.encryptRoomEvent = async () => {
        encrypted++;
        return { ciphertext: "ciphertext", session_id: "session" };
    };
    await assert.rejects(x.bot.deliver(x.item), /recipient_not_joined/);
    x.bot = new WelcomeBot(x.client, x.store, x.bot.config);
    await assert.rejects(x.bot.deliver(x.item), /recipient_not_joined/);
    assert.equal(created, 1);
    assert.equal(encrypted, 0);
    assert.equal(x.calls.length, 0);
    recipient.content.membership = "join";
    await x.bot.deliver(x.item);
    await x.bot.deliver(x.item);
    assert.equal(created, 1);
    assert.equal(encrypted, 1);
    assert.equal(x.calls.length, 1);
});

test("declined pending welcome is never reinvited, encrypted or sent", async () => {
    const x = setup();
    const recipient = x.state.find((event) => event.state_key === x.item.recipient);
    recipient.content.membership = "invite";
    await assert.rejects(x.bot.deliver(x.item));
    recipient.content.membership = "leave";
    let created = 0,
        encrypted = 0;
    x.client.createRoom = async () => {
        created++;
    };
    x.client.crypto.encryptRoomEvent = async () => {
        encrypted++;
    };
    await assert.rejects(x.bot.deliver(x.item), /guide_room_unavailable/);
    assert.equal(created, 0);
    assert.equal(encrypted, 0);
    assert.equal(x.calls.length, 0);
});

test("saved ambiguous ciphertext shares keys again without changing its transaction or body", async () => {
    const x = setup();
    const body = "Спецсимволы: <>&\"'\nhttps://example.org/path?a=1&b=2\nКод: 123";
    x.item.body = body;
    let encrypted = 0;
    x.client.crypto.encryptRoomEvent = async (room, type, content) => {
        assert.equal(type, "m.room.message");
        assert.equal(content.body, body);
        return { ciphertext: "cipher" + ++encrypted, session_id: "session" };
    };
    x.client.doRequest = async (...args) => {
        x.calls.push(args);
        if (x.calls.length === 1) throw new Error("ambiguous_response");
    };
    await assert.rejects(x.bot.deliver(x.item));
    x.bot = new WelcomeBot(x.client, x.store, x.bot.config);
    await x.bot.deliver(x.item);
    assert.equal(encrypted, 2);
    assert.deepEqual(x.calls[0], x.calls[1]);
    assert.equal(x.calls[1][3].ciphertext, "cipher1");
});

test("a rotated session never confirms an undelivered saved ciphertext", async () => {
    const x = setup();
    x.store.setValue("guide-room:" + x.item.recipient, "!dm");
    const saved = {
        room: "!dm",
        type: "m.room.encrypted",
        content: { session_id: "old", ciphertext: "original" }
    };
    x.store.setValue("delivery:" + x.item.id, saved);
    x.client.crypto.encryptRoomEvent = async () => ({
        session_id: "new",
        ciphertext: "replacement"
    });
    await assert.rejects(x.bot.deliver(x.item), /delivery_key_session_changed/);
    assert.equal(x.calls.length, 0);
    assert.deepEqual(x.store.getValue("delivery:" + x.item.id), saved);
});

test("legacy sent welcome shares the current key after acceptance without resending", async () => {
    const x = setup();
    x.store.setValue("guide-room:" + x.item.recipient, "!dm");
    x.store.setValue("delivery:" + x.item.id, "sent");
    let shares = 0;
    x.client.crypto.encryptRoomEvent = async () => {
        shares++;
        return { session_id: "session" };
    };
    await x.bot.shareCurrentKeys("!dm");
    await x.bot.deliver(x.item);
    assert.equal(shares, 1);
    assert.equal(x.calls.length, 0);
    assert.equal(x.store.getValue("delivery:" + x.item.id), "sent");
    x.state.push({
        type: "m.room.member",
        state_key: "@eve:example.org",
        content: { membership: "join" }
    });
    await x.bot.shareCurrentKeys("!dm");
    assert.equal(shares, 1);
});

test("administrative outbox waits for owner acceptance and acknowledges only after sending", async () => {
    const x = setup();
    const actor = x.item.recipient;
    const recipient = x.state.find((event) => event.state_key === actor);
    recipient.content.membership = "invite";
    const receipts = [],
        acknowledgements = [];
    x.store.setValue("admin-room", "!dm");
    const control = async (request) => {
        if (request.action === "panel-outbox") return { items: [{ ...x.item, via: "admin" }] };
        acknowledgements.push(request.argument);
        receipts.push(request.argument.delivered);
        return { recorded: true };
    };
    const deliveries = new DeliveryQueue(
        x.client,
        x.bot,
        control,
        x.store,
        { user_id: x.bot.config.user_id, administrator_mxids: [actor] },
        { authorize: async () => {} }
    );
    await deliveries.poll();
    assert.deepEqual(receipts, [false]);
    assert.equal(acknowledgements[0].waiting_for_join, true);
    assert.equal(x.calls.length, 0);
    recipient.content.membership = "join";
    await deliveries.poll();
    assert.deepEqual(receipts, [false, true]);
    assert.equal(acknowledgements[1].waiting_for_join, undefined);
    assert.equal(x.calls.length, 1);
});

test("delivery failure diagnostics contain fixed codes only and never confirm rotated ciphertext", async () => {
    const x = setup(),
        diagnostics = [],
        acknowledgements = [];
    x.store.setValue("guide-room:" + x.item.recipient, "!dm");
    x.store.setValue("delivery:" + x.item.id, {
        room: "!dm",
        type: "m.room.encrypted",
        content: { session_id: "old", ciphertext: "private-ciphertext" }
    });
    x.client.crypto.encryptRoomEvent = async () => ({ session_id: "new" });
    const deliveries = new DeliveryQueue(
        x.client,
        x.bot,
        async (request) => {
            if (request.action === "panel-outbox") return { items: [x.item] };
            acknowledgements.push(request.argument);
            return { recorded: true };
        },
        x.store,
        { administrator_mxids: [x.item.recipient] },
        {},
        (code) => diagnostics.push(code)
    );
    await deliveries.poll();
    assert.deepEqual(diagnostics, ["delivery_key_session_changed"]);
    assert.equal(acknowledgements[0].delivered, false);
    assert.equal(acknowledgements[0].waiting_for_join, undefined);
    assert.equal(x.calls.length, 0);
});

test("ambiguous room creation recovers a pending invitation without a duplicate room", async () => {
    const x = setup();
    x.state.find((event) => event.state_key === x.item.recipient).content.membership = "invite";
    let created = 0;
    x.client.createRoom = async () => {
        created++;
        throw new Error("lost_response");
    };
    await assert.rejects(x.bot.deliver(x.item));
    await assert.rejects(x.bot.deliver(x.item), /recipient_not_joined/);
    assert.equal(created, 1);
    assert.equal(x.store.getValue("guide-room:" + x.item.recipient), "!dm");
    assert.equal(x.calls.length, 0);
});

test("outbox preserves user text literally and formats authored administrator commands", async () => {
    for (const via of ["welcome", "admin"]) {
        const x = setup();
        let payload;
        x.client.crypto.encryptRoomEvent = async (room, type, content) => {
            payload = content;
            return { ciphertext: content.body, session_id: "session" };
        };
        const item = { ...x.item, via, body: "Сообщение\n1. `обновить` — описание <&>" };
        await sendOnce(x.client, x.store, "!dm", item);
        assert.equal(payload.body, via === "welcome" ? item.body : item.body.replaceAll("`", ""));
        assert.equal(payload.formatted_body.includes("<code>обновить</code>"), via === "admin");
        assert.ok(payload.formatted_body.includes("&lt;&amp;&gt;"));
    }
});
