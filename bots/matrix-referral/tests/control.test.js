"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { EventEmitter } = require("node:events");
const { test } = require("node:test");
const source = fs.readFileSync(path.join(__dirname, "../main.js"), "utf8");

function fixture() {
    const socket = new EventEmitter();
    socket.write = () => {};
    socket.destroy = () => {
        socket.destroyed = true;
        socket.emit("close");
    };
    const context = {
        net: { createConnection: () => socket },
        setTimeout,
        clearTimeout
    };
    vm.createContext(context);
    vm.runInContext(
        source.slice(source.indexOf("function control("), source.indexOf("async function main(")),
        context
    );
    return { socket, control: context.control };
}

for (const event of ["end", "close"]) {
    test("RPC rejects " + event + " with incomplete response", async () => {
        const { socket, control } = fixture();
        const pending = control({ action: "servers" }, 100);
        socket.emit("data", Buffer.from('{"ok":true'));
        socket.emit(event);
        await assert.rejects(pending, /Incomplete control response/);
        assert.equal(socket.destroyed, true);
    });
}

test("RPC deadline is absolute despite trickle data", async () => {
    const { socket, control } = fixture();
    const timer = setInterval(() => socket.emit("data", Buffer.from(" ")), 2);
    try {
        await assert.rejects(control({}, 20), /Control timeout/);
    } finally {
        clearInterval(timer);
    }
    assert.equal(socket.destroyed, true);
});

test("complete response settles once before close and late error", async () => {
    const { socket, control } = fixture();
    const pending = control({});
    socket.emit("data", Buffer.from('{"ok":true,"result":7}\n'));
    socket.emit("error", new Error("late"));
    assert.equal(await pending, 7);
});
