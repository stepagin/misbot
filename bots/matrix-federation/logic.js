"use strict";

const { createHash } = require("node:crypto");

const HELP = `Панель администратора Misbot

статистика — выбрать период
состояние — службы и ресурсы сервера
проверить — проверить работоспособность
федерация — серверы, заявки и API

обслуживание — перезапуск, обновления и резервные копии
ещё — приглашения, сообщения и настройки
меню — вернуться к этому экрану`;
const MORE = `Дополнительные действия

приглашения — открыть реферального бота
рассылка — сообщение пользователям через серверного помощника
приветствие — текст для новых пользователей
уведомления — настройки уведомлений
настройки федерации — ручная политика и восстановление

Назад: меню`;
const MAINTENANCE = `Обслуживание сервера

перезапустить сервер — перезапуск служб
запустить службы / остановить службы
обновления — версия и предложение обновления
копии — создание и проверка резервных копий
хранилище — состояние медиа и копий
операции — состояние задач федерации
задача ID — состояние задачи обслуживания

Изменения требуют подтверждения. Отмена предложения не прерывает запущенную задачу.
Назад: меню`;
const FEDERATION = `Федерация: взаимное подключение серверов

проверить сервер example.org
пригласить сервер example.org
заявки — входящие, исходящие и отклонённые
серверы — разрешённые серверы
api — состояние межсерверного API Misbot

Настройки: настройки федерации
Назад: меню`;
const FEDERATION_SETTINGS = `Настройки федерации

api включить / api выключить
добавить сервер example.org / удалить сервер example.org — местная политика
операции / восстановить / откатить

Изменения требуют подтверждения. Выключение API сохраняет Matrix-политику.
Назад: федерация`;
const REQUESTS = `Заявки федерации

заявки входящие / заявки исходящие / заявки отклонённые
подтвердить MB-0123456789abcdef0123456789abcdef
отклонить MB-… / отменить MB-… / повторить MB-… / статус MB-…
очистить отклонённые — убрать завершённые заявки из списка

Назад: федерация`;
const COPIES = `Резервные копии

создать копию — создать резервную копию
проверить копию — проверить восстановление в изолированной среде
хранилище — состояние хранилища и последней проверки

Назад: обслуживание`;
const WRITES = new Set([
    "add",
    "remove",
    "invite",
    "approve",
    "reject",
    "cancel",
    "retry",
    "recover",
    "rollback",
    "api-enable",
    "api-disable",
    "clear-rejected"
]);

function parse(text) {
    const single = {
        "серверы": "servers",
        "заявки": "requests",
        "операции": "operations",
        "восстановить": "recover",
        "откатить": "rollback",
        "api": "api-status",
        "api включить": "api-enable",
        "api выключить": "api-disable",
        "очистить отклонённые": "clear-rejected",
        "очистить отклоненные": "clear-rejected"
    };
    const command = text.toLowerCase();
    if (Object.hasOwn(single, command)) return { action: single[command] };
    // Fold command words only. Request IDs are opaque.
    let m = text.match(/^(проверить|добавить|удалить|пригласить) сервер ([^\s]+)$/i);
    if (m)
        return {
            action: {
                "проверить": "check",
                "добавить": "add",
                "удалить": "remove",
                "пригласить": "invite"
            }[m[1].toLowerCase()],
            argument: m[2]
        };
    const requests = {
        "подтвердить": "approve",
        "отклонить": "reject",
        "отменить": "cancel",
        "повторить": "retry",
        "статус": "request-status",
        "очистить": "clear-rejected"
    };
    m = text.match(/^([^\s]+) (MB-[0-9a-f]{32})$/);
    if (m && Object.hasOwn(requests, m[1].toLowerCase()))
        return { action: requests[m[1].toLowerCase()], argument: m[2] };
    return null;
}

