/**
 * Step two: the save.
 *
 * One rule, learned the hard way: this screen answers the question. It does not
 * hand over a file listing and ask the player to work it out. The Invincible
 * keeps sixty-two rolling backups, four settings files and one save; the older
 * version of this screen showed all sixty-seven, and an Elden Ring save was
 * listed under "format not recognised" directly beneath the words "nothing
 * found".
 *
 * So: the player's saves are shown, everything else is counted in one line, and
 * files that are not saves are never listed at all. The backend decides which
 * is which — `kind` — so the command line and the window agree.
 */

import type { FoundGame, FoundSave } from "../rpc";

const ASIDE_WORDS: Record<string, [string, string]> = {
  backup: ["резервную копию", "резервных копий"],
  settings: ["файл настроек", "файлов настроек"],
  other: ["служебный файл", "служебных файлов"],
};

export function GameScreen({
  found,
  onOpen,
  onBack,
}: {
  found: FoundGame;
  onOpen: (save: FoundSave) => void;
  onBack: () => void;
}) {
  const { game, saves, prefs, bottle } = found;
  const mine = saves.filter((save) => save.kind === "save");
  // A plugin beats the generic ladder: it is what turns bytes into "Руны".
  const editable = mine.filter((save) => save.plugin !== null || save.recognised);
  const byAddress = mine.filter((save) => save.plugin === null && !save.recognised);
  // The same game can be installed twice — two bottles, a reinstall — and then
  // two saves both look right. The newest is the one being played.
  const newest = mine.reduce<number>((best, save) => Math.max(best, save.modified), 0);

  return (
    <section className="game">
      <button className="back" onClick={onBack}>
        ← другая игра
      </button>

      <h1>{game.title}</h1>
      <p className="hint">
        Движок: {game.engine}
        {bottle && ` · в бутылке ${bottle}`}
        {game.anticheat.length > 0 && ` · анти-чит: ${game.anticheat.join(", ")}`}
      </p>

      {editable.length > 0 && (
        <>
          <h2>{editable.length === 1 ? "Сохранение" : "Сохранения"}</h2>
          <ul className="saves">
            {editable.map((save) => (
              <li key={save.path}>
                <button onClick={() => onOpen(save)}>
                  <span className="name">{basename(save.path)}</span>
                  <span className="meta">
                    {bytes(save.size)} · {when(save.modified)}
                    {save.modified === newest && mine.length > 1 && (
                      <span className="tag latest">последний</span>
                    )}
                  </span>
                </button>
              </li>
            ))}
          </ul>
        </>
      )}

      {byAddress.length > 0 && (
        <>
          <h2>
            {byAddress.length === 1 ? "Сохранение" : "Сохранения"} — правится по числу
          </h2>
          <p className="hint">
            Для этой игры никто не описал, что означают байты внутри, поэтому полей
            по именам здесь нет. Зато есть другой путь: говоришь, какое число видишь
            в игре, и SaveSmith находит его в файле.
          </p>
          <ul className="saves">
            {byAddress.map((save) => (
              <li key={save.path}>
                <button onClick={() => onOpen(save)}>
                  <span className="name">{basename(save.path)}</span>
                  <span className="meta">
                    {bytes(save.size)} · {when(save.modified)}
                    {save.modified === newest && mine.length > 1 && (
                      <span className="tag latest">последний</span>
                    )}
                  </span>
                </button>
              </li>
            ))}
          </ul>
        </>
      )}

      {prefs && prefs.entries.length > 0 && (
        <>
          <h2>Настройки Unity</h2>
          <p className="hint">
            Эта игра держит часть прогресса не в файле, а здесь: {prefs.location}
          </p>
          <ul className="prefs">
            {prefs.entries.slice(0, 20).map((entry) => (
              <li key={entry.name}>
                <span className="name">{entry.name}</span>
                <span className="value">{String(entry.value)}</span>
              </li>
            ))}
          </ul>
        </>
      )}

      {mine.length === 0 && !prefs && (
        <p className="empty">
          Сохранений не нашлось. Искал здесь:
          <br />
          {found.searched.map((place) => (
            <code key={place}>{place}</code>
          ))}
        </p>
      )}

      <Aside aside={found.aside} />
    </section>
  );
}

/**
 * Everything that is not the player's save, counted and not listed.
 *
 * It is here so the screen is not silently hiding things — but a count is the
 * right amount of detail. Nobody wants to scroll sixty-two backups.
 */
function Aside({ aside }: { aside: FoundGame["aside"] }) {
  const parts = Object.entries(aside)
    .filter(([, count]) => (count ?? 0) > 0)
    .map(([kind, count]) => {
      const words = ASIDE_WORDS[kind] ?? [kind, kind];
      const n = count ?? 0;
      return `${n} ${n === 1 ? words[0] : words[1]}`;
    });
  if (parts.length === 0) return null;

  return (
    <p className="note aside">
      Рядом лежит ещё {parts.join(", ")} — это файлы самой игры, не твой прогресс.
    </p>
  );
}

/** When the game last wrote it, in words a person reads at a glance. */
function when(seconds: number): string {
  if (!seconds) return "";
  return new Date(seconds * 1000).toLocaleString("ru-RU", {
    day: "numeric",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function basename(path: string): string {
  const parts = path.split(/[\\/]/);
  return parts[parts.length - 1] ?? path;
}

function bytes(size: number): string {
  if (size < 1024) return `${size} Б`;
  if (size < 1024 * 1024) return `${Math.round(size / 1024)} КБ`;
  return `${(size / 1024 / 1024).toFixed(1)} МБ`;
}
