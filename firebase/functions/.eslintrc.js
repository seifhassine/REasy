module.exports = {
  env: {
    es6: true,
    node: true,
  },
  parserOptions: {ecmaVersion: 2022},
  extends: [
    "eslint:recommended",
    "google",
  ],
  rules: {
    "no-restricted-globals": ["error", "name", "length"],
    "prefer-arrow-callback": "error",
    "quotes": ["error", "double", {"allowTemplateLiterals": true}],
    "require-jsdoc": "off",
    "max-len": ["error", {code: 500, ignoreUrls: true}],
    "valid-jsdoc": "off",
  },
  overrides: [
    {
      files: ["**/*.spec.*"],
      env: {mocha: true},
      rules: {},
    },
  ],
  globals: {},
};