function statisticsText(result) {
    const period =
        result.start_utc && result.end_utc
            ? `${result.start_utc} — ${result.end_utc}`
            : "данные периода недоступны";
    return `Сообщений: ${result.messages ?? "недоступно"}\nРегистраций: ${result.registrations ?? "недоступно"}\nЧисло звонков: недоступно\nСредняя длительность звонка: недоступно\nПериод: ${period}\n${result.note || ""}`.trimEnd();
}
function versionText(result) {
    return `Установленная версия: Misbot ${result.installed}\nПоследняя стабильная версия: ${result.latest || "релиз ещё не опубликован"}\n${result.update_available ? "Доступно обновление." : "Более новая стабильная версия не найдена."}\n${result.notes || ""}\n${result.url || ""}\nКоманда: ${result.command}`;
}

function stateText(state) {
    if (typeof state !== "string" || !state) throw new Error("Invalid operation state");
    const labels = {
        queued: "принято",
        running: "выполняется",
        complete: "готово",
        failed: "завершение не подтверждено",
        interrupted: "прервано",
        applied: "изменения применены",
        unchanged: "изменения не требуются",
        already_processed: "уже обработано",
        rolled_back: "прежние настройки восстановлены",
        pending: "ожидает решения",
        accepted: "одобрено",
        applying: "настройки применяются",
        partially_applied: "настройки применены частично",
        connected: "политика применена на обоих серверах",
        rejected: "отклонено",
        expired: "срок истёк",
        cancelled: "отменено"
    };
    return Object.hasOwn(labels, state) ? `${labels[state]} (${state})` : state;
}

function resultText(result) {
    // One required result object; status is optional, other backend fields stay literal.
    if (!result || typeof result !== "object" || Array.isArray(result))
        throw new Error("Invalid operation result");
    const details = { ...result };
    const heading = Object.hasOwn(details, "status")
        ? `Операция: ${stateText(details.status)}.`
        : "Результат запроса:";
    delete details.status;
    const body = JSON.stringify(
        details,
        (key, value) => (/backup|migration|move|checkpoint/i.test(key) ? undefined : value),
        2
    );
    return Object.keys(details).length ? `${heading}\n${body}` : heading;
}

function serverText(result) {
    const lines = [result.ok === true ? "Проверка завершена." : "Результат требует проверки."];
    if (result.checked_at)
        lines.push(
            `Время проверки: ${typeof result.checked_at === "number" ? new Date(result.checked_at * 1000).toISOString() : result.checked_at}`
        );
    if (result.statistics) lines.push(statisticsText(result.statistics));
    for (const item of result.containers || [])
        lines.push(`${item.name}: ${item.running && item.healthy ? "работает" : "нужна проверка"}`);
    for (const item of result.checks || [])
        lines.push(`${item.name}: ${item.passed ? "проверено" : "нужна проверка"}`);
    if (result.resources || result.statistics) {
        const r = result.resources || result.statistics;
        if (r.cpu_count != null) lines.push(`Процессоров: ${r.cpu_count}`);
        if (r.cpu_load != null) lines.push(`Средняя нагрузка за минуту: ${r.cpu_load}`);
        if (r.load_1m != null) lines.push(`Средняя нагрузка за минуту: ${r.load_1m}`);
        if (r.memory_used_bytes != null && r.memory_total_bytes != null)
            lines.push(
                `Память: ${Math.round(r.memory_used_bytes / 1048576)} / ${Math.round(r.memory_total_bytes / 1048576)} МиБ`
            );
        if (r.memory_available_bytes != null)
            lines.push(`Доступно памяти: ${Math.round(r.memory_available_bytes / 1048576)} МиБ`);
        if (r.disk_free_bytes != null)
            lines.push(`Свободное место: ${(r.disk_free_bytes / 1073741824).toFixed(1)} ГиБ`);
        if (r.uptime_seconds != null)
            lines.push(`Сервер работает: ${Math.floor(r.uptime_seconds / 3600)} ч.`);
    }
    if (result.storage) {
        const s = result.storage;
        lines.push(`Внешнее хранилище медиа: ${s.media_s3 ? "подключено" : "не подключено"}`);
        lines.push(`Внешнее хранилище копий: ${s.backup_s3 ? "подключено" : "не подключено"}`);
        lines.push(
            `Последняя копия: ${s.last_backup_verified ? "проверена" : "проверка не подтверждена"}`
        );
        if (s.backup_pending || s.media_pending)
            lines.push("Есть незавершённая операция хранилища.");
    }
    if (result.backup_pending) lines.push("Есть незавершённая операция резервного копирования.");
    if (result.recovery_required)
        lines.push("Проверьте сохранённое состояние через SSH перед повтором.");
    if (result.code) lines.push(`Код: ${result.code}`);
    if (result.next_action) lines.push(`Следующее действие: ${result.next_action}`);
    if (
        !result.containers &&
        !result.checks &&
        !result.resources &&
        !result.statistics &&
        !result.storage
    )
        lines.push(resultText(result));
    return lines.join("\n");
}

