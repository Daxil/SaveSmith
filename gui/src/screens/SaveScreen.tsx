/**
 * Step three: change a number.
 *
 * The important part of this screen is what it refuses to do. The Save button
 * follows `may_write` from the backend and nothing else; the reasons it is off
 * are printed as the backend worded them. A field the backend marked
 * `editable: false` is shown but cannot be typed into — the core would refuse
 * the write anyway, and letting someone type a value that will be thrown away
 * is a worse experience than saying why up front.
 */

import { useState } from "react";

import type { Backend, Field, Session } from "../rpc";
import { Inventory } from "./Inventory";

export function SaveScreen({
  backend,
  session,
  onSession,
  onBack,
  onEverything,
  onBackups,
  onFailure,
}: {
  backend: Backend;
  session: Session;
  onSession: (session: Session) => void;
  onBack: () => void;
  /** Show every value in the file, not only the ones the plugin describes. */
  onEverything: () => void;
  /** The copies made before each write, and the way back to one. */
  onBackups: () => void;
  onFailure: (message: string) => void;
}) {
  const [saved, setSaved] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

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

  const change = (field: Field, raw: string) =>
    guard(async () => {
      setSaved(null);
      const value = field.type === "int" || field.type === "float" ? Number(raw) : raw;
      onSession(await backend.change(session.session, field.address, value));
    });

  /** Every confirmation the core is waiting for, from one informed click. */
  const agreeToEverything = () =>
    guard(async () => {
      let next = session;
      if (session.risk.required.length > 0) {
        next = await backend.acknowledge(session.session, session.risk.required);
      }
      const steps = (session.cloud?.steps ?? [])
        .filter((step) => !step.done)
        .map((step) => step.number);
      if (steps.length > 0) {
        next = await backend.confirmCloud(session.session, steps);
      }
      onSession(next);
    });

  const write = () =>
    guard(async () => {
      const done = await backend.write(session.session);
      setSaved(done.backup.folder);
    });

  const groups = new Map<string, Field[]>();
  for (const field of session.fields) {
    const name = field.group ?? "";
    groups.set(name, [...(groups.get(name) ?? []), field]);
  }

  return (
    <section className="save">
      <button className="back" onClick={onBack}>
        ← к списку сохранений
      </button>

      <h1>{session.plugin.game}</h1>
      <p className="hint">{session.path}</p>

      <Risk session={session} onAgree={agreeToEverything} />

      {[...groups].map(([group, fields]) => (
        <div key={group} className="group">
          {group && <h2>{group}</h2>}
          <ul className="fields">
            {fields.map((field) => (
              <li key={field.address} className={field.editable ? "" : "locked"}>
                <label htmlFor={field.address}>
                  {field.label}
                  {field.achievement && <span className="tag">достижения</span>}
                  {field.online_linked && <span className="tag danger">онлайн</span>}
                </label>
                <input
                  id={field.address}
                  defaultValue={String(field.value ?? "")}
                  disabled={!field.editable || !field.present || busy}
                  onBlur={(event) => {
                    if (event.target.value !== String(field.value ?? "")) {
                      void change(field, event.target.value);
                    }
                  }}
                />
                {!field.present && <span className="note">этого нет в этом сейве</span>}
                {field.warn && <span className="note">{field.warn}</span>}
              </li>
            ))}
          </ul>
        </div>
      ))}

      <Inventory
        backend={backend}
        session={session}
        onSession={onSession}
        onFailure={onFailure}
      />

      <footer>
        {session.pending.length > 0 && (
          <ul className="pending">
            {session.pending.map((item) => (
              <li key={item.field}>
                {item.field}: {String(item.before)} → {String(item.after)}
              </li>
            ))}
          </ul>
        )}

        {!session.may_write && session.blockers.length > 0 && (
          <ul className="blockers">
            {session.blockers.map((blocker) => (
              <li key={blocker}>{blocker}</li>
            ))}
          </ul>
        )}

        <button
          className="write"
          disabled={busy || !session.may_write || session.pending.length === 0}
          onClick={write}
        >
          Записать
        </button>

        {saved && (
          <p className="saved">
            Записано. Копия сохранения лежит здесь: {saved}
          </p>
        )}

        {/* Placed beside the Save button on purpose: the moment somebody wants
            this is the moment just after pressing that one. */}
        <button className="everything" onClick={onBackups}>
          Вернуть сохранение как было
        </button>

        {/* The plugin lists what somebody thought worth naming. The file may
            hold more, and hiding it would be its own kind of dishonesty. */}
        <button className="everything" onClick={onEverything}>
          Показать все поля из файла
        </button>
      </footer>
    </section>
  );
}

/**
 * The warning, and one button.
 *
 * Everything here is still enforced by the core — it refuses to write until
 * every acknowledgement is in and the cloud steps are confirmed. What this
 * screen decides is only how many times a person is made to click to say the
 * same thing. Somebody who has come this far to edit an Elden Ring save knows
 * they are choosing to play offline; making them tick that box three times in
 * three panels is not extra safety, it is a worse way of telling them once.
 *
 * So: the reasons are shown in full, in the core's own words, and one button
 * gives every confirmation the core is waiting for.
 */
function Risk({
  session,
  onAgree,
}: {
  session: Session;
  onAgree: () => void;
}) {
  const { risk, cloud } = session;
  const cloudSteps = cloud?.needed
    ? cloud.steps.filter((step) => step.before_editing && !step.done)
    : [];
  const outstanding = risk.required.length > 0 || cloudSteps.length > 0;

  return (
    <div className={`risk ${risk.tier}`}>
      <h2>Прежде чем менять — прочитай</h2>
      <ul>
        {risk.signals.map((signal) => (
          <li key={signal.name}>{signal.text}</li>
        ))}
        {cloudSteps.map((step) => (
          <li key={`cloud-${step.number}`}>{step.text}</li>
        ))}
      </ul>
      {outstanding ? (
        <button className="agree-all" onClick={onAgree}>
          Понимаю и всё равно хочу менять
        </button>
      ) : (
        <p className="saved">Подтверждено. Можно править.</p>
      )}
    </div>
  );
}
