import { build } from "esbuild";
import { mkdir } from "node:fs/promises";
import { spawnSync } from "node:child_process";

await mkdir("assets", { recursive: true });

const shared = {
  bundle: true,
  format: "esm",
  jsx: "automatic",
  minify: true,
  sourcemap: false,
  target: ["es2020"],
};

await Promise.all([
  build({ ...shared, entryPoints: ["entries/landing.jsx"], outfile: "assets/landing.js" }),
  build({ ...shared, entryPoints: ["entries/dashboard.jsx"], outfile: "assets/dashboard.js" }),
  build({ ...shared, entryPoints: ["entries/all-in-one.jsx"], outfile: "assets/all-in-one.js" }),
]);

const tailwind = process.platform === "win32"
  ? "node_modules/.bin/tailwindcss.cmd"
  : "node_modules/.bin/tailwindcss";
const css = spawnSync(tailwind, ["-c", "tailwind.config.cjs", "-i", "styles.css", "-o", "assets/verdict.css", "--minify"], {
  env: { ...process.env, BROWSERSLIST_IGNORE_OLD_DATA: "true" },
  stdio: "inherit",
});
if (css.status !== 0) {
  process.exit(css.status ?? 1);
}

const python = process.env.PYTHON || "python3";
const html = spawnSync(python, ["build.py"], { stdio: "inherit" });
if (html.status !== 0) {
  process.exit(html.status ?? 1);
}
