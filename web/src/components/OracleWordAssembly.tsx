/**
 * OracleWordAssembly — word-by-word animated rendering of the Oracle declaration.
 *
 * When `text` changes (new synthesis arrives), words stagger in individually:
 *   - Each word: translateY(6px) opacity:0 → settled opacity:1
 *   - Stagger: 30ms per word
 *   - Duration: 200ms per word
 *
 * Reduced-motion: full text appears at once via opacity fade (120ms),
 * no per-word stagger, no translate.
 *
 * aria-live="polite" on the container is handled by the parent OracleRail.
 * This component handles only the visual assembly.
 */

import { useEffect, useRef, useState } from 'react';

interface OracleWordAssemblyProps {
  text: string;
  /** CSS class for individual word spans */
  wordClassName?: string;
}

interface WordToken {
  word: string;
  index: number;
  /** Key that changes when text changes, so React remounts spans */
  key: string;
}

export function OracleWordAssembly({ text, wordClassName = '' }: OracleWordAssemblyProps): JSX.Element {
  const [tokens, setTokens] = useState<WordToken[]>([]);
  const generationRef = useRef<number>(0);

  useEffect(() => {
    if (!text) { setTokens([]); return; }

    const gen = ++generationRef.current;
    const words = text.split(/\s+/).filter(Boolean);

    // Check prefers-reduced-motion inside effect, safe from SSR / jsdom quirks
    let reduceMotion = false;
    try {
      if (typeof window !== 'undefined' && typeof window.matchMedia === 'function') {
        const mq = window.matchMedia('(prefers-reduced-motion: reduce)');
        reduceMotion = mq != null ? Boolean(mq.matches) : false;
      }
    } catch {
      // matchMedia unavailable — assume motion allowed
    }

    if (reduceMotion) {
      // Reduced motion: all words at once
      setTokens(words.map((w, i) => ({ word: w, index: i, key: `${gen}-${i}` })));
      return;
    }

    // Word-by-word stagger: reveal tokens sequentially
    // We set them all at once (React reconciles) but the CSS animation delay
    // handles the visual stagger via --word-index custom property.
    setTokens(words.map((w, i) => ({ word: w, index: i, key: `${gen}-${i}` })));
  }, [text]);

  if (!tokens.length) return <></>;

  return (
    <>
      {tokens.map((t, i) => (
        <span
          key={t.key}
          className={`oracle-word${wordClassName ? ` ${wordClassName}` : ''}`}
          style={{ '--word-index': i } as React.CSSProperties}
          aria-hidden={false}
        >
          {t.word}{' '}
        </span>
      ))}
    </>
  );
}
