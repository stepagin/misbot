"use strict";

// Keep one crypto operation active. User input precedes waiting periodic work,
// and an unavailable controller cannot build a queue of identical timer jobs.
class SerialQueue {
    constructor(onError = () => {}) {
        this.onError = onError;
        this.inputs = [];
        this.periodic = [];
        this.keys = new Set();
        this.running = false;
        this.waiters = [];
    }
    add(key, operation, background = false) {
        if (this.keys.has(key)) return false;
        if (this.keys.size >= 256) {
            this.onError("queue_capacity");
            return false;
        }
        this.keys.add(key);
        (background ? this.periodic : this.inputs).push({ key, operation });
        if (!this.running) {
            this.running = true;
            queueMicrotask(() => this.drain());
        }
        return true;
    }
    async drain() {
        while (this.inputs.length || this.periodic.length) {
            const task = (this.inputs.length ? this.inputs : this.periodic).shift();
            try {
                await task.operation();
            } catch {
                this.onError("operation_unconfirmed");
            } finally {
                this.keys.delete(task.key);
            }
        }
        this.running = false;
        for (const resolve of this.waiters.splice(0)) resolve();
    }
    idle() {
        return this.running
            ? new Promise((resolve) => this.waiters.push(resolve))
            : Promise.resolve();
    }
}

function controlTimeout(action) {
    if (["authorize", "panel-outbox", "panel-ack", "notifications"].includes(action)) return 10000;
    if (["check", "panel-server-read"].includes(action)) return 90000;
    return 30000;
}

// Store only bounded encrypted events. No decrypted text, keys or exception
// payloads are recorded; retry still goes through the bot's current rights check.
class DecryptionRetry {
    constructor(store, decrypt, handle, allowed, clock = Date.now) {
        Object.assign(this, { store, decrypt, handle, allowed, clock });
    }
    pending() {
        const now = this.clock();
        return (this.store.getValue("decryption-pending") || [])
            .filter(
                (item) =>
                    Number.isFinite(item.created) &&
                    now - item.created >= 0 &&
                    now - item.created < 600000 &&
                    this.allowed(item.event.sender)
            )
            .slice(-64);
    }
    remember(room, event) {
        if (
            !event ||
            !this.allowed(event.sender) ||
            event.type !== "m.room.encrypted" ||
            event.content?.algorithm !== "m.megolm.v1.aes-sha2" ||
            typeof event.event_id !== "string" ||
            !event.event_id ||
            event.event_id.length > 255 ||
            typeof event.content.ciphertext !== "string" ||
            typeof event.content.session_id !== "string" ||
            Buffer.byteLength(JSON.stringify(event)) > 65536
        )
            return false;
        const pending = this.pending();
        if (pending.some((item) => item.event.event_id === event.event_id)) return false;
        const encrypted = {
            event_id: event.event_id,
            sender: event.sender,
            type: event.type,
            origin_server_ts: event.origin_server_ts,
            content: {
                algorithm: event.content.algorithm,
                ciphertext: event.content.ciphertext,
                session_id: event.content.session_id,
                sender_key: event.content.sender_key,
                device_id: event.content.device_id
            }
        };
        pending.push({ room, event: encrypted, created: this.clock() });
        this.store.setValue("decryption-pending", pending.slice(-64));
        return true;
    }
    forget(event) {
        const pending = this.pending();
        if (!pending.some((item) => item.event.event_id === event.event_id)) return;
        this.store.setValue(
            "decryption-pending",
            pending.filter((item) => item.event.event_id !== event.event_id)
        );
    }
    async retry() {
        const pending = this.pending();
        if (!pending.length) {
            if (this.store.getValue("decryption-pending")?.length)
                this.store.setValue("decryption-pending", []);
            return;
        }
        // A single missing key cannot monopolize input or block other rooms.
        for (const item of pending.slice(0, 8)) {
            try {
                const decrypted = await this.decrypt(item.room, item.event);
                await this.handle(item.room, decrypted.raw);
                this.forget(item.event);
            } catch {
                // Missing key or unavailable authorization: retain ciphertext.
                const pending = this.pending();
                const saved = pending.find((entry) => entry.event.event_id === item.event.event_id);
                if (saved)
                    this.store.setValue("decryption-pending", [
                        ...pending.filter((entry) => entry !== saved),
                        saved
                    ]);
            }
        }
        this.store.setValue("decryption-pending", this.pending());
    }
}

module.exports = { SerialQueue, controlTimeout, DecryptionRetry };
