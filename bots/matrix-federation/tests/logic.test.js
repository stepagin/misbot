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
    errorText
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
        sendNotice: async (r, b) => sent.push(b),
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
    assert.equal(x.sent[0], HELP);
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
    assert.match(FEDERATION, /взаимное подключение/);
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
            assert.deepEqual(x.sent, [HELP], body);
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
        assert.ok(body.startsWith(`Операция: ${meaning} (${state}).\n`));
        assert.match(body, /FO-fictional/);
        assert.match(body, /misbot federation operations/);
        assert.deepEqual(result, before);
        assert.equal(resultText(result), body);
    }
    assert.equal(stateText("connected"), "политика применена на обоих серверах (connected)");
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
    assert.deepEqual(JSON.parse(body.slice(body.indexOf("\n") + 1)), {
        servers: result.servers,
        note: result.note,
        nested: { code: "busy" }
    });
    assert.doesNotMatch(body, /FICTIONAL_SECRET/);
    assert.equal(
        resultText({ status: "unchanged" }),
        "Операция: изменения не требуются (unchanged)."
    );
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
    assert.match(x.sent[0], /ожидает решения \(pending\)/);
    assert.ok(x.sent[0].includes("подтвердить " + id));
    assert.ok(x.sent[0].includes("отклонить " + id));
    assert.match(x.sent[1], /прервано \(interrupted\)/);
    assert.match(x.sent[1], /sudo misbot federation recover/);
    assert.doesNotMatch(x.sent[1], /FICTIONAL_SECRET/);
    const restart = new FederationBot(x.client, x.bot.control, x.bot.store, x.cfg);
    await restart.notifications();
    assert.equal(x.sent.length, 2);
    snapshot.requests[0].state = "connected";
    await restart.notifications();
    assert.equal(x.sent.length, 3);
    assert.match(x.sent[2], /политика применена на обоих серверах \(connected\)/);
    assert.doesNotMatch(x.sent[2], /комнаты проверены/);
    assert.doesNotMatch(x.sent[2], /подтвердить |отклонить /);
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
        assert.equal(x.sent.at(-1), expected);
    }
    assert.match(HELP, /статистика[\s\S]*состояние[\s\S]*проверить[\s\S]*федерация/);
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
    assert.ok(x.sent.some((s) => s.includes("задача SO-fictional")));
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
                            id: "MB-in",
                            server: "incoming.example",
                            direction: "incoming",
                            state: "pending"
                        },
                        {
                            id: "MB-out",
                            server: "outgoing.example",
                            direction: "outgoing",
                            state: "pending"
                        },
                        {
                            id: "MB-no",
                            server: "rejected.example",
                            direction: "outgoing",
                            state: "rejected"
                        },
                        {
                            id: "MB-hidden",
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
    assert.match(x.sent.at(-1), /matrix_https: проверено/);
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
    assert.match(errorText("FICTIONAL_SECRET"), /задача ID/);
});

test("statistics keep unavailable activity unknown while rendering actual host resources", () => {
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
    assert.match(body, /Память: 512 \/ 1024 МиБ/);
    assert.match(body, /Свободное место: 3.0 ГиБ/);
    assert.match(body, /Средняя нагрузка за минуту: 0.12/);
    assert.match(body, /Сервер работает: 2 ч/);
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
