"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs"),
    os = require("node:os"),
    path = require("node:path");
const {
    canonical,
    newKey,
    signed,
    validSignature,
    crossKey,
    ensureCrossSigning
} = require("../signing");

test("canonical signing survives ordering/unsigned fields but rejects tampering", () => {
    const key = newKey(),
        user = "@bot:misbot.ru";
    assert.equal(
        canonical({ z: [2, "a"], a: { "10": 1, "2": 2 } }),
        '{"a":{"10":1,"2":2},"z":[2,"a"]}'
    );
    const object = signed(
        { user_id: user, usage: ["self_signing"], keys: { "ed25519:x": "abc" } },
        key,
        user
    );
    assert.equal(
        validSignature({ ...object, unsigned: { display_name: "anything" } }, user, key.public),
        true
    );
    assert.equal(validSignature({ ...object, usage: ["master"] }, user, key.public), false);
    assert.equal(validSignature(object, "@other:misbot.ru", key.public), false);
    assert.throws(() => canonical({ number: 1.5 }));
});

function client() {
    const user = "@bot:misbot.ru",
        device = "BOT-DEVICE",
        identity = newKey();
    let uploaded = 0,
        keyset = null;
    let deviceObject = {
        user_id: user,
        device_id: device,
        algorithms: ["m.olm.v1.curve25519-aes-sha2"],
        keys: { ["ed25519:" + device]: identity.public }
    };
    const object = {
        getUserId: async () => user,
        crypto: {
            clientDeviceId: device,
            clientDeviceEd25519: identity.public,
            sign: async (key) => {
                assert.equal(
                    JSON.stringify(key),
                    canonical(key),
                    "SDK needs canonical order before signing"
                );
                return {
                    [user]: {
                        ["ed25519:" + device]: signed(key, identity, user).signatures[user][
                            "ed25519:" + identity.public
                        ]
                    }
                };
            }
        },
        doRequest: async (method, url, qs, body) => {
            if (url.endsWith("/keys/query"))
                return {
                    device_keys: { [user]: { [device]: deviceObject } },
                    ...(keyset
                        ? {
                              master_keys: { [user]: keyset.master_key },
                              self_signing_keys: { [user]: keyset.self_signing_key }
                          }
                        : {})
                };
            if (url.endsWith("/device_signing/upload")) {
                uploaded++;
                keyset = body;
                return {};
            }
            if (url.endsWith("/signatures/upload")) {
                deviceObject = body[user][device];
                keyset.master_key = body[user][Object.values(keyset.master_key.keys)[0]];
                return {};
            }
            throw new Error("Unexpected endpoint");
        }
    };
    return {
        object,
        count: () => uploaded,
        setRemote: (v) => {
            keyset = v;
        }
    };
}

test("bootstrap signs existing device; restarts reuse identity without rotation", async () => {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), "referral-signing-test-"));
    try {
        const file = path.join(dir, "keys.json"),
            c = client();
        assert.equal(await ensureCrossSigning(c.object, file), true);
        const saved = fs.readFileSync(file, "utf8");
        assert.equal(fs.statSync(file).mode & 0o777, 0o600);
        assert.equal(await ensureCrossSigning(c.object, file), true);
        assert.equal(fs.readFileSync(file, "utf8"), saved);
        assert.equal(c.count(), 1);
    } finally {
        fs.rmSync(dir, { recursive: true, force: true });
    }
});

test("bootstrap preserves unknown remote identities instead of replacing keys", async () => {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), "referral-signing-test-"));
    try {
        const file = path.join(dir, "keys.json"),
            c = client();
        c.setRemote({ master_key: crossKey(newKey(), "master", "@bot:misbot.ru") });
        await assert.rejects(ensureCrossSigning(c.object, file), /preserved/);
        assert.equal(fs.existsSync(file), false);
        c.setRemote(null);
        await ensureCrossSigning(c.object, file);
        c.setRemote({ master_key: crossKey(newKey(), "master", "@bot:misbot.ru") });
        await assert.rejects(ensureCrossSigning(c.object, file), /refusing replacement/);
        assert.equal(c.count(), 1);
    } finally {
        fs.rmSync(dir, { recursive: true, force: true });
    }
});
