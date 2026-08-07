/**
 * Step one: pick the game.
 *
 * The screen this replaced asked for a path, and a path is the one thing a
 * player does not have. Steam puts a Windows game in `steamapps/common` inside
 * a bottle inside an `.app`, and nobody should have to know that to give
 * themselves gold.
 *
 * So the list comes first and typing a path is the fallback, not the other way
 * round. What is offered is only what the backend actually found; this screen
 * adds no games of its own and hides none of the ones it was given.
 */

import { useEffect, useMemo, useRef, useState } from "react";

import { browseForFolder, browseForGame, onFolderDropped } from "../native";
import type { Backend, InstalledGame } from "../rpc";

export function PickGame({
  backend,
  onPick,
  busy,
  canBrowse,
  onFailure,
}: {
  backend: Backend;
  onPick: (path: string) => void;
  busy: boolean;
  canBrowse: boolean;
  onFailure: (message: string) => void;
}) {
  const [games, setGames] = useState<InstalledGame[] | null>(null);
  const [problems, setProblems] = useState<string[]>([]);
  const [filter, setFilter] = useState("");
  const [manual, setManual] = useState("");

  // The parent rebuilds these closures on every render. Held in refs so that
  // neither effect below lists them as a dependency: scanning the machine for
  // games once per render would be a great deal of disk for no new answer.
  const latest = useRef({ onPick, onFailure });
  latest.current = { onPick, onFailure };

  useEffect(() => {
    let gone = false;
    backend
      .games()
      .then((answer) => {
        if (gone) return;
        setGames(answer.games);
        setProblems(answer.problems);
      })
      .catch((problem: unknown) => {
        if (gone) return;
        setGames([]);
        latest.current.onFailure(
          problem instanceof Error ? problem.message : String(problem),
        );
      });
    return () => {
      gone = true;
    };
    // Once, on the way in.
  }, [backend]);

  useEffect(() => {
    let stop: (() => void) | undefined;
    let gone = false;
    void onFolderDropped((path) => latest.current.onPick(path)).then((off) =>
      gone ? off() : (stop = off),
    );
    return () => {
      gone = true;
      stop?.();
    };
  }, []);

  const shown = useMemo(() => {
    const wanted = filter.trim().toLowerCase();
    if (!wanted) return games ?? [];
    return (games ?? []).filter((game) => game.name.toLowerCase().includes(wanted));
  }, [games, filter]);

  const grouped = useMemo(() => {
    const groups = new Map<string, InstalledGame[]>();
    for (const game of shown) {
      groups.set(game.source, [...(groups.get(game.source) ?? []), game]);
    }
    return [...groups];
  }, [shown]);

  async function browse(dialog: () => Promise<string | null>) {
    try {
      const picked = await dialog();
      if (picked) onPick(picked);
    } catch (problem: unknown) {
      onFailure(problem instanceof Error ? problem.message : String(problem));
    }
  }

  return (
    <section className="pick-game">
      <h1>Какую игру правим?</h1>

      {games === null && <p className="hint">Ищу установленные игры…</p>}

      {games !== null && games.length > 0 && (
        <>
          <input
            className="filter"
            type="search"
            value={filter}
            placeholder="Поиск по названию"
            onChange={(event) => setFilter(event.target.value)}
            autoFocus
          />

          {grouped.map(([source, entries]) => (
            <div key={source} className="group">
              <h2>{source}</h2>
              <ul className="games">
                {entries.map((game) => (
                  <li key={game.path}>
                    <button disabled={busy} onClick={() => onPick(game.path)}>
                      <span className="name">{game.name}</span>
                      {game.risk_tier && (
                        <span className={`tier ${game.risk_tier}`}>{game.risk_tier}</span>
                      )}
                      {!game.installed && <span className="note">не установлена</span>}
                    </button>
                  </li>
                ))}
              </ul>
            </div>
          ))}

          {shown.length === 0 && <p className="hint">Ничего с таким названием.</p>}
        </>
      )}

      {games !== null && games.length === 0 && (
        <p className="hint">
          Установленных игр не нашлось. SaveSmith смотрит в Steam, в бутылки Wine и
          в программы на маке — всё остальное можно указать вручную.
        </p>
      )}

      {problems.map((problem) => (
        <p key={problem} className="note">
          {problem}
        </p>
      ))}

      <details className="manual">
        <summary>Моей игры здесь нет</summary>
        <p className="hint">
          Укажи саму игру: папку, куда она установлена, её <code>.exe</code> или,
          на маке, её <code>.app</code>. Где она прячет сохранения, SaveSmith
          разберётся сам. Можно перетащить в окно.
        </p>
        {canBrowse && (
          <div className="browsers">
            <button type="button" className="browse" disabled={busy} onClick={() => void browse(browseForGame)}>
              Указать саму игру…
            </button>
            <button type="button" disabled={busy} onClick={() => void browse(browseForFolder)}>
              …или её папку
            </button>
          </div>
        )}
        <form
          onSubmit={(event) => {
            event.preventDefault();
            const trimmed = manual.trim().replace(/^["']|["']$/g, "");
            if (trimmed) onPick(trimmed);
          }}
        >
          <input
            type="text"
            value={manual}
            placeholder="C:\Games\Coin Quest"
            onChange={(event) => setManual(event.target.value)}
          />
          <button type="submit" disabled={busy || !manual.trim()}>
            Найти сохранения
          </button>
        </form>
      </details>
    </section>
  );
}
