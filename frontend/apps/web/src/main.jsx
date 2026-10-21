import React, { useEffect, useState } from 'react';
import { createRoot } from 'react-dom/client';
import { BrowserRouter, Routes, Route, Link, useLocation } from 'react-router-dom';
import { ArrowDown, ArrowRight, AudioLines, ChevronRight, Command, Globe2, LockKeyhole, ShieldCheck, Smartphone, Sparkles } from 'lucide-react';
import './styles.css';
import { SvarahHeader, SvarahHero } from './SvarahHero.jsx';
import FormulaStream from './FormulaStream.jsx';
import TextRevealScroll from './TextRevealScroll.jsx';
import Wordmark from './Wordmark.jsx';
import { AuthProvider, useAuth } from './AuthContext.jsx';
import { ThemeProvider } from './ThemeContext.jsx';
import Login from './Login.jsx';
import Signup from './Signup.jsx';
import GitHubCallback from './GitHubCallback.jsx';
import DemoVideo from './DemoVideo.jsx';
import DownloadApp from './DownloadApp.jsx';
import VoiceWorkspace from './workspace/VoiceWorkspace.jsx';
import Workspace from './workspace/Workspace.jsx';
import { RibbonFieldBackground } from './shaders/ribbon-field/RibbonFieldBackground.tsx';
import './shaders/threeui.css';

