"use strict";

const globals = require("globals");

module.exports = [
    { ignores: ["dist/**", "**/node_modules/**"] },
    {
        files: ["bots/**/*.js", "eslint.config.cjs"],
        languageOptions: {
            ecmaVersion: 2024,
            sourceType: "commonjs",
            globals: globals.node
        },
        rules: {
            "no-undef": "error",
            "no-dupe-args": "error",
            "no-dupe-keys": "error",
            "no-unreachable": "error",
            "no-unsafe-finally": "error",
            "constructor-super": "error"
        }
    }
];
