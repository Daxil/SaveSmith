/**
 * The promises the screens make, checked.
 *
 * Everything here is a guard on somebody's save file rather than a check on
 * how a screen looks. The window owns no rules — the core decides what may be
 * written — but the window is where those decisions are *shown*, and a screen
 * that shows them wrongly is as dangerous as a core that decides wrongly. A
 * Save button that stays clickable when the backend said no, a restore that
 * fires without asking, an assistant handed a save nobody agreed to send:
 * each of those is silent, and none of them were checked by anything until
 * this file existed.
 */

import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { Backend, Session } from "../rpc";
import { Analyse } from "./Analyse";
import { Backups } from "./Backups";
import { SaveScreen } from "./SaveScreen";

/** Whether the control refuses to be used, without another matcher library. */
function disabled(element: HTMLElement): boolean {
  return (element as HTMLButtonElement | HTMLInputElement).disabled;
}

function aSession(over: Partial<Session> = {}): Session {
  return {
    session: "s1",
    path: "/games/user1.dat",
    plugin: {
      id: "coin-quest",
      version: 1,
      game: "Coin Quest",
      engine: "test",
      confidence: "probable",
      steam_appid: null,
      risk_tier: "caution",
    },
    risk: { tier: "caution", known: true, signals: [], required: [] },
    cloud: null,
    fields: [
      {
        address: "gold",
        label: "Золото",
        group: null,
        type: "int",
        value: 100,
        present: true,
        min: 0,
        max: null,
        options: [],
        warn: null,
        advanced: false,
        achievement: false,
        online_linked: false,
        editable: true,
      },
    ],
    pending: [{ field: "gold", before: 100, after: 999 }],
    blockers: [],
    may_write: true,
    ...over,
  };
}

/** A backend that answers nothing, so a screen cannot pass by accident. */
function aBackend(over: Partial<Backend> = {}): Backend {
  return {
    kind: "bridge",
    items: vi.fn().mockResolvedValue({ containers: [], sheets: {} }),
    assistants: vi.fn().mockResolvedValue({ assistants: [] }),
    backups: vi.fn().mockResolvedValue({ backups: [] }),
    ...over,
  } as unknown as Backend;
}

describe("Экран сохранения", () => {
  it("не даёт записать, пока ядро не разрешило", () => {
    const session = aSession({ may_write: false, blockers: ["needs confirmation: ban_risk"] });

    render(
      <SaveScreen
        backend={aBackend()}
        session={session}
        onSession={vi.fn()}
        onBack={vi.fn()}
        onEverything={vi.fn()}
        onBackups={vi.fn()}
        onFailure={vi.fn()}
      />,
    );

    expect(disabled(screen.getByRole("button", { name: "Записать" }))).toBe(true);
    // And says why, in the core's own words rather than a friendlier summary.
    expect(screen.getByText("needs confirmation: ban_risk")).toBeTruthy();
  });

  it("не даёт записать, когда менять нечего", () => {
    render(
      <SaveScreen
        backend={aBackend()}
        session={aSession({ pending: [] })}
        onSession={vi.fn()}
        onBack={vi.fn()}
        onEverything={vi.fn()}
        onBackups={vi.fn()}
        onFailure={vi.fn()}
      />,
    );

    expect(disabled(screen.getByRole("button", { name: "Записать" }))).toBe(true);
  });

  it("поле, которое ядро запретило менять, нельзя ввести", () => {
    const session = aSession();
    session.fields[0]!.editable = false;
    session.fields[0]!.online_linked = true;

    render(
      <SaveScreen
        backend={aBackend()}
        session={session}
        onSession={vi.fn()}
        onBack={vi.fn()}
        onEverything={vi.fn()}
        onBackups={vi.fn()}
        onFailure={vi.fn()}
      />,
    );

    expect(disabled(screen.getByLabelText(/Золото/))).toBe(true);
  });
});

