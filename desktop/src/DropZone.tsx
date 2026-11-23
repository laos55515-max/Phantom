import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type DragEvent,
  type ReactNode,
} from "react";
import { isDemoMode } from "./bridge";
import { getCurrentWebview } from "@tauri-apps/api/webview";

export interface DroppedPath {
  path: string;
  name: string;
  sizeBytes: number | null;
}

/**
 * Drop a file onto this zone.
 *
 * Inside Tauri the webview receives OS drag-drop with absolute paths
 * (`tauri://drag-drop`).  In the browser preview we fall back to HTML5 drop
 * events and hand the caller a `demo://` pseudo-path with the real file name
 * and size.
 */
export function DropZone({
  children,
  onDrop,
  onDragStateChange,
  ariaLabel,
}: {
  children: ReactNode;
  onDrop: (file: DroppedPath) => void;
  onDragStateChange?: (active: boolean) => void;
  ariaLabel: string;
}) {
  const [active, setActive] = useState(false);
  const zoneRef = useRef<HTMLDivElement>(null);
  const depthRef = useRef(0);

  const setBoth = useCallback(
    (value: boolean) => {
      setActive(value);
      onDragStateChange?.(value);
    },
    [onDragStateChange],
  );

  // Tauri OS-level drag & drop (absolute paths).
  useEffect(() => {
    if (isDemoMode()) return undefined;
    let unlisten: (() => void) | undefined;
    let cancelled = false;
    getCurrentWebview()
      .onDragDropEvent((event) => {
        const payload = event.payload;
        if (payload.type === "enter" || payload.type === "over") {
          setBoth(true);
        } else if (payload.type === "leave") {
          setBoth(false);
        } else if (payload.type === "drop") {
          setBoth(false);
          const first = payload.paths[0];
          if (!first) return;
          const segments = first.split(/[\\/]/);
          const name = segments[segments.length - 1] ?? first;
          onDrop({ path: first, name, sizeBytes: null });
        }
      })
      .then((un) => {
        if (cancelled) un();
        else unlisten = un;
      })
      .catch(() => {
        // Webview events unavailable — HTML5 handlers below still work.
      });
    return () => {
      cancelled = true;
      unlisten?.();
    };
  }, [onDrop, setBoth]);

  // HTML5 drag & drop (browser preview / demo).
  const onDragEnter = useCallback((event: DragEvent<HTMLDivElement>) => {
    event.preventDefault();
    depthRef.current += 1;
    setBoth(true);
  }, [setBoth]);
  const onDragOver = useCallback((event: DragEvent<HTMLDivElement>) => {
    event.preventDefault();
    event.dataTransfer.dropEffect = "copy";
    setBoth(true);
  }, [setBoth]);
  const onDragLeave = useCallback(
    (event: DragEvent<HTMLDivElement>) => {
      event.preventDefault();
      depthRef.current = Math.max(0, depthRef.current - 1);
      if (depthRef.current === 0) setBoth(false);
    },
    [setBoth],
  );
  const onDropHtml5 = useCallback(
    (event: DragEvent<HTMLDivElement>) => {
      event.preventDefault();
      depthRef.current = 0;
      setBoth(false);
      const file = event.dataTransfer.files?.[0];
      if (!file) return;
      onDrop({ path: `demo://${file.name}`, name: file.name, sizeBytes: file.size });
    },
    [onDrop, setBoth],
  );

  return (
    <div
      ref={zoneRef}
      role="group"
      aria-label={ariaLabel}
      aria-dropeffect="copy"
      className={`dropzone${active ? " is-active" : ""}`}
      onDragEnter={onDragEnter}
      onDragOver={onDragOver}
      onDragLeave={onDragLeave}
      onDrop={onDropHtml5}
    >
      {children}
    </div>
  );
}
