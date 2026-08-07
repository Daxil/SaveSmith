/**
 * A save no plugin describes, opened as far as it can honestly be opened.
 *
 * Two cases, and the difference is in the file, not in how hard we try:
 *
 * **The file carries its own names.** A GVAS save says `ObjectiveTime` and
 * `CheckpointName`; RPG Maker says `party._gold`. Those are the developer's
 * words, read out of the file, not a guess — so every one of them is listed
 * with its value and a box to change it. No plugin needed for any of it.
 *
 * **The file is bytes.** Elden Ring's slot decrypts to a flat block with no
 * names anywhere in it. Nothing can list "health" there, because the word
 * health does not exist in the file; labelling byte 0x124 "Руны" would be a
 * guess dressed up as an answer, and a wrong one corrupts a save. What works
 * instead is the number the player can see on screen: they name it, we find it.
 *
 * So the screen shows fields when there are fields, and searching by number
 * when there are not — never a made-up label.
 */

import { useEffect, useState } from "react";

import type { Backend, FoundSave, Leaf, PokeResult, Site } from "../rpc";

export function NumberScreen({
  backend,
  save,
  gameFolder,
  onBack,
  onFailure,
}: {
  backend: Backend;
  save: FoundSave;
  gameFolder: string | null;
  onBack: () => void;
  onFailure: (message: string) => void;
}) {
  const [fields, setFields] = useState<Leaf[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState<PokeResult | null>(null);

  useEffect(() => {
    let gone = false;
    backend
      .fields(save.path)
      .then((answer) => !gone && setFields(answer.fields))
      .catch(() => !gone && setFields([]));
    return () => {
      gone = true;
    };
  }, [backend, save.path]);

  async function guard(work: () => Promise<void>) {
    setBusy(true);
    try {
      await work();
    } catch (problem: unknown) {
      onFailure(problem instanceof Error ? problem.message : String(problem));
    } finally {
      setBusy(false);
    }
  }

  const put = (address: string, value: number) =>
    guard(async () => {
      const result = await backend.poke(save.path, address, value, {
        confirmed: true,
        gameFolder: gameFolder ?? undefined,
      });
      setDone(result);
      const fresh = await backend.fields(save.path);
      setFields(fresh.fields);
    });

  return (
    <section className="number">
      <button className="back" onClick={onBack}>
        ← к сохранениям
      </button>

      <h1>{basename(save.path)}</h1>
      <p className="hint">
        {save.format} · {bytes(save.size)}
      </p>

      {fields === null && <p className="hint">Читаю…</p>}

      {fields !== null && fields.length > 0 && (
        <Fields fields={fields} busy={busy} onPut={put} />
      )}

      {fields !== null && fields.length === 0 && (
        <ByNumber
          backend={backend}
          save={save}
          busy={busy}
          guard={guard}
          onPut={put}
        />
      )}

      {done?.written && (
        <p className="saved">
          Записано: {String(done.before)} → {String(done.after)}. Копия: {done.backup}
        </p>
      )}
    </section>
  );
}

/**
 * Every value in the file, with the name the game gave it.
 *
 * The names are not translated and not prettied up. `ObjectiveTime` is what the
 * developer called it, and a friendlier label invented here would be a guess
 * about meaning that this screen is in no position to make.
 */
function Fields({
  fields,
  busy,
  onPut,
}: {
  fields: Leaf[];
  busy: boolean;
  onPut: (address: string, value: number) => void;
}) {
  const groups = new Map<string, Leaf[]>();
  for (const leaf of fields) {
    groups.set(leaf.group, [...(groups.get(leaf.group) ?? []), leaf]);
  }

  return (
    <>
      <h2>Что лежит в сохранении</h2>
      <p className="hint">
        Имена — те, что записал сам разработчик игры; SaveSmith их не выдумывает и
        не переводит. Числа можно менять прямо здесь: копия делается до записи.
      </p>
      {[...groups].map(([group, leaves]) => (
        <div key={group} className="group">
          {group && <h3>{group}</h3>}
          <ul className="leaves">
            {leaves.map((leaf) => (
              <Field key={leaf.address} leaf={leaf} busy={busy} onPut={onPut} />
            ))}
          </ul>
        </div>
      ))}
    </>
  );
}

function Field({
  leaf,
  busy,
  onPut,
}: {
  leaf: Leaf;
  busy: boolean;
  onPut: (address: string, value: number) => void;
}) {
  const [draft, setDraft] = useState(String(leaf.value ?? ""));
  const changed = draft !== String(leaf.value ?? "");
  const valid = draft.trim() !== "" && Number.isFinite(Number(draft));

  if (!leaf.editable) {
    return (
      <li className="locked">
        <span className="name">{leaf.name}</span>
        <span className="value">{String(leaf.value)}</span>
      </li>
    );
  }

  return (
    <li>
      <span className="name">{leaf.name}</span>
      <span className="row">
        <input
          type="text"
          inputMode="numeric"
          value={draft}
          disabled={busy}
          onChange={(event) => setDraft(event.target.value)}
        />
        <button
          disabled={busy || !changed || !valid}
          onClick={() => onPut(leaf.address, Number(draft))}
        >
          Записать
        </button>
      </span>
    </li>
  );
}

/**
 * The fallback for a save that is genuinely just bytes.
 *
 * Nothing here pretends to know what anything means. The player supplies the
 * meaning — "this number on my screen" — and SaveSmith supplies the arithmetic.
 */
function ByNumber({
  backend,
  save,
  busy,
  guard,
  onPut,
}: {
  backend: Backend;
  save: FoundSave;
  busy: boolean;
  guard: (work: () => Promise<void>) => Promise<void>;
  onPut: (address: string, value: number) => void;
}) {
  const [needle, setNeedle] = useState("");
  const [sites, setSites] = useState<Site[] | null>(null);
  const [chosen, setChosen] = useState<Site | null>(null);
  const [replacement, setReplacement] = useState("");
  const [understood, setUnderstood] = useState(false);

  const look = () =>
    guard(async () => {
      setChosen(null);
      setSites((await backend.search(save.path, Number(needle))).sites);
    });

  const searchable = needle.trim() !== "" && Number.isFinite(Number(needle));
  const writable =
    chosen !== null &&
    understood &&
    replacement.trim() !== "" &&
    Number.isFinite(Number(replacement));

  return (
    <>
      <h2>Имён полей в этом файле нет</h2>
      <p className="hint">
        Внутри — сплошные байты: слова «здоровье» или «руны» в файле не записаны,
        поэтому показать их списком невозможно, а подписать байт наугад — это
        испорченный сейв. Работает другой путь: назови число, которое видишь в
        игре, и SaveSmith найдёт его здесь.
      </p>

      <h3>1. Какое число видно в игре?</h3>
      <p className="hint">
        Руны, деньги, патроны — то, что показано на экране. Игру перед этим
        закрой: она держит сохранение в памяти и, выходя, перезапишет файл своей
        версией.
      </p>
      <form
        className="row"
        onSubmit={(event) => {
          event.preventDefault();
          if (searchable) void look();
        }}
      >
        <input
          type="text"
          inputMode="numeric"
          value={needle}
          placeholder="например 12400"
          onChange={(event) => setNeedle(event.target.value)}
          autoFocus
        />
        <button type="submit" disabled={busy || !searchable}>
          Найти
        </button>
      </form>
      {busy && <p className="hint">Ищу…</p>}

      {sites !== null && sites.length === 0 && (
        <p className="empty">
          Такого числа в файле нет. Проверь, что оно списано с экрана точно и что
          игра успела сохраниться после того, как ты его увидел.
        </p>
      )}

      {sites !== null && sites.length > 0 && (
        <>
          <h3>2. Где оно лежит</h3>
          <p className="hint">
            {sites.length === 1
              ? "Нашлось одно место — скорее всего оно и есть."
              : `Нашлось ${sites.length} мест. Какое из них твоё, снаружи не видно: число вроде 100 встречается в файле повсюду. Если не угадать с первого раза — откатись на копию и попробуй следующее.`}
          </p>
          <ul className="sites">
            {sites.slice(0, 40).map((site) => (
              <li key={site.address}>
                <button
                  className={chosen?.address === site.address ? "picked" : ""}
                  onClick={() => setChosen(site)}
                >
                  <span className="name">{site.address}</span>
                  <span className="meta">{site.context}</span>
                </button>
              </li>
            ))}
          </ul>
        </>
      )}

      {chosen && (
        <>
          <h3>3. На что поменять</h3>
          <form
            className="row"
            onSubmit={(event) => {
              event.preventDefault();
              if (writable) onPut(chosen.address, Number(replacement));
            }}
          >
            <input
              type="text"
              inputMode="numeric"
              value={replacement}
              placeholder="например 99999"
              onChange={(event) => setReplacement(event.target.value)}
            />
            <button type="submit" className="write" disabled={busy || !writable}>
              Записать
            </button>
          </form>

          <div className="risk caution">
            <h2>Что SaveSmith про это не знает</h2>
            <ul>
              <li>
                Про эту игру нет плагина, поэтому неизвестно, что именно означает
                это число и примет ли игра новое значение.
              </li>
              <li>
                Слишком большое значение некоторые игры считают ошибкой и
                отказываются загружать сохранение.
              </li>
              <li>Копия делается до записи автоматически, откатить можно всегда.</li>
            </ul>
            <label className="agree">
              <input
                type="checkbox"
                checked={understood}
                onChange={(event) => setUnderstood(event.target.checked)}
              />
              Я прочитал и понимаю
            </label>
          </div>
        </>
      )}
    </>
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