function Logo(){return <Wordmark size="md"/>}
function Button({children,to='/signup',href,light=false}){const cls=`button ${light?'button-light':'button-dark'}`;return href?<a className={cls} href={href} target={href?.startsWith('http')?'_blank':undefined} rel="noreferrer">{children}</a>:<Link className={cls} to={to}>{children}</Link>}
function Pill({children}){return <span className="pill"><span className="pill-dot"/>{children}</span>}
function Reveal({children,className=''}){const [visible,setVisible]=useState(false);const ref=React.useRef(null);useEffect(()=>{const node=ref.current;if(!node)return;const observer=new IntersectionObserver(([entry])=>{if(entry.isIntersecting){setVisible(true);observer.unobserve(node)}},{threshold:.12,rootMargin:'0px 0px -35px 0px'});observer.observe(node);return()=>observer.disconnect()},[]);return <div ref={ref} className={`reveal ${visible?'is-visible':''} ${className}`}>{children}</div>}
function SpotlightCard({children,className=''}){const ref=React.useRef(null);function move(e){const r=ref.current?.getBoundingClientRect();if(!r)return;ref.current.style.setProperty('--mx',`${e.clientX-r.left}px`);ref.current.style.setProperty('--my',`${e.clientY-r.top}px`)}return <article ref={ref} onPointerMove={move} className={`spotlight-card ${className}`}>{children}</article>}
function Home(){const [personalize,setPersonalize]=useState('Hindi');const [speed,setSpeed]=useState(false);const personalizeCopy={Hindi:{h:'हिन्दी में बोलें।',p:'Svarah आपकी हिन्दी समझता है और उसे किसी भी ऐप में साफ़, तैयार लेखन में बदल देता है — भाव और लहज़ा वैसा ही रहता है।'},Kannada:{h:'ಕನ್ನಡದಲ್ಲಿ ಮಾತನಾಡಿ.',p:'Svarah ನಿಮ್ಮ ಕನ್ನಡವನ್ನು ಅರ್ಥಮಾಡಿಕೊಂಡು, ಯಾವುದೇ ಆಪ್‌ನಲ್ಲಿ ಸ್ಪಷ್ಟವಾದ, ಸಿದ್ಧ ಬರಹವಾಗಿ ಪರಿವರ್ತಿಸುತ್ತದೆ — ಧ್ವನಿ ಮತ್ತು ಭಾವ ಅದೇ ರೀತಿ ಉಳಿಯುತ್ತದೆ.'},Tamil:{h:'தமிழில் பேசுங்கள்.',p:'Svarah உங்கள் தமிழைப் புரிந்துகொண்டு, எந்தச் செயலியிலும் தெளிவான, தயார் நிலை எழுத்தாக மாற்றுகிறது — குரலும் உணர்வும் அப்படியே இருக்கும்.'},Telugu:{h:'తెలుగులో మాట్లాడండి.',p:'Svarah మీ తెలుగును అర్థం చేసుకుని, ఏ యాప్‌లోనైనా స్పష్టమైన, సిద్ధమైన రచనగా మారుస్తుంది — స్వరం మరియు భావం యథాతథంగా ఉంటాయి.'},Malayalam:{h:'മലയാളത്തിൽ സംസാരിക്കൂ.',p:'Svarah നിങ്ങളുടെ മലയാളം മനസ്സിലാക്കി, ഏത് ആപ്പിലും വ്യക്തവും തയ്യാറുമായ എഴുത്താക്കി മാറ്റുന്നു — സ്വരവും ഭാവവും അതേപടി നിലനിർത്തുന്നു.'}};return <>
<div style={{ position: 'relative' }}>
  <div style={{ position: 'absolute', inset: 0, zIndex: 0, opacity: 0.15 }}>
    <RibbonFieldBackground
      speed={1.00}
      pointerAmount={1.00}
      smoothing={0.035}
      hue={0}
      saturation={1.00}
      brightness={1.00}
      opacity={1.00}
    />
  </div>
  <div style={{ position: 'relative', zIndex: 1 }}>
    <SvarahHero />
    <section className="wf-speed section-wrap"><TextRevealScroll className="wf-speed-heading"><span className="wf-section-kicker">HOW IT WORKS</span><h2>4× faster than typing.</h2><p>Voice that finally works is here. Create, code, message, and write at the speed of thought.</p></TextRevealScroll><Reveal className="wf-speed-panel"><div className="speed-topline"><span>ONE THOUGHT, TWO WAYS</span><span className="speed-toggle"><button className={!speed?'selected':''} onClick={()=>setSpeed(false)}>Keyboard</button><button className={speed?'selected':''} onClick={()=>setSpeed(true)}>Svarah</button></span></div><div className="speed-columns"><div className={`speed-column ${!speed?'speed-active':''}`}><div className="speed-column-head"><span className="speed-icon keyboard-icon"><Command size={18}/></span><div><b>Keyboard</b><small>One word at a time</small></div><strong>45 <small>wpm</small></strong></div><div className="speed-track"><span style={{width:'21%'}}/></div><p className="raw-words">“Hey so um can you actually wait can you tell the team that the launch is gonna slip I think to like not Friday, the following Monday...”</p><span className="speed-status">RAW TRANSCRIPTION</span></div><div className={`speed-column flow-column ${speed?'speed-active':''}`}><div className="speed-column-head"><span className="speed-icon flow-icon"><AudioLines size={19}/></span><div><b>Svarah.AI</b><small>Speak your mind</small></div><strong>220 <small>wpm</small></strong></div><div className="speed-track flow-track"><span style={{width:'100%'}}/></div><p className="polished-words">Can you let the team know the launch is slipping to Monday? We’re still waiting on legal to sign off on the new terms page. We’ll have a firm timeline by end of day Thursday.</p><span className="speed-status flow-status"><Sparkles size={12}/> CLEAN, POLISHED WRITING</span></div></div><div className="speed-foot"><span><span className="tick-dot"/> Removes filler words</span><span><span className="tick-dot"/> Understands corrections</span><span><span className="tick-dot"/> Adds punctuation</span></div></Reveal></section>
    <section className="wf-how"><div className="section-wrap"><TextRevealScroll className="wf-section-heading"><span className="wf-section-kicker">MADE FOR THE WAY YOU THINK</span><h2>Speak at the speed you think,<br/><em>in every app, on every device.</em></h2></TextRevealScroll><div className="wf-feature-grid"><Reveal><SpotlightCard className="wf-feature wf-feature-speak"><div className="wf-card-label">01 <span> / SPEAK NATURALLY</span></div><div className="wf-chat-mock"><div className="wf-chat-head"><span className="wf-avatar">M</span><span><b>Message to Maya</b><small>Message</small></span><span className="wf-green-dot"/></div><div className="wf-audio-line"><span className="wf-audio-mark"><AudioLines size={16}/></span><div className="wf-bars">{Array.from({length:25},(_,i)=><i key={i} style={{'--bar':`${7+Math.abs(Math.sin(i*.63))*22}px`}}/>)}</div></div><div className="wf-chat-text">Hey Maya, can you send over the latest deck before tomorrow’s meeting?</div><div className="wf-chat-bottom"><span><Sparkles size={13}/> Written with Svarah</span><span className="tick-dot"/></div></div><h3>Say it like you mean it.</h3><p>Ramble, pause, or change your mind mid-sentence. Svarah understands what you mean, not just what you say.</p></SpotlightCard></Reveal><Reveal><SpotlightCard className="wf-feature wf-feature-refine"><div className="wf-card-label">02 <span> / REFINE AS YOU SPEAK</span></div><div className="wf-refine-demo"><div className="wf-refine-top"><span className="wf-mini-dot"/><span>Message draft</span><span className="wf-refine-ai"><Sparkles size={12}/> Svarah edit</span></div><p className="wf-rough">“Hey so <del>um</del> can you actually <del>wait</del> tell the team the launch is gonna slip...”</p><div className="wf-refine-arrow"><span/><ArrowDown size={17}/></div><p className="wf-clean">Can you let the team know the launch is slipping to Monday?</p><div className="wf-refine-tags"><span><span className="tick-dot"/> Filler removed</span><span><span className="tick-dot"/> Clearer wording</span></div></div><h3>Svarah edits as you speak.</h3><p>Text that reads like you wrote it, not like you spoke it. Automatic cleanup, corrections, and formatting.</p></SpotlightCard></Reveal><Reveal><SpotlightCard className="wf-feature wf-feature-anywhere"><div className="wf-card-label">03 <span> / USE IT ANYWHERE</span></div><div className="wf-app-orbit"><div className="wf-app-core"><AudioLines size={28}/></div><span className="wf-app app-gmail">M</span><span className="wf-app app-chat">◌</span><span className="wf-app app-code">⌘</span><span className="wf-app app-doc">▤</span><span className="wf-app app-web">◎</span></div><h3>One shortcut. Every app.</h3><p>Svarah works anywhere you can type, with no plugins required. Your voice stays in sync across your Android apps.</p><div className="wf-platform-pills"><span><Smartphone size={13}/> Android</span></div></SpotlightCard></Reveal></div></div></section>
  </div>
</div>
<section className="wf-personalize" style={{position:'relative',overflow:'hidden'}}><div style={{position:'absolute',inset:0,zIndex:0}}><FormulaStream style={{minWidth:0,minHeight:0,width:'100%',height:'100%'}} background="#171814" textColor="rgba(235,238,228,0.22)" accent="#d8f878" /></div><div className="section-wrap"><TextRevealScroll className="wf-personalize-copy"><span className="wf-section-kicker">MULTILINGUAL SUPPORT</span><h2>Speaks your language,<br/><em>understands your context.</em></h2><p>Svarah understands users and conveys messages seamlessly in multiple regional languages.</p><div className="wf-personalize-tabs" style={{flexWrap: 'wrap'}}>{['Hindi', 'Kannada', 'Tamil', 'Telugu', 'Malayalam'].map(t=><button key={t} onClick={()=>setPersonalize(t)} className={personalize===t?'active':''}>{t}</button>)}</div><div className="wf-personalize-description"><h3>{personalizeCopy[personalize].h}</h3><p>{personalizeCopy[personalize].p}</p></div></TextRevealScroll><Reveal className="wf-personalize-art"><div className="wf-settings-window"><div className="wf-settings-header"><span className="wf-settings-logo"><AudioLines size={16}/></span><b>Language settings</b><span className="wf-window-dots">•••</span></div><div className="wf-settings-tabs"><span className="current">Indic Languages</span><span>Global</span></div><div className="wf-settings-body" style={{ maxHeight: '280px', overflowY: 'auto' }}><div className="wf-settings-title"><div><b>Supported Languages</b><small>Speak naturally in your mother tongue</small></div></div>{[['Kannada','ಕನ್ನಡ'],['Hindi','हिन्दी'],['Tamil','தமிழ்'],['Telugu','తెలుగు'],['Malayalam','മലയാളം'],['Marathi','मराठी'],['Bengali','বাংলা'],['Gujarati','ગુજરાતી'],['Punjabi','ਪੰਜਾਬੀ'],['Odia','ଓଡ଼ିଆ']].map(([a,b])=><div className="wf-dict-row" key={a}><span className="wf-dict-check"><span className="tick-dot"/></span><span><b>{a}</b><small>{b}</small></span><ChevronRight size={15}/></div>)}</div></div><div className="wf-floating-tip"><Globe2 size={15}/><span><b>Multilingual</b><small>10+ Indian Languages</small></span></div></Reveal></div></section>
<section className="wf-privacy"><div className="section-wrap wf-privacy-inner"><Reveal className="wf-privacy-art"><div className="wf-privacy-orbit orbit-a"/><div className="wf-privacy-orbit orbit-b"/><div className="wf-privacy-shield"><ShieldCheck size={56} strokeWidth={1.5}/></div><div className="wf-privacy-chip privacy-chip-a"><LockKeyhole size={15}/> You control your data</div><div className="wf-privacy-chip privacy-chip-b"><span className="tick-dot"/> SOC 2 · ISO 27001</div></Reveal><TextRevealScroll className="wf-privacy-copy"><span className="wf-section-kicker">YOUR VOICE STAYS YOURS</span><h2>Privacy first.<br/><em>Security always.</em></h2><p>You choose what Svarah.AI keeps, what it learns from, and when it listens. Your data is never sold.</p><ul><li><span className="tick-dot"/> Clear data and training controls</li><li><span className="tick-dot"/> Independently audited security</li><li><span className="tick-dot"/> Built for the work you trust it with</li></ul></TextRevealScroll></div></section>
<section className="wf-final-cta"><div className="section-wrap wf-final-inner"><div className="wf-final-mark"><AudioLines size={28}/></div><span className="wf-section-kicker">SVARAH.AI</span><h2>Say your offer once.<br/><em>Publish it everywhere.</em></h2><p>Speak your offer and Svarah turns it into a poster, captions, a voice-over and a video — with every number checked against what you said.</p><Button to="/signup">Start creating free <ArrowRight size={17}/></Button></div></section>
</>}
function PageHero({eyebrow,title,highlight,sub,children}){return <section className="inner-hero section-wrap"><Pill>{eyebrow}</Pill><h1>{title}<br/><span>{highlight}</span></h1><p>{sub}</p>{children}</section>}
function Footer(){const {user}=useAuth();return <footer className="site-footer"><div className="section-wrap"><div className="footer-top"><div className="footer-brand"><Logo/><p>Voice for business.<br/>Say your offer once, publish it everywhere.</p></div><div className="footer-col"><b>Product</b><Link to="/workspace">Voice workspace</Link><Link to="/studio">Live studio</Link><Link to="/web-demo">Watch the demo</Link><Link to="/download">Android app (coming soon)</Link></div><div className="footer-col"><b>Account</b>{user?<Link to="/workspace">Open workspace</Link>:<><Link to="/login">Log in</Link><Link to="/signup">Sign up free</Link></>}</div></div><div className="footer-bottom"><span>© 2026 Svarah.AI</span><span>Say it once. Publish everywhere.</span></div></div></footer>}
function Connected(){useEffect(()=>{const t=setTimeout(()=>window.close(),1800);return()=>clearTimeout(t)},[]);return <div className="auth-page"><div className="auth-ambient-glow"/><div className="demo-wrap" style={{maxWidth:460,textAlign:'center',alignItems:'center'}}><Wordmark size="md"/><h1 style={{fontSize:26,margin:0,color:'var(--wf-dark-text)'}}>Your accounts are connected.</h1><p style={{margin:0,color:'var(--wf-dark-muted)',lineHeight:1.6}}>You can close this tab — Svarah.AI is posting your campaign in the tab you came from.</p><Link className="button button-auth-primary demo-cta" to="/workspace">Back to my workspace <ArrowRight size={16}/></Link></div></div>}
function NotFound(){return <PageHero eyebrow="404 — NOT FOUND" title="Looks like this" highlight="thought wandered off." sub="The page you're looking for doesn't exist."><Button to="/">Back to home <ArrowRight size={16}/></Button></PageHero>}
function ScrollToTop(){const {pathname}=useLocation();useEffect(()=>{window.scrollTo({top:0,behavior:'instant'})},[pathname]);return null}

