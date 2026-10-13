"use strict";
const test = require("node:test"),
    assert = require("node:assert/strict");
const {
    FederationBot,
    parse,
    HELP,
    MORE,
    MAINTENANCE,
    FEDERATION,
    resultText,
    stateText,
    requestsText,
    serverText,
    errorText,
    noticeContent
} = require("../logic");
function setup() {
    const cfg = {
            user_id: "@federation-bot:example.org",
            server_name: "example.org",
            administrator_mxids: ["@admin:example.org"]
        },
        sent = [],
        calls = [],
        saved = new Map();
    let allowed = true;
    const client = {
        getRoomState: async () => [
            { type: "m.room.member", state_key: cfg.user_id, content: { membership: "join" } },
            {
                type: "m.room.member",
                state_key: "@admin:example.org",
                content: { membership: "join" }
            },
            { type: "m.room.encryption", content: { algorithm: "m.megolm.v1.aes-sha2" } }
        ],
        sendMessage: async (r, content) => {
            assert.equal(content.format, "org.matrix.custom.html");
            sent.push(content.body);
        },
        joinRoom: async (r) => sent.push("join " + r),
        getRoomStateEvent: async () => ({ membership: "invite" })
    };
    const control = async (r) => {
        calls.push(r);
        if (r.action === "authorize" && !allowed) {
            const error = new Error("denied");
            error.code = "admin_unconfirmed";
            throw error;
        }
        return r.action === "authorize"
            ? { authorized: true }
            : r.action === "auto-invitation"
              ? { allowed: false }
              : r.action === "check"
                ? { invitation: { supported: true, ready: true, expires_after: 2592000 } }
                : { status: "applied" };
    };
    const bot = new FederationBot(
        client,
        control,
        { getValue: (k) => saved.get(k), setValue: (k, v) => saved.set(k, v) },
        cfg
    );
    const event = {
        event_id: "$a",
        sender: "@admin:example.org",
        content: { msgtype: "m.text", body: "добавить сервер other.example" }
    };
    return {
        bot,
        event,
        calls,
        sent,
        client,
        cfg,
        revoke: () => {
            allowed = false;
        }
    };
}

test("server invitation checks Misbot capabilities before creating a confirmation", async () => {
    for (const code of [
        "misbot_not_found",
        "remote_api_disabled",
        "invitation_unsupported",
        "unsupported_version"
    ]) {
        const x = setup();
        const control = x.bot.control;
        x.bot.control = async (request) => {
            if (request.action === "check") {
                x.calls.push(request);
                return { invitation: { supported: false, ready: false, code } };
            }
            return control(request);
        };
        await x.bot.message("!private", {
            ...x.event,
            content: { msgtype: "m.text", body: "пригласить сервер other.example" }
        });
        await x.bot.message("!private", {
            ...x.event,
            event_id: "$confirm",
            content: { msgtype: "m.text", body: "выполнить подтверждаю" }
        });
        assert.equal(x.calls.filter((request) => request.action === "invite").length, 0);
        assert.ok(x.sent.some((text) => text.includes("Приглашение не отправлено")));
    }
});

test("unavailable authorization is visible to the safe adapter and a later command recovers", async () => {
    const x = setup();
    const control = x.bot.control;
    let unavailable = true;
    x.bot.control = async (request) => {
        if (request.action === "authorize" && unavailable) {
            const error = new Error("private transport payload");
            error.code = "control_unavailable";
            throw error;
        }
        return control(request);
    };
    const event = { ...x.event, content: { msgtype: "m.text", body: "помощь" } };
    await assert.rejects(x.bot.message("!private", event), { code: "control_unavailable" });
    assert.equal(x.sent.length, 0);
    unavailable = false;
    await x.bot.message("!private", event);
    assert.equal(x.sent.length, 1);
    assert.equal(x.sent[0], noticeContent(HELP).body);
    x.revoke();
    await x.bot.message("!private", { ...event, event_id: "$after-revocation" });
    assert.equal(x.sent.length, 1);
    assert.match(errorText("control_unavailable"), /Повторите команду позже/);
    assert.doesNotMatch(errorText("private transport payload"), /private transport payload/);
});

test("capability preview uses a different event from the confirmed invitation", async () => {
    const x = setup();
    await x.bot.message("!private", {
        ...x.event,
        content: { msgtype: "m.text", body: "пригласить сервер other.example" }
    });
    await x.bot.message("!private", {
        ...x.event,
        event_id: "$confirm",
        content: { msgtype: "m.text", body: "выполнить подтверждаю" }
    });
    const preview = x.calls.find((request) => request.action === "check");
    const mutation = x.calls.find((request) => request.action === "invite");
    assert.ok(preview && mutation);
    assert.notEqual(preview.event_id, mutation.event_id);
});

test("a failed new write attempt invalidates the previous operation before its preview", async () => {
    for (const mode of ["unsupported", "timeout", "missing-request", "version-timeout"]) {
        const x = setup();
        const control = x.bot.control;
        x.bot.control = async (request) => {
            if (["check", "request-status", "panel-version"].includes(request.action)) {
                x.calls.push(request);
                assert.equal(x.bot.store.getValue("draft:!private"), null, mode);
                if (mode !== "unsupported") throw new Error("preview unavailable");
                return { invitation: { ready: false, code: "misbot_not_found" } };
            }
            return control(request);
        };
        const send = (body, event_id) =>
            x.bot.message("!private", {
                ...x.event,
                event_id,
                content: { msgtype: "m.text", body }
            });
        await send("api выключить", "$old");
        const next =
            mode === "missing-request"
                ? "подтвердить MB-" + "a".repeat(32)
                : mode === "version-timeout"
                  ? "обновить"
                  : "пригласить сервер unsupported.example";
        if (mode === "unsupported") await send(next, "$new");
        else await assert.rejects(send(next, "$new"));
        await send("выполнить подтверждаю", "$confirm");
        assert.equal(
            x.calls.filter((r) =>
                ["api-disable", "invite", "approve", "panel-update"].includes(r.action)
            ).length,
            0,
            mode
        );
        assert.match(x.sent.at(-1), /Действующего предложения нет/, mode);
    }
});

