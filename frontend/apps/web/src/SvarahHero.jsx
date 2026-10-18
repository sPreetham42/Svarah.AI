import React, { useEffect, useRef } from 'react';
import { gsap } from 'gsap';
import { ScrollTrigger } from 'gsap/ScrollTrigger';
import { ArrowRight, Check, Play, Sparkles, X, Menu, Search, Image as ImageIcon, Video, Calendar, User, UploadCloud, Link as LinkIcon, Smartphone, Settings, Heart, MessageCircle, Send, Bookmark, MoreHorizontal, Share2, Music, Repeat2, BarChart2, Share, ThumbsUp, MessageSquare, Globe2 } from 'lucide-react';
import './svarah.css';
import { Link, NavLink, useLocation } from 'react-router-dom';
import Wordmark from './Wordmark.jsx';

import { useAuth } from './AuthContext';

gsap.registerPlugin(ScrollTrigger);

export function SvarahHeader() {
  const [open, setOpen] = React.useState(false);
  const loc = useLocation();
  const { user } = useAuth();
  
  useEffect(() => setOpen(false), [loc.pathname]);
  
  return (
    <header className="site-header svarah-header">
      <div className="nav-wrap">
        <Wordmark size="lg" />
        <nav className={open ? 'nav-links open' : 'nav-links'}>
          <div className="mobile-nav-cta">
            <Link className="nav-link" to="/download">Get the Android app</Link>
            {user ? (
              <Link className="button btn-lime" to="/workspace">Open workspace <ArrowRight size={16}/></Link>
            ) : (
              <>
                <Link className="nav-link" to="/login">Log in</Link>
                <Link className="button btn-lime" to="/signup">Start Free <ArrowRight size={16}/></Link>
              </>
            )}
          </div>
        </nav>
        <div className="nav-actions">
          <Link className="nav-link nav-download hide-mobile" to="/download"><Smartphone size={15}/> Get the app</Link>
          {user ? (
            <Link className="button btn-lime nav-cta" to="/workspace">
              <span style={{width: 20, height: 20, borderRadius: '50%', background: '#171814', color: '#d8f878', display: 'grid', placeItems: 'center', fontSize: 10, fontWeight: 700}}>
                {user.avatar || 'U'}
              </span>
              <span>Workspace</span>
              <ArrowRight size={15}/>
            </Link>
          ) : (
            <>
              <Link className="nav-link hide-mobile" to="/login">Log in</Link>
              <Link className="button btn-lime nav-cta hide-mobile" to="/signup">Start Free <ArrowRight size={16}/></Link>
            </>
          )}
          <button className="menu-button" onClick={() => setOpen(v => !v)}>
            {open ? <X/> : <Menu/>}
          </button>
        </div>
      </div>
    </header>
  );
}

