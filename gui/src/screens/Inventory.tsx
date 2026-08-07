/**
 * The inventory: what the character is carrying, and what could be.
 *
 * Two halves. The left is the save — every stack as a picture with its name
 * under it, because "goods:1007" is exactly as useful to a player as the hex
 * offset it came from. The right is everything the game has, to drag from.
 *
 * **Dragging is not the only way in.** Every tile in the pool is also a
 * button: dropping onto a target is unreliable with a trackpad, impossible
 * from a keyboard, and a screen where the only route to a feature is a drag
 * is a screen some people cannot use at all. The drag is the pleasant path,
 * not the required one.
 *
 * **The drag is done with pointer events, not the HTML5 drag-and-drop API.**
 * That is not a preference. The window listens for a game folder dropped onto
 * it from Finder or Explorer, and Tauri delivers that by taking the webview's
 * drop handling for itself; the two cannot both be had. Turning it off to get
 * HTML5 dragging back here would break the way most people open a game in the
 * first place, to gain a nicer way to do something there is already a button
 * for. So the tile follows the cursor by hand.
 *
 * **The count is the thing most people came for.** Changing 1 to 99 is a far
 * more common wish than adding something new, so the number on each held tile
 * is a plain input rather than something behind a menu.
 *
 * Nothing here decides what may be written. Every call goes to the backend,
 * which stages the change against the same session as the fields above, under
 * the same acknowledgements and the same backup.
 */

import { useCallback, useEffect, useRef, useState } from "react";

import type {
  Backend,
  CatalogItem,
  ContainerView,
  IconRef,
  Session,
  Sheet,
  Stack,
} from "../rpc";

/** How large a tile is drawn, whatever size the game's own icons are. */
const TILE = 44;

export function Inventory({
  backend,
  session,
  onSession,
  onFailure,
}: {
  backend: Backend;
  session: Session;
  onSession: (session: Session) => void;
  onFailure: (message: string) => void;
}) {
  const [containers, setContainers] = useState<ContainerView[] | null>(null);
  const [sheets, setSheets] = useState<Record<string, Sheet>>({});
  const [chosen, setChosen] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [carried, setCarried] = useState<Carried | null>(null);
  const bag = useRef<HTMLElement | null>(null);

  const key = session.session;

  const guard = useCallback(
    async (work: () => Promise<void>) => {
      setBusy(true);
      try {
        await work();
      } catch (problem: unknown) {
        onFailure(problem instanceof Error ? problem.message : String(problem));
      } finally {
        setBusy(false);
      }
    },
    [onFailure],
  );

  useEffect(() => {
    let current = true;
    void (async () => {
      try {
        const found = await backend.items(key);
        if (!current) return;
        setContainers(found.containers);
        setSheets((was) => ({ ...was, ...found.sheets }));
        setChosen((was) => was ?? found.containers[0]?.id ?? null);
      } catch (problem: unknown) {
        // A save with no inventory is the common case, not a failure worth a
        // banner: the section simply does not appear.
        if (current) setContainers([]);
        console.warn("inventory unavailable", problem);
      }
    })();
    return () => {
      current = false;
    };
  }, [backend, key]);

  if (containers === null || containers.length === 0) return null;

  const container = containers.find((one) => one.id === chosen) ?? containers[0];
  if (!container) return null;

  /** Replace one container in place; the session comes back whole from the backend. */
  const absorb = (next: Session & { container: ContainerView }) => {
    setContainers((was) =>
      (was ?? []).map((one) => (one.id === next.container.id ? next.container : one)),
    );
    onSession(next);
  };

  const give = (item: string, count = 1) =>
    guard(async () => absorb(await backend.giveItem(key, container.id, item, count)));

  const setCount = (stack: Stack, count: number) =>
    guard(async () => absorb(await backend.setItemCount(key, container.id, stack.position, count)));

  const remove = (stack: Stack) =>
    guard(async () => absorb(await backend.removeItem(key, container.id, stack.position)));

  return (
    <div className="inventory">
      <h2>Инвентарь</h2>

      {containers.length > 1 && (
        <div className="tabs">
          {containers.map((one) => (
            <button
              key={one.id}
              className={one.id === container.id ? "chosen" : ""}
              onClick={() => setChosen(one.id)}
            >
              {one.label} <span className="count">{one.stacks.length}</span>
            </button>
          ))}
        </div>
      )}

      <div className="inventory-panes">
        <Held
          bag={bag}
          container={container}
          sheets={sheets}
          busy={busy}
          over={carried?.over ?? false}
          onCount={setCount}
          onRemove={remove}
        />
        <Pool
          backend={backend}
          session={key}
          container={container}
          sheets={sheets}
          onSheets={(more) => setSheets((was) => ({ ...was, ...more }))}
          busy={busy}
          onGive={give}
          onCarry={setCarried}
          target={bag}
        />
      </div>

      {carried && (
        <span
          className={`carried${carried.over ? " over" : ""}`}
          style={{ left: carried.x, top: carried.y }}
        >
          <Picture icon={carried.item.icon} sheets={sheets} name={carried.item.name} />
          <span className="name">{carried.item.name}</span>
        </span>
      )}
    </div>
  );
}

