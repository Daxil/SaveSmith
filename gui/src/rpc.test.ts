/**
 * The transport, which is the only part of the window with logic worth testing.
 *
 * The screens display what the backend says and forward what the user typed;
 * the interesting failures live here, in what happens when the backend answers
 * with an error, or does not answer at all.
 */

import { afterEach, describe, expect, it, vi } from "vitest";

import { Backend, BridgeTransport, RpcError } from "./rpc";

function answering(body: unknown, ok = true, status = 200) {
  const fetching = vi.fn(async () => ({
    ok,
    status,
    json: async () => body,
  }));
  vi.stubGlobal("fetch", fetching);
  return fetching;
}

function sentBody(fetching: ReturnType<typeof answering>, call = 0) {
  const options = fetching.mock.calls[call]?.[1] as { body: string } | undefined;
  return JSON.parse(options?.body ?? "{}");
}

afterEach(() => vi.unstubAllGlobals());

describe("BridgeTransport", () => {
  it("unwraps a result", async () => {
    answering({ jsonrpc: "2.0", id: 1, result: { ok: true } });
    expect(await new BridgeTransport().send("ping", {})).toEqual({ ok: true });
  });

  it("turns an error answer into a thrown sentence", async () => {
    answering({
      jsonrpc: "2.0",
      id: 1,
      error: { code: -32602, message: "Этой игры нет в базе рисков." },
    });

    await expect(new BridgeTransport().send("open", {})).rejects.toThrow(
      "Этой игры нет в базе рисков.",
    );
  });

  it("keeps the error code, so a caller can tell them apart", async () => {
    answering({ jsonrpc: "2.0", id: 1, error: { code: -32601, message: "нет такого метода" } });

    await expect(new BridgeTransport().send("nope", {})).rejects.toMatchObject({
      code: -32601,
    });
  });

  it("says the backend is not running rather than leaking a fetch failure", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        throw new TypeError("Failed to fetch");
      }),
    );

    await expect(new BridgeTransport().send("ping", {})).rejects.toThrow(/не отвечает/);
  });

  it("reports an HTTP failure with its status", async () => {
    answering({}, false, 503);
    await expect(new BridgeTransport().send("ping", {})).rejects.toMatchObject({ code: 503 });
  });

  it("numbers its requests, so answers cannot be mistaken for each other", async () => {
    const fetching = answering({ result: null });
    const transport = new BridgeTransport();
    await transport.send("ping", {});
    await transport.send("ping", {});

    expect(sentBody(fetching, 0).id).toBe(1);
    expect(sentBody(fetching, 1).id).toBe(2);
  });
});

describe("Backend", () => {
  it("asks in the chosen language without every screen remembering to", async () => {
    const fetching = answering({ result: {} });
    const backend = new Backend(new BridgeTransport());

    await backend.open("/games/save.dat");
    expect(sentBody(fetching).params.language).toBe("ru");

    backend.language = "en";
    await backend.open("/games/save.dat");
    expect(sentBody(fetching, 1).params.language).toBe("en");
  });

  it("passes the game folder along, because that is where anti-cheat lives", async () => {
    const fetching = answering({ result: {} });

    await new Backend(new BridgeTransport()).open("/games/save.dat", "/games/Coin Quest");

    expect(sentBody(fetching).params).toMatchObject({
      path: "/games/save.dat",
      game_folder: "/games/Coin Quest",
    });
  });

  it("uses the names the backend uses, not prettier ones", async () => {
    const fetching = answering({ result: {} });

    await new Backend(new BridgeTransport()).change("s1", "party._gold", 999);

    expect(sentBody(fetching).method).toBe("set");
    expect(sentBody(fetching).params).toMatchObject({
      session: "s1",
      field: "party._gold",
      value: 999,
    });
  });

  it("cannot be told to write without a session", async () => {
    const fetching = answering({ result: { written: true } });
    await new Backend(new BridgeTransport()).write("s1");
    expect(sentBody(fetching).params.session).toBe("s1");
  });
});

describe("RpcError", () => {
  it("is an Error, so it survives being caught generically", () => {
    const failure = new RpcError("нельзя", -1);
    expect(failure).toBeInstanceOf(Error);
    expect(failure.message).toBe("нельзя");
  });
});
