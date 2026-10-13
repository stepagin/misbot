"use strict";

const { createHash } = require("node:crypto");

const HELP = `Панель администратора Misbot

1. \`статистика\` — сообщения и регистрации
2. \`состояние\` — службы и доступность сервера
3. \`федерация\` — подключение серверов и заявки
4. \`обслуживание\` — службы, обновления и копии
5. \`ещё\` — приглашения, сообщения и настройки

Напишите команду из списка. \`меню\` — вернуться сюда`;
const MORE = `Дополнительные действия

1. \`приглашения\` — открыть реферального бота
2. \`рассылка\` — отправить сообщение пользователям
3. \`приветствие\` — текст для новых пользователей
4. \`уведомления\` — выбрать уведомления
5. \`настройки федерации\` — местная политика
6. \`безопасность\` — проверка сеанса и управление доступом

Назад: \`меню\``;
const MAINTENANCE = `Обслуживание сервера

1. \`перезапустить сервер\` — перезапустить службы
2. \`запустить службы\` / \`остановить службы\`
3. \`обновления\` — проверить версию
4. \`копии\` — создать или проверить резервную копию
5. \`хранилище\` — медиа и резервные копии
6. \`операции\` — задачи федерации
7. \`задача ID\` — результат задачи обслуживания

Памятка: изменения требуют подтверждения. Отмена предложения не прерывает запущенную задачу.
Назад: \`меню\``;
const FEDERATION = `Федерация

1. \`проверить сервер example.org\` — проверить подключение
2. \`пригласить сервер example.org\` — предложить подключение
3. \`заявки\` — входящие, исходящие и отклонённые
4. \`серверы\` — разрешённые серверы
5. \`api\` — межсерверный API Misbot

Настройки: \`настройки федерации\`
Назад: \`меню\``;
const FEDERATION_SETTINGS = `Настройки федерации

1. \`api включить\` / \`api выключить\`
2. \`добавить сервер example.org\` / \`удалить сервер example.org\` — местная политика
3. \`операции\` — результат изменений

Памятка: изменения требуют подтверждения. Выключение API сохраняет Matrix-политику.
Назад: \`федерация\``;
const REQUESTS = `Действия с заявками

1. \`заявки входящие\` / \`заявки исходящие\` / \`заявки отклонённые\`
2. \`подтвердить N\` / \`отклонить N\` — решение по входящей заявке
3. \`отменить N\` — отозвать своё приглашение
4. \`повторить N\` / \`статус N\` — продолжить или посмотреть результат
5. \`очистить отклонённые\` — убрать завершённые заявки

Памятка: N — номер заявки в списке; номер сохраняется после перезапуска.
Назад: \`федерация\``;
const COPIES = `Резервные копии

1. \`создать копию\` — создать резервную копию
2. \`проверить копию\` — проверить восстановление в изолированной среде
3. \`хранилище\` — состояние копий

Назад: \`обслуживание\``;