export function SvarahHero() {
  const container = useRef(null);
  
  useEffect(() => {
    let ctx = gsap.context(() => {
      const tl = gsap.timeline();
      
      tl.from('.svarah-eyebrow', { y: 20, opacity: 0, duration: 0.6, ease: "power3.out" })
        .to('.reveal-char', { opacity: 1, stagger: 0.04, duration: 0.1, ease: "none" }, "-=0.4")
        .from('.svarah-sub', { y: 20, opacity: 0, duration: 0.6, ease: "power3.out" }, "-=0.6")
        .from('.svarah-actions', { y: 20, opacity: 0, duration: 0.6, ease: "power3.out" }, "-=0.5")
        .from('.svarah-checks', { y: 20, opacity: 0, duration: 0.6, ease: "power3.out" }, "-=0.4")
        .from('.visual-item', { 
           y: 100, 
           opacity: 0, 
           duration: 1, 
           stagger: 0.08, 
           ease: "back.out(1.2)" 
        }, "-=0.2");
        
      gsap.to('.float-slow', { y: "-=18", duration: 3.5, yoyo: true, repeat: -1, ease: "sine.inOut" });
      gsap.to('.float-med', { y: "+=15", duration: 2.8, yoyo: true, repeat: -1, ease: "sine.inOut", delay: 0.5 });
      gsap.to('.float-fast', { y: "-=12", duration: 2.2, yoyo: true, repeat: -1, ease: "sine.inOut", delay: 1 });
      
    }, container);
    
    return () => ctx.revert();
  }, []);

  return (
    <section className="svarah-hero" ref={container}>
      <div className="svarah-hero-inner">
        
        <div className="svarah-hero-copy">
          <div className="svarah-eyebrow">VOICE FOR BUSINESS</div>
          <h1 className="svarah-headline">
            {"CREATE ONCE.".split('').map((char, i) => (
              <span key={`l1-${i}`} className="reveal-char" style={{opacity: 0.2}}>{char}</span>
            ))}
            <br/>
            {"PUBLISH EVERYWHERE.".split('').map((char, i) => (
              <span key={`l2-${i}`} className="reveal-char" style={{opacity: 0.2}}>{char}</span>
            ))}
          </h1>
          <p className="svarah-sub">Speak, and Svarah.AI turns it into polished content - ready to publish across every channel.</p>
          <div className="svarah-actions">
            <Link className="button btn-lime" to="/signup">Start Creating Free <ArrowRight size={17}/></Link>
            <Link className="button btn-demo" to="/web-demo"><Play fill="currentColor" size={14}/> See the interactive demo</Link>
            <Link className="button btn-demo" to="/download"><Smartphone size={15}/> Download the Android app</Link>
          </div>
          <div className="svarah-checks">
            <span><span className="tick-dot"/> Social media</span>
            <span><span className="tick-dot"/> Ads</span>
            <span><span className="tick-dot"/> Email campaigns</span>
            <span><span className="tick-dot"/> Websites</span>
            <span><span className="tick-dot"/> All in one place</span>
          </div>
        </div>

        <div className="svarah-hero-visuals">
          
          {/* Main Central App Mockup */}
          <div className="visual-item parallax-layer-1 mock-app">
            <div className="app-header">
              <span className="app-logo">SVARAH.AI <span className="slash">/</span></span>
              <div className="app-search">
                <Search size={12}/>
                <div className="search-dots"><i/><i/><i/><i/><i/></div>
              </div>
              <div className="app-controls">
                <div className="app-avatars"><User size={12}/><Settings size={12}/></div>
                <button className="btn-outline">Preview</button>
                <button className="btn-solid">Publish</button>
              </div>
            </div>
            
            <div className="app-body">
              <div className="app-sidebar">
                <div className="sb-item active"><Calendar size={14}/> Campaigns</div>
                <div className="sb-item"><ImageIcon size={14}/> Templates</div>
                <div className="sb-item"><UploadCloud size={14}/> Brand Kit</div>
                <div className="sb-item"><Video size={14}/> Media</div>
                <div className="sb-item"><Settings size={14}/> Analytics</div>
              </div>
              
              <div className="app-main">
                <div className="app-assets">
                  <div className="asset-card active">
                    <div className="asset-img product-img-1"></div>
                  </div>
                  <div className="asset-card">
                    <div className="asset-img product-img-2"></div>
                  </div>
                  <div className="asset-card">
                    <div className="asset-img product-img-3"></div>
                  </div>
                </div>
                
                <div className="app-canvas-area">
                   <div className="app-canvas">
                      <div className="canvas-img"></div>
                      <div className="canvas-text-box">
                        <div className="box-corners"><i/><i/><i/><i/></div>
                        <h2>Automate<br/>Your Marketing</h2>
                        <p>Create. Customize. Publish.<br/>Everywhere.</p>
                        <button className="canvas-btn">Get Started {'->'}</button>
                      </div>
                   </div>
                   
                   <div className="app-timeline">
                      <div className="tl-head"><Play size={10} fill="currentColor"/> 00:03 / 00:15</div>
                      <div className="tl-tracks">
                        <div className="tl-track"><div className="tl-clip clip-1"></div><div className="tl-clip clip-2"></div></div>
                        <div className="tl-track"><div className="tl-clip clip-3"></div></div>
                        <div className="tl-track"><div className="tl-clip clip-4"></div><div className="tl-clip clip-5"></div></div>
                      </div>
                   </div>
                </div>
                
                <div className="app-properties">
                  <div className="prop-tabs"><span>Design</span><span>Animate</span><span>Export</span></div>
                  <div className="prop-group">
                    <label>Text</label>
                    <input type="text" value="Automate Your Marketing" readOnly/>
                  </div>
                  <div className="prop-group">
                    <label>Style</label>
                    <div className="prop-selects">
                      <select><option>Inter</option></select>
                      <select><option>72</option></select>
                    </div>
                  </div>
                  <div className="prop-group">
                    <label>Color</label>
                    <div className="color-swatches">
                      <i style={{background:'#d8f878'}}></i><i style={{background:'#fff'}}></i><i style={{background:'#000'}}></i>
                    </div>
                  </div>
                </div>
              </div>
            </div>
          </div>

          {/* Floating Phone: Social feed */}
          <div className="visual-item parallax-layer-2 float-slow phone-ig">
            <div className="phone-mock">
              <div className="phone-head ig-head">
                <span className="ig-logo">Social</span>
                <span className="ig-icons"><Heart size={16}/><MessageCircle size={16}/></span>
              </div>
              <div className="ph-body">
                <div className="ph-post-header">
                  <span className="ph-avatar"></span>
                  <div className="ig-user-info"><b>svarah.ai</b><span>Sponsored</span></div>
                  <MoreHorizontal size={14} className="ig-more"/>
                </div>
                <div className="ph-content green-post">
                  <div className="ph-product-small product-img-2"></div>
                </div>
                <div className="ig-action-bar">
                  <div className="ig-actions-left"><Heart size={16}/><MessageCircle size={16}/><Send size={16}/></div>
                  <Bookmark size={16}/>
                </div>
                <div className="ig-likes">2,412 likes</div>
                <p className="ph-caption"><b>svarah.ai</b> Create, automate and publish your marketing content...</p>
              </div>
            </div>
          </div>

          {/* Floating Phone: Short video */}
          <div className="visual-item parallax-layer-3 float-fast phone-tk">
            <div className="phone-mock dark-mock">
               <div className="ph-body full-bg product-img-1">
                 <div className="ph-overlay tk-overlay">
                   <div className="ph-tk-right">
                     <span className="ph-avatar tk-avatar"></span>
                     <div className="tk-action"><Heart size={20} fill="#fff"/><span>24.8K</span></div>
                     <div className="tk-action"><MessageCircle size={20} fill="#fff"/><span>320</span></div>
                     <div className="tk-action"><Bookmark size={20} fill="#fff"/><span>1.2K</span></div>
                     <div className="tk-action"><Share2 size={20}/><span>Share</span></div>
                     <div className="tk-music-disc"><Music size={12}/></div>
                   </div>
                   <div className="ph-tk-bottom">
                     <div className="ph-tk-user"><b>@svarah.ai</b></div>
                     <p>AI-powered content for every channel. #marketing #ai</p>
                     <div className="tk-music-ticker"><Music size={10}/> <span>Original sound - svarah.ai</span></div>
                   </div>
                 </div>
               </div>
            </div>
          </div>

          {/* Floating Phone: Microblog */}
          <div className="visual-item parallax-layer-2 float-med phone-tw">
             <div className="phone-mock dark-mock tw-mock">
               <div className="tw-top-bar">
                 <span className="ph-avatar"></span>
                 <svg viewBox="0 0 24 24" className="tw-x-logo"><path d="M18.244 2.25h3.308l-7.227 8.26 8.502 11.24H16.17l-5.214-6.817L4.99 24.75H1.68l7.73-8.835L1.254 2.25H8.08l4.713 6.231zm-1.161 17.52h1.833L7.007 3.75H5.059z" fill="currentColor"></path></svg>
                 <Settings size={14}/>
               </div>
               <div className="ph-body tw-body">
                  <div className="ph-tw-post">
                    <div className="ph-tw-head">
                      <span className="ph-avatar"></span>
                      <div className="tw-user-info"><b>Svarah.AI</b> <span>@svarahai · 2h</span></div>
                      <MoreHorizontal size={14} className="tw-more"/>
                    </div>
                    <p>One idea. Endless possibilities.<br/>Create, customize and publish your marketing content with AI.</p>
                    <div className="ph-tw-card">
                      <div className="ph-product-mini product-img-3"></div>
                      <div className="tw-card-text">
                        <small>svarah.ai</small>
                        <h3>Automate Your Marketing</h3>
                      </div>
                    </div>
                    <div className="ph-tw-actions">
                      <span><MessageCircle size={13}/> 12</span>
                      <span><Repeat2 size={13}/> 128</span>
                      <span><Heart size={13}/> 1.2K</span>
                      <span><BarChart2 size={13}/> 45K</span>
                      <span><Share size={13}/></span>
                    </div>
                  </div>
               </div>
             </div>
          </div>

          {/* Floating Phone: Community */}
          <div className="visual-item parallax-layer-3 float-slow phone-fb">
            <div className="phone-mock">
              <div className="ph-fb-head">
                <span className="fb-logo">Social</span> 
                <div className="fb-head-icons">
                  <Search size={16}/>
                  <MessageCircle size={16}/>
                </div>
              </div>
              <div className="ph-body fb-body">
                <div className="ph-post-header fb-post-header">
                  <span className="ph-avatar"></span>
                  <div className="fb-user-info"><b>Svarah.AI</b><span>Sponsored · <Globe2 size={10}/></span></div>
                  <MoreHorizontal size={14} className="fb-more"/>
                </div>
                <p className="ph-fb-text">Build your brand. Grow faster. Let AI handle your marketing content across every channel.</p>
                <div className="ph-content sand-post">
                  <div className="ph-product-small product-img-1"></div>
                  <div className="fb-card-bottom">
                    <small>SVARAH.AI</small>
                    <h3>Automate Your Marketing</h3>
                    <button className="fb-btn">Learn more</button>
                  </div>
                </div>
                <div className="fb-reactions">
                  <span className="fb-react-icons"><ThumbsUp size={10} fill="#fff"/></span> 2.4K
                  <span className="fb-comments">128 comments · 45 shares</span>
                </div>
                <div className="fb-action-bar">
                  <span><ThumbsUp size={14}/> Like</span>
                  <span><MessageSquare size={14}/> Comment</span>
                  <span><Share2 size={14}/> Share</span>
                </div>
              </div>
            </div>
          </div>
          
          {/* Floating Phone: Professional */}
          <div className="visual-item parallax-layer-1 float-fast phone-li">
             <div className="phone-mock">
              <div className="ph-li-head">
                <span className="li-avatar-mini"></span>
                <div className="li-search">
                  <Search size={12}/> Search
                </div>
                <MessageSquare size={16} className="li-msg"/>
              </div>
              <div className="ph-body li-body">
                <div className="ph-post-header li-post-header">
                  <span className="ph-avatar li-avatar"></span>
                  <div className="li-user-info">
                    <b>Svarah.AI</b>
                    <small>24,832 followers</small>
                    <small>Promoted</small>
                  </div>
                  <MoreHorizontal size={14} className="li-more"/>
                </div>
                <p className="ph-fb-text li-text">Automate your marketing and focus on what matters most.</p>
                <div className="ph-content green-post horizontal">
                  <div className="ph-product-small product-img-2"></div>
                </div>
                <div className="li-card-bottom">
                  <h3>Turn Ideas Into Impact</h3>
                  <small>svarah.ai</small>
                </div>
                <div className="fb-reactions li-reactions">
                  <span className="li-react-icons"><ThumbsUp size={10} fill="#fff"/></span> 1,204
                  <span className="fb-comments">42 comments</span>
                </div>
                <div className="li-action-bar">
                  <span><ThumbsUp size={14}/> Like</span>
                  <span><MessageSquare size={14}/> Comment</span>
                  <span><Repeat2 size={14}/> Repost</span>
                  <span><Send size={14}/> Send</span>
                </div>
              </div>
            </div>
          </div>

          {/* Widgets and Annotations */}
          <div className="visual-item float-med widget-stats">
            <span className="w-label">Total Reach <ArrowRight size={10}/></span>
            <strong>2.8M <span className="w-growth">▲+320%</span></strong>
            <div className="w-chart">
              <i style={{height: '40%'}}/><i style={{height: '60%'}}/><i style={{height: '30%'}}/><i style={{height: '80%'}}/><i style={{height: '100%'}}/><i style={{height: '70%'}}/>
            </div>
          </div>

        </div>
      </div>
    </section>
  );
}
