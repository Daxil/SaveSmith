/**
 * Talking to the backend.
 *
 * The window owns no rules. Every rule that matters — no write without a
 * backup, no dangerous edit without an acknowledgement, no plugin without a
 * round-trip — lives in the Python core, and this file only carries messages
 * to it. A screen here that could bypass one of those rules would be a bug in
 * this file, not a feature of the interface.
 *
 * Two transports behind one interface:
 *
 * - **Packaged**: Tauri runs the `savesmith` binary as a sidecar speaking
 *   JSON-RPC over stdin and stdout.
 * - **Development**: a small HTTP bridge in front of the same binary, so the
 *   interface can be built and looked at in a browser without Rust installed.
 *
 * The types below were read off the running server rather than guessed, which
 * is why the field key is `address` and not the `path` one would expect: that
 * is what the core calls it, and renaming it here would only make the two
 * halves disagree.
 */

import { invoke } from "@tauri-apps/api/core";

export type RpcParams = Record<string, unknown>;

export class RpcError extends Error {
  readonly code: number;

  constructor(message: string, code: number) {
    super(message);
    this.name = "RpcError";
    this.code = code;
  }
}

export interface Transport {
  readonly kind: "sidecar" | "bridge";
  send(method: string, params: RpcParams): Promise<unknown>;
}

interface Answer {
  result?: unknown;
  error?: { code: number; message: string };
}

/**
 * The development transport: one HTTP request per call.
 *
 * Deliberately not a WebSocket. Progress notifications are the only thing that
 * would need one, and a scan that reports nothing until it finishes is a fair
 * trade for being able to open the interface in a browser.
 */
export class BridgeTransport implements Transport {
  readonly kind = "bridge";
  readonly #url: string;
  #next = 1;

  constructor(url = "/rpc") {
    this.#url = url;
  }

  async send(method: string, params: RpcParams): Promise<unknown> {
    let answer: Answer;
    try {
      const response = await fetch(this.#url, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ jsonrpc: "2.0", id: this.#next++, method, params }),
      });
      if (!response.ok) {
        throw new RpcError(`SaveSmith ответил ошибкой ${response.status}.`, response.status);
      }
      answer = (await response.json()) as Answer;
    } catch (failure) {
      if (failure instanceof RpcError) throw failure;
      throw new RpcError("SaveSmith не отвечает. Он запущен?", 0);
    }
    if (answer.error) {
      throw new RpcError(answer.error.message, answer.error.code);
    }
    return answer.result;
  }
}

/**
 * The packaged transport: the shell holds the backend open and matches answers
 * by id, so several calls may be in flight and progress notifications do not
 * get mistaken for results.
 *
 * The envelope that comes back is the one the core wrote, so a refusal is
 * unpacked here exactly as the bridge unpacks it. Only a backend that has
 * stopped talking arrives as a rejected `invoke`.
 */
export class SidecarTransport implements Transport {
  readonly kind = "sidecar";

  async send(method: string, params: RpcParams): Promise<unknown> {
    let answer: Answer;
    try {
      answer = (await invoke("rpc", { method, params })) as Answer;
    } catch (failure) {
      throw new RpcError(typeof failure === "string" ? failure : String(failure), 0);
    }
    if (answer.error) {
      throw new RpcError(answer.error.message, answer.error.code);
    }
    return answer.result;
  }
}

/** True when the window is the packaged app rather than a browser tab. */
export function packaged(): boolean {
  return typeof window !== "undefined" && "__TAURI_INTERNALS__" in window;
}

/**
 * The right transport for wherever this is running.
 *
 * Development and the shipped app talk to the same binary over the same
 * protocol; only the pipe differs, and nothing above this line knows which.
 */
export function transportForHere(): Transport {
  return packaged() ? new SidecarTransport() : new BridgeTransport();
}

/** Everything the interface can ask for, named the way the backend names it. */
export class Backend {
  readonly #transport: Transport;
  #language: Language = "ru";

  constructor(transport: Transport) {
    this.#transport = transport;
  }

  get kind(): Transport["kind"] {
    return this.#transport.kind;
  }

  set language(value: Language) {
    this.#language = value;
  }

  ping(): Promise<{ ok: boolean; version: string }> {
    return this.#call("ping", {});
  }

  doctor(): Promise<{ text: string }> {
    return this.#call("doctor", {});
  }

  scan(): Promise<ScanResult> {
    return this.#call("scan", {});
  }

  /** Every game on this machine, for the first screen to offer as a list. */
  games(): Promise<GameList> {
    return this.#call("games", {});
  }

  findSaves(folder: string): Promise<FoundGame> {
    return this.#call("find_saves", { folder });
  }

  open(path: string, gameFolder?: string): Promise<Session> {
    return this.#call("open", { path, game_folder: gameFolder });
  }

  /** Returns the whole session again, so a screen never has to guess the new state. */
  change(session: string, field: string, value: unknown): Promise<Session & { change: Change }> {
    return this.#call("set", { session, field, value });
  }

  acknowledge(session: string, items: string[]): Promise<Session> {
    return this.#call("acknowledge", { session, items });
  }

  confirmCloud(session: string, steps: number[]): Promise<Session> {
    return this.#call("confirm_cloud", { session, steps });
  }

  /**
   * Writes the pending changes. The backup comes back as an object, not a
   * string — this said `string` for a while, the screen rendered it as one,
   * and React unmounted the whole tree over it. A black window is what a type
   * that lies about the wire looks like from the outside.
   */
  write(session: string): Promise<Session & { written: boolean; backup: Backup }> {
    return this.#call("write", { session });
  }

