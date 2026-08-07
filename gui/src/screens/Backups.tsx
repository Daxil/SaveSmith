/**
 * "Верни как было" — the screen somebody opens in a hurry.
 *
 * SaveSmith has made a copy before every write since the first milestone, and
 * until now the only way to use one was to install the command line and learn
 * a command — in exactly the moment somebody has just broken a save they care
 * about and is in no state to learn anything. The safety net existed and was
 * out of reach of the person falling.
 *
 * Two things this screen owes them.
 *
 * **Say which copy is which.** A list of timestamps is not an answer to "which
 * one was before I ruined it". So the newest is marked, each row says how long
 * ago and how big, and the file it came from is shown in full: somebody with
 * two characters needs to see which one they are about to overwrite.
 *
 * **Say what restoring costs.** It overwrites the save that is there now — and
 * the core makes a copy of *that* first, which turns a misclick from a
 * catastrophe into one more row in this list. That is worth saying out loud
 * rather than leaving as a pleasant surprise nobody discovers.
 */

import { useEffect, useState } from "react";

import type { Backend, BackupEntry } from "../rpc";

export function Backups({
  backend,
  plugin,
  onBack,
  onRestored,
}: {
  backend: Backend;
  /** Backups are kept per plugin, which is how the core files them. */
  plugin: string;
  onBack: () => void;
  /** The file on disk changed; whatever is showing it should re-read it. */
  onRestored: () => void;
}) {
  const [backups, setBackups] = useState<BackupEntry[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState<string | null>(null);
  const [failure, setFailure] = useState<string | null>(null);
  const [asking, setAsking] = useState<number | null>(null);

  const load = () =>
    backend
      .backups(plugin)
      .then((answer) => setBackups(answer.backups))
      .catch((problem: unknown) => {
        setBackups([]);
        setFailure(problem instanceof Error ? problem.message : String(problem));
      });

  useEffect(() => {
    void load();
    // `plugin` is the only thing this depends on; `load` is redefined per render.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [backend, plugin]);

  const restore = async (index: number) => {
    setBusy(true);
    setFailure(null);
    try {
      await backend.restore(plugin, index);
      setDone(backups?.[index]?.label ?? null);
      setAsking(null);
      await load();
      onRestored();
    } catch (problem: unknown) {
      setFailure(problem instanceof Error ? problem.message : String(problem));
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="backups">
      <button className="back" onClick={onBack} disabled={busy}>
        ← назад
      </button>

      <h1>Копии сохранений</h1>
      <p className="hint">
        SaveSmith делает копию перед каждой записью. Здесь их можно вернуть на место.
      </p>

      {failure && <p className="failure">{failure}</p>}

      {done && (
        <p className="saved">
          Готово, сохранение от «{done}» вернулось на место. То, что было до этого,
          тоже сохранено — оно теперь первое в списке, так что откатить откат можно.
        </p>
      )}

      {backups === null ? (
        <p className="hint">Смотрю…</p>
      ) : backups.length === 0 ? (
        <p className="hint">
          Копий пока нет — SaveSmith ещё ничего не записывал в сохранения этой игры.
        </p>
      ) : (
        <ul className="backup-list">
          {backups.map((backup, index) => (
            <li key={backup.file}>
              <div className="what">
                <span className="when">
                  {backup.label}
                  {index === 0 && <span className="tag latest">последняя</span>}
                </span>
                <span className="meta">
                  {bytes(backup.size)} · вернётся в {backup.original}
                </span>
              </div>

              {asking === index ? (
                <div className="confirm">
                  <span>Перезаписать сохранение этой копией?</span>
                  <button className="write" disabled={busy} onClick={() => restore(index)}>
                    Да, вернуть
                  </button>
                  <button disabled={busy} onClick={() => setAsking(null)}>
                    Отмена
                  </button>
                </div>
              ) : (
                <button disabled={busy} onClick={() => setAsking(index)}>
                  Вернуть эту
                </button>
              )}
            </li>
          ))}
        </ul>
      )}

      <p className="hint">
        Возврат перезаписывает то сохранение, которое лежит на месте сейчас, — но
        сначала делает копию и с него. Промахнуться безвозвратно тут нельзя.
      </p>
    </section>
  );
}

function bytes(size: number): string {
  if (size < 1024) return `${size} Б`;
  if (size < 1024 * 1024) return `${Math.round(size / 1024)} КБ`;
  return `${(size / 1024 / 1024).toFixed(1)} МБ`;
}