function requestsText(result, category) {
    const terminal = new Set(["rejected", "expired"]);
    const items = (result.requests || []).filter((item) => {
        if (item.hidden || item.cleared || ["cancelled", "connected"].includes(item.state))
            return false;
        if (category === "rejected") return terminal.has(item.state);
        if (terminal.has(item.state)) return false;
        return !category || item.direction === category;
    });
    const labels = { incoming: "Входящие", outgoing: "Исходящие", rejected: "Отклонённые" };
    const lines = [`${labels[category] || "Актуальные"} заявки: ${items.length}.`];
    for (const item of items.slice(0, 20)) {
        lines.push(`\n${item.server}: ${stateText(item.state)}.\n${item.id}`);
        if (item.expires_at)
            lines.push(
                `Срок: ${new Date(item.expires_at * 1000).toISOString().replace("T", " ").replace(".000Z", " UTC")}`
            );
        if (item.state === "pending" && item.direction === "incoming")
            lines.push(`Принять: подтвердить ${item.id}\nОтклонить: отклонить ${item.id}`);
        if (item.state === "pending" && item.direction === "outgoing")
            lines.push(`Отозвать: отменить ${item.id}`);
        if (item.delivery_pending) lines.push("Доставка решения пока не подтверждена.");
    }
    if (items.length > 20) lines.push("Показаны первые 20. Полный список доступен через CLI.");
    return lines.join("\n");
}

function confirmationText(action, argument) {
    const labels = {
        add: "Добавить сервер в местную политику",
        remove: "Удалить сервер из местной политики",
        invite: "Отправить приглашение серверу",
        approve: "Принять заявку",
        reject: "Отклонить заявку",
        cancel: "Отозвать приглашение",
        retry: "Продолжить согласование заявки",
        recover: "Продолжить незавершённое изменение федерации",
        rollback: "Восстановить прежнюю местную политику",
        "api-enable": "Включить межсерверный API Misbot",
        "api-disable": "Выключить межсерверный API Misbot",
        "clear-rejected": "Очистить отклонённые заявки",
        start: "Запустить службы",
        stop: "Остановить службы",
        restart: "Перезапустить службы",
        backup: "Создать резервную копию",
        backup_verify: "Проверить восстановление резервной копии"
    };
    const lines = [labels[action] + (argument ? `: ${argument}` : ".")];
    if (
        ["add", "remove", "approve", "recover", "rollback", "stop", "restart", "backup"].includes(
            action
        )
    )
        lines.push("Возможен временный перерыв в работе сервера и бота.");
    if (action === "stop")
        lines.push(
            "При остановке Matrix или MAS управление через этот чат станет недоступным. Для запуска потребуется SSH."
        );
    if (action === "api-disable")
        lines.push(
            "Существующая Matrix-политика сохраняется. Новые межсерверные обмены Misbot будут недоступны до включения API."
        );
    if (action === "clear-rejected")
        lines.push(
            "Очистка убирает карточки из местного списка и сохраняет сведения для безопасных повторов."
        );
    lines.push(
        "Подтверждение: выполнить подтверждаю. Отмена: отмена. Предложение действует 5 минут."
    );
    return lines.join("\n");
}

