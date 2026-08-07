/**
 * The window.
 *
 * Three screens, in the order a person actually moves through them: point at a
 * game, pick a save, change a number. Everything dangerous — the risk tier, the
 * acknowledgements, the Steam Cloud steps, the backup — is decided by the
 * backend and only displayed here. The Save button is disabled when the backend
 * says `may_write` is false, and the reasons it gives are shown verbatim rather
 * than summarised into something friendlier.
 */

import { useCallback, useEffect, useState } from "react";

import {
  Backend,
  RpcError,
  packaged,
  transportForHere,
  type FoundGame,
  type FoundSave,
  type Session,
} from "./rpc";
import { Updates } from "./Updates";
import { GameScreen } from "./screens/GameScreen";
import { NumberScreen } from "./screens/NumberScreen";
import { PickGame } from "./screens/PickGame";
import { SaveScreen } from "./screens/SaveScreen";

const backend = new Backend(transportForHere());

export type Failure = { message: string } | null;

export function App() {
  const [version, setVersion] = useState<string | null>(null);
  const [folder, setFolder] = useState<string | null>(null);
  const [found, setFound] = useState<FoundGame | null>(null);
  const [session, setSession] = useState<Session | null>(null);
  // A save no plugin describes: edited by number rather than by field.
  const [byNumber, setByNumber] = useState<FoundSave | null>(null);
  const [failure, setFailure] = useState<Failure>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    backend
      .ping()
      .then((answer) => setVersion(answer.version))
      .catch((problem: unknown) => setFailure({ message: describe(problem) }));
  }, []);

  const guard = useCallback(async (work: () => Promise<void>) => {
    setBusy(true);
    setFailure(null);
    try {
      await work();
    } catch (problem: unknown) {
      setFailure({ message: describe(problem) });
    } finally {
      setBusy(false);
    }
  }, []);

  const openFolder = (picked: string) =>
    guard(async () => {
      setSession(null);
      setByNumber(null);
      setFolder(picked);
      setFound(await backend.findSaves(picked));
    });

  const openSave = (save: FoundSave) =>
    guard(async () => {
      // Fields by name when a plugin knows the format; otherwise the only
      // honest route in is the number the player can see on screen.
      if (save.plugin === null && !save.recognised) {
        setByNumber(save);
        return;
      }
      // The folder goes with it: that is where anti-cheat lives, and the risk
      // screen is worth less without it.
      setSession(await backend.open(save.path, folder ?? undefined));
    });

  const leaveSave = () =>
    guard(async () => {
      if (session) await backend.close(session.session);
      setSession(null);
    });

  return (
    <div className="app">
      <header>
        <span className="brand">SaveSmith</span>
        {version && <span className="version">{version}</span>}
        {busy && <span className="busy">…</span>}
      </header>

      <Updates />

      {failure && (
        <div className="failure" role="alert">
          {failure.message}
        </div>
      )}

      <main>
        {byNumber ? (
          <NumberScreen
            backend={backend}
            save={byNumber}
            gameFolder={folder}
            onBack={() => setByNumber(null)}
            onFailure={(message) => setFailure({ message })}
          />
        ) : session ? (
          <SaveScreen
            backend={backend}
            session={session}
            onSession={setSession}
            onBack={leaveSave}
            onEverything={() => {
              const save = found?.saves.find((item) => item.path === session.path);
              if (save) setByNumber(save);
            }}
            onFailure={(message) => setFailure({ message })}
          />
        ) : found ? (
          <GameScreen found={found} onOpen={openSave} onBack={() => setFound(null)} />
        ) : (
          <PickGame
            backend={backend}
            onPick={openFolder}
            busy={busy}
            // Only the packaged app has native dialogs; in a browser tab the
            // text field is all there is.
            canBrowse={packaged()}
            onFailure={(message) => setFailure({ message })}
          />
        )}
      </main>
    </div>
  );
}

function describe(problem: unknown): string {
  if (problem instanceof RpcError) return problem.message;
  if (problem instanceof Error) return problem.message;
  return String(problem);
}
