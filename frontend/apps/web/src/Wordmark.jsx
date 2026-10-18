import React from 'react';
import { Link } from 'react-router-dom';
import './wordmark.css';

/**
 * The Svarah.AI wordmark — the single source of truth for the brand lockup.
 *
 * Text-only, exactly the style the marketing top bar already used, so every
 * surface (top bar, footer, login, signup, workspace) renders the same thing.
 * Colour is inherited, so it reads correctly on light and dark surfaces.
 */

const SIZES = { sm: 18, md: 22, lg: 26 };

export default function Wordmark({ size = 'md', className = '', to = '/' }) {
  return (
    <Link
      to={to}
      className={`wm wm--${SIZES[size] ? size : 'md'}${className ? ` ${className}` : ''}`}
      aria-label="Svarah.AI — home"
    >
      SVARAH.AI
    </Link>
  );
}