function errorText(code) {
    const messages = {
        api_disabled: "API Misbot выключен. Откройте «федерация», чтобы проверить настройки.",
        invitation_rate_limit:
            "Новое приглашение можно создать не чаще одного раза в минуту. Повторите позже.",
        peer_already_connected:
            "Сервер уже добавлен. Заявка больше не требуется; откройте «серверы».",
        pending_request_exists: "Для этого сервера уже есть актуальная заявка. Откройте «заявки».",
        peer_upgrade_required:
            "Удалённому серверу нужно обновить Misbot для приглашений сроком 30 дней.",
        request_not_found: "Заявка не найдена. Откройте актуальный список «заявки».",
        invalid_transition: "Это действие уже недоступно. Проверьте актуальное состояние заявки.",
        request_in_progress:
            "Настройки уже применяются. Проверьте состояние операции перед следующим действием.",
        peer_identity_changed:
            "Identity удалённого сервера изменилась. Проверьте её через SSH; прежнее доверие не перенесено.",
        self_invitation: "Нужно указать другой сервер.",
        misbot_not_found: "Другой сервер не публикует Misbot API. Приглашение не отправлено.",
        invalid_misbot_discovery:
            "Другой сервер вернул некорректные сведения Misbot API. Приглашение не отправлено.",
        invitation_unsupported:
            "Другой сервер не поддерживает приглашения Misbot. Приглашение не отправлено.",
        remote_api_disabled: "На другом сервере API Misbot выключен. Приглашение не отправлено.",
        unsupported_version:
            "Версия API другого сервера не поддерживается. Приглашение не отправлено.",
        request_capacity: "Достигнут предел сохранённых заявок. Нужна проверка состояния через SSH."
    };
    return typeof code === "string" && Object.hasOwn(messages, code)
        ? messages[code]
        : "Результат команды не подтверждён. Проверьте состояние сервера и сохранённой задачи. Команды: состояние, задача ID, операции. Для восстановления используйте SSH. Справка: помощь.";
}

