import { useEffect, useRef } from "react";
import { createPortal } from "react-dom";
import { useI18n } from "./i18n";

export function ImageWorkspace({ src, name, onClose, onError }: { src: string; name: string; onClose: () => void; onError: () => void }) {
  const { t } = useI18n();
  const closeButton = useRef<HTMLButtonElement>(null);
  const close = useRef(onClose);
  close.current = onClose;
  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    closeButton.current?.focus();
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") { event.preventDefault(); close.current(); }
      if (event.key === "Tab") { event.preventDefault(); closeButton.current?.focus(); }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => { window.removeEventListener("keydown", onKeyDown); previous?.focus(); };
  }, []);
  return createPortal(
    <section className="text-workspace" role="dialog" aria-modal="true" aria-label={t("extract.image_preview")}>
      <div className="text-workspace-header">
        <strong>{name}</strong>
        <button ref={closeButton} type="button" className="btn btn-subtle" onClick={onClose}>{t("common.close")}</button>
      </div>
      <div className="image-preview-frame"><img src={src} alt={name} onError={onError} /></div>
    </section>, document.body,
  );
}
