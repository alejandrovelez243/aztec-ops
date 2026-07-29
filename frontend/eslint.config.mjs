// @ts-check
// Aztec Ops frontend lint rules — see docs/standards/FRONTEND.md §9.
// The two rules that carry the architecture:
//   - `fetch` and `EventSource` are banned outside src/lib/api/ and src/lib/stream/
//     (DIP: components depend on the client and the store, never on the wire).
//   - default exports are banned in .ts modules (named exports only); Astro
//     components/pages/layouts are exempt because the framework requires them.
import js from "@eslint/js";
import tseslint from "typescript-eslint";
import astro from "eslint-plugin-astro";
import prettier from "eslint-config-prettier";

const wireBan = {
  "no-restricted-globals": [
    "error",
    {
      name: "fetch",
      message:
        "Use src/lib/api/client.ts — the typed error contract is lost otherwise.",
    },
    {
      name: "EventSource",
      message: "Use src/lib/stream/store.ts — one shared connection per tab.",
    },
  ],
};

export default tseslint.config(
  {
    ignores: [
      "dist/",
      "node_modules/",
      ".astro/",
      "src/env.d.ts",
      "src/lib/api/types.ts",
    ],
  },
  js.configs.recommended,
  ...tseslint.configs.recommended,
  ...astro.configs.recommended,
  prettier,
  {
    rules: {
      "@typescript-eslint/no-explicit-any": "error",
      "@typescript-eslint/no-non-null-assertion": "error",
      "no-restricted-syntax": [
        "error",
        {
          selector: "ExportDefaultDeclaration",
          message: "Named exports only (Astro .astro files are exempt).",
        },
      ],
    },
  },
  {
    // Type-aware rules need the project service; scope them to plain TS so the
    // astro parser does not have to resolve a tsconfig for virtual modules.
    files: ["src/**/*.ts"],
    languageOptions: {
      parserOptions: {
        projectService: true,
        tsconfigRootDir: import.meta.dirname,
      },
    },
    rules: {
      "@typescript-eslint/switch-exhaustiveness-check": "error",
    },
  },
  {
    files: ["src/**/*.{ts,astro}"],
    ignores: ["src/lib/api/**", "src/lib/stream/**"],
    rules: wireBan,
  },
  {
    // Astro components ARE the default export; pages/layouts/components/islands exempt.
    files: ["src/**/*.astro"],
    rules: { "no-restricted-syntax": "off" },
  },
  {
    // Root config files (astro.config.mjs, eslint.config.mjs) require default exports.
    files: ["*.config.mjs"],
    rules: { "no-restricted-syntax": "off" },
  },
);
