/**
 * What the window shows when the window itself is broken.
 *
 * React unmounts the entire tree when a render throws, and an unmounted tree is
 * a black rectangle. That is the worst possible failure: the user cannot tell a
 * crash from a hang from a save that quietly did nothing, and there is nothing
 * to report. It happened here for a real and stupid reason — the backend sent
 * the backup as an object, the screen rendered it as a string — and the only
 * symptom anybody had was "the screen goes dark".
 *
 * So the tree is wrapped. A crash now says so, in words, with the message kept
 * on screen and a copy written to the log.
 */

import { Component, type ErrorInfo, type ReactNode } from "react";

import { note } from "./log";

export class Boundary extends Component<{ children: ReactNode }, { failed: Error | null }> {
  state: { failed: Error | null } = { failed: null };

  static getDerivedStateFromError(failed: Error): { failed: Error } {
    return { failed };
  }

  componentDidCatch(failed: Error, info: ErrorInfo): void {
    note("error", "окно упало на отрисовке:", failed, info.componentStack ?? "");
  }

  render(): ReactNode {
    const { failed } = this.state;
    if (!failed) return this.props.children;

    return (
      <div className="app">
        <div className="failure" role="alert">
          <p>
            <strong>Окно сломалось.</strong> Сохранение при этом не пострадало:
            SaveSmith пишет файл только после того, как сделает копию, и никогда
            во время отрисовки экрана.
          </p>
          <p className="note">{failed.message}</p>
          <button onClick={() => this.setState({ failed: null })}>Попробовать снова</button>
        </div>
      </div>
    );
  }
}
