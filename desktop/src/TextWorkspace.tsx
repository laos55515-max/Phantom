import { useEffect, useRef, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { useI18n } from "./i18n";

/** Window-sized editor/reader. The underlying screen stays mounted with its draft. */
export function TextWorkspace({ value, onChange, onClose, actions, label }: {
  value: string;
  onChange?: (text: string) => void;
  onClose: () => void;
  actions?: ReactNode;
  label: string;
}) {
  const { t } = useI18n();
  const input = useRef<HTMLTextAreaElement>(null);
  const dialog = useRef<HTMLElement>(null);
  const closeRef = useRef(onClose);
  closeRef.current = onClose;
  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    input.current?.focus();
    const handle = (event: KeyboardEvent) => {
      if (event.key === "Escape") { event.preventDefault(); closeRef.current(); }
      if (event.key === "Tab" && dialog.current) {
        const items = Array.from(dialog.current.querySelectorAll<HTMLElement>("button:not(:disabled), textarea"));
        if (!items.length) return;
        const index = items.indexOf(document.activeElement as HTMLElement);
        if (event.shiftKey && index <= 0) { event.preventDefault(); items[items.length - 1].focus(); }
        else if (!event.shiftKey && (index === -1 || index === items.length - 1)) { event.preventDefault(); items[0].focus(); }
      }
    };
    window.addEventListener("keydown", handle);
    return () => { window.removeEventListener("keydown", handle); previous?.focus(); };
  }, []);
  return createPortal(
    <section ref={dialog} className="text-workspace" role="dialog" aria-modal="true" aria-label={label}>
      <div className="text-workspace-header">
        <strong>{label}</strong>
        <button type="button" className="btn btn-subtle" onClick={onClose}>{t("text.collapse")}</button>
      </div>
      <textarea ref={input} className="textarea text-workspace-input" aria-label={label}
        value={value} readOnly={!onChange} onChange={onChange ? (event) => onChange(event.target.value) : undefined} />
      {actions ? <div className="text-workspace-actions">{actions}</div> : null}
    </section>, document.body,
  );
}
