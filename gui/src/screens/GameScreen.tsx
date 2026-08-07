/**
 * Step two: what was found, and which save to open.
 *
 * Saves the decoder ladder recognised come first and are the only ones offered
 * for editing; the rest are listed so that "nothing found" is never a silent
 * answer. Unity settings appear here too — a game that keeps its coins in the
 * registry is not a game without saves.
 */

import type { FoundGame } from "../rpc";

export function GameScreen({
  found,
  onOpen,
  onBack,
}: {
  found: FoundGame;
  onOpen: (path: string) => void;
  onBack: () => void;
}) {
  const { game, saves, prefs, bottle } = found;
  const readable = saves.filter((save) => save.recognised);
  const rest = saves.filter((save) => !save.recognised);

  return (
    <section className="game">
      <button className="back" onClick={onBack}>
        ← другая игра
      </button>

      <h1>{game.title}</h1>
      <p className="hint">
        Движок: {game.engine}
        {bottle && ` · ${bottle}`}
        {game.anticheat.length > 0 && ` · анти-чит: ${game.anticheat.join(", ")}`}
      </p>

      {readable.length > 0 && (
        <>
          <h2>Сохранения</h2>
          <ul className="saves">
            {readable.map((save) => (
              <li key={save.path}>
                <button onClick={() => onOpen(save.path)}>
                  <span className="name">{basename(save.path)}</span>
                  <span className="meta">
                    {save.format} · {bytes(save.size)}
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

      {readable.length === 0 && !prefs && (
        <p className="empty">
          Ничего похожего на сохранение не нашлось. Проверено:
          <br />
          {found.searched.map((place) => (
            <code key={place}>{place}</code>
          ))}
        </p>
      )}

      {rest.length > 0 && (
        <details>
          <summary>Ещё {rest.length} файл(ов), формат которых не распознан</summary>
          <ul className="saves muted">
            {rest.slice(0, 30).map((save) => (
              <li key={save.path}>
                <span className="name">{basename(save.path)}</span>
                <span className="meta">{bytes(save.size)}</span>
              </li>
            ))}
          </ul>
        </details>
      )}
    </section>
  );
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
