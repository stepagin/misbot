"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const { ReferralBot, tokenMessage, expiry, remaining } = require("../logic");

function setup() {
    const config = { user_id: "@referral-bot:misbot.ru", server_name: "misbot.ru" };
    const sent = [],
        calls = [],
        saved = new Map();
    const item = {
        id: "own-token",
        attributes: {
            token: "MIS-test_<&>",
            valid: true,
            expires_at: "2026-12-31T23:30:00Z",
            times_used: 0,
            usage_limit: 1
        }
    };
    const client = {
        getRoomState: async () =>
            [config.user_id, "@member:misbot.ru"].map((state_key) => ({
                type: "m.room.member",
                state_key,
                content: { membership: "join" }
            })),
        getRoomStateEvent: async () => ({ algorithm: "m.megolm.v1.aes-sha2" }),
        sendMessage: async (room, msg) => sent.push(msg),
        sendNotice: async (room, body) => sent.push({ body }),
        joinRoom: async (room) => sent.push({ joined: room })
    };
    const control = async (req) => {
        calls.push(req);
        return req.action === "matrix_invite"
            ? item
            : req.action === "matrix_tokens"
              ? { tokens: [item], has_more: false }
              : {};
    };
    const store = {
        getValue: (key) => saved.get(key),
        setValue: (key, val) => saved.set(key, val)
    };
    const bot = new ReferralBot(client, control, store, config);
    const event = {
        event_id: "$unique",
        sender: "@member:misbot.ru",
        content: { msgtype: "m.text", body: "пригласить" }
    };
    return { bot, client, sent, calls, item, event, saved, store, config };
}

test("local encrypted direct chat creates one invitation per event", async () => {
    const { bot, event, calls, sent } = setup();
    await bot.message("!room", event);
    await bot.message("!room", event);
    assert.equal(calls.filter((x) => x.action === "matrix_invite").length, 1);
    assert.equal(sent.length, 1);
    assert.equal(calls[1].mxid, "@member:misbot.ru");
    assert.equal(calls[1].request_id, "matrix:$unique");
    assert.match(sent[0].formatted_body, /<code>MIS-test_&lt;&amp;&gt;<\/code>/);
    assert.match(sent[0].body, /01.01.2027 02:30 МСК/);
    assert.doesNotMatch(sent[0].body, /Отозвать|\отозвать|own-token/);
});

test("old slash input only shows Russian help; links contain no invitation", async () => {
    const { bot, event, calls, sent, item } = setup();
    event.content.body = "/invite";
    await bot.message("!room", event);
    assert.equal(
        calls.some((x) => x.action === "matrix_invite"),
        false
    );
    assert.match(sent[0].body, /пригласить/);
    assert.match(sent[0].body, /помощь/);
    const message = tokenMessage(item, "example.org");
    assert.match(message.body, /Реферальный бот/);
    assert.match(message.body, /Следующая команда боту: токены/);
    const links = message.body.match(/https:\/\/\S+/g);
    assert.equal(links.length, 2);
    for (const link of links) assert.equal(link.includes(item.attributes.token), false);
    assert.match(message.body, /Один код — один человек/);
});

test("foreign accounts, group rooms and plaintext rooms cannot issue codes", async () => {
    for (const kind of ["foreign", "group", "plain"]) {
        const { bot, client, calls, event } = setup();
        if (kind === "foreign") event.sender = "@remote:matrix.primango.ru";
        if (kind === "group") {
            const state = await client.getRoomState();
            state.push({
                type: "m.room.member",
                state_key: "@extra:misbot.ru",
                content: { membership: "invite" }
            });
            client.getRoomState = async () => state;
        }
        if (kind === "plain") client.getRoomStateEvent = async () => null;
        await bot.message("!room", event);
        assert.equal(calls.length, 0, kind);
    }
});

test("revoke stays bound to sender and no federation action is exposed", async () => {
    const { bot, calls, sent, event } = setup();
    event.content.body = "отозвать own-token";
    await bot.message("!room", event);
    assert.equal(calls[1].action, "matrix_revoke");
    assert.equal(calls[1].resource, "own-token");
    event.event_id = "$other";
    event.content.body = "/federation_add evil.example";
    await bot.message("!room", event);
    assert.equal(
        calls.some((x) => x.action.includes("federation")),
        false
    );
    assert.match(sent.at(-1).body, /Реферальный бот/);
});

test("active identity is required before invitation operations", async () => {
    const { bot, event, sent } = setup();
    bot.control = async () => {
        throw new Error("inactive");
    };
    await assert.rejects(bot.message("!room", event));
    assert.equal(sent.length, 0);
});

test("invitation acceptance requires local active inviter", async () => {
    const { bot, sent } = setup();
    await bot.invitation("!foreign", { sender: "@remote:foreign.example" });
    assert.equal(sent.length, 0);
    await bot.invitation("!local", { sender: "@member:misbot.ru" });
    assert.deepEqual(sent, [{ joined: "!local" }]);
});

