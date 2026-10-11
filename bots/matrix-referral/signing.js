"use strict";
const crypto = require("node:crypto");
const fs = require("node:fs");

// Matrix canonical JSON excludes top-level signatures/unsigned. Ed25519 uses
// Node's OpenSSL implementation; this module only assembles standard key objects.
function canonical(value) {
    if (Array.isArray(value)) return "[" + value.map(canonical).join(",") + "]";
    if (value && typeof value === "object")
        return (
            "{" +
            Object.keys(value)
                .sort()
                .map((k) => JSON.stringify(k) + ":" + canonical(value[k]))
                .join(",") +
            "}"
        );
    if (typeof value === "number" && !Number.isSafeInteger(value))
        throw new Error("Invalid canonical number");
    return JSON.stringify(value);
}
function payload(object) {
    const { signatures, unsigned, ...body } = object;
    return Buffer.from(canonical(body));
}
function newKey() {
    const { privateKey, publicKey } = crypto.generateKeyPairSync("ed25519");
    return {
        private: privateKey.export({ type: "pkcs8", format: "der" }).toString("base64"),
        public: publicKey
            .export({ type: "spki", format: "der" })
            .subarray(-32)
            .toString("base64")
            .replace(/=+$/, "")
    };
}
function publicKey(value) {
    return crypto.createPublicKey({
        key: Buffer.concat([
            Buffer.from("302a300506032b6570032100", "hex"),
            Buffer.from(value, "base64")
        ]),
        type: "spki",
        format: "der"
    });
}
function signed(object, key, user) {
    const privateKey = crypto.createPrivateKey({
        key: Buffer.from(key.private, "base64"),
        type: "pkcs8",
        format: "der"
    });
    const sig = crypto
        .sign(null, payload(object), privateKey)
        .toString("base64")
        .replace(/=+$/, "");
    return {
        ...object,
        signatures: {
            ...object.signatures,
            [user]: { ...object.signatures?.[user], ["ed25519:" + key.public]: sig }
        }
    };
}
function validSignature(object, signer, value, keyId = "ed25519:" + value) {
    const signature = object.signatures?.[signer]?.[keyId];
    return (
        !!signature &&
        crypto.verify(null, payload(object), publicKey(value), Buffer.from(signature, "base64"))
    );
}
function crossKey(key, usage, user) {
    return { user_id: user, usage: [usage], keys: { ["ed25519:" + key.public]: key.public } };
}

async function ensureCrossSigning(client, path = "/data/cross-signing.json") {
    const user = await client.getUserId(),
        device = client.crypto.clientDeviceId;
    const query = async () =>
        client.doRequest("POST", "/_matrix/client/v3/keys/query", null, {
            device_keys: { [user]: [device] }
        });
    let remote = await query();
    let keys;
    if (fs.existsSync(path)) {
        keys = JSON.parse(fs.readFileSync(path, "utf8"));
        if (keys.user !== user) throw new Error("Cross-signing owner mismatch");
    } else {
        if (remote.master_keys?.[user])
            throw new Error("Existing cross-signing identity preserved; local keys absent");
        keys = { user, master: newKey(), self: newKey(), users: newKey() };
        const tmp = path + ".tmp";
        fs.writeFileSync(tmp, JSON.stringify(keys), { mode: 0o600 });
        fs.renameSync(tmp, path);
    }
    if (
        remote.master_keys?.[user] &&
        remote.master_keys[user].keys?.["ed25519:" + keys.master.public] !== keys.master.public
    )
        throw new Error("Existing cross-signing identity differs; refusing replacement");
    const master = crossKey(keys.master, "master", user);
    // Bot SDK signs JSON.stringify directly; supply canonical key ordering.
    master.signatures = await client.crypto.sign(JSON.parse(canonical(master)));
    const self = signed(crossKey(keys.self, "self_signing", user), keys.master, user);
    const users = signed(crossKey(keys.users, "user_signing", user), keys.master, user);
    if (!remote.master_keys?.[user]) {
        await client.doRequest("POST", "/_matrix/client/v3/keys/device_signing/upload", null, {
            master_key: master,
            self_signing_key: self,
            user_signing_key: users
        });
    }
    const raw = remote.device_keys?.[user]?.[device];
    if (!raw || raw.keys?.["ed25519:" + device] !== client.crypto.clientDeviceEd25519)
        throw new Error("Device key mismatch");
    const signedDevice = signed(raw, keys.self, user);
    const response = await client.doRequest(
        "POST",
        "/_matrix/client/v3/keys/signatures/upload",
        null,
        { [user]: { [device]: signedDevice, [keys.master.public]: master } }
    );
    if (Object.keys(response.failures || {}).length)
        throw new Error("Cross-signing signature upload failed");
    remote = await query();
    if (
        !validSignature(
            remote.master_keys?.[user] || {},
            user,
            client.crypto.clientDeviceEd25519,
            "ed25519:" + device
        ) ||
        !validSignature(remote.self_signing_keys?.[user] || {}, user, keys.master.public) ||
        !validSignature(remote.device_keys?.[user]?.[device] || {}, user, keys.self.public)
    )
        throw new Error("Cross-signing verification failed");
    return true;
}
module.exports = {
    canonical,
    payload,
    newKey,
    signed,
    validSignature,
    crossKey,
    ensureCrossSigning
};