/** A tile in mid-flight: what is being dragged, and where the cursor is. */
interface Carried {
  item: CatalogItem;
  x: number;
  y: number;
  /** Over the bag, so letting go would put it in. */
  over: boolean;
}

/** The left half: what is in the save now. Also where a dragged tile lands. */
function Held({
  bag,
  container,
  sheets,
  busy,
  over,
  onCount,
  onRemove,
}: {
  bag: React.MutableRefObject<HTMLElement | null>;
  container: ContainerView;
  sheets: Record<string, Sheet>;
  busy: boolean;
  over: boolean;
  onCount: (stack: Stack, count: number) => void;
  onRemove: (stack: Stack) => void;
}) {
  const [find, setFind] = useState("");

  const shown = find
    ? container.stacks.filter((stack) => stack.name.toLowerCase().includes(find.toLowerCase()))
    : container.stacks;

  return (
    <section ref={bag} className={`held${over ? " over" : ""}`}>
      <header>
        <h3>
          {container.label} <span className="count">{container.stacks.length}</span>
        </h3>
        <input
          type="search"
          placeholder="найти у себя"
          value={find}
          onChange={(event) => setFind(event.target.value)}
        />
      </header>

      {!container.named && (
        <p className="note">
          Названий для этой игры нет, поэтому вещи показаны её собственными
          номерами. Количество это менять не мешает.
        </p>
      )}

      {container.stacks.length === 0 && <p className="note">Здесь пусто.</p>}

      <ul className="tiles">
        {shown.map((stack) => (
          <li key={`${stack.position}`} className="tile">
            <Picture icon={stack.icon} sheets={sheets} name={stack.name} />
            <span className="name" title={stack.description ?? stack.item}>
              {stack.name}
            </span>
            <span className="controls">
              <input
                type="number"
                min={0}
                max={container.max_count}
                defaultValue={stack.count}
                disabled={busy}
                onBlur={(event) => {
                  const wanted = Number(event.target.value);
                  if (wanted !== stack.count) onCount(stack, wanted);
                }}
              />
              <button className="drop" disabled={busy} onClick={() => onRemove(stack)} title="убрать">
                ×
              </button>
            </span>
          </li>
        ))}
      </ul>

      <p className="hint">
        Перетащи вещь справа сюда, чтобы добавить её. Или нажми на неё — это то же самое.
      </p>
    </section>
  );
}

