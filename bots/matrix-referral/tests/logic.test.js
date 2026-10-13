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

test("menu formats commands and both invitation list aliases preserve ownership", async () => {
    const { bot, event, calls, sent } = setup();
    await bot.menu("!room");
    assert.match(sent[0].body, /мои приглашения — показать ваши приглашения/);
    assert.match(sent[0].formatted_body, /<code>мои приглашения<\/code>/);
    assert.match(sent[0].formatted_body, /<code>отозвать \[ID\]<\/code>/);
    assert.equal(sent[0].format, "org.matrix.custom.html");
    for (const [index, body] of ["мои приглашения", "токены"].entries()) {
        await bot.message("!room", {
            ...event,
            event_id: `$list-${index}`,
            content: { msgtype: "m.text", body }
        });
        assert.equal(calls.at(-1).action, "matrix_tokens");
        assert.equal(calls.at(-1).mxid, event.sender);
        assert.equal(calls.at(-1).offset, 0);
    }
});

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

test("old slash input only shows Russian help; invitation links contain no code", async () => {
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
    assert.doesNotMatch(message.body, /Реферальный бот|Следующая команда|токены|отозвать/);
    const links = [...message.formatted_body.matchAll(/href="([^"]+)"/g)].map((match) => match[1]);
    assert.equal(links.length, 2);
    for (const link of links) assert.equal(link.includes(item.attributes.token), false);
    assert.deepEqual(links, ["https://example.org/", "https://web.example.org"]);
    assert.match(message.body, /Для одной регистрации/);
});

test("invitation uses configured title, landing and web chat with Matrix formatting", async () => {
    const { bot, event, config, item, sent } = setup();
    Object.assign(config, {
        site_name: "Наш сервер",
        site_url: "https://community.example.org/",
        web_url: "https://community.example.org"
    });
    item.attributes.token = "MIS-fictional-code";
    await bot.message("!room", event);
    assert.equal(
        sent[0].body,
        `Приглашение в Наш сервер

Код: MIS-fictional-code

Для одной регистрации. Действует до 01.01.2027 02:30 МСК.

На телефоне:
1. Установите Element X — ссылки на community.example.org (https://community.example.org/).
2. В приложении укажите сервер misbot.ru.
3. Создайте аккаунт и введите код приглашения при регистрации.

На компьютере: откройте веб-чат (https://community.example.org) и выберите «Создать учётную запись».`
    );
    assert.equal(sent[0].format, "org.matrix.custom.html");
    assert.match(sent[0].formatted_body, /<strong>Приглашение в Наш сервер<\/strong>/);
    assert.match(sent[0].formatted_body, /Код: <code>MIS-fictional-code<\/code>/);
    assert.match(sent[0].formatted_body, /Действует до <code>01\.01\.2027 02:30 МСК<\/code>\./);
    assert.match(sent[0].formatted_body, /<strong>На телефоне:<\/strong>/);
    assert.match(sent[0].formatted_body, /<ol>[\s\S]*<li>[\s\S]*<\/ol>/);
    assert.match(sent[0].formatted_body, /сервер <code>misbot\.ru<\/code>/);
    assert.match(
        sent[0].formatted_body,
        /<a href="https:\/\/community\.example\.org\/">community\.example\.org<\/a>/
    );
    assert.match(
        sent[0].formatted_body,
        /<strong>На компьютере:<\/strong> откройте <a href="https:\/\/community\.example\.org">веб-чат<\/a>/
    );
});

test("custom web chat paths and HTML characters remain safe", () => {
    const { item } = setup();
    const message = tokenMessage(item, "example.org", {
        site_name: '<Наш & "сервер">',
        site_url: "https://community.example.org/",
        web_url: "https://chat.example.org/?from=invite&lang=ru#/register"
    });
    assert.match(message.formatted_body, /&lt;Наш &amp; &quot;сервер&quot;&gt;/);
    assert.match(message.formatted_body, /<code>MIS-test_&lt;&amp;&gt;<\/code>/);
    assert.match(
        message.formatted_body,
        /href="https:\/\/chat\.example\.org\/\?from=invite&amp;lang=ru#\/register"/
    );
    assert.doesNotMatch(message.formatted_body, /<Наш/);
});

test("invitation describes real limits and an unlimited lifetime", () => {
    const { item } = setup();
    item.attributes.usage_limit = 3;
    item.attributes.expires_at = null;
    const message = tokenMessage(item, "example.org");
    assert.match(message.body, /Регистраций по этому коду: 3\. Без срока действия\./);
    assert.doesNotMatch(message.body, /Для одной регистрации|Действует до/);
    assert.match(message.formatted_body, /Регистраций по этому коду: 3\. Без срока действия\./);
});

test("invitation rejects links with credentials or a non-HTTPS scheme", () => {
    const { item } = setup();
    for (const key of ["site_url", "web_url"]) {
        for (const value of [
            "http://example.org",
            "javascript:alert(1)",
            "https://user:fictional@example.org/"
        ]) {
            assert.throws(
                () => tokenMessage(item, "example.org", { [key]: value }),
                /Invalid invitation link/
            );
        }
    }
});

test("upgraded presentation preserves processed events across a bot restart", async () => {
    const { bot, client, store, config, calls, event, sent } = setup();
    await bot.message("!room", event);
    const restart = new ReferralBot(client, bot.control, store, {
        ...config,
        site_name: "Обновлённое имя",
        site_url: "https://community.example.org/",
        web_url: "https://chat.example.org/"
    });
    await restart.message("!room", event);
    assert.equal(calls.filter((call) => call.action === "matrix_invite").length, 1);
    assert.equal(sent.length, 1);
    await restart.message("!room", { ...event, event_id: "$next-invite" });
    assert.equal(calls.filter((call) => call.action === "matrix_invite").length, 2);
    assert.match(sent[1].body, /Приглашение в Обновлённое имя/);
});