function AppContent(){
  const location = useLocation();
  const isStandalone = ['/login', '/signup', '/web-demo', '/download', '/connected', '/workspace', '/studio', '/dashboard', '/app', '/account'].includes(location.pathname);

  return (
    <>
      <ScrollToTop/>
      {!isStandalone && <SvarahHeader/>}
      <main className={isStandalone ? 'standalone-main' : ''}>
        <Routes>
          <Route path="/" element={<Home/>}/>
          <Route path="/web-demo" element={<DemoVideo/>}/>
          <Route path="/download" element={<DownloadApp/>}/>
          <Route path="/connected" element={<Connected/>}/>
          <Route path="/login" element={<Login/>}/>
          <Route path="/signup" element={<Signup/>}/>
          <Route path="/auth/github" element={<GitHubCallback/>}/>
          {/* The voice workspace. /dashboard, /app and /account are kept as
              aliases so existing links keep working. */}
          <Route path="/workspace" element={<VoiceWorkspace/>}/>
          {/* The full step-by-step campaign studio: fact review + JSON,
              plan, posters, speech, video, verify, publish. */}
          <Route path="/studio" element={<Workspace/>}/>
          <Route path="/dashboard" element={<VoiceWorkspace/>}/>
          <Route path="/app" element={<VoiceWorkspace/>}/>
          <Route path="/account" element={<VoiceWorkspace/>}/>
          <Route path="*" element={<NotFound/>}/>
        </Routes>
      </main>
      {!isStandalone && <Footer/>}
    </>
  );
}

function App(){
  return (
    <BrowserRouter>
      <ThemeProvider>
        <AuthProvider>
          <AppContent/>
        </AuthProvider>
      </ThemeProvider>
    </BrowserRouter>
  );
}

createRoot(document.getElementById('root')).render(<React.StrictMode><App/></React.StrictMode>);
