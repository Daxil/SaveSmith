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

export function SaveScreen({
  backend,
  session,
  onSession,
  onBack,
  onFailure,
}: {
  backend: Backend;
  session: Session;
  onSession: (session: Session) => void;
  onBack: () => void;
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

  const acknowledge = (item: string) =>
    guard(async () => {
      onSession(await backend.acknowledge(session.session, [item]));
    });

  const confirmCloud = (steps: number[]) =>
    guard(async () => {
      onSession(await backend.confirmCloud(session.session, steps));
    });

  const write = () =>
    guard(async () => {
      const done = await backend.write(session.session);
      setSaved(done.backup);
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

      <Risk session={session} onAcknowledge={acknowledge} />
      {session.cloud?.needed && <CloudSteps session={session} onConfirm={confirmCloud} />}

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

        {saved && <p className="saved">Записано. Копия: {saved}</p>}
      </footer>
    </section>
  );
}

function Risk({
  session,
  onAcknowledge,
}: {
  session: Session;
  onAcknowledge: (item: string) => void;
}) {
  const { risk } = session;
  return (
    <div className={`risk ${risk.tier}`}>
      <h2>Риск: {risk.tier}</h2>
      <ul>
        {risk.signals.map((signal) => (
          <li key={signal.name}>{signal.text}</li>
        ))}
      </ul>
      {risk.required.length > 0 && (
        <div className="acknowledgements">
          {risk.required.map((item) => (
            <button key={item} onClick={() => onAcknowledge(item)}>
              Я прочитал и понимаю: {item}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

function CloudSteps({
  session,
  onConfirm,
}: {
  session: Session;
  onConfirm: (steps: number[]) => void;
}) {
  const cloud = session.cloud;
  if (!cloud) return null;
  const before = cloud.steps.filter((step) => step.before_editing);
  const undone = before.filter((step) => !step.done).map((step) => step.number);

  return (
    <div className="cloud">
      <h2>Steam Cloud</h2>
      <p className="hint">
        Облако может перезаписать правленый сейв старой версией. Сначала эти шаги:
      </p>
      <ol>
        {cloud.steps.map((step) => (
          <li key={step.number} className={step.done ? "done" : ""}>
            {step.text}
          </li>
        ))}
      </ol>
      {undone.length > 0 && (
        <button onClick={() => onConfirm(undone)}>Я это сделал</button>
      )}
    </div>
  );
}