test("retry after failed delivery keeps the controller request ID", async () => {
    const { bot, client, event, calls, sent, saved } = setup();
    const send = client.sendMessage;
    client.sendMessage = async () => {
        throw new Error("fictional delivery failure");
    };
    await assert.rejects(bot.message("!room", event), /fictional delivery failure/);
    assert.equal(saved.has("processed:" + event.event_id), false);
    client.sendMessage = send;
    await bot.message("!room", event);
    assert.deepEqual(
        calls.filter((call) => call.action === "matrix_invite").map((call) => call.request_id),
        ["matrix:$unique", "matrix:$unique"]
    );
    assert.equal(sent.length, 1);
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
    assert.match(sent.at(-1).body, /Выбор приглашения не активен/);
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
            scenario === "invalid" ? /Такого номера нет/ : /Выбор приглашения не активен/
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
    assert.equal(sent[1].body, "Название для вас: Для Антона");
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

// Read the supported notice markup as a client without formatting, retaining
// link destinations and ordered steps. Decode text after removing markup.
function readableNotice(html) {
    let number = 0;
    return html
        .replace(/>\s+</g, "><")
        .replace(/<a href="([^"]+)">([^<]+)<\/a>/g, "$2 ($1)")
        .replace(/<p>/g, "")
        .replace(/<\/p>/g, "\n\n")
        .replace(/\n\n<ol>/g, "\n")
        .replace(/<ol>/g, "")
        .replace(/<li>/g, () => `${++number}. `)
        .replace(/<\/li>/g, "\n")
        .replace(/<\/ol>/g, "\n")
        .replace(/<br>/g, "\n")
        .replace(/<\/?(?:strong|code)>/g, "")
        .replace(
            /&(?:amp|lt|gt|quot|#39);/g,
            (value) =>
                ({
                    "&amp;": "&",
                    "&lt;": "<",
                    "&gt;": ">",
                    "&quot;": '"',
                    "&#39;": "'"
                })[value]
        )
        .trimEnd();
}

test("Matrix invitation keeps identical meaning and lines with or without HTML", () => {
    const { item } = setup();
    item.attributes.token = "MIS-<&>\"'";
    const config = {
        site_name: '<Наш & "сервер">',
        site_url: "https://example.org/",
        web_url: "https://chat.example.org/?from=invite&lang=ru#/register"
    };
    for (const [limit, expiry] of [
        [1, "2026-12-31T23:30:00Z"],
        [3, null]
    ]) {
        item.attributes.usage_limit = limit;
        item.attributes.expires_at = expiry;
        const message = tokenMessage(item, "matrix.example.org", config);
        assert.equal(readableNotice(message.formatted_body), message.body);
        assert.deepEqual(tokenMessage(item, "matrix.example.org", config), message);
        assert.doesNotMatch(message.formatted_body, /<Наш|<script/);
        assert.equal((message.formatted_body.match(/<li>/g) || []).length, 3);
    }
});

test("revoke list preserves labels and code text in both formats", async () => {
    const { bot, item, event, store, sent } = setup();
    const label = '<Для & "друга">';
    store.setValue(JSON.stringify(["label", event.sender, item.id]), label);
    await bot.message("!room", { ...event, content: { msgtype: "m.text", body: "отозвать" } });
    const message = sent[0];
    assert.equal(readableNotice(message.formatted_body), message.body);
    assert.ok(message.body.includes(label));
    assert.ok(message.body.includes(item.attributes.token));
    assert.match(message.body, /Напишите номер приглашения/);
    assert.doesNotMatch(message.formatted_body, /<Для/);
});

test("plain and HTML lists share the same remaining time at an expiry boundary", async () => {
    const { bot, item, event, sent } = setup();
    const original = Date.now;
    let now = Date.parse(item.attributes.expires_at) - 500;
    Date.now = () => {
        const value = now;
        now += 60000;
        return value;
    };
    try {
        await bot.list("!room", event.sender, 0);
        assert.match(sent[0].body, /осталось 1 мин/);
        assert.equal(readableNotice(sent[0].formatted_body), sent[0].body);
    } finally {
        Date.now = original;
    }
});

test("invitation list pages continue after restart without a manually entered offset", async () => {
    const x = setup();
    x.bot.control = async (request) => {
        x.calls.push(request);
        return request.action === "matrix_tokens"
            ? {
                  tokens: Array.from({ length: 20 }, (_, index) => ({
                      ...x.item,
                      id: "token-" + (request.offset + index)
                  })),
                  has_more: request.offset === 0
              }
            : {};
    };
    const send = (body, id) =>
        x.bot.message("!room", { ...x.event, event_id: id, content: { msgtype: "m.text", body } });
    await send("токены", "$list");
    assert.match(x.sent.at(-1).body, /Следующая страница: ещё/);
    x.bot = new ReferralBot(x.client, x.bot.control, x.store, x.config);
    await send("ещё", "$next");
    assert.equal(x.calls.at(-1).offset, 20);
    assert.match(x.sent.at(-1).body, /Предыдущая страница: назад/);
    await send("назад", "$previous");
    assert.equal(x.calls.at(-1).offset, 0);
    await send("отмена", "$cancel");
    await send("ещё", "$no-list");
    assert.match(x.sent.at(-1).body, /Список приглашений не открыт/);
    assert.equal(x.calls.filter((request) => request.action === "matrix_revoke").length, 0);
});