test("paging and expiration formatting are stable", async () => {
    const { bot, event, calls, item } = setup();
    event.content.body = "токены 5";
    await bot.message("!room", event);
    assert.equal(calls[1].offset, 5);
    assert.equal(expiry("2026-10-04T00:00:00Z"), "04.10.2026 03:00 МСК");
    assert.equal(tokenMessage(item, "misbot.ru").msgtype, "m.notice");
});

test("revoke dialog binds number to shown ID, survives restart, and clears after use", async () => {
    const { bot, client, event, calls, sent, saved, store, config } = setup();
    event.content.body = "отозвать";
    await bot.message("!room", event);
    assert.match(sent[0].body, /1\. MIS-test/);
    assert.match(sent[0].body, /\отмена/);
    const restart = new ReferralBot(client, bot.control, store, config);
    // A newly added token changes list positions, but not the displayed ID.
    restart.control = async (req) => {
        calls.push(req);
        return { tokens: [{ id: "new-token" }], has_more: false };
    };
    await restart.message("!room", {
        ...event,
        event_id: "$choice",
        content: { msgtype: "m.text", body: "1" }
    });
    assert.equal(calls.at(-1).action, "matrix_revoke");
    assert.equal(calls.at(-1).resource, "own-token");
    assert.equal(saved.get(bot.key("revoke", "!room", event.sender)), null);
    await restart.message("!room", {
        ...event,
        event_id: "$stale",
        content: { msgtype: "m.text", body: "1" }
    });
    assert.match(sent.at(-1).body, /Нет активного выбора/);
    assert.equal(calls.filter((x) => x.action === "matrix_revoke").length, 1);
});

test("invalid choice, cancel, timeout and a different room cannot revoke", async () => {
    for (const scenario of ["cancel", "invalid", "timeout", "room"]) {
        const { bot, event, calls, sent, saved } = setup();
        await bot.message("!room", { ...event, content: { msgtype: "m.text", body: "отозвать" } });
        if (scenario === "cancel")
            await bot.message("!room", {
                ...event,
                event_id: "$cancel",
                content: { msgtype: "m.text", body: "отмена" }
            });
        if (scenario === "timeout") saved.get(bot.key("revoke", "!room", event.sender)).expires = 0;
        await bot.message(scenario === "room" ? "!other" : "!room", {
            ...event,
            event_id: "$choose",
            content: { msgtype: "m.text", body: scenario === "invalid" ? "9" : "1" }
        });
        assert.equal(
            calls.some((x) => x.action === "matrix_revoke"),
            false,
            scenario
        );
        assert.match(
            sent.at(-1).body,
            scenario === "invalid" ? /Такого номера нет/ : /Нет активного выбора/
        );
    }
});

test("next revoke page extends stable numbering and selection", async () => {
    const { bot, event, item, calls, sent } = setup();
    bot.control = async (req) => {
        calls.push(req);
        if (req.action !== "matrix_tokens") return {};
        return { tokens: [{ ...item, id: req.offset ? "older" : "newer" }], has_more: !req.offset };
    };
    await bot.message("!room", { ...event, content: { msgtype: "m.text", body: "отозвать" } });
    await bot.message("!room", {
        ...event,
        event_id: "$next",
        content: { msgtype: "m.text", body: "ещё" }
    });
    assert.match(sent.at(-1).body, /2\. MIS-test/);
    await bot.message("!room", {
        ...event,
        event_id: "$choice",
        content: { msgtype: "m.text", body: "2" }
    });
    assert.equal(calls.at(-1).resource, "older");
});

test("name stays private in creator metadata and lists, not the forwarded invitation", async () => {
    const { bot, event, sent, store, client, config } = setup();
    event.content.body = "пригласить Для Антона";
    await bot.message("!room", event);
    assert.doesNotMatch(sent[0].body, /Для Антона|\отозвать/);
    assert.equal(sent[1].body, "Название для тебя: Для Антона");
    const restart = new ReferralBot(client, bot.control, store, config);
    await restart.message("!room", {
        ...event,
        event_id: "$list",
        content: { msgtype: "m.text", body: "токены" }
    });
    assert.match(sent.at(-1).body, /Для Антона/);
    assert.equal(restart.label("@other:misbot.ru", "own-token"), "");
});

test("short remaining time and used status ignore expiry", () => {
    const now = Date.parse("2026-09-27T10:00:00Z");
    const item = { attributes: { expires_at: "2026-09-29T13:00:00Z" } };
    assert.equal(remaining(item, now), "осталось 2 д 3 ч");
    item.attributes.expires_at = "2026-09-27T10:20:00Z";
    assert.equal(remaining(item, now), "осталось 20 мин");
    item.attributes.expires_at = "2026-09-26T10:00:00Z";
    assert.equal(remaining(item, now), "истёк");
    item.attributes.times_used = 1;
    assert.equal(remaining(item, now), "использован");
});