class FederationBot {
    constructor(client, control, store, config) {
        Object.assign(this, { client, control, store, config });
    }
    local(sender) {
        return (
            sender ===
            (this.config.administrator_mxids?.[0] || "@matrix-admin:" + this.config.server_name)
        );
    }
    async authorize(sender) {
        if (!this.local(sender)) throw new Error("admin_unconfirmed");
        const result = await this.control({ action: "authorize", actor: sender });
        if (!result.authorized) throw new Error("admin_unconfirmed");
    }
    async privateRoom(room) {
        const state = await this.client.getRoomState(room);
        const members = state
            .filter(
                (e) =>
                    e.type === "m.room.member" && ["join", "invite"].includes(e.content?.membership)
            )
            .map((e) => e.state_key);
        return (
            members.length === 2 &&
            members.includes(this.config.user_id) &&
            members.some((x) => this.local(x)) &&
            state.some(
                (e) =>
                    e.type === "m.room.encryption" &&
                    e.content?.algorithm === "m.megolm.v1.aes-sha2"
            )
        );
    }
    async invitation(room, event) {
        if (
            event?.state_key !== this.config.user_id ||
            event.content?.membership !== "invite" ||
            event.sender === this.config.user_id
        )
            return;
        if (this.local(event.sender)) {
            await this.authorize(event.sender);
            await this.client.joinRoom(room);
            return;
        }
        const check = await this.control({
            action: "auto-invitation",
            argument: JSON.stringify({ sender: event.sender, room })
        });
        if (check.allowed) await this.client.joinRoom(room);
    }
    async call(action, argument, event) {
        return this.control({ action, argument, actor: event.sender, event_id: event.event_id });
    }
    async message(room, event) {
        if (
            !event?.event_id ||
            !this.local(event.sender) ||
            event.content?.msgtype !== "m.text" ||
            event.content?.["m.relates_to"]?.rel_type === "m.replace"
        )
            return;
        const original = String(event.content.body || "").trim(),
            text = original.toLowerCase();
        if (
            !text ||
            Buffer.byteLength(original) > 8000 ||
            this.store.getValue("processed:" + event.event_id)
        )
            return;
        try {
            await this.authorize(event.sender);
            if (!(await this.privateRoom(room))) return;
        } catch {
            return;
        }
        const reply = async (body) => {
            await this.authorize(event.sender);
            if (!(await this.privateRoom(room))) throw new Error("private_room_required");
            return this.client.sendNotice(room, body);
        };
        const key = "draft:" + room;
        let draft = this.store.getValue(key);
        if (
            draft &&
            Date.now() - draft.created >
                (["operation", "update"].includes(draft.kind) ? 300000 : 24 * 3600000)
        ) {
            this.store.setValue(key, null);
            draft = null;
        }
        // Authorization and room membership precede every draft mutation and response.
        this.store.setValue("processed:" + event.event_id, Date.now());
        if (text === "отмена" || (draft && text === "отменить")) {
            this.store.setValue(key, null);
            await reply("Действие отменено.");
            return;
        }
        const clearProposal = () => {
            if (["operation", "update"].includes(draft?.kind)) {
                this.store.setValue(key, null);
                draft = null;
            }
        };
        if (text === "выполнить подтверждаю") {
            if (
                !draft ||
                draft.kind !== "operation" ||
                draft.actor !== event.sender ||
                draft.room !== room
            ) {
                await reply("Действующего предложения нет. Выберите действие заново.");
                return;
            }
            const result = await this.call(draft.action, draft.argument, {
                ...event,
                event_id: draft.id
            });
            this.store.setValue(key, null);
            await reply(
                resultText(result) +
                    (result.operation_id && draft.action === "panel-server-operation"
                        ? `\nПроверить: задача ${result.operation_id}`
                        : "")
            );
            return;
        }
        if (draft?.kind === "broadcast") {
            if (text === "отправить" && draft.body) {
                // Keep the same event ID on retry after a lost RPC response.
                const result = await this.call("panel-broadcast", draft.body, {
                    ...event,
                    event_id: draft.id
                });
                this.store.setValue(key, null);
                await reply(`${result.message}\nПолучателей: ${result.queued}.`);
                return;
            }
            if (text === "рассылка") return reply("Пришлите текст нового сообщения или «отмена».");
            draft = { kind: "broadcast", body: original, id: event.event_id, created: Date.now() };
            this.store.setValue(key, draft);
            await reply(
                `Сообщение от администратора сервера ${this.config.server_name}:\n\n${original}`
            );
            await reply(
                "Черновик готов. Подтверждение рассылки: «отправить» или «отмена». Чтобы изменить текст, отправьте новый вариант в этот чат."
            );
            return;
        }
        if (draft?.kind === "welcome") {
            await this.call("panel-welcome", original, event);
            this.store.setValue(key, null);
            await reply(
                "Приветствие сохранено для новых пользователей. {domain} заменяется доменом сервера."
            );
            return;
        }
        if (text === "обновить подтверждаю") {
            if (draft?.kind !== "update" || draft.actor !== event.sender || draft.room !== room) {
                await reply(
                    "Действующего предложения обновления нет. Отправьте «обновления» заново."
                );
                return;
            }
            const result = await this.call("panel-update", draft.version, {
                ...event,
                event_id: draft.id
            });
            this.store.setValue(key, null);
            await reply(
                `Обновление ${result.version}: ${stateText(result.status)}.\n${result.message || ""}`.trimEnd()
            );
            return;
        }
        if (["помощь", "меню", "help", "/start"].includes(text)) {
            await reply(HELP);
            return;
        }
        if (text === "ещё" || text === "еще") {
            await reply(MORE);
            return;
        }
        if (text === "обслуживание") {
            await reply(MAINTENANCE);
            return;
        }
        if (text === "копии" || text === "резервные копии") {
            await reply(COPIES);
            return;
        }
        if (text === "статистика") {
            await reply(
                "Статистика сервера\n\nстатистика день / неделя / месяц / всего\n\nНазад: меню"
            );
            return;
        }
        if (text === "приглашения") {
            const result = await this.call("panel-referral", null, event);
            await reply(
                `Для приглашения пользователя откройте реферального бота и отправьте «пригласить».\n${result.url}\n\nНазад: ещё`
            );
            return;
        }
        if (text === "настройки федерации") {
            await reply(FEDERATION_SETTINGS);
            return;
        }
        if (text === "федерация") {
            const api = await this.call("api-status", null, event);
            await reply(`API Misbot: ${api.enabled ? "включён" : "выключен"}.\n\n${FEDERATION}`);
            return;
        }
        const reads = {
            "состояние": "status",
            "статус сервера": "status",
            "проверить": "check",
            "проверить сервер": "check",
            "хранилище": "storage"
        };
        if (Object.hasOwn(reads, text)) {
            await reply(
                serverText(await this.call("panel-server-read", { operation: reads[text] }, event))
            );
            return;
        }
        const serverWrites = {
            "запустить службы": "start",
            "остановить службы": "stop",
            "перезапустить сервер": "restart",
            "перезапустить службы": "restart",
            "создать копию": "backup",
            "проверить копию": "backup_verify"
        };
        if (Object.hasOwn(serverWrites, text)) {
            clearProposal();
            const operation = serverWrites[text];
            this.store.setValue(key, {
                kind: "operation",
                action: "panel-server-operation",
                argument: { operation },
                actor: event.sender,
                room,
                id: event.event_id,
                created: Date.now()
            });
            await reply(confirmationText(operation));
            return;
        }
        const categories = {
            "заявки входящие": "incoming",
            "заявки исходящие": "outgoing",
            "заявки отклонённые": "rejected",
            "заявки отклоненные": "rejected"
        };
        if (text === "заявки" || Object.hasOwn(categories, text)) {
            const result = await this.call("requests", null, event);
            await reply(requestsText(result, categories[text]));
            if (text === "заявки") await reply(REQUESTS);
            return;
        }
        const job = original.match(/^задача ([A-Za-z0-9_-]{1,100})$/i);
        if (job) {
            const result = await this.call("panel-server-result", job[1], event);
            await reply(
                result.result
                    ? `${resultText(result)}\n${serverText(result.result)}`
                    : resultText(result)
            );
            return;
        }
        if (text === "версия") {
            await reply(versionText(await this.call("panel-version", null, event)));
            return;
        }
        if (text === "обновить" || text === "обновления") {
            clearProposal();
            const result = await this.call("panel-version", null, event);
            await reply(versionText(result));
            if (result.update_available && result.update_supported !== false) {
                this.store.setValue(key, {
                    kind: "update",
                    version: result.latest,
                    id: event.event_id,
                    actor: event.sender,
                    room,
                    created: Date.now()
                });
                await reply(
                    "Обновление доступно. Для запуска напишите «обновить подтверждаю». Отмена: «отмена». Перед применением проверим свободное место. Сервер временно остановится. Результат сохранится при перезапуске."
                );
            }
            return;
        }
        if (text === "рассылка") {
            this.store.setValue(key, {
                kind: "broadcast",
                id: event.event_id,
                created: Date.now()
            });
            await reply(
                "Пришлите текст рассылки. Затем покажу черновик для подтверждения. Отмена: «отмена»."
            );
            return;
        }
        if (text === "приветствие") {
            const result = await this.call("panel-welcome", null, event);
            await reply(result.text);
            await reply("Чтобы изменить текст, отправьте «изменить приветствие».");
            return;
        }
        if (text === "изменить приветствие") {
            this.store.setValue(key, { kind: "welcome", created: Date.now() });
            await reply(
                "Пришлите новое приветствие. Подстановки: {server_name} — название сервера, " +
                    "{domain} — Matrix-домен, {referral_url} — ссылка на реферального бота, " +
                    "{administrator_url} — ссылка на администратора. Отмена: «отмена»."
            );
            return;
        }
        if (text === "уведомления") {
            const result = await this.call("panel-status", null, event),
                p = result.preferences;
            await reply(
                `Обновления: ${p.updates ? "включены" : "выключены"}\nРегистрации: ${p.registrations ? "включены" : "выключены"}\nЕжемесячная статистика: ${p.statistics ? "включена" : "выключена"}\nНастройка: уведомления обновления / регистрации / статистика включить / выключить.`
            );
            return;
        }
        let m = text.match(
            /^уведомления (обновления|регистрации|статистика) (включить|выключить)$/
        );
        if (m) {
            await this.call(
                "panel-preferences",
                {
                    key: {
                        обновления: "updates",
                        регистрации: "registrations",
                        статистика: "statistics"
                    }[m[1]],
                    enabled: m[2] === "включить"
                },
                event
            );
            await reply("Настройка уведомлений сохранена.");
            return;
        }
        m = text.match(/^статистика (день|неделя|месяц|всего)$/);
        if (m) {
            await reply(
                serverText(
                    await this.call(
                        "panel-server-read",
                        {
                            operation: "statistics",
                            period: { день: "day", неделя: "week", месяц: "month", всего: "all" }[
                                m[1]
                            ]
                        },
                        event
                    )
                )
            );
            return;
        }
        const request = parse(original);
        if (!request) {
            await reply(HELP);
            return;
        }
        if (WRITES.has(request.action)) {
            clearProposal();
            if (request.action === "invite") {
                const preview = await this.call("check", request.argument, {
                    ...event,
                    event_id:
                        "preview:" +
                        createHash("sha256")
                            .update("invite:" + event.event_id)
                            .digest("hex")
                });
                if (preview.invitation?.ready !== true) {
                    await reply(errorText(preview.invitation?.code || "invitation_unsupported"));
                    return;
                }
            }
            const details = request.argument?.startsWith("MB-")
                ? await this.call("request-status", request.argument, {
                      ...event,
                      event_id:
                          "preview:" + createHash("sha256").update(event.event_id).digest("hex")
                  })
                : null;
            this.store.setValue(key, {
                kind: "operation",
                ...request,
                actor: event.sender,
                room,
                id: event.event_id,
                created: Date.now()
            });
            await reply(
                (details?.server ? `Сервер: ${details.server}.\n` : "") +
                    confirmationText(request.action, request.argument)
            );
            return;
        }
        const result = await this.control({
            ...request,
            actor: event.sender,
            event_id: event.event_id
        });
        await reply(
            request.action === "api-status"
                ? `API Misbot: ${result.enabled ? "включён" : "выключен"}. Изменить: api включить / api выключить.`
                : resultText(result)
        );
    }
    async notifications() {
        const snapshot = await this.control({ action: "notifications" });
        for (const room of await this.client.getJoinedRooms()) {
            if (!(await this.privateRoom(room))) continue;
            const admin =
                this.config.administrator_mxids?.[0] || "@matrix-admin:" + this.config.server_name;
            try {
                await this.authorize(admin);
            } catch {
                continue;
            }
            for (const item of snapshot.requests || []) {
                if (
                    item.hidden ||
                    item.cleared ||
                    (item.direction === "outgoing" && item.state === "pending")
                )
                    continue;
                const key = "notified:" + item.id + ":" + item.state;
                if (this.store.getValue(key)) continue;
                const actions =
                    item.state === "pending" && item.direction !== "outgoing"
                        ? `\n\nПринять: подтвердить ${item.id}\nОтклонить: отклонить ${item.id}`
                        : ["rejected", "expired"].includes(item.state)
                          ? "\n\nПосмотреть: заявки отклонённые"
                          : `\n\nПроверить: статус ${item.id}`;
                await this.authorize(admin);
                if (!(await this.privateRoom(room))) break;
                await this.client.sendNotice(
                    room,
                    `Заявка на подключение сервера ${item.server}.\nИдентификатор: ${item.id}\nСостояние: ${stateText(item.state)}.${actions}`
                );
                this.store.setValue(key, true);
            }
            for (const item of snapshot.operations || []) {
                const key = "operation-notified:" + item.id + ":" + item.state;
                if (this.store.getValue(key)) continue;
                await this.authorize(admin);
                if (!(await this.privateRoom(room))) break;
                await this.client.sendNotice(
                    room,
                    "Операция " +
                        item.id +
                        ": " +
                        stateText(item.state) +
                        "\n" +
                        JSON.stringify(
                            item.result,
                            (key, value) =>
                                /backup|migration|move|checkpoint/i.test(key) ? undefined : value,
                            2
                        )
                );
                this.store.setValue(key, true);
            }
            break; // Exactly one private admin room, even if multiple such rooms exist.
        }
    }
}
module.exports = {
    FederationBot,
    parse,
    HELP,
    MORE,
    MAINTENANCE,
    FEDERATION,
    REQUESTS,
    statisticsText,
    stateText,
    resultText,
    serverText,
    requestsText,
    errorText
};
