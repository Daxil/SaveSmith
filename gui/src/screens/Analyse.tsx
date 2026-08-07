/**
 * "Разобрать эту игру" — a button, and everything that used to be a terminal.
 *
 * The person has a game nobody wrote a plugin for. Before this screen their
 * options were to learn what a pipeline is, or to type at an assistant
 * themselves. Now they press a button, answer one question about numbers they
 * can already see in their game, and watch it work. SaveSmith writes the
 * prompt, drives the assistant and installs the result; none of that is the
 * player's job, and none of it is something they would do better than we do.
 *
 * **Two things this screen must not do.**
 *
 * It must not start anything before it has been agreed to. Running this shows
 * parts of the save to somebody else's service, so the sentence saying so is
 * on the same screen as the button, not behind a link, and the backend refuses
 * a call that does not carry the agreement.
 *
 * It must not look frozen. Working a format out takes minutes. A window that
 * sits still for that long has, to anybody watching, crashed — so every step
 * the assistant takes appears as a line as it happens.
 */

import { useEffect, useRef, useState } from "react";

import { note } from "../log";
import { packaged, type AnalysisResult, type Assistant, type Backend, type ProgressLine } from "../rpc";

/**
 * What the player is asked for, and why it is worth asking.
 *
 * A number the person read off their own screen is the difference between a
 * field that is *named* and a field that is *guessed*: the assistant searches
 * the file for it, and whatever holds it is that field. Without one, only
 * fields the game names itself can be trusted, and most binary saves name
 * nothing.
 */
const ASKED = [
  { key: "золото", label: "Золото / деньги" },
  { key: "уровень", label: "Уровень" },
  { key: "здоровье", label: "Здоровье" },
] as const;