  close(session: string): Promise<unknown> {
    return this.#call("close", { session });
  }

  /** Every value in a save, named the way the game named it. No plugin. */
  fields(path: string): Promise<FieldList> {
    return this.#call("fields", { path });
  }

  search(path: string, value: number): Promise<SearchResult> {
    return this.#call("search", { path, value });
  }

  /**
   * Change a number by address, in a save no plugin describes.
   *
   * `confirmed` is a wall in the backend, not a formality: without it the call
   * is refused. The screen has to have shown the player that SaveSmith cannot
   * say what this number does before it may pass it.
   */
  poke(
    path: string,
    address: string,
    value: number,
    options: { confirmed?: boolean; dryRun?: boolean; gameFolder?: string } = {},
  ): Promise<PokeResult> {
    return this.#call("poke", {
      path,
      address,
      value,
      confirmed: options.confirmed ?? false,
      dry_run: options.dryRun ?? false,
      game_folder: options.gameFolder,
    });
  }

  backups(plugin: string): Promise<{ backups: BackupEntry[] }> {
    return this.#call("backups.list", { plugin });
  }

  restore(plugin: string, index: number): Promise<{ restored: boolean }> {
    return this.#call("backups.restore", { plugin, index });
  }

  async #call<T>(method: string, params: RpcParams): Promise<T> {
    return (await this.#transport.send(method, {
      language: this.#language,
      ...params,
    })) as T;
  }
}

export type Language = "ru" | "en";

// -- what comes back ---------------------------------------------------------

export interface ScanResult {
  games: { appid: number; name: string; install_dir: string; installed: boolean }[];
  bottles: { name: string; kind: string; path: string; users: string[] }[];
}

export interface InstalledGame {
  name: string;
  /** What to hand back as the folder — an install folder, an .exe or an .app. */
  path: string;
  source: string;
  bottle: string | null;
  steam_appid: number | null;
  installed: boolean;
  risk_tier: string | null;
}

export interface GameList {
  games: InstalledGame[];
  problems: string[];
}

export interface FoundSave {
  path: string;
  format: string;
  /** Fields can be read out of it, so it can be edited by name. */
  recognised: boolean;
  /** Format understood and rebuilds exactly, but its fields are unmapped. */
  openable: boolean;
  /**
   * The plugin that reads this file by name, if one does.
   *
   * This and not `recognised` decides whether named fields can be shown: the
   * generic decoder ladder has nothing to say about an Elden Ring slot, and
   * the plugin has everything.
   */
  plugin: string | null;
  /** What it is. Only "save" is the player's; the rest are the game's own. */
  kind: "save" | "backup" | "settings" | "other";
  /** Unix seconds. Which save is the live one is a question only this answers. */
  modified: number;
  size: number;
}

export interface PrefsEntry {
  name: string;
  value: unknown;
  kind: string;
}

export interface FoundGame {
  bottle: string | null;
  game: {
    title: string;
    engine: string;
    project: string | null;
    steam_appid: number | null;
    anticheat: string[];
  };
  searched: string[];
  prefs: { location: string; entries: PrefsEntry[] } | null;
  saves: FoundSave[];
  /** How many of everything that is not a save, counted rather than listed. */
  aside: Partial<Record<"backup" | "settings" | "other", number>>;
}

export interface Field {
  address: string;
  label: string;
  group: string | null;
  type: string;
  value: unknown;
  present: boolean;
  min: number | null;
  max: number | null;
  options: unknown[];
  warn: string | null;
  advanced: boolean;
  achievement: boolean;
  online_linked: boolean;
  /** False for a field the core refuses to change — online-linked, or absent. */
  editable: boolean;
}

export interface Signal {
  name: string;
  text: string;
}

export interface Risk {
  tier: "safe" | "caution" | "risky" | "blocked" | string;
  known: boolean;
  signals: Signal[];
  /** Acknowledgements the core insists on before it will write. */
  required: string[];
}

export interface CloudStep {
  number: number;
  text: string;
  before_editing: boolean;
  done: boolean;
}

export interface Cloud {
  needed: boolean;
  evidence: string[];
  steps: CloudStep[];
}

export interface Backup {
  /** Where the copy was put. */
  folder: string;
  /** How it is listed in `savesmith backups`. */
  label: string;
}

export interface Change {
  field: string;
  before: unknown;
  after: unknown;
}

export interface Session {
  session: string;
  path: string;
  plugin: {
    id: string;
    version: number;
    game: string;
    engine: string;
    confidence: string;
    steam_appid: number | null;
    risk_tier: string;
  };
  risk: Risk;
  cloud: Cloud | null;
  fields: Field[];
  pending: Change[];
  blockers: string[];
  may_write: boolean;
}

export interface Leaf {
  address: string;
  /** The field's own name, as the game wrote it into the file. */
  name: string;
  /** Everything before it in the path, for grouping. */
  group: string;
  value: unknown;
  kind: "number" | "text" | "flag" | "list";
  /** Numbers only, for now: that is all the backend will write. */
  editable: boolean;
}

export interface FieldList {
  format: string;
  structured: boolean;
  fields: Leaf[];
}

export interface Site {
  address: string;
  value: number;
  context: string;
}

export interface SearchResult {
  format: string;
  structured: boolean;
  sites: Site[];
}

export interface PokeResult {
  written: boolean;
  address: string;
  before: unknown;
  after: unknown;
  backup?: string;
  risk: { tier: string; signals: Signal[] } | null;
}

export interface BackupEntry {
  index: number;
  label: string;
  file: string;
  original: string;
  size: number;
}
