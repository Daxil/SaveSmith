/**
 * Puts the backend where Tauri expects to find it.
 *
 * Tauri looks for an `externalBin` under the exact host triple, so the same
 * `dist/savesmith` that the CLI ships becomes `savesmith-aarch64-apple-darwin`
 * here. Copied rather than symlinked: the bundler follows the path into the
 * .app and a link would put a dangling one there.
 *
 * The binary itself is built by PyInstaller — `uv run pyinstaller savesmith.spec`
 * — so the window and the command line are the same program, and a bug fixed in
 * one cannot survive in the other.
 */

import { execFileSync } from "node:child_process";
import { copyFileSync, existsSync, mkdirSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const suffix = process.platform === "win32" ? ".exe" : "";
const built = resolve(here, "..", "..", "dist", `savesmith${suffix}`);

if (!existsSync(built)) {
  console.error(
    `Бэкенда нет: ${built}\nСобери его сначала: uv run pyinstaller savesmith.spec`,
  );
  process.exit(1);
}

let described;
try {
  described = execFileSync("rustc", ["-vV"], { encoding: "utf8" });
} catch {
  console.error(
    "rustc не найден. Поставь Rust — https://rustup.rs — и открой терминал заново\n" +
      "(или добавь его в PATH на этот раз: source $HOME/.cargo/env).",
  );
  process.exit(1);
}

const host = described.match(/^host: (.+)$/m);
if (!host) {
  console.error("rustc не сказал, под какую платформу он собирает:\n" + described);
  process.exit(1);
}

const folder = resolve(here, "..", "src-tauri", "binaries");
const target = resolve(folder, `savesmith-${host[1]}${suffix}`);
mkdirSync(folder, { recursive: true });
copyFileSync(built, target);
console.log(`сайдкар: savesmith-${host[1]}${suffix}`);
