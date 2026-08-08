/**
 * "Скопировать для отчёта" — turning "у меня не работает" into something fixable.
 *
 * A person who hits a problem has exactly one thing to say about it: that it
 * did not work. Everything a maintainer needs to act — which system, which
 * version, what SaveSmith could see, what the error actually said — is either
 * invisible to them or buried in a log file whose location they do not know.
 * So the window collects it and puts it on the clipboard in one press.
 *
 * **What it deliberately does not do is send anything.** It copies, and the
 * person pastes it wherever they choose, having read it. A crash reporter that
 * phones home from a program which reads save files would be a poor trade for
 * a project whose whole argument is that it can be trusted with them.
 *
 * The diagnostics come from `doctor`, the same command the command line has,
 * so a report from the window and a report from a terminal say the same things
 * and are worth the same.
 */

import { useState } from "react";

import type { Backend } from "./rpc";

export function Report({ backend, about }: { backend: Backend; about?: string }) {
  const [copied, setCopied] = useState(false);
  const [text, setText] = useState<string | null>(null);

  const collect = async () => {
    let doctor = "(диагностика не собралась)";
    let version = "неизвестна";
    try {
      version = (await backend.ping()).version;
      doctor = (await backend.doctor()).text;
    } catch (problem: unknown) {
      doctor = `(диагностика не собралась: ${String(problem)})`;
    }

    const report = [
      "## SaveSmith — отчёт о проблеме",
      "",
      `Версия: ${version}`,
      `Система: ${navigator.userAgent}`,
      `Способ связи с ядром: ${backend.kind}`,
      "",
      ...(about ? ["Что произошло:", "", "```", about, "```", ""] : []),
      "Что SaveSmith видит на этом компьютере:",
      "",
      "```",
      doctor.trim(),
      "```",
    ].join("\n");

    try {
      await navigator.clipboard.writeText(report);
      setCopied(true);
      setText(null);
    } catch {
      // No clipboard permission, or an old webview. Showing the text is not a
      // consolation prize: it is the same information, and it can be selected.
      setText(report);
    }
  };

  return (
    <div className="report">
      <button onClick={collect}>
        {copied ? "Скопировано" : "Скопировать сведения для отчёта"}
      </button>
      <span className="note">
        Ничего никуда не отправляется — текст ложится в буфер обмена, и ты решаешь,
        куда его вставить.
      </span>
      {text && <textarea readOnly rows={12} value={text} onFocus={(e) => e.target.select()} />}
    </div>
  );
}