test("an unavailable invitation invalidates an earlier update while read commands keep a proposal", async () => {
    const x = setup();
    const control = x.bot.control;
    x.bot.control = async (request) => {
        if (request.action === "panel-version") {
            x.calls.push(request);
            return {
                installed: "1.1.0",
                latest: "1.2.0",
                update_available: true,
                update_supported: true
            };
        }
        if (request.action === "check") {
            x.calls.push(request);
            return { invitation: { ready: false, code: "misbot_not_found" } };
        }
        return control(request);
    };
    let sequence = 0;
    const send = (body) =>
        x.bot.message("!private", {
            ...x.event,
            event_id: "$context" + ++sequence,
            content: { msgtype: "m.text", body }
        });
    await send("обновить");
    await send("пригласить сервер unsupported.example");
    await send("обновить подтверждаю");
    assert.equal(x.calls.filter((r) => r.action === "panel-update").length, 0);
    await send("api выключить");
    await send("api");
    await send("выполнить подтверждаю");
    assert.equal(x.calls.filter((r) => r.action === "api-disable").length, 1);
});
test("Russian grammar and opaque IDs", () => {
    assert.equal(parse("подтвердить MB-" + "a".repeat(32)).action, "approve");
    assert.equal(parse("подтвердить 1"), null);
    assert.match(FEDERATION, /пригласить сервер/);
    assert.doesNotMatch(HELP, /создать копию|обновить подтверждаю|перезапустить сервер/i);
    assert.match(MAINTENANCE, /перезапустить сервер/);
});
test("all federation command words ignore case without changing their arguments", () => {
    const id = "MB-" + "0123456789abcdef".repeat(2),
        server = "Peer.Example";
    for (const [command, action] of Object.entries({
        серверы: "servers",
        заявки: "requests",
        операции: "operations",
        восстановить: "recover",
        откатить: "rollback"
    })) {
        assert.deepEqual(parse(command.toUpperCase()), { action }, command);
    }
    for (const [command, action] of Object.entries({
        проверить: "check",
        добавить: "add",
        удалить: "remove",
        пригласить: "invite"
    })) {
        assert.deepEqual(
            parse(command.toUpperCase() + " СеРвЕр " + server),
            { action, argument: server },
            command
        );
    }
    for (const [command, action] of Object.entries({
        подтвердить: "approve",
        отклонить: "reject",
        отменить: "cancel",
        повторить: "retry",
        статус: "request-status"
    })) {
        assert.deepEqual(
            parse(command.toUpperCase() + " " + id),
            { action, argument: id },
            command
        );
    }
});
test("removed room commands never dispatch and are absent from help", async () => {
    const commands = [
        "принять комнату !MiXeD:example.org",
        "отклонить комнату !MiXeD:example.org",
        "покинуть комнату !MiXeD:example.org",
        "пригласить бота peer.example !MiXeD:example.org",
        "автоприём включить",
        "автоприём выключить"
    ];
    for (const command of commands) {
        for (const body of [command, command.toUpperCase()]) {
            assert.equal(parse(body), null, body);
            const x = setup();
            await x.bot.message("!room", { ...x.event, content: { msgtype: "m.text", body } });
            assert.equal(x.calls.filter((c) => c.action !== "authorize").length, 0, body);
            assert.deepEqual(x.sent, [noticeContent(HELP).body], body);
        }
    }
    assert.doesNotMatch(
        HELP + MORE,
        /принять комнату|отклонить комнату|покинуть комнату|пригласить бота|автоприём/i
    );
});
test("request IDs require their exact case and unknown commands stay unknown", () => {
    const id = "MB-" + "a".repeat(32);
    for (const changed of [id.toLowerCase(), id.toUpperCase(), "Mb-" + "a".repeat(32)]) {
        assert.equal(parse("ПОДТВЕРДИТЬ " + changed), null, changed);
    }
    for (const unknown of ["constructor", "toString", "__proto__", "НЕИЗВЕСТНО"]) {
        assert.equal(parse(unknown), null);
    }
});
test("mixed-case federation commands reach the backend with untouched IDs", async () => {
    const x = setup(),
        id = "MB-" + "a".repeat(32);
    for (const [body, action, argument] of [
        ["ПОДТВЕРДИТЬ " + id, "approve", id],
        ["ПРИГЛАСИТЬ сервер Peer.Example", "invite", "Peer.Example"]
    ]) {
        await x.bot.message("!room", {
            ...x.event,
            event_id: "$" + action,
            content: { msgtype: "m.text", body }
        });
        assert.equal(
            x.calls.some((c) => c.action === action),
            false
        );
        await x.bot.message("!room", {
            ...x.event,
            event_id: "$confirm" + action,
            content: { msgtype: "m.text", body: "выполнить подтверждаю" }
        });
        assert.equal(x.calls.find((c) => c.action === action)?.argument, argument);
    }
    for (const changed of [id.toLowerCase(), id.toUpperCase()]) {
        await x.bot.message("!room", {
            ...x.event,
            event_id: "$" + changed,
            content: { msgtype: "m.text", body: "ПОДТВЕРДИТЬ " + changed }
        });
    }
    assert.equal(x.calls.filter((c) => c.action === "approve").length, 1);
});
test("everyday commands and settings work in either case", async () => {
    const commands = [
        ["помощь"],
        ["меню"],
        ["help"],
        ["/start"],
        ["ещё"],
        ["еще"],
        ["версия", "panel-version"],
        ["уведомления", "panel-status"],
        ["приветствие", "panel-welcome"],
        ...Object.entries({ день: "day", неделя: "week", месяц: "month", всего: "all" }).map(
            ([period, value]) => [
                "статистика " + period,
                "panel-server-read",
                { operation: "statistics", period: value }
            ]
        ),
        ...Object.entries({
            обновления: "updates",
            регистрации: "registrations",
            статистика: "statistics"
        }).flatMap(([name, key]) => [
            ["уведомления " + name + " включить", "panel-preferences", { key, enabled: true }],
            ["уведомления " + name + " выключить", "panel-preferences", { key, enabled: false }]
        ])
    ];
    for (const [command, action, argument] of commands) {
        for (const body of [command, command.toUpperCase()]) {
            const x = setup();
            x.bot.control = async (request) => {
                x.calls.push(request);
                return request.action === "authorize"
                    ? { authorized: true }
                    : {
                          installed: "1.1.0",
                          latest: "1.1.0",
                          preferences: { updates: true, registrations: true, statistics: true },
                          text: "Добро пожаловать!",
                          messages: 3,
                          registrations: 1
                      };
            };
            await x.bot.message("!room", { ...x.event, content: { msgtype: "m.text", body } });
            assert.ok(x.sent.length, body);
            const operations = x.calls.filter((c) => c.action !== "authorize");
            assert.equal(operations.length, action ? 1 : 0, body);
            if (action) {
                assert.equal(operations[0].action, action, body);
                if (argument !== undefined)
                    assert.deepEqual(operations[0].argument, argument, body);
            }
        }
    }
});
test("welcome editing preserves text case and recognizes mixed-case cancellation", async () => {
    const x = setup();
    let seq = 0;
    const send = (body) =>
        x.bot.message("!room", {
            ...x.event,
            event_id: "$welcome" + ++seq,
            content: { msgtype: "m.text", body }
        });
    await send("ИзМеНиТь ПрИвЕтСтВиЕ");
    for (const field of ["{domain}", "{server_name}", "{referral_url}", "{administrator_url}"])
        assert.ok(x.sent.at(-1).includes(field));
    await send("Добро пожаловать на {domain}! Matrix ID: @Alice:example.org");
    const welcome = x.calls.find((c) => c.action === "panel-welcome");
    assert.equal(welcome.argument, "Добро пожаловать на {domain}! Matrix ID: @Alice:example.org");
    for (const cancel of ["ОтМеНа", "ОТМЕНИТЬ"]) {
        await send("ИЗМЕНИТЬ ПРИВЕТСТВИЕ");
        await send(cancel);
        assert.equal(x.sent.at(-1), "Действие отменено.");
    }
    assert.equal(x.calls.filter((c) => c.action === "panel-welcome").length, 1);
});
test("current rights and event replay/self/edit", async () => {
    const x = setup();
    await x.bot.message("!room", x.event);
    await x.bot.message("!room", x.event);
    await x.bot.message("!room", {
        ...x.event,
        event_id: "$confirm",
        content: { msgtype: "m.text", body: "выполнить подтверждаю" }
    });
    assert.equal(x.calls.filter((r) => r.action === "add").length, 1);
    for (const sender of [x.cfg.user_id, "@admin:foreign.example"]) {
        await x.bot.message("!room", { ...x.event, event_id: "$foreign", sender });
    }
    await x.bot.message("!room", {
        ...x.event,
        event_id: "$edit",
        content: { ...x.event.content, "m.relates_to": { rel_type: "m.replace" } }
    });
    x.revoke();
    await x.bot.message("!room", { ...x.event, event_id: "$revoked" });
    assert.equal(x.calls.filter((r) => r.action === "add").length, 1);
});
test("manual default and explicit trusted auto invitation", async () => {
    const x = setup(),
        event = {
            state_key: x.cfg.user_id,
            sender: "@federation-bot:other.example",
            content: { membership: "invite" }
        };
    await x.bot.invitation("!room", event);
    assert.equal(x.sent.length, 0);
    x.bot.control = async () => ({ allowed: true });
    await x.bot.invitation("!room", event);
    assert.equal(x.sent[0], "join !room");
    x.bot.control = async () => ({ allowed: false });
    await x.bot.invitation("!revoked", event);
    assert.equal(x.sent.length, 1);
});
test("only the configured owner receives replies; group commands are ignored", async () => {
    const x = setup();
    await x.bot.message("!room", { ...x.event, sender: "@another-admin:example.org" });
    assert.equal(x.sent.length, 0);
    assert.equal(x.calls.length, 0);
    x.client.getRoomState = async () =>
        [x.cfg.user_id, "@admin:example.org", "@guest:example.org"].map((state_key) => ({
            type: "m.room.member",
            state_key,
            content: { membership: "join" }
        }));
    await x.bot.message("!group", x.event);
    assert.equal(x.sent.length, 0);
    assert.equal(x.calls.filter((c) => c.action === "add").length, 0);
});
test("broadcast preview is separate from confirmation, edit and cancel require no delivery", async () => {
    const x = setup();
    let seq = 0;
    const send = (body) =>
        x.bot.message("!room", {
            ...x.event,
            event_id: "$" + ++seq,
            content: { msgtype: "m.text", body }
        });
    await send("РаСсЫлКа");
    await send("Первые Новости");
    assert.equal(
        x.sent.at(-2),
        "Сообщение от администратора сервера example.org:\n\nПервые Новости"
    );
    assert.match(x.sent.at(-1), /Подтверждение/);
    await send("Новые НОВОСТИ");
    assert.equal(
        x.sent.at(-2),
        "Сообщение от администратора сервера example.org:\n\nНовые НОВОСТИ"
    );
    assert.equal(x.calls.filter((c) => c.action === "panel-broadcast").length, 0);
    x.bot.control = async (r) => {
        x.calls.push(r);
        return r.action === "authorize" ? { authorized: true } : { queued: 2, message: "очередь" };
    };
    await send("ОТПРАВИТЬ");
    const calls = x.calls.filter((c) => c.action === "panel-broadcast");
    assert.equal(calls.length, 1);
    assert.equal(calls[0].argument, "Новые НОВОСТИ");
    assert.equal(calls[0].event_id, "$3");
    await send("Рассылка");
    await send("Не отправлять");
    await send("ОтМеНа");
    assert.equal(x.calls.filter((c) => c.action === "panel-broadcast").length, 1);
});
test("confirmed update is pinned and retries use the original event", async () => {
    const x = setup();
    x.bot.control = async (r) => {
        x.calls.push(r);
        return r.action === "authorize"
            ? { authorized: true }
            : r.action === "panel-version"
              ? {
                    installed: "1.1.0",
                    latest: "1.2.0",
                    update_available: true,
                    command: "sudo misbot update latest"
                }
              : { version: r.argument, status: "queued" };
    };
    await x.bot.message("!room", {
        ...x.event,
        event_id: "$version",
        content: { msgtype: "m.text", body: "ОбНоВиТь" }
    });
    assert.equal(x.calls.filter((r) => r.action === "panel-update").length, 0);
    await x.bot.message("!room", {
        ...x.event,
        event_id: "$confirm",
        content: { msgtype: "m.text", body: "ОБНОВИТЬ ПодТверЖдаЮ" }
    });
    const update = x.calls.find((r) => r.action === "panel-update");
    assert.equal(update.argument, "1.2.0");
    assert.equal(update.event_id, "$version");
});
test("owner revocation during an operation prevents its reply", async () => {
    const x = setup(),
        original = x.bot.control;
    x.bot.control = async (request) => {
        const result = await original(request);
        if (request.action === "add") x.revoke();
        return result;
    };
    await x.bot.message("!room", x.event);
    await assert.rejects(
        x.bot.message("!room", {
            ...x.event,
            event_id: "$confirm",
            content: { msgtype: "m.text", body: "выполнить подтверждаю" }
        })
    );
    assert.equal(x.sent.length, 1);
});