/** The right half: everything the game has. */
function Pool({
  backend,
  session,
  container,
  sheets,
  onSheets,
  busy,
  onGive,
  onCarry,
  target,
}: {
  backend: Backend;
  session: string;
  container: ContainerView;
  sheets: Record<string, Sheet>;
  onSheets: (sheets: Record<string, Sheet>) => void;
  busy: boolean;
  onGive: (item: string) => void;
  onCarry: (carried: Carried | null) => void;
  target: React.MutableRefObject<HTMLElement | null>;
}) {
  const [items, setItems] = useState<CatalogItem[]>([]);
  const [total, setTotal] = useState(0);
  const [named, setNamed] = useState(true);
  const [find, setFind] = useState("");
  const timer = useRef<number | undefined>(undefined);
  /** Set while a real drag happened, so the click that follows is not a second give. */
  const dragged = useRef(false);

  /**
   * Follow the cursor until it is let go.
   *
   * Below the threshold nothing is carried and the click does the work, so a
   * clumsy click is still a click rather than a drag that goes nowhere.
   */
  const carry = (item: CatalogItem, start: React.PointerEvent<HTMLElement>) => {
    if (start.button !== 0 || busy) return;
    const element = start.currentTarget;
    element.setPointerCapture(start.pointerId);
    dragged.current = false;
    const from = { x: start.clientX, y: start.clientY };

    const inside = (x: number, y: number) => {
      const box = target.current?.getBoundingClientRect();
      return !!box && x >= box.left && x <= box.right && y >= box.top && y <= box.bottom;
    };

    const move = (event: PointerEvent) => {
      const far = Math.hypot(event.clientX - from.x, event.clientY - from.y) > 5;
      if (!far && !dragged.current) return;
      dragged.current = true;
      onCarry({
        item,
        x: event.clientX + 12,
        y: event.clientY + 12,
        over: inside(event.clientX, event.clientY),
      });
    };

    const finish = (event: PointerEvent) => {
      element.removeEventListener("pointermove", move);
      element.removeEventListener("pointerup", finish);
      element.removeEventListener("pointercancel", finish);
      onCarry(null);
      if (dragged.current && inside(event.clientX, event.clientY)) onGive(item.item);
    };

    element.addEventListener("pointermove", move);
    element.addEventListener("pointerup", finish);
    element.addEventListener("pointercancel", finish);
  };

  useEffect(() => {
    // Typing should not fire a call per keystroke; the backend reads files.
    window.clearTimeout(timer.current);
    timer.current = window.setTimeout(() => {
      void (async () => {
        try {
          const found = await backend.itemsCatalog(session, container.id, { find, limit: 120 });
          setItems(found.items);
          setTotal(found.total);
          setNamed(found.named);
          onSheets(found.sheets);
        } catch (problem: unknown) {
          console.warn("catalog unavailable", problem);
          setItems([]);
          setNamed(false);
        }
      })();
    }, 150);
    return () => window.clearTimeout(timer.current);
    // onSheets is stable enough for this: it only merges into a state setter.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [backend, session, container.id, find]);

  return (
    <section className="pool">
      <header>
        <h3>Что бывает в игре</h3>
        <input
          type="search"
          placeholder="найти вещь"
          value={find}
          onChange={(event) => setFind(event.target.value)}
        />
      </header>

      {!named ? (
        <p className="note">
          Списка вещей для этой игры нет: у Elden Ring названия и картинки лежат
          внутри запакованных архивов игры, и достать их — отдельная работа.
          Когда рядом с плагином появится файл со списком, вещи покажутся здесь
          сами.
        </p>
      ) : (
        <>
          <ul className="tiles">
            {items.map((item) => (
              <li key={item.item} className={`tile choosable${item.held ? " held" : ""}`}>
                <button
                  disabled={busy}
                  title={item.name}
                  onPointerDown={(event) => carry(item, event)}
                  onClick={() => {
                    // The click that ends a drag is not a second request.
                    if (dragged.current) {
                      dragged.current = false;
                      return;
                    }
                    onGive(item.item);
                  }}
                >
                  <Picture icon={item.icon} sheets={sheets} name={item.name} />
                  <span className="name">{item.name}</span>
                </button>
                {item.held && <span className="tag">уже есть</span>}
              </li>
            ))}
          </ul>
          {total > items.length && (
            <p className="hint">
              Показано {items.length} из {total}. Начни печатать, чтобы сузить.
            </p>
          )}
        </>
      )}
    </section>
  );
}

/**
 * One icon out of a sheet, or a stand-in.
 *
 * The sheet is one image for the whole game; this shows the one square of it
 * that belongs to this thing. When there is no picture the first letter of the
 * name stands in, which at least differs between neighbours.
 */
function Picture({
  icon,
  sheets,
  name,
}: {
  icon: IconRef | null;
  sheets: Record<string, Sheet>;
  name: string;
}) {
  const sheet = icon ? sheets[icon.sheet] : undefined;
  if (!icon || !sheet) {
    return (
      <span className="picture blank" aria-hidden="true">
        {name.slice(0, 1).toUpperCase()}
      </span>
    );
  }

  const column = icon.index % sheet.columns;
  const row = Math.floor(icon.index / sheet.columns);
  return (
    <span
      className="picture"
      aria-hidden="true"
      style={{
        backgroundImage: `url(${sheet.url})`,
        // The sheet is scaled so one of its tiles is exactly TILE across;
        // "auto" keeps the rows in step without needing the image's height.
        backgroundSize: `${sheet.columns * TILE}px auto`,
        backgroundPosition: `-${column * TILE}px -${row * TILE}px`,
      }}
    />
  );
}