describe("Возврат копии", () => {
  const copies = {
    backups: [
      { index: 0, label: "сегодня в 14:05", file: "/b/0", original: "/games/user1.dat", size: 2048 },
    ],
  };

  it("не откатывает с первого нажатия, а сначала спрашивает", async () => {
    const restore = vi.fn().mockResolvedValue({ restored: true });
    render(
      <Backups
        backend={aBackend({ backups: vi.fn().mockResolvedValue(copies), restore } as never)}
        plugin="coin-quest"
        onBack={vi.fn()}
        onRestored={vi.fn()}
      />,
    );

    await userEvent.click(await screen.findByRole("button", { name: "Вернуть эту" }));

    expect(restore).not.toHaveBeenCalled();
    expect(screen.getByText(/Перезаписать сохранение/)).toBeTruthy();
  });

  it("откатывает только после подтверждения", async () => {
    const restore = vi.fn().mockResolvedValue({ restored: true });
    const onRestored = vi.fn();
    render(
      <Backups
        backend={aBackend({ backups: vi.fn().mockResolvedValue(copies), restore } as never)}
        plugin="coin-quest"
        onBack={vi.fn()}
        onRestored={onRestored}
      />,
    );

    await userEvent.click(await screen.findByRole("button", { name: "Вернуть эту" }));
    await userEvent.click(screen.getByRole("button", { name: "Да, вернуть" }));

    await waitFor(() => expect(restore).toHaveBeenCalledWith("coin-quest", 0));
    // Whatever is showing the old contents has to be told: the file changed.
    await waitFor(() => expect(onRestored).toHaveBeenCalled());
  });

  it("без копий говорит об этом, а не показывает пустоту", async () => {
    render(
      <Backups
        backend={aBackend()}
        plugin="coin-quest"
        onBack={vi.fn()}
        onRestored={vi.fn()}
      />,
    );

    expect(await screen.findByText(/Копий пока нет/)).toBeTruthy();
  });
});

describe("Разбор незнакомой игры", () => {
  const withClaude = {
    assistants: vi.fn().mockResolvedValue({
      assistants: [{ id: "claude", name: "Claude Code", path: "/x/claude" }],
    }),
  };

  it("не отправляет ничего, пока человек не согласился", async () => {
    const analyse = vi.fn();
    render(
      <Analyse
        backend={aBackend({ ...withClaude, analyse } as never)}
        save="/games/user1.dat"
        onDone={vi.fn()}
        onBack={vi.fn()}
      />,
    );

    const button = await screen.findByRole("button", { name: "Разобрать" });

    expect(disabled(button)).toBe(true);
    await userEvent.click(button);
    expect(analyse).not.toHaveBeenCalled();
  });

  it("после согласия отправляет его вместе с числами, которые вписал человек", async () => {
    const analyse = vi.fn().mockResolvedValue({
      installed: true, plugin: "x", summary: "готово", log: [],
    });
    render(
      <Analyse
        backend={aBackend({ ...withClaude, analyse } as never)}
        save="/games/user1.dat"
        onDone={vi.fn()}
        onBack={vi.fn()}
      />,
    );

    await userEvent.type(await screen.findByLabelText("Золото / деньги"), "4200");
    await userEvent.click(screen.getByRole("checkbox"));
    await userEvent.click(screen.getByRole("button", { name: "Разобрать" }));

    await waitFor(() =>
      expect(analyse).toHaveBeenCalledWith(
        "/games/user1.dat",
        expect.objectContaining({ consented: true, numbers: { золото: 4200 } }),
      ),
    );
  });

  it("без помощника объясняет, чего не хватает, вместо мёртвой кнопки", async () => {
    render(
      <Analyse
        backend={aBackend()}
        save="/games/user1.dat"
        onDone={vi.fn()}
        onBack={vi.fn()}
      />,
    );

    expect(await screen.findByText(/Помощника на этом компьютере нет/)).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Разобрать" })).toBeNull();
  });
});