test("operation presentation separates acceptance, execution and completion", () => {
    for (const [state, meaning] of [
        ["queued", "принято"],
        ["running", "выполняется"],
        ["complete", "готово"],
        ["failed", "завершение не подтверждено"],
        ["interrupted", "прервано"]
    ]) {
        const result = {
            status: state,
            operation_id: "FO-fictional",
            next_action: "misbot federation operations"
        };
        const before = structuredClone(result);
        const body = resultText(result);
        assert.ok(body.startsWith(`Операция: ${meaning}.\n`));
        assert.match(body, /FO-fictional/);
        assert.match(body, /misbot federation operations/);
        assert.deepEqual(result, before);
        assert.equal(resultText(result), body);
    }
    assert.equal(stateText("connected"), "политика применена на обоих серверах");
    assert.equal(stateText("future_state"), "future_state");
});

test("plain results keep special characters and suppress private recovery details", () => {
    const result = {
        servers: ["example.org"],
        note: "<&>\"'\nСледующая строка",
        backup: "FICTIONAL_SECRET",
        nested: { checkpoint: "FICTIONAL_SECRET", code: "busy" }
    };
    const body = resultText(result);
    assert.ok(body.startsWith("Результат запроса:\n"));
    assert.ok(body.includes("1. example.org"));
    assert.ok(body.includes(result.note));
    assert.doesNotMatch(body, /FICTIONAL_SECRET/);
    assert.equal(resultText({ status: "unchanged" }), "Операция: изменения не требуются.");
});

test("invalid formatter parameters fail without including their values", () => {
    for (const value of [undefined, null, "FICTIONAL_SECRET", ["FICTIONAL_SECRET"]]) {
        assert.throws(() => resultText(value), { message: "Invalid operation result" });
    }
    for (const value of [undefined, null, "", { secret: "FICTIONAL_SECRET" }]) {
        assert.throws(() => stateText(value), { message: "Invalid operation state" });
    }
});

test("notifications keep commands, distinct states and deduplication after restart", async () => {
    const x = setup(),
        id = "MB-" + "a".repeat(32);
    const payloads = [];
    x.client.sendMessage = async (room, content) => {
        x.sent.push(content.body);
        payloads.push(content);
    };
    const snapshot = {
        requests: [{ id, server: "peer.example.org", state: "pending" }],
        operations: [
            {
                id: "FO-fictional",
                state: "interrupted",
                result: {
                    code: "interrupted",
                    next_action: "sudo misbot federation recover",
                    checkpoint: "FICTIONAL_SECRET"
                }
            }
        ]
    };
    x.client.getJoinedRooms = async () => ["!room", "!other"];
    x.bot.control = async (request) =>
        request.action === "authorize" ? { authorized: true } : snapshot;
    await x.bot.notifications();
    assert.equal(x.sent.length, 2);
    assert.match(x.sent[0], /ожидает решения/);
    assert.ok(x.sent[0].includes("подтвердить 1"));
    assert.ok(x.sent[0].includes("отклонить 1"));
    assert.match(payloads[0].formatted_body, /<code>подтвердить 1<\/code>/);
    assert.match(payloads[0].formatted_body, /<code>отклонить 1<\/code>/);
    assert.match(x.sent[1], /прервано/);
    assert.match(x.sent[1], /sudo misbot federation recover/);
    assert.doesNotMatch(x.sent[1], /FICTIONAL_SECRET/);
    const restart = new FederationBot(x.client, x.bot.control, x.bot.store, x.cfg);
    await restart.notifications();
    assert.equal(x.sent.length, 2);
    snapshot.requests[0].state = "connected";
    await restart.notifications();
    assert.equal(x.sent.length, 3);
    assert.match(x.sent[2], /политика применена на обоих серверах/);
    assert.doesNotMatch(x.sent[2], /комнаты проверены/);
    assert.doesNotMatch(x.sent[2], /подтвердить |отклонить /);
    assert.match(payloads[2].formatted_body, /<code>статус 1<\/code>/);
    snapshot.requests[0].state = "rejected";
    await restart.notifications();
    assert.match(payloads[3].formatted_body, /<code>заявки отклонённые<\/code>/);
});