export function Analyse({
  backend,
  save,
  gameFolder,
  onDone,
  onBack,
}: {
  backend: Backend;
  save: string;
  gameFolder?: string;
  /** The plugin is installed; the save can be opened by name now. */
  onDone: () => void;
  onBack: () => void;
}) {
  const [assistants, setAssistants] = useState<Assistant[] | null>(null);
  const [chosen, setChosen] = useState<string | null>(null);
  const [agreed, setAgreed] = useState(false);
  const [numbers, setNumbers] = useState<Record<string, string>>({});
  const [lines, setLines] = useState<ProgressLine[]>([]);
  const [running, setRunning] = useState(false);
  const [result, setResult] = useState<AnalysisResult | null>(null);
  const [failure, setFailure] = useState<string | null>(null);
  const bottom = useRef<HTMLLIElement | null>(null);

  useEffect(() => {
    backend
      .assistants()
      .then((answer) => {
        setAssistants(answer.assistants);
        setChosen(answer.assistants[0]?.id ?? null);
      })
      .catch(() => setAssistants([]));
  }, [backend]);

  // The steps arrive as notifications rather than in the answer, because the
  // answer comes minutes later and by then nobody is still watching.
  useEffect(() => {
    if (!packaged()) return;
    let stop: (() => void) | undefined;
    void (async () => {
      const { listen } = await import("@tauri-apps/api/event");
      const unlisten = await listen<ProgressLine>("progress", (event) => {
        if (event.payload?.text) setLines((was) => [...was, event.payload]);
      });
      stop = unlisten;
    })();
    return () => stop?.();
  }, []);

  useEffect(() => {
    bottom.current?.scrollIntoView({ block: "nearest" });
  }, [lines.length]);

  const start = async () => {
    if (!chosen) return;
    setRunning(true);
    setFailure(null);
    setLines([]);
    try {
      const answer = await backend.analyse(save, {
        assistant: chosen,
        consented: agreed,
        gameFolder,
        numbers: Object.fromEntries(
          Object.entries(numbers)
            .map(([key, value]) => [key, Number(value)] as const)
            .filter(([, value]) => Number.isFinite(value) && value !== 0),
        ),
      });
      setResult(answer);
      // In a browser tab there are no notifications, so the log that came back
      // with the answer is the only record of what happened.
      if (lines.length === 0) setLines(answer.log);
    } catch (problem: unknown) {
      const message = problem instanceof Error ? problem.message : String(problem);
      note("info", "разбор не удался:", message);
      setFailure(message);
    } finally {
      setRunning(false);
    }
  };

  if (assistants === null) {
    return (
      <section className="analyse">
        <p className="hint">Смотрю, что установлено…</p>
      </section>
    );
  }

  const list = assistants;

  return (
    <section className="analyse">
      <button className="back" onClick={onBack} disabled={running}>
        ← назад
      </button>

      <h1>Разобрать эту игру</h1>
      <p className="hint">{save}</p>

      {list.length === 0 ? (
        <NothingInstalled />
      ) : result ? (
        <Finished result={result} onDone={onDone} onAgain={() => setResult(null)} />
      ) : (
        <>
          <p>
            Для этой игры никто не написал плагин. SaveSmith может разобраться сам —
            он попросит помощи у {list.length === 1 ? list[0]!.name : "установленного помощника"},
            подберёт разбор файла и, если сойдётся, покажет поля по именам. Тебе
            печатать ничего не нужно.
          </p>

          <h2>Какие числа ты видишь в игре?</h2>
          <p className="hint">
            Не обязательно, но с ними получается гораздо точнее: по числу видно, где
            именно оно лежит в файле. Заполни то, что помнишь, — остальное пропусти.
          </p>
          <ul className="numbers">
            {ASKED.map((asked) => (
              <li key={asked.key}>
                <label htmlFor={`n-${asked.key}`}>{asked.label}</label>
                <input
                  id={`n-${asked.key}`}
                  type="number"
                  inputMode="numeric"
                  placeholder="—"
                  disabled={running}
                  value={numbers[asked.key] ?? ""}
                  onChange={(event) =>
                    setNumbers((was) => ({ ...was, [asked.key]: event.target.value }))
                  }
                />
              </li>
            ))}
          </ul>

          {list.length > 1 && (
            <p>
              Чей помощник:{" "}
              <select value={chosen ?? ""} onChange={(event) => setChosen(event.target.value)}>
                {list.map((one) => (
                  <option key={one.id} value={one.id}>
                    {one.name}
                  </option>
                ))}
              </select>
            </p>
          )}

          <label className="agree">
            <input
              type="checkbox"
              checked={agreed}
              disabled={running}
              onChange={(event) => setAgreed(event.target.checked)}
            />
            <span>
              Понимаю, что для разбора куски моего сохранения уйдут туда, где работает{" "}
              {list.find((one) => one.id === chosen)?.name ?? "помощник"}. Менять файл
              ему нельзя — это делаю только я.
            </span>
          </label>

          <button className="write" disabled={!agreed || running || !chosen} onClick={start}>
            {running ? "Разбираю…" : "Разобрать"}
          </button>

          {running && (
            <p className="hint">
              Это занимает несколько минут. Можно свернуть окно — работа не прервётся.
            </p>
          )}
        </>
      )}

      {failure && <p className="failure">{failure}</p>}

      {lines.length > 0 && (
        <ol className="steps">
          {lines.map((line, index) => (
            <li
              key={index}
              className={line.kind}
              ref={index === lines.length - 1 ? bottom : undefined}
            >
              {line.text}
            </li>
          ))}
        </ol>
      )}
    </section>
  );
}

function Finished({
  result,
  onDone,
  onAgain,
}: {
  result: AnalysisResult;
  onDone: () => void;
  onAgain: () => void;
}) {
  if (!result.installed) {
    return (
      <>
        <h2>Не получилось</h2>
        <p>{result.summary}</p>
        <p className="hint">
          Ничего не изменено. Иногда помогает попробовать ещё раз с другими числами —
          или сделать два сохранения, до и после того как число в игре поменялось.
        </p>
        <button onClick={onAgain}>Попробовать ещё раз</button>
      </>
    );
  }
  return (
    <>
      <h2>Готово</h2>
      <p>{result.summary}</p>
      <p className="hint">
        Плагин установлен на этом компьютере. Проверь в игре, что правки применяются
        так, как ты ждёшь, — и тогда его стоит отдать остальным, чтобы следующему
        человеку с этой игрой уже ничего разбирать не пришлось.
      </p>
      <button className="write" onClick={onDone}>
        Открыть сохранение
      </button>
    </>
  );
}

function NothingInstalled() {
  return (
    <>
      <h2>Помощника на этом компьютере нет</h2>
      <p>
        Чтобы SaveSmith мог разобрать незнакомую игру сам, нужен ИИ-помощник с
        командной строкой — например Claude Code или Codex. Если он у тебя уже есть,
        но SaveSmith его не видит, скорее всего дело в том, где он установлен.
      </p>
      <p className="hint">
        Без помощника остаётся ручной путь: открыть сохранение и поискать в нём число,
        которое видно в игре. Это дольше, но работает и без всякого ИИ.
      </p>
    </>
  );
}
