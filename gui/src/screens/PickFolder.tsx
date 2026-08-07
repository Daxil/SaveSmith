/**
 * Step one: point at the folder the game is installed in.
 *
 * Not at the save file. Nobody knows where their game hides its saves, and
 * being asked for one is where most people give up on a save editor.
 */

import { useState, type FormEvent } from "react";

export function PickFolder({
  onPick,
  busy,
}: {
  onPick: (folder: string) => void;
  busy: boolean;
}) {
  const [folder, setFolder] = useState("");

  function submit(event: FormEvent) {
    event.preventDefault();
    const trimmed = folder.trim().replace(/^["']|["']$/g, "");
    if (trimmed) onPick(trimmed);
  }

  return (
    <form className="pick" onSubmit={submit}>
      <h1>Где установлена игра?</h1>
      <p className="hint">
        Папка, куда установлена игра, — не сейв. Где она прячет сохранения,
        SaveSmith разберётся сам. Можно перетащить папку сюда.
      </p>
      <input
        type="text"
        value={folder}
        placeholder="C:\Games\Coin Quest"
        onChange={(event) => setFolder(event.target.value)}
        onDrop={(event) => {
          event.preventDefault();
          const dropped = event.dataTransfer.files[0];
          if (dropped) setFolder((dropped as File & { path?: string }).path ?? dropped.name);
        }}
        autoFocus
      />
      <button type="submit" disabled={busy || !folder.trim()}>
        Найти сохранения
      </button>
    </form>
  );
}
