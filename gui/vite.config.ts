import { spawn, type ChildProcessWithoutNullStreams } from "node:child_process";
import { existsSync } from "node:fs";
import { resolve } from "node:path";

import react from "@vitejs/plugin-react";
import { defineConfig, type Plugin } from "vite";

const BINARY = resolve(__dirname, "..", "dist", "savesmith");

/**
 * Lets the interface be built and looked at in a browser, without Rust.
 *
 * The dev server starts the real backend as a child process and forwards
 * `POST /rpc` to its stdin, one line in, one line out. That is the same
 * transport Tauri will use in the packaged app, so the interface is exercised
 * against the real thing rather than against a mock that agrees with it.
 *
 * The backend deliberately has no HTTP mode of its own: a local port with no
 * authentication is reachable by any page the user happens to have open, and
 * this process can rewrite save files. Here the port is Vite's, it only exists
 * while somebody is developing, and it is not part of what ships.
 */
function backendBridge(): Plugin {
  let backend: ChildProcessWithoutNullStreams | null = null;
  const waiting: ((line: string) => void)[] = [];
  let buffered = "";

  function start(): ChildProcessWithoutNullStreams {
    if (backend && !backend.killed) return backend;
    if (!existsSync(BINARY)) {
      throw new Error(
        `сначала собери бинарь: uv run pyinstaller savesmith.spec (нет ${BINARY})`,
      );
    }
    const child = spawn(BINARY, ["rpc"], { stdio: ["pipe", "pipe", "pipe"] });
    child.stdout.setEncoding("utf8");
    child.stdout.on("data", (chunk: string) => {
      buffered += chunk;
      let newline = buffered.indexOf("\n");
      while (newline >= 0) {
        const line = buffered.slice(0, newline);
        buffered = buffered.slice(newline + 1);
        waiting.shift()?.(line);
        newline = buffered.indexOf("\n");
      }
    });
    child.stderr.setEncoding("utf8");
    child.stderr.on("data", (text: string) => process.stderr.write(`[savesmith] ${text}`));
    child.on("exit", (code) => {
      console.error(`[savesmith] backend exited with ${code}`);
      backend = null;
    });
    backend = child;
    return child;
  }

  return {
    name: "savesmith-backend-bridge",
    configureServer(server) {
      server.middlewares.use("/rpc", (request, response) => {
        if (request.method !== "POST") {
          response.statusCode = 405;
          response.end();
          return;
        }
        const chunks: Buffer[] = [];
        request.on("data", (chunk: Buffer) => chunks.push(chunk));
        request.on("end", () => {
          let child: ChildProcessWithoutNullStreams;
          try {
            child = start();
          } catch (failure) {
            response.statusCode = 503;
            response.setHeader("content-type", "application/json");
            response.end(JSON.stringify({ error: { code: -1, message: String(failure) } }));
            return;
          }
          waiting.push((line) => {
            response.setHeader("content-type", "application/json");
            response.end(line);
          });
          child.stdin.write(Buffer.concat(chunks).toString("utf8").trim() + "\n");
        });
      });

      server.httpServer?.on("close", () => backend?.kill());
    },
  };
}

export default defineConfig({
  plugins: [react(), backendBridge()],
  // Tauri serves the built files from disk, so every asset path has to be
  // relative rather than rooted at /.
  base: "./",
  build: { outDir: "dist", emptyOutDir: true },
  server: { port: 5173, strictPort: true },
});
