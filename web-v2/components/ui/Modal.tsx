// components/ui/Modal.tsx
// 基础弹层（framer-motion 200-300ms，DD-19 §2.4；遮罩点击/ESC 关闭）。
"use client";

import { useEffect, type ReactNode } from "react";
import { AnimatePresence, motion } from "framer-motion";

export interface ModalProps {
  open: boolean;
  onClose: () => void;
  title?: string;
  children: ReactNode;
  footer?: ReactNode;
}

export function Modal({ open, onClose, title, children, footer }: ModalProps) {
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  return (
    <AnimatePresence>
      {open && (
        <motion.div
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          exit={{ opacity: 0 }}
          transition={{ duration: 0.2 }}
          className="fixed inset-0 z-50 flex items-end sm:items-center justify-center bg-black/30"
          onClick={onClose}
        >
          <motion.div
            role="dialog"
            aria-modal="true"
            initial={{ opacity: 0, y: 24 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: 24 }}
            transition={{ duration: 0.25 }}
            className="w-full sm:max-w-md bg-card border border-border rounded-t-card sm:rounded-card
                       max-h-[85vh] overflow-y-auto"
            onClick={(e) => e.stopPropagation()}
          >
            {title && (
              <div className="flex items-center justify-between px-4 pt-4 pb-2">
                <h2 className="font-medium text-primary">{title}</h2>
                <button
                  type="button"
                  onClick={onClose}
                  aria-label="关闭"
                  className="min-w-[44px] min-h-[44px] -mr-2 inline-flex items-center justify-center
                             text-secondary hover:text-primary"
                >
                  ✕
                </button>
              </div>
            )}
            <div className="px-4 py-3">{children}</div>
            {footer && <div className="px-4 pb-4 pt-1">{footer}</div>}
          </motion.div>
        </motion.div>
      )}
    </AnimatePresence>
  );
}
