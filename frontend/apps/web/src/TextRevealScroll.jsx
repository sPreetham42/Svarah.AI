import React, { useEffect, useRef } from 'react';
import gsap from 'gsap';
import { ScrollTrigger } from 'gsap/ScrollTrigger';

gsap.registerPlugin(ScrollTrigger);

export default function TextRevealScroll({ children, className = '' }) {
  const containerRef = useRef(null);

  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;

    // Get all text nodes and split them into words/characters wrapped in spans
    const splitText = (node) => {
      if (node.nodeType === Node.TEXT_NODE) {
        const text = node.textContent;
        if (!text.trim()) return;
        
        // One wrapper per text node, so a flex parent with a gap (list items,
        // buttons) lays out a single item instead of one per character.
        const fragment = document.createElement('span');
        fragment.className = 'trs-text';
        // Split on graphemes, not code units, so Indic matras and conjuncts
        // stay attached to their base letter.
        const chars = typeof Intl !== 'undefined' && Intl.Segmenter
          ? Array.from(new Intl.Segmenter(undefined, { granularity: 'grapheme' }).segment(text), (s) => s.segment)
          : Array.from(text);
        chars.forEach((char) => {
          const span = document.createElement('span');
          span.textContent = char;
          span.className = 'trs-char';
          span.style.opacity = '0.2';
          span.style.willChange = 'opacity';
          fragment.appendChild(span);
        });
        node.parentNode.replaceChild(fragment, node);
      } else if (node.nodeType === Node.ELEMENT_NODE && node.nodeName !== 'SCRIPT' && node.nodeName !== 'STYLE' && !node.classList.contains('trs-text')) {
        // Already-split text is skipped: the effect runs twice under StrictMode.
        Array.from(node.childNodes).forEach(splitText);
      }
    };

    // Deep clone children elements into a wrapper, then split text
    const textWrapper = el.querySelector('.trs-content');
    if (textWrapper) {
      Array.from(textWrapper.childNodes).forEach(splitText);

      // Create scroll trigger timeline
      const chars = textWrapper.querySelectorAll('.trs-char');
      if (chars.length > 0) {
        let ctx = gsap.context(() => {
          gsap.to(chars, {
            opacity: 1,
            stagger: 0.1,
            scrollTrigger: {
              trigger: el,
              start: "top 85%",
              end: "center 50%",
              scrub: 1,
            }
          });
        }, el);
        
        return () => ctx.revert();
      }
    }
  }, []);

  return (
    <div ref={containerRef} className={`trs-container ${className}`}>
      <div className="trs-content">
        {children}
      </div>
    </div>
  );
}