function noticeContent(source, { literal = false } = {}) {
    if (typeof source !== "string") throw new Error("Invalid notice body");
    const escape = (text) =>
        text.replace(
            /[&<>"']/g,
            (char) =>
                ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[char]
        );
    // Commands are marked explicitly by the author, never inferred from numbering,
    // current values or words occurring in user-authored announcements.
    const parts = literal ? [source] : source.split(/(`[^`\n]+`)/g);
    const body = parts
        .map((part) =>
            !literal && part.startsWith("`") && part.endsWith("`") ? part.slice(1, -1) : part
        )
        .join("");
    const formatted = parts
        .map((part) =>
            !literal && part.startsWith("`") && part.endsWith("`")
                ? "<code>" + escape(part.slice(1, -1)) + "</code>"
                : escape(part)
        )
        .join("");
    const html = formatted
        .split("\n")
        .map((line, index) => {
            if (index === 0) return "<strong>" + line + "</strong>";
            if (!literal && line.startsWith("Памятка:")) return "<em>" + line + "</em>";
            return line;
        })
        .join("<br>");
    return { msgtype: "m.notice", body, format: "org.matrix.custom.html", formatted_body: html };
}
async function sendNotice(client, room, body, options) {
    return client.sendMessage(room, noticeContent(body, options));
}

function humanDate(value) {
    const date = new Date(value);
    if (!Number.isFinite(date.getTime())) return "дата недоступна";
    return (
        date.toLocaleString("ru-RU", {
            timeZone: "UTC",
            year: "numeric",
            month: "long",
            day: "numeric",
            hour: "2-digit",
            minute: "2-digit"
        }) + " UTC"
    );
}
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
        "продолжить изменение федерации": "recover",
        "откатить": "rollback",
        "откатить изменение федерации": "rollback",
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
    const periods = {
        day: "за последние 24 часа",
        week: "за последние 7 дней",
        month: "за последние 30 дней",
        all: "за всё доступное время"
    };
    const lines = [
        "Статистика " + (periods[result.period] || "за выбранный период"),
        `Сообщений: ${result.messages ?? "недоступно"}`,
        `Регистраций: ${result.registrations ?? "недоступно"}`
    ];
    if (result.start_utc && result.end_utc && result.period !== "all")
        lines.push(`${humanDate(result.start_utc)} — ${humanDate(result.end_utc)}`);
    if (result.messages == null || result.registrations == null)
        lines.push("Не все счётчики удалось получить. Повторите запрос позже.");
    return lines.join("\n");
}
function versionText(result) {
    return `Установленная версия: Misbot ${result.installed}\nПоследняя стабильная версия: ${result.latest || "релиз ещё не опубликован"}\n${result.update_available ? "Доступно обновление." : "Более новая стабильная версия не найдена."}\n${result.notes || ""}\n${result.url || ""}\nКоманда: \`${result.command}\``;
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
        changed: "изменения сохранены",
        cleared: "завершённые заявки убраны",
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

function shellCommands(text) {
    return text.replace(
        /\b(?:sudo )?misbot(?: (?:[A-Za-z0-9_/=-]+(?:\.[A-Za-z0-9_/=-]+)*|'[^'\n]+'|"[^"\n]+"))+/g,
        (command) => "`" + command + "`"
    );
}

function resultText(result) {
    // Render documented safe fields; transport records and private checkpoints stay off chat.
    if (!result || typeof result !== "object" || Array.isArray(result))
        throw new Error("Invalid operation result");
    const lines = [
        Object.hasOwn(result, "status")
            ? `Операция: ${stateText(result.status)}.`
            : "Результат запроса:"
    ];
    if (result.server) lines.push(`Сервер: ${result.server}`);
    if (result.state) lines.push(`Состояние: ${stateText(result.state)}`);
    if (result.servers) {
        lines.push("Разрешённые серверы:");
        result.servers.forEach((server, index) => lines.push(`${index + 1}. ${server}`));
    }
    if (typeof result.enabled === "boolean")
        lines.push(`API Misbot: ${result.enabled ? "включён" : "выключен"}`);
    if (result.operation_id) lines.push(`Задача: ${result.operation_id}`);
    for (const item of result.operations || []) lines.push(`${item.id}: ${stateText(item.state)}`);
    if (result.operations?.length === 0) lines.push("Задач федерации пока нет.");
    if (result.recovery_available === false) lines.push("Незавершённых изменений федерации нет.");
    for (const item of result.peers || [])
        lines.push(
            `${item.server || item.server_name}: ${item.state ? stateText(item.state) : item.approved ? "согласовано" : "ожидает решения"}`
        );
    if (result.expires_at) lines.push(`Срок: ${humanDate(result.expires_at * 1000)}`);
    if (typeof result.local_applied === "boolean")
        lines.push(
            `Местная политика: ${result.local_applied ? "применена" : "ожидает применения"}`
        );
    if (typeof result.remote_applied === "boolean")
        lines.push(
            `Политика другого сервера: ${result.remote_applied ? "применена" : "ожидает применения"}`
        );
    if (result.delivery_pending) lines.push("Доставка решения пока не подтверждена.");
    if (result.matrix_discovery)
        lines.push(
            `Matrix: ${result.matrix_discovery === "available" ? "доступен" : "доступность не подтверждена"}`
        );
    if (result.https)
        lines.push(
            `Misbot API: ${result.https === "available" ? "доступен" : "доступность не подтверждена"}`
        );
    if (result.matrix)
        lines.push(
            `Matrix: ${result.matrix.available ? "доступен" : "доступность не подтверждена"}`
        );
    if (result.misbot)
        lines.push(
            `Misbot API: ${result.misbot.available ? "доступен" : "доступность не подтверждена"}`
        );
    if (result.invitation)
        lines.push(
            result.invitation.ready
                ? "Можно отправить приглашение."
                : errorText(result.invitation.code)
        );
    if (result.status === "cleared" && result.count != null)
        lines.push(`Убрано заявок: ${result.count}`);
    if (typeof result.policy === "boolean")
        lines.push(`Местная политика: ${result.policy ? "сервер разрешён" : "сервер не добавлен"}`);
    if (result.federation_exchange === "not_verified")
        lines.push("Работа федеративных комнат не проверялась.");
    if (result.note) lines.push(result.note);
    if (result.code || result.safe_error) lines.push(errorText(result.code || result.safe_error));
    if (result.next_action) lines.push(`Следующее действие: ${shellCommands(result.next_action)}`);
    return lines.join("\n");
}

function serverText(result) {
    if (result.statistics) return statisticsText(result.statistics);
    const lines = [
        result.ok === true
            ? "Сервер: проверка пройдена."
            : "Сервер: есть недоступные службы или проверки."
    ];
    if (result.checked_at)
        lines.push(
            `Время проверки: ${typeof result.checked_at === "number" ? humanDate(result.checked_at * 1000) : result.checked_at}`
        );
    for (const item of result.containers || [])
        lines.push(
            `${item.running && item.healthy ? "✓" : "✗"} ${item.name}: ${!item.running ? "остановлено" : item.healthy ? "работает" : "работает, проверка здоровья не пройдена"}`
        );
    for (const item of result.checks || [])
        lines.push(
            `${item.passed ? "✓" : "✗"} ${item.name}: ${item.passed ? "доступно" : "недоступно"}`
        );
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
    if (result.next_action) lines.push(`Следующее действие: ${shellCommands(result.next_action)}`);
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

function requestsText(result, category, number = (_, index) => index + 1) {
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
    for (const [index, item] of items.slice(0, 20).entries()) {
        const reference = number(item, index);
        lines.push(`\n${reference}. ${item.server} — ${stateText(item.state)}.`);
        if (item.expires_at) lines.push(`Срок: ${humanDate(item.expires_at * 1000)}`);
        if (item.state === "pending" && item.direction === "incoming")
            lines.push(
                `Принять: \`подтвердить ${reference}\`\nОтклонить: \`отклонить ${reference}\``
            );
        if (item.state === "pending" && item.direction === "outgoing")
            lines.push(`Отозвать: \`отменить ${reference}\``);
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
        "Подтверждение: `выполнить подтверждаю`. Отмена: `отмена`. Предложение действует 5 минут."
    );
    return lines.join("\n");
}

function errorText(code) {
    const messages = {
        api_disabled: "API Misbot выключен. Откройте «\`федерация\`», чтобы проверить настройки.",
        invitation_rate_limit:
            "Новое приглашение можно создать не чаще одного раза в минуту. Повторите позже.",
        peer_already_connected:
            "Сервер уже добавлен. Заявка больше не требуется; откройте «\`серверы\`».",
        pending_request_exists:
            "Для этого сервера уже есть актуальная заявка. Откройте «\`заявки\`».",
        peer_upgrade_required:
            "Удалённому серверу нужно обновить Misbot для приглашений сроком 30 дней.",
        request_not_found: "Заявка не найдена. Откройте актуальный список «\`заявки\`».",
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
    if (
        ["control_timeout", "control_unavailable", "busy_pending_or_operation_failed"].includes(
            code
        )
    )
        return "Управляющая служба сейчас недоступна или занята. Ответ не получен. Повторите команду позже; результат ранее принятой задачи можно проверить командой «\`задача ID\`».";
    return typeof code === "string" && Object.hasOwn(messages, code)
        ? messages[code]
        : "Результат команды не подтверждён. Проверьте состояние сервера и сохранённой задачи. Команды: `состояние`, `задача ID`, `операции`. Для восстановления используйте SSH. Справка: `помощь`.";
}

class FederationBot {
    constructor(client, control, store, config) {
        Object.assign(this, { client, control, store, config });
    }
    local(sender) {
        return (
            this.config.administrator_mxids || ["@matrix-admin:" + this.config.server_name]
        ).includes(sender);
    }
    requestNumber(id) {
        if (typeof id !== "string" || !/^MB-[0-9a-f]{32}$/.test(id))
            throw new Error("Invalid request identity");
        let saved = this.store.getValue("request-index");
        if (!saved && this.store.getValue("request-index-created"))
            throw new Error("Saved request index missing");
        saved ||= { format: 1, ids: [] };
        if (
            saved.format !== 1 ||
            !Array.isArray(saved.ids) ||
            saved.ids.length > 1000 ||
            new Set(saved.ids).size !== saved.ids.length ||
            saved.ids.some((value) => typeof value !== "string" || !/^MB-[0-9a-f]{32}$/.test(value))
        )
            throw new Error("Invalid saved request index");
        let index = saved.ids.indexOf(id);
        if (index < 0) {
            // Federation keeps at most 1000 request records, including terminal
            // tombstones. The index follows the same permanent capacity.
            if (saved.ids.length >= 1000) throw new Error("Request index capacity");
            index = saved.ids.length;
            this.store.setValue("request-index-created", true);
            this.store.setValue("request-index", { format: 1, ids: [...saved.ids, id] });
        }
        return index + 1;
    }
    resolveRequest(original) {
        const match = original.match(
            /^(подтвердить|отклонить|отменить|повторить|статус|очистить) (\d{1,4})$/i
        );
        if (!match) return parse(original);
        const saved = this.store.getValue("request-index");
        const id = saved?.ids?.[Number(match[2]) - 1];
        if (!id) return { unknownNumber: true };
        // Validate the persistent mapping before accepting a number. Numbers are
        // never reused across filters, deleted requests, notifications or restart.
        if (this.requestNumber(id) !== Number(match[2])) throw new Error("Request index mismatch");
        return parse(match[1] + " " + id);
    }
    async authorize(sender) {
        const denied = () => {
            const error = new Error("admin_unconfirmed");
            error.code = "admin_unconfirmed";
            return error;
        };
        if (!this.local(sender)) throw denied();
        const result = await this.control({ action: "authorize", actor: sender });
        if (result.authorized !== true) throw denied();
    }
    async privateRoom(room, recipient) {
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
            (!recipient || members.includes(recipient)) &&
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
            if (!(await this.privateRoom(room, event.sender))) return;
        } catch (error) {
            if (error?.code === "admin_unconfirmed") return;
            throw error;
        }
        const reply = async (body, options) => {
            await this.authorize(event.sender);
            if (!(await this.privateRoom(room, event.sender)))
                throw new Error("private_room_required");
            return sendNotice(this.client, room, body, options);
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
                        ? `\nПроверить: \`задача ${result.operation_id}\``
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
            if (text === "рассылка")
                return reply("Пришлите текст нового сообщения или «\`отмена\`».");
            draft = { kind: "broadcast", body: original, id: event.event_id, created: Date.now() };
            this.store.setValue(key, draft);
            await reply(
                `Сообщение от администратора сервера ${this.config.server_name}:\n\n${original}`,
                { literal: true }
            );
            await reply(
                "Черновик готов. Подтверждение рассылки: «\`отправить\`» или «\`отмена\`». Чтобы изменить текст, отправьте новый вариант в этот чат."
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
                    "Действующего предложения обновления нет. Отправьте «\`обновления\`» заново."
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
        if (text === "безопасность") {
            await reply(
                "Безопасность аккаунта\n\n" +
                    "Если около сообщения от matrix-admin указано непроверенное устройство, откройте в Matrix-клиенте настройки безопасности этого аккаунта и проверьте текущий сеанс с другого проверенного устройства или ключом восстановления.\n\n" +
                    "Памятка: пароль сервера не проверяет сеанс шифрования. Сервер не может подтвердить личное устройство вместо владельца. Сохраните ключ восстановления; не сбрасывайте шифрование и не удаляйте устройства ради предупреждения.\n\n" +
                    "В SSH откройте `sudo misbot ui`.\nАдминистраторы и пароль matrix-admin: «Администраторы и доступ».\nПароли остальных пользователей: «Пользователи и приглашения».\nНазад: `ещё`"
            );
            return;
        }
        if (text === "статистика") {
            await reply(
                "Статистика сервера\n\n`статистика день` / `статистика неделя` / `статистика месяц` / `статистика всего`\n\nНазад: `меню`"
            );
            return;
        }
        if (text === "приглашения") {
            const result = await this.call("panel-referral", null, event);
            await reply(
                `Для приглашения пользователя откройте реферального бота и отправьте «\`пригласить\`».\n${result.url}\n\nНазад: \`ещё\``
            );
            return;
        }
        if (text === "настройки федерации") {
            const result = await this.call("operations", null, event);
            await reply(
                FEDERATION_SETTINGS +
                    (result.recovery_available
                        ? "\n\nНезавершённое изменение федерации:\n" +
                          "1. `продолжить изменение федерации` — завершить применение\n" +
                          "2. `откатить изменение федерации` — вернуть прежнюю политику"
                        : "")
            );
            return;
        }
        if (text === "федерация") {
            const api = await this.call("api-status", null, event);
            await reply(`API Misbot: ${api.enabled ? "включён" : "выключен"}.\n\n${FEDERATION}`);
            return;
        }
        const reads = {
            "состояние": "check",
            "статус сервера": "check",
            "проверить": "check",
            "проверить сервер": "check",
            "хранилище": "storage"
        };
        if (Object.hasOwn(reads, text)) {
            const result = await this.call("panel-server-read", { operation: reads[text] }, event);
            if (reads[text] === "check") {
                const status = await this.call(
                    "panel-server-read",
                    { operation: "status" },
                    {
                        ...event,
                        event_id:
                            "status:" + createHash("sha256").update(event.event_id).digest("hex")
                    }
                );
                await reply(
                    serverText({
                        ...status,
                        ...result,
                        containers: status.containers,
                        ok: status.ok === true && result.ok === true
                    })
                );
            } else await reply(serverText(result));
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
            await reply(
                requestsText(result, categories[text], (item) => this.requestNumber(item.id))
            );
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
                    "Обновление доступно. Для запуска напишите «\`обновить подтверждаю\`». Отмена: «\`отмена\`». Перед применением проверим свободное место. Сервер временно остановится. Результат сохранится при перезапуске."
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
                "Пришлите текст рассылки. Затем покажу черновик для подтверждения. Отмена: «\`отмена\`»."
            );
            return;
        }
        if (text === "приветствие") {
            const result = await this.call("panel-welcome", null, event);
            await reply(result.text, { literal: true });
            await reply("Чтобы изменить текст, отправьте «\`изменить приветствие\`».");
            return;
        }
        if (text === "изменить приветствие") {
            this.store.setValue(key, { kind: "welcome", created: Date.now() });
            await reply(
                "Пришлите новое приветствие. Подстановки: {server_name} — название сервера, " +
                    "{domain} — Matrix-домен, {referral_url} — ссылка на реферального бота, " +
                    "{administrator_url} — ссылка на администратора. Отмена: «\`отмена\`»."
            );
            return;
        }
        if (text === "уведомления") {
            const result = await this.call("panel-status", null, event),
                p = result.preferences;
            await reply(
                `Обновления: ${p.updates ? "включены" : "выключены"}\nРегистрации: ${p.registrations ? "включены" : "выключены"}\nЕжемесячная статистика: ${p.statistics ? "включена" : "выключена"}\nНастройка:\n\`уведомления обновления ${p.updates ? "выключить" : "включить"}\`\n\`уведомления регистрации ${p.registrations ? "выключить" : "включить"}\`\n\`уведомления статистика ${p.statistics ? "выключить" : "включить"}\``
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
        const request = this.resolveRequest(original);
        if (request?.unknownNumber) {
            await reply(
                "Номер заявки не найден. Откройте «\`заявки\`» и выберите номер из списка."
            );
            return;
        }
        if (!request) {
            await reply(HELP);
            return;
        }
        if (WRITES.has(request.action)) {
            clearProposal();
            if (["recover", "rollback"].includes(request.action)) {
                const result = await this.call("operations", null, event);
                if (result.recovery_available !== true) {
                    await reply(
                        "Незавершённых изменений федерации нет. Действие не требуется.\nНазад: `федерация`"
                    );
                    return;
                }
            }
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
                    confirmationText(
                        request.action,
                        request.argument?.startsWith("MB-")
                            ? "заявка №" + this.requestNumber(request.argument)
                            : request.argument
                    )
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
                ? `API Misbot: ${result.enabled ? "включён" : "выключен"}. Изменить: \`api включить\` / \`api выключить\`.`
                : resultText(result)
        );
    }
    async notifications() {
        const snapshot = await this.control({ action: "notifications" });
        const delivered = new Set();
        for (const room of await this.client.getJoinedRooms()) {
            if (!(await this.privateRoom(room))) continue;
            const state = await this.client.getRoomState(room);
            const admin = state.find(
                (item) =>
                    item.type === "m.room.member" &&
                    item.content?.membership === "join" &&
                    this.local(item.state_key)
            )?.state_key;
            if (!admin || delivered.has(admin)) continue;
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
                const key = "notified:" + item.id + ":" + item.state + ":" + admin;
                const legacy = "notified:" + item.id + ":" + item.state;
                if (
                    this.store.getValue(key) ||
                    (admin === this.config.administrator_mxids?.[0] && this.store.getValue(legacy))
                )
                    continue;
                const number = this.requestNumber(item.id);
                const actions =
                    item.state === "pending" && item.direction !== "outgoing"
                        ? `\n\nПринять: \`подтвердить ${number}\`\nОтклонить: \`отклонить ${number}\``
                        : ["rejected", "expired"].includes(item.state)
                          ? "\n\nПосмотреть: `заявки отклонённые`"
                          : `\n\nПосмотреть: \`статус ${number}\``;
                await this.authorize(admin);
                if (!(await this.privateRoom(room))) break;
                await sendNotice(
                    this.client,
                    room,
                    `Заявка №${number}: ${item.server}\nСостояние: ${stateText(item.state)}.${actions}`
                );
                this.store.setValue(key, true);
            }
            for (const item of snapshot.operations || []) {
                const key = "operation-notified:" + item.id + ":" + item.state + ":" + admin;
                const legacy = "operation-notified:" + item.id + ":" + item.state;
                if (
                    this.store.getValue(key) ||
                    (admin === this.config.administrator_mxids?.[0] && this.store.getValue(legacy))
                )
                    continue;
                await this.authorize(admin);
                if (!(await this.privateRoom(room))) break;
                await sendNotice(
                    this.client,
                    room,
                    "Задача " + item.id + "\n" + resultText({ ...item.result, status: item.state })
                );
                this.store.setValue(key, true);
            }
            delivered.add(admin);
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
    FEDERATION_SETTINGS,
    COPIES,
    REQUESTS,
    statisticsText,
    stateText,
    resultText,
    serverText,
    requestsText,
    errorText,
    noticeContent,
    sendNotice
};
