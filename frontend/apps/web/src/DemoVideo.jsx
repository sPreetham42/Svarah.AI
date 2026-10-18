import React from 'react';
import { Link } from 'react-router-dom';
import { ArrowLeft, ArrowRight } from 'lucide-react';
import ThemeToggle from './ThemeToggle';
import './auth.css';

/**
 * The product demo: the launch video, served from `public/demo/`. Opened by "See the interactive demo" on the home page.
 */
export default function DemoVideo() {
  return (
    <div className="auth-page demo-page">
      <ThemeToggle />
      <div className="auth-ambient-glow" />

      <div className="demo-wrap">
        <Link to="/" className="demo-back">
          <ArrowLeft size={16} /> Back to home
        </Link>

        <video
          className="demo-video"
          src="/demo/svarah-demo.mp4"
          poster="/demo/svarah-demo.jpg"
          controls
          autoPlay
          playsInline
        >
          Your browser cannot play this video.
        </video>

        <div className="demo-foot">
          <p>Say your offer once. Svarah.AI hands back the poster and the posts, with every number checked.</p>
          <Link className="button button-auth-primary demo-cta" to="/signup">
            Start creating free <ArrowRight size={16} />
          </Link>
        </div>
      </div>
    </div>
  );
}