test("grouped menus keep destructive actions inside maintenance and invitations inside more", async () => {
    const x = setup();
    for (const [body, expected] of [
        ["меню", HELP],
        ["обслуживание", MAINTENANCE],
        ["ещё", MORE]
    ]) {
        await x.bot.message("!room", {
            ...x.event,
            event_id: "$" + body,
            content: { msgtype: "m.text", body }
        });
        assert.equal(x.sent.at(-1), noticeContent(expected).body);
    }
    assert.match(HELP, /статистика[\s\S]*состояние[\s\S]*федерация/);
    assert.doesNotMatch(HELP, /приглашения —|создать копию|остановить службы/);
    x.bot.control = async (request) => {
        x.calls.push(request);
        return request.action === "authorize"
            ? { authorized: true }
            : { url: "https://matrix.to/#/@invites:example.org" };
    };
    await x.bot.message("!room", {
        ...x.event,
        event_id: "$referral",
        content: { msgtype: "m.text", body: "приглашения" }
    });
    assert.match(x.sent.at(-1), /https:\/\/matrix.to\/#\/@invites:example.org/);
    assert.equal(x.calls.filter((c) => c.action === "panel-referral").length, 1);
});

test("server maintenance needs one bound confirmation and returns its saved task identifier", async () => {
    const x = setup();
    x.bot.control = async (request) => {
        x.calls.push(request);
        return request.action === "authorize"
            ? { authorized: true }
            : { status: "queued", operation_id: "SO-fictional" };
    };
    const send = (body, event_id) =>
        x.bot.message("!room", { ...x.event, event_id, content: { msgtype: "m.text", body } });
    await send("перезапустить сервер", "$restart");
    assert.equal(x.calls.filter((c) => c.action === "panel-server-operation").length, 0);
    assert.match(x.sent.at(-1), /перерыв/);
    await send("выполнить подтверждаю", "$confirm1");
    await send("выполнить подтверждаю", "$confirm2");
    const operations = x.calls.filter((c) => c.action === "panel-server-operation");
    assert.equal(operations.length, 1);
    assert.deepEqual(operations[0].argument, { operation: "restart" });
    assert.equal(operations[0].event_id, "$restart");
    assert.ok(x.sent.some((s) => s.includes("задача 1")));
    await send("задача SO-fictional", "$result");
    assert.equal(x.calls.find((c) => c.action === "panel-server-result").argument, "SO-fictional");
});

test("expired, cancelled and transferred confirmation drafts never execute", async () => {
    for (const mode of ["expired", "cancelled", "room", "actor"]) {
        const x = setup();
        const send = (body, id) =>
            x.bot.message("!room", {
                ...x.event,
                event_id: id,
                content: { msgtype: "m.text", body }
            });
        await send("api выключить", "$off");
        const draft = x.bot.store.getValue("draft:!room");
        if (mode === "expired") draft.created = Date.now() - 301000;
        if (mode === "room") draft.room = "!different";
        if (mode === "actor") draft.actor = "@old-owner:example.org";
        if (mode === "cancelled") await send("отмена", "$cancel");
        await send("выполнить подтверждаю", "$confirm");
        assert.equal(x.calls.filter((c) => c.action === "api-disable").length, 0, mode);
        assert.match(x.sent.at(-1), /Действующего предложения нет/, mode);
    }
});

test("an ambiguous confirmation reply retries the exact same proposal event", async () => {
    const x = setup();
    let attempts = 0;
    x.bot.control = async (request) => {
        x.calls.push(request);
        if (request.action === "authorize") return { authorized: true };
        if (request.action === "api-enable" && ++attempts === 1) throw new Error("timeout");
        return { status: "already_processed" };
    };
    const send = (body, id) =>
        x.bot.message("!room", { ...x.event, event_id: id, content: { msgtype: "m.text", body } });
    await send("api включить", "$enable");
    await assert.rejects(send("выполнить подтверждаю", "$confirm1"));
    await send("выполнить подтверждаю", "$confirm2");
    const attemptsMade = x.calls.filter((c) => c.action === "api-enable");
    assert.deepEqual(
        attemptsMade.map((c) => c.event_id),
        ["$enable", "$enable"]
    );
});

test("server reads and federation request categories remain separate", async () => {
    const x = setup();
    x.bot.control = async (request) => {
        x.calls.push(request);
        return request.action === "authorize"
            ? { authorized: true }
            : request.action === "requests"
              ? {
                    requests: [
                        {
                            id: "MB-" + "1".repeat(32),
                            server: "incoming.example",
                            direction: "incoming",
                            state: "pending"
                        },
                        {
                            id: "MB-" + "2".repeat(32),
                            server: "outgoing.example",
                            direction: "outgoing",
                            state: "pending"
                        },
                        {
                            id: "MB-" + "3".repeat(32),
                            server: "rejected.example",
                            direction: "outgoing",
                            state: "rejected"
                        },
                        {
                            id: "MB-" + "4".repeat(32),
                            server: "hidden.example",
                            direction: "incoming",
                            state: "rejected",
                            cleared: true
                        }
                    ]
                }
              : { ok: true, checks: [{ name: "matrix_https", passed: true }] };
    };
    for (const [body, expected, absent] of [
        ["заявки входящие", "incoming.example", "outgoing.example"],
        ["заявки исходящие", "outgoing.example", "incoming.example"],
        ["заявки отклонённые", "rejected.example", "hidden.example"]
    ]) {
        await x.bot.message("!room", {
            ...x.event,
            event_id: "$" + body,
            content: { msgtype: "m.text", body }
        });
        assert.ok(x.sent.at(-1).includes(expected));
        assert.ok(!x.sent.at(-1).includes(absent));
    }
    await x.bot.message("!room", {
        ...x.event,
        event_id: "$check",
        content: { msgtype: "m.text", body: "проверить" }
    });
    assert.deepEqual(x.calls.find((c) => c.action === "panel-server-read").argument, {
        operation: "check"
    });
    assert.match(x.sent.at(-1), /matrix_https: доступно/);
    assert.equal(requestsText({ requests: [] }, "rejected"), "Отклонённые заявки: 0.");
    assert.match(serverText({ ok: false, recovery_required: true }), /через SSH/);
});

test("outgoing declines notify without offering invalid incoming actions", async () => {
    const x = setup();
    x.client.getJoinedRooms = async () => ["!room"];
    const id = "MB-" + "b".repeat(32);
    x.bot.control = async (request) =>
        request.action === "authorize"
            ? { authorized: true }
            : {
                  requests: [
                      { id, server: "peer.example", direction: "outgoing", state: "rejected" },
                      {
                          id: "pending",
                          server: "other.example",
                          direction: "outgoing",
                          state: "pending"
                      }
                  ]
              };
    await x.bot.notifications();
    assert.equal(x.sent.length, 1);
    assert.match(x.sent[0], /отклонено/);
    assert.doesNotMatch(x.sent[0], /подтвердить |отклонить /);
});

test("expected federation refusals are actionable and arbitrary remote error text stays hidden", () => {
    assert.match(errorText("invitation_rate_limit"), /одного раза в минуту/);
    assert.match(errorText("pending_request_exists"), /уже есть актуальная заявка/);
    assert.match(errorText("api_disabled"), /выключен/);
    assert.match(errorText("peer_upgrade_required"), /обновить Misbot/);
    assert.doesNotMatch(errorText("FICTIONAL_SECRET"), /FICTIONAL_SECRET/);
    assert.doesNotMatch(
        errorText({ toString: null, secret: "FICTIONAL_SECRET" }),
        /FICTIONAL_SECRET/
    );
    assert.match(errorText("FICTIONAL_SECRET"), /задачи/);
});

test("statistics keep unavailable counters unknown and show a compact activity report", () => {
    const body = serverText({
        operation: "statistics",
        ok: false,
        checked_at: 2000000000,
        statistics: {
            disk_free_bytes: 3 * 1073741824,
            memory_used_bytes: 512 * 1048576,
            memory_total_bytes: 1024 * 1048576,
            cpu_load: 0.12,
            uptime_seconds: 7200,
            note: "Статистика сообщений недоступна."
        }
    });
    assert.match(body, /Сообщений: недоступно/);
    assert.match(body, /Регистраций: недоступно/);
    assert.doesNotMatch(body, /Память|Свободное место|нагрузка|Число звонков|Средняя длительность/);
    assert.match(body, /Повторите запрос позже/);
    assert.doesNotMatch(body, /undefined|NaN|Сообщений: 0/);
});

test("a request preview cannot consume the mutation event and always identifies the target server", async () => {
    const x = setup();
    const consumed = new Set();
    x.bot.control = async (request) => {
        x.calls.push(request);
        if (request.action === "authorize") return { authorized: true };
        assert.ok(!consumed.has(request.event_id));
        consumed.add(request.event_id);
        return request.action === "request-status"
            ? { server: "target.example", state: "pending" }
            : { status: "queued" };
    };
    const id = "MB-" + "f".repeat(32);
    await x.bot.message("!room", {
        ...x.event,
        event_id: "$proposal",
        content: { msgtype: "m.text", body: "подтвердить " + id }
    });
    assert.match(x.sent.at(-1), /Сервер: target.example/);
    await x.bot.message("!room", {
        ...x.event,
        event_id: "$confirm",
        content: { msgtype: "m.text", body: "выполнить подтверждаю" }
    });
    const read = x.calls.find((c) => c.action === "request-status"),
        write = x.calls.find((c) => c.action === "approve");
    assert.notEqual(read.event_id, write.event_id);
    assert.equal(write.event_id, "$proposal");
});

test("numbered requests keep their actual identity across filters, new requests and restart", async () => {
    const x = setup();
    const first = "MB-" + "1".repeat(32),
        second = "MB-" + "2".repeat(32),
        third = "MB-" + "3".repeat(32);
    let requests = [
        { id: first, server: "first.example", direction: "incoming", state: "pending" },
        { id: second, server: "second.example", direction: "outgoing", state: "pending" }
    ];
    x.bot.control = async (request) => {
        x.calls.push(request);
        if (request.action === "authorize") return { authorized: true };
        if (request.action === "requests") return { requests };
        if (request.action === "request-status")
            return { server: "first.example", state: "pending" };
        return { status: "queued" };
    };
    let event = 0;
    const send = (body) =>
        x.bot.message("!room", {
            ...x.event,
            event_id: "$numbered" + ++event,
            content: { msgtype: "m.text", body }
        });
    await send("заявки");
    assert.match(x.sent.at(-2), /1\. first.example[\s\S]*2\. second.example/);
    assert.ok(!x.sent.some((body) => body.includes(first) || body.includes(second)));
    requests = [
        requests[1],
        { id: third, server: "third.example", direction: "incoming", state: "pending" }
    ];
    await send("заявки исходящие");
    assert.match(x.sent.at(-1), /2\. second.example/);
    await send("заявки входящие");
    assert.match(x.sent.at(-1), /3\. third.example/);
    x.bot = new FederationBot(x.client, x.bot.control, x.bot.store, x.cfg);
    await send("подтвердить 1");
    assert.match(x.sent.at(-1), /заявка №1/);
    await send("выполнить подтверждаю");
    assert.equal(x.calls.find((request) => request.action === "approve").argument, first);
    await send("подтвердить 999");
    assert.match(x.sent.at(-1), /Номер заявки не найден/);
    assert.equal(x.calls.filter((request) => request.action === "approve").length, 1);
});

test("several configured admins use separate private rooms; unrelated or revoked accounts cannot command", async () => {
    const x = setup();
    const other = "@personal:example.org";
    x.cfg.administrator_mxids.push(other);
    x.client.getRoomState = async (room) =>
        [x.cfg.user_id, room === "!personal" ? other : "@admin:example.org"]
            .map((state_key) => ({
                type: "m.room.member",
                state_key,
                content: { membership: "join" }
            }))
            .concat({ type: "m.room.encryption", content: { algorithm: "m.megolm.v1.aes-sha2" } });
    const send = (room, sender, id) =>
        x.bot.message(room, {
            ...x.event,
            sender,
            event_id: id,
            content: { msgtype: "m.text", body: "меню" }
        });
    await send("!personal", other, "$personal");
    await send("!room", "@admin:example.org", "$primary");
    assert.equal(x.sent.length, 2);
    await send("!room", other, "$wrong-room");
    await send("!personal", "@unlisted:example.org", "$unlisted");
    x.revoke();
    await send("!personal", other, "$revoked");
    assert.equal(x.sent.length, 2);
});

test("notice HTML escapes inserted text and preserves all plain text commands and line breaks", () => {
    const body =
        "Результат <&>\"'\n\n1. `команда example.org` — описание\nhttps://example.org/?a=1&b=2";
    const content = noticeContent(body);
    assert.equal(content.body, body.replaceAll("`", ""));
    assert.equal(content.format, "org.matrix.custom.html");
    assert.ok(content.formatted_body.includes("1. <code>команда example.org</code>"));
    const restored = content.formatted_body
        .replace(/<br>/g, "\n")
        .replace(/<[^>]+>/g, "")
        .replace(/&quot;/g, '"')
        .replace(/&#39;/g, "'")
        .replace(/&lt;/g, "<")
        .replace(/&gt;/g, ">")
        .replace(/&amp;/g, "&");
    assert.equal(restored, content.body);
});

test("state and old check aliases show the same service and availability checks", async () => {
    const x = setup();
    x.bot.control = async (request) => {
        x.calls.push(request);
        if (request.action === "authorize") return { authorized: true };
        return request.argument.operation === "check"
            ? { ok: false, checks: [{ name: "matrix_https", passed: false }] }
            : { ok: true, containers: [{ name: "synapse", running: false, healthy: true }] };
    };
    for (const [index, body] of [
        "состояние",
        "проверить",
        "статус сервера",
        "проверить сервер"
    ].entries()) {
        await x.bot.message("!room", {
            ...x.event,
            event_id: "$alias" + index,
            content: { msgtype: "m.text", body }
        });
        assert.match(x.sent.at(-1), /✗ synapse: остановлено/);
        assert.match(x.sent.at(-1), /✗ matrix_https: недоступно/);
    }
    assert.equal(x.calls.filter((request) => request.action === "panel-server-read").length, 8);
});

test("a lost persistent number mapping is never silently rebuilt", () => {
    const x = setup();
    const id = "MB-" + "f".repeat(32);
    assert.equal(x.bot.requestNumber(id), 1);
    x.bot.store.setValue("request-index", null);
    assert.throws(() => x.bot.requestNumber(id), /index missing/);
});

test("actual backend cleanup and API changes keep their human result and federation check scope", () => {
    assert.equal(
        resultText({ status: "cleared", count: 12 }),
        "Операция: завершённые заявки убраны.\nУбрано заявок: 12"
    );
    assert.match(
        resultText({ status: "changed", enabled: false }),
        /изменения сохранены[\s\S]*API Misbot: выключен/
    );
    const checked = resultText({
        server: "peer.example.org",
        policy: true,
        matrix_discovery: "available",
        https: "available",
        invitation: { ready: true },
        federation_exchange: "not_verified"
    });
    assert.match(checked, /Местная политика: сервер разрешён/);
    assert.match(checked, /Работа федеративных комнат не проверялась/);
    assert.match(checked, /Можно отправить приглашение/);
    assert.doesNotMatch(
        resultText({
            peers: [{ server: "peer.example", approved: true, state: "partially_applied" }]
        }),
        /подключён/
    );
});

test("recovery is offered only for a pending federation change, including old aliases", async () => {
    for (const pending of [false, true]) {
        for (const command of [
            "восстановить",
            "откатить",
            "продолжить изменение федерации",
            "откатить изменение федерации"
        ]) {
            const x = setup(),
                control = x.bot.control;
            x.bot.control = async (request) =>
                request.action === "operations"
                    ? { recovery_available: pending, operations: [] }
                    : control(request);
            const send = (body, id) =>
                x.bot.message("!private", {
                    ...x.event,
                    event_id: id,
                    content: { msgtype: "m.text", body }
                });
            await send("настройки федерации", "$settings");
            assert.equal(x.sent.at(-1).includes("продолжить изменение федерации"), pending);
            await send(command, "$request");
            await send("выполнить подтверждаю", "$confirm");
            assert.equal(
                x.calls.filter((call) => ["recover", "rollback"].includes(call.action)).length,
                pending ? 1 : 0
            );
            if (!pending) assert.ok(x.sent.some((body) => body.includes("Действие не требуется")));
        }
    }
});

test("all advertised menu commands have an actual route and a visible result", async () => {
    // Follow the real router: plain unrecognised commands fall back to HELP.
    const menus = [
        HELP,
        MORE,
        MAINTENANCE,
        FEDERATION,
        require("../logic").FEDERATION_SETTINGS,
        require("../logic").COPIES,
        require("../logic").REQUESTS
    ];
    for (const menu of menus) {
        for (const match of menu.matchAll(/`([^`]+)`/g)) {
            const command = match[1].replace(/\bN\b/g, "1").replace(/\bID\b/g, "SO-fixture");
            const x = setup(),
                control = x.bot.control;
            x.bot.control = async (request) => {
                if (request.action === "panel-status") return { preferences: {} };
                if (request.action === "panel-version")
                    return { installed: "1.2.0", update_available: false, command: "обновления" };
                if (request.action === "panel-welcome") return { text: "Ваше приветствие" };
                return control(request);
            };
            await x.bot.message("!private", {
                ...x.event,
                content: { msgtype: "m.text", body: command }
            });
            assert.ok(x.sent.length, command);
            if (!["меню"].includes(command))
                assert.notEqual(x.sent.at(-1), noticeContent(HELP).body, command);
        }
    }
});

test("commands, current data and permanent notes have separate formatting", () => {
    const content = noticeContent(
        "Сейчас\n1. example.org — доступен\n1. `api включить` / `api выключить`\nПамятка: решение требует подтверждения."
    );
    assert.match(content.formatted_body, /1\. example.org — доступен/);
    assert.match(
        content.formatted_body,
        /1\. <code>api включить<\/code> \/ <code>api выключить<\/code>/
    );
    assert.match(content.formatted_body, /<em>Памятка:/);
    assert.doesNotMatch(content.formatted_body, /<code>1\./);
    const literal = noticeContent("Ваш `текст` <&>\nПамятка: пример", { literal: true });
    assert.equal(literal.body, "Ваш `текст` <&>\nПамятка: пример");
    assert.doesNotMatch(literal.formatted_body, /<code>/);
});

test("backend recovery command keeps a quoted directory and excludes sentence punctuation", () => {
    const content = noticeContent(
        resultText({
            next_action: "Откройте sudo misbot --directory '/opt/matrix stack' federation recover."
        })
    );
    assert.match(
        content.formatted_body,
        /<code>sudo misbot --directory &#39;\/opt\/matrix stack&#39; federation recover<\/code>\./
    );
});

test("security guidance names the actual separate SSH sections", async () => {
    const x = setup();
    await x.bot.message("!private", {
        ...x.event,
        content: { msgtype: "m.text", body: "безопасность" }
    });
    assert.match(x.sent[0], /Администраторы и пароль matrix-admin: «Администраторы и доступ»/);
    assert.match(x.sent[0], /Пароли остальных пользователей: «Пользователи и приглашения»/);
});

test("completed or duplicate requests are refused before offering or executing an operation", async () => {
    for (const code of ["invalid_transition", "pending_request_exists", "request_in_progress"]) {
        const x = setup();
        const control = x.bot.control;
        x.bot.control = async (request) => {
            if (request.action === "validate") {
                assert.deepEqual(request.argument, { action: "invite", argument: "peer.example" });
                throw Object.assign(new Error("private detail"), { code });
            }
            return control(request);
        };
        await x.bot.message("!room", {
            ...x.event,
            content: { msgtype: "m.text", body: "пригласить сервер peer.example" }
        });
        assert.equal(x.bot.store.getValue("draft:!room"), undefined);
        assert.match(x.sent.at(-1), /недоступно|актуальная заявка|уже принято/);
        assert.doesNotMatch(x.sent.at(-1), /private detail|выполнить подтверждаю/);
        assert.equal(
            x.calls.some((r) => r.action === "invite"),
            false
        );
    }
});

test("a request decided by another admin between proposal and confirmation never starts", async () => {
    const x = setup(),
        id = "MB-" + "c".repeat(32);
    const control = x.bot.control;
    let terminal = false;
    x.bot.control = async (request) => {
        if (request.action === "validate" && terminal)
            throw Object.assign(new Error("stale"), { code: "invalid_transition" });
        return control(request);
    };
    const send = (body, event_id) =>
        x.bot.message("!room", {
            ...x.event,
            event_id,
            content: { msgtype: "m.text", body }
        });
    await send("подтвердить " + id, "$proposal");
    assert.match(x.sent.at(-1), /заявка №1/);
    terminal = true;
    await send("выполнить подтверждаю", "$confirm");
    assert.match(x.sent.at(-1), /действие уже недоступно/);
    assert.equal(
        x.calls.some((r) => r.action === "approve"),
        false
    );
    assert.equal(x.bot.store.getValue("draft:!room"), null);
});

test("repeated proposals keep one event and distinct cancellation targets are explicit", async () => {
    const x = setup(),
        first = "MB-" + "1".repeat(32),
        second = "MB-" + "2".repeat(32);
    x.bot.requestNumber(first);
    x.bot.requestNumber(second);
    const send = (body, event_id) =>
        x.bot.message("!room", {
            ...x.event,
            event_id,
            content: { msgtype: "m.text", body }
        });
    await send("отменить 1", "$first");
    await send("отменить 1", "$duplicate");
    assert.match(x.sent.at(-1), /Уже ожидает подтверждения:[\s\S]*заявка №1/);
    assert.equal(x.bot.store.getValue("draft:!room").id, "$first");
    await send("отменить 2", "$second");
    assert.match(x.sent.at(-1), /Предыдущее предложение заменено[\s\S]*заявка №2/);
    assert.doesNotMatch(x.sent.at(-1), /5 минут/);
    await send("выполнить подтверждаю", "$confirm");
    await send("выполнить подтверждаю", "$again");
    const mutations = x.calls.filter((r) => r.action === "cancel");
    assert.equal(mutations.length, 1);
    assert.equal(mutations[0].argument, second);
    assert.equal(mutations[0].event_id, "$second");
});

test("queued federation replies describe the target and only persisted results confirm delivery", async () => {
    const x = setup(),
        id = "MB-" + "e".repeat(32);
    const control = x.bot.control;
    let completed = false;
    x.client.getJoinedRooms = async () => ["!room"];
    x.bot.control = async (request) => {
        if (request.action === "invite")
            return {
                status: "queued",
                operation_id: "FO-example",
                next_action: "technical details"
            };
        if (request.action === "notifications")
            return {
                operations: completed
                    ? [
                          {
                              id: "FO-example",
                              state: "complete",
                              action: "invite",
                              argument: "peer.example",
                              actor: x.event.sender,
                              result: {
                                  id,
                                  server: "peer.example",
                                  state: "pending",
                                  delivery_confirmed: true
                              }
                          }
                      ]
                    : []
            };
        return control(request);
    };
    const send = (body, event_id) =>
        x.bot.message("!room", {
            ...x.event,
            event_id,
            content: { msgtype: "m.text", body }
        });
    await send("пригласить сервер peer.example", "$invite");
    await send("выполнить подтверждаю", "$confirm");
    assert.match(x.sent.at(-1), /peer.example[\s\S]*Ожидает выполнения/);
    assert.doesNotMatch(x.sent.at(-1), /queued|FO-|technical details|Выполнено|получение/i);
    const sent = x.sent.length;
    await x.bot.notifications();
    assert.equal(x.sent.length, sent);
    completed = true;
    await x.bot.notifications();
    assert.match(x.sent.at(-1), /заявка №1[\s\S]*Получение заявки другим сервером подтверждено/);
    assert.doesNotMatch(x.sent.at(-1), /FO-|MB-|queued/);
    const unknownDelivery = x.bot.operationText(
        { action: "invite", argument: "peer.example" },
        {
            id,
            status: "complete",
            state: "pending",
            delivery_pending: true
        }
    );
    assert.match(unknownDelivery, /Доставка приглашения пока не подтверждена/);
    assert.doesNotMatch(unknownDelivery, /Выполнено|Получение заявки/);
});

test("all administrators receive actionable requests and decisions despite a failed private room", async () => {
    const x = setup(),
        first = "MB-" + "1".repeat(32),
        second = "MB-" + "2".repeat(32);
    x.cfg.administrator_mxids.push("@second:example.org");
    const originalState = x.client.getRoomState;
    x.client.getRoomState = async (room) =>
        (await originalState()).map((item) =>
            room === "!second" && item.state_key === x.event.sender
                ? { ...item, state_key: "@second:example.org" }
                : item
        );
    x.client.getJoinedRooms = async () => ["!first", "!second"];
    let failing = true;
    const sent = [];
    x.client.sendMessage = async (room, content) => {
        if (room === "!first" && failing) throw new Error("room unavailable");
        sent.push({ room, body: content.body });
    };
    let requests = [{ id: first, server: "peer.example", direction: "incoming", state: "pending" }];
    x.bot.control = async (request) =>
        request.action === "authorize" ? { authorized: true } : { requests };
    await x.bot.notifications();
    assert.deepEqual(
        sent.map((x) => x.room),
        ["!second"]
    );
    assert.match(sent[0].body, /подтвердить 1[\s\S]*отклонить 1/);
    failing = false;
    await x.bot.notifications();
    assert.deepEqual(
        sent.map((x) => x.room),
        ["!second", "!first"]
    );
    requests[0].state = "rejected";
    await x.bot.notifications();
    assert.deepEqual(
        sent.slice(-2).map((x) => x.room),
        ["!first", "!second"]
    );
    assert.ok(sent.slice(-2).every((x) => x.body.includes("отклонено")));
    requests.push({ ...requests[0], id: second, state: "pending" });
    await x.bot.notifications();
    assert.ok(sent.slice(-2).every((x) => x.body.includes("подтвердить 2")));
    requests[1].state = "connected";
    await x.bot.notifications();
    assert.ok(sent.slice(-2).every((x) => x.body.includes("политика применена на обоих серверах")));
    const count = sent.length;
    const restart = new FederationBot(x.client, x.bot.control, x.bot.store, x.cfg);
    await restart.notifications();
    assert.equal(sent.length, count);
    requests = [{ ...requests[1], id: "MB-" + "3".repeat(32) }];
    await restart.notifications();
    assert.ok(sent.slice(-2).every((x) => /Получена заявка №3[\s\S]*одобрено/.test(x.body)));
    assert.ok(sent.slice(-2).every((x) => !/подтвердить |отклонить /.test(x.body)));
});

test("request dates use Moscow and removed cleanup commands never dispatch", () => {
    const body = requestsText({
        requests: [
            {
                id: "MB-" + "1".repeat(32),
                server: "peer.example",
                state: "pending",
                direction: "incoming",
                expires_at: Date.parse("2026-10-13T22:30:00Z") / 1000
            }
        ]
    });
    assert.match(body, /14 октября 2026[\s\S]*01:30 МСК/);
    assert.doesNotMatch(body, /UTC/);
    for (const command of [
        "очистить отклонённые",
        "очистить отклоненные",
        "очистить MB-" + "1".repeat(32)
    ])
        assert.equal(parse(command), null);
});

test("server status presents measured CPU RAM disk swap and does not invent unavailable values", () => {
    const body = serverText({
        ok: true,
        resources: {
            cpu_used_percent: 12.3,
            cpu_count: 2,
            cpu_load: 0.4,
            memory_total_bytes: 2048 * 1048576,
            memory_used_bytes: 512 * 1048576,
            disk_total_bytes: 20 * 1073741824,
            disk_used_bytes: 5 * 1073741824,
            disk_free_bytes: 15 * 1073741824,
            swap_total_bytes: 1024 * 1048576,
            swap_used_bytes: 0,
            uptime_seconds: 7200
        }
    });
    assert.match(body, /CPU: 12.3%/);
    assert.match(body, /RAM: 512 \/ 2048 МиБ/);
    assert.match(body, /Диск: 5.0 \/ 20.0 ГиБ/);
    assert.match(body, /Swap: 0 \/ 1024 МиБ/);
    assert.match(body, /Свободное место: 15.0 ГиБ/);
    assert.doesNotMatch(
        serverText({ ok: true, resources: {} }),
        /CPU:|RAM:|Диск:|Swap:|NaN|undefined/
    );
});

test("notification pages preserve their cursor across restart and retry failed RPC without skipping", async () => {
    const x = setup();
    const first = "MB-" + "1".repeat(32),
        second = "MB-" + "2".repeat(32);
    x.client.getJoinedRooms = async () => ["!room"];
    let fail = false;
    const cursors = [];
    x.bot.control = async (request) => {
        if (request.action === "authorize") return { authorized: true };
        const cursor = request.argument?.cursor;
        cursors.push(cursor || null);
        if (fail) throw new Error("RPC unavailable");
        return {
            requests: [
                {
                    id: cursor ? second : first,
                    server: "peer.example",
                    state: "pending",
                    direction: "incoming"
                }
            ],
            next_cursor: cursor ? null : "request:" + first
        };
    };
    assert.equal(await x.bot.notifications(), true);
    assert.match(x.sent.at(-1), /подтвердить 1/);
    const restart = new FederationBot(x.client, x.bot.control, x.bot.store, x.cfg);
    fail = true;
    await assert.rejects(restart.notifications());
    fail = false;
    assert.equal(await restart.notifications(), false);
    assert.match(x.sent.at(-1), /подтвердить 2/);
    assert.deepEqual(cursors, [null, "request:" + first, "request:" + first]);
    await restart.notifications();
    assert.equal(x.sent.length, 2);
});

test("a failed notification delivery is retried on the next scan without blocking later pages", async () => {
    const x = setup();
    const first = "MB-" + "1".repeat(32),
        second = "MB-" + "2".repeat(32);
    let fail = true;
    x.client.getJoinedRooms = async () => ["!room"];
    const send = x.client.sendMessage;
    x.client.sendMessage = async (room, content) => {
        if (fail && content.body.includes("first.example"))
            throw new Error("temporary send failure");
        return send(room, content);
    };
    x.bot.control = async (request) => {
        if (request.action === "authorize") return { authorized: true };
        const cursor = request.argument?.cursor;
        return {
            requests: [
                {
                    id: cursor ? second : first,
                    server: cursor ? "second.example" : "first.example",
                    state: "pending",
                    direction: "incoming"
                }
            ],
            next_cursor: cursor ? null : "request:" + first
        };
    };
    assert.equal(await x.bot.notifications(), true);
    assert.equal(await x.bot.notifications(), false);
    assert.equal(x.sent.length, 1);
    assert.match(x.sent[0], /second.example/);
    fail = false;
    await x.bot.notifications();
    await x.bot.notifications();
    assert.equal(x.sent.length, 2);
    assert.match(x.sent[1], /first.example/);
});

test("a late signed receipt notifies after an initial timeout and survives restart without duplicates", async () => {
    const x = setup(),
        id = "MB-" + "a".repeat(32);
    const request = {
        id,
        server: "peer.example",
        direction: "outgoing",
        state: "pending",
        delivery_pending: true
    };
    const operation = {
        id: "FO-late",
        action: "invite",
        argument: "peer.example",
        actor: x.event.sender,
        state: "complete",
        result: { ...request }
    };
    x.client.getJoinedRooms = async () => ["!room"];
    x.bot.control = async (r) =>
        r.action === "authorize"
            ? { authorized: true }
            : { requests: [request], operations: [operation] };
    await x.bot.notifications();
    assert.equal(x.sent.length, 1);
    assert.match(x.sent[0], /Доставка приглашения пока не подтверждена/);
    request.delivery_pending = false;
    request.delivery_confirmed = true;
    await x.bot.notifications();
    assert.equal(x.sent.length, 2);
    assert.match(
        x.sent[1],
        /Заявка №1: peer.example[\s\S]*Получение заявки другим сервером подтверждено/
    );
    assert.match(x.sent[1], /статус 1[\s\S]*отменить 1/);
    const restart = new FederationBot(x.client, x.bot.control, x.bot.store, x.cfg);
    await restart.notifications();
    assert.equal(x.sent.length, 2);
});

test("request receipts and successful invite jobs share a recipient receipt across notification pages", async () => {
    for (const first of ["request", "operation"]) {
        const x = setup(),
            id = "MB-" + "b".repeat(32);
        const request = {
            id,
            server: "peer.example",
            direction: "outgoing",
            state: "pending",
            delivery_confirmed: true
        };
        const operation = {
            id: "FO-ack",
            action: "invite",
            argument: "peer.example",
            actor: x.event.sender,
            state: "complete",
            result: { ...request }
        };
        x.client.getJoinedRooms = async () => ["!room"];
        let page = first;
        x.bot.control = async (r) =>
            r.action === "authorize"
                ? { authorized: true }
                : {
                      requests: page === "request" ? [request] : [],
                      operations: page === "operation" ? [operation] : []
                  };
        await x.bot.notifications();
        page = first === "request" ? "operation" : "request";
        await x.bot.notifications();
        assert.equal(x.sent.length, 1, first);
        assert.match(x.sent[0], /Получение заявки другим сервером подтверждено/);
    }
});

test("recipient-less legacy markers replay once to each admin regardless of room order", async () => {
    const x = setup(),
        id = "MB-" + "d".repeat(32);
    x.cfg.administrator_mxids.push("@second:example.org");
    const originalState = x.client.getRoomState;
    x.client.getRoomState = async (room) =>
        (await originalState()).map((item) =>
            room === "!second" && item.state_key === x.event.sender
                ? { ...item, state_key: "@second:example.org" }
                : item
        );
    x.client.getJoinedRooms = async () => ["!second", "!first"];
    x.bot.store.setValue("notified:" + id + ":rejected", true);
    x.bot.store.setValue("operation-notified:FO-legacy:complete", true);
    const sent = [];
    x.client.sendMessage = async (room, content) => sent.push({ room, body: content.body });
    x.bot.control = async (request) =>
        request.action === "authorize"
            ? { authorized: true }
            : {
                  requests: [
                      { id, server: "peer.example", direction: "incoming", state: "rejected" }
                  ],
                  operations: [
                      {
                          id: "FO-legacy",
                          state: "complete",
                          result: { status: "changed", enabled: true }
                      }
                  ]
              };
    await x.bot.notifications();
    assert.deepEqual(
        sent.map((item) => item.room),
        ["!second", "!second", "!first", "!first"]
    );
    assert.equal(sent.filter((item) => item.body.includes("отклонено")).length, 2);
    assert.equal(sent.filter((item) => item.body.includes("API Misbot: включён")).length, 2);
    x.client.getJoinedRooms = async () => ["!first", "!second"];
    const restart = new FederationBot(x.client, x.bot.control, x.bot.store, x.cfg);
    await restart.notifications();
    assert.equal(sent.length, 4);
});

test("a recovered delivery receipt supersedes only the old delivery failure across pages and restart", async () => {
    for (const order of ["same-page", "receipt-first", "operation-first"]) {
        const x = setup(),
            id = "MB-" + "e".repeat(32);
        const request = {
            id,
            server: "peer.example",
            direction: "outgoing",
            state: "pending",
            delivery_confirmed: true
        };
        const operation = {
            id: "FO-timeout",
            action: "invite",
            argument: "peer.example",
            actor: x.event.sender,
            state: "failed",
            result: {
                ...request,
                delivery_confirmed: false,
                delivery_pending: true,
                code: "timeout",
                safe_error: "timeout"
            }
        };
        x.client.getJoinedRooms = async () => ["!room"];
        let page = order;
        x.bot.control = async (r) =>
            r.action === "authorize"
                ? { authorized: true }
                : {
                      requests: page !== "operation-first" ? [request] : [],
                      operations: page !== "receipt-first" ? [operation] : []
                  };
        await x.bot.notifications();
        if (order !== "same-page") {
            page = order === "receipt-first" ? "operation-first" : "receipt-first";
            await x.bot.notifications();
        }
        assert.equal(x.sent.length, order === "operation-first" ? 2 : 1, order);
        assert.match(x.sent.at(-1), /Получение заявки другим сервером подтверждено/, order);
        if (order === "operation-first") assert.match(x.sent[0], /завершение не подтверждено/);
        else assert.doesNotMatch(x.sent.join("\n"), /завершение не подтверждено/);
        const count = x.sent.length;
        page = "same-page";
        const restart = new FederationBot(x.client, x.bot.control, x.bot.store, x.cfg);
        await restart.notifications();
        assert.equal(x.sent.length, count, order);
    }
});

test("a receipt never suppresses a later policy or confirmed-delivery operation failure", async () => {
    for (const result of [
        { state: "applying", delivery_confirmed: false, delivery_pending: true },
        { state: "partially_applied", delivery_confirmed: false, delivery_pending: true },
        { state: "pending", delivery_confirmed: true, delivery_pending: true },
        { state: "pending", delivery_pending: true }
    ]) {
        const x = setup(),
            id = "MB-" + "f".repeat(32);
        x.client.getJoinedRooms = async () => ["!room"];
        x.bot.store.setValue("notified:" + id + ":receipt:" + x.event.sender, true);
        x.bot.control = async (r) =>
            r.action === "authorize"
                ? { authorized: true }
                : {
                      operations: [
                          {
                              id: "FO-policy-failure",
                              action: "invite",
                              argument: "peer.example",
                              actor: x.event.sender,
                              state: "failed",
                              result: {
                                  id,
                                  server: "peer.example",
                                  direction: "outgoing",
                                  code: "operation_unconfirmed",
                                  ...result
                              }
                          }
                      ]
                  };
        await x.bot.notifications();
        assert.equal(x.sent.length, 1);
        assert.match(x.sent[0], /завершение не подтверждено/);
    }
});

test("remote decisions supersede a saved initial delivery timeout, but local terminal states do not", async () => {
    for (const state of ["connected", "rejected", "cancelled", "expired"]) {
        for (const samePage of [true, false]) {
            const x = setup(),
                id = "MB-" + "c".repeat(32);
            const request = {
                id,
                server: "peer.example",
                direction: "outgoing",
                state,
                local_applied: state === "connected",
                remote_applied: state === "connected"
            };
            const operation = {
                id: "FO-old-timeout",
                action: "invite",
                argument: "peer.example",
                actor: x.event.sender,
                state: "failed",
                result: {
                    ...request,
                    state: "pending",
                    delivery_confirmed: false,
                    delivery_pending: true,
                    code: "timeout",
                    safe_error: "timeout"
                }
            };
            x.client.getJoinedRooms = async () => ["!room"];
            let showOperation = samePage;
            x.bot.control = async (r) =>
                r.action === "authorize"
                    ? { authorized: true }
                    : {
                          requests: [request],
                          operations: showOperation ? [operation] : []
                      };
            await x.bot.notifications();
            showOperation = true;
            await x.bot.notifications();
            const remoteProof = ["connected", "rejected"].includes(state);
            assert.equal(x.sent.length, remoteProof ? 1 : 2);
            if (remoteProof) {
                assert.match(
                    x.sent[0],
                    state === "connected" ? /политика применена на обоих серверах/ : /отклонено/
                );
                assert.doesNotMatch(x.sent[0], /завершение не подтверждено/);
            } else assert.match(x.sent[1], /завершение не подтверждено/);
            const count = x.sent.length;
            const restart = new FederationBot(x.client, x.bot.control, x.bot.store, x.cfg);
            await restart.notifications();
            assert.equal(x.sent.length, count);
        }
    }
});

test("request pages keep permanent numbers and never send later pages to CLI", async () => {
    const x = setup();
    const requests = Array.from({ length: 43 }, (_, index) => ({
        id: "MB-" + index.toString(16).padStart(32, "0"),
        server: `server${index}.example.org`,
        direction: "incoming",
        state: "pending"
    }));
    const control = x.bot.control;
    x.bot.control = async (request) =>
        request.action === "requests" ? { requests } : control(request);
    const send = (body, event_id) =>
        x.bot.message("!room", { ...x.event, event_id, content: { msgtype: "m.text", body } });
    await send("заявки входящие", "$page1");
    assert.match(x.sent.at(-1), /заявки входящие 2/);
    assert.doesNotMatch(x.sent.at(-1), /server20|CLI/);
    await send("заявки входящие 2", "$page2");
    assert.match(x.sent.at(-1), /21\. server20/);
    assert.match(x.sent.at(-1), /подтвердить 21/);
    assert.match(x.sent.at(-1), /заявки входящие 3/);
    // Recreate the bot with persisted storage, then change a filter and order.
    x.bot = new FederationBot(x.client, x.bot.control, x.bot.store, x.cfg);
    requests.reverse();
    await send("заявки входящие 1", "$afterRestart");
    assert.match(x.sent.at(-1), /31\. server30/);
    assert.doesNotMatch(x.sent.at(-1), /CLI/);
    await send("заявки входящие 99", "$outside");
    assert.match(x.sent.at(-1), /Такой страницы нет/);
});

test("service tasks have actor-bound stable numbers, list and legacy ID lookup", async () => {
    const x = setup();
    const control = x.bot.control;
    x.bot.control = async (request) => {
        if (request.action === "panel-server-operation")
            return { status: "queued", operation_id: "SO-saved", operation: "backup" };
        if (request.action === "panel-server-result") {
            x.calls.push(request);
            return {
                status: "complete",
                operation_id: request.argument,
                operation: "backup",
                result: { ok: true }
            };
        }
        return control(request);
    };
    const send = (body, event_id) =>
        x.bot.message("!room", { ...x.event, event_id, content: { msgtype: "m.text", body } });
    await send("создать копию", "$backup");
    await send("выполнить подтверждаю", "$apply");
    assert.match(x.sent.at(-1), /задача 1/);
    x.bot = new FederationBot(x.client, x.bot.control, x.bot.store, x.cfg);
    await send("задачи", "$tasks");
    assert.match(x.sent.at(-1), /Создание копии.*задача 1/);
    assert.deepEqual(x.bot.serviceTasks("@another:example.org"), []);
    await send("задача 1", "$short");
    assert.equal(
        x.calls.filter((r) => r.action === "panel-server-result").at(-1).argument,
        "SO-saved"
    );
    await send("задача SO-legacy", "$legacy");
    await send("задачи", "$tasks2");
    assert.match(x.sent.at(-1), /задача 2/);
    const before = x.calls.filter((r) => r.action === "panel-server-result").length;
    await send("задача 999", "$unknown");
    assert.match(x.sent.at(-1), /Такого номера задачи нет/);
    assert.equal(x.calls.filter((r) => r.action === "panel-server-result").length, before);
});

test("full service task index preserves accepted operations and legacy result lookup", async () => {
    const x = setup();
    const tasks = Array.from({ length: 1000 }, (_, index) => ({
        id: `SO-history-${index}`,
        operation: "backup"
    }));
    x.bot.store.setValue("service-tasks:" + x.event.sender, tasks);
    const control = x.bot.control;
    x.bot.control = async (request) => {
        if (request.action === "panel-server-operation") {
            x.calls.push(request);
            return { status: "queued", operation_id: "SO-overflow", operation: "backup" };
        }
        if (request.action === "panel-server-result") {
            x.calls.push(request);
            return {
                status: "complete",
                operation_id: request.argument,
                operation: "backup",
                result: { ok: true }
            };
        }
        return control(request);
    };
    const send = (body, event_id) =>
        x.bot.message("!room", { ...x.event, event_id, content: { msgtype: "m.text", body } });
    await send("создать копию", "$new");
    await send("выполнить подтверждаю", "$confirm");
    assert.match(x.sent.at(-1), /задача SO-overflow/);
    assert.match(x.sent.at(-1), /принято/);
    assert.doesNotMatch(x.sent.at(-1), /не подтверждён|null/);
    await send("выполнить подтверждаю", "$repeat");
    assert.match(x.sent.at(-1), /Действующего предложения нет/);
    assert.equal(x.calls.filter((r) => r.action === "panel-server-operation").length, 1);
    await send("задача SO-overflow", "$result");
    assert.match(x.sent.at(-1), /готово/);
    assert.match(x.sent.at(-1), /задача SO-overflow/);
    await send("задача 1", "$previous");
    assert.equal(
        x.calls.filter((r) => r.action === "panel-server-result").at(-1).argument,
        "SO-history-0"
    );
    assert.deepEqual(x.bot.serviceTasks(x.event.sender), tasks);
    await send("задачи", "$list");
    assert.match(x.sent.at(-1), /Список заполнен/);
});
