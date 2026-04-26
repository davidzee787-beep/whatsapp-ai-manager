'use strict';

/* ============================================================
   Scroll Progress Bar
   ============================================================ */
const progressBar = document.getElementById('scrollProgress');
window.addEventListener('scroll', () => {
  const scrollTop  = window.scrollY;
  const docHeight  = document.documentElement.scrollHeight - window.innerHeight;
  const pct = docHeight > 0 ? (scrollTop / docHeight) * 100 : 0;
  progressBar.style.width = pct + '%';
}, { passive: true });

/* ============================================================
   Nav Scroll Behaviour
   ============================================================ */
const nav = document.getElementById('nav');
window.addEventListener('scroll', () => {
  nav.classList.toggle('scrolled', window.scrollY > 40);
}, { passive: true });

/* ============================================================
   Reveal on Scroll (Intersection Observer)
   Hardware-accelerated: only transform + opacity animated
   ============================================================ */
const revealEls = document.querySelectorAll('.reveal-up, .reveal-left, .reveal-right');
const revealObserver = new IntersectionObserver((entries) => {
  entries.forEach(entry => {
    if (entry.isIntersecting) {
      entry.target.classList.add('is-visible');
      revealObserver.unobserve(entry.target);
    }
  });
}, { threshold: 0.1, rootMargin: '0px 0px -32px 0px' });

revealEls.forEach(el => revealObserver.observe(el));

/* ============================================================
   Animated Counters — spring-eased (cubic ease-out)
   ============================================================ */
function animateCounter(el) {
  const target   = parseInt(el.getAttribute('data-count'), 10);
  const duration = 1600;
  const start    = performance.now();

  function update(now) {
    const t = Math.min((now - start) / duration, 1);
    // Spring-like cubic ease-out
    const eased = 1 - Math.pow(1 - t, 3);
    el.textContent = Math.floor(eased * target);
    if (t < 1) requestAnimationFrame(update);
    else el.textContent = target;
  }
  requestAnimationFrame(update);
}

const counterObserver = new IntersectionObserver((entries) => {
  entries.forEach(entry => {
    if (entry.isIntersecting) {
      animateCounter(entry.target);
      counterObserver.unobserve(entry.target);
    }
  });
}, { threshold: 0.6 });

document.querySelectorAll('[data-count]').forEach(el => counterObserver.observe(el));

/* ============================================================
   Skill Bar Animation
   ============================================================ */
const barObserver = new IntersectionObserver((entries) => {
  entries.forEach(entry => {
    if (entry.isIntersecting) {
      entry.target.querySelectorAll('.skill-bar-fill-mini').forEach(fill => {
        fill.style.width = fill.getAttribute('data-width') + '%';
      });
      barObserver.unobserve(entry.target);
    }
  });
}, { threshold: 0.2 });

const skillRows = document.querySelector('.skills-rows');
if (skillRows) barObserver.observe(skillRows);

/* ============================================================
   Hero Canvas — Particle Constellation
   Only transform/opacity mutated via canvas draw — no DOM animation
   ============================================================ */
const canvas = document.getElementById('heroCanvas');
const ctx    = canvas.getContext('2d');
let W, H, particles;

function resize() {
  W = canvas.width  = canvas.offsetWidth;
  H = canvas.height = canvas.offsetHeight;
}

function initParticles() {
  const count = Math.min(Math.floor((W * H) / 13000), 90);
  particles = Array.from({ length: count }, () => ({
    x:  Math.random() * W,
    y:  Math.random() * H,
    vx: (Math.random() - 0.5) * 0.28,
    vy: (Math.random() - 0.5) * 0.28,
    r:  Math.random() * 1.4 + 0.4,
    a:  Math.random() * 0.45 + 0.15,
  }));
}

let mx = -9999, my = -9999;

document.addEventListener('mousemove', e => {
  const rect = canvas.getBoundingClientRect();
  mx = e.clientX - rect.left;
  my = e.clientY - rect.top;
});

let rafId;
function loop() {
  ctx.clearRect(0, 0, W, H);

  for (let i = 0; i < particles.length; i++) {
    const p = particles[i];

    // Mouse repulsion
    const dx   = mx - p.x;
    const dy   = my - p.y;
    const dist = Math.hypot(dx, dy);
    if (dist < 110 && dist > 0) {
      p.vx -= (dx / dist) * 0.035;
      p.vy -= (dy / dist) * 0.035;
    }

    // Dampen — hardware-safe velocity decay
    p.vx *= 0.992;
    p.vy *= 0.992;
    p.x  += p.vx;
    p.y  += p.vy;

    if (p.x < 0) p.x = W;
    if (p.x > W) p.x = 0;
    if (p.y < 0) p.y = H;
    if (p.y > H) p.y = 0;

    // Dot
    ctx.beginPath();
    ctx.arc(p.x, p.y, p.r, 0, Math.PI * 2);
    ctx.fillStyle = `rgba(200,169,110,${p.a})`;
    ctx.fill();

    // Connections
    for (let j = i + 1; j < particles.length; j++) {
      const q = particles[j];
      const d = Math.hypot(p.x - q.x, p.y - q.y);
      if (d < 115) {
        const op = (1 - d / 115) * 0.15;
        ctx.beginPath();
        ctx.moveTo(p.x, p.y);
        ctx.lineTo(q.x, q.y);
        ctx.strokeStyle = `rgba(200,169,110,${op})`;
        ctx.lineWidth   = 0.5;
        ctx.stroke();
      }
    }
  }

  rafId = requestAnimationFrame(loop);
}

window.addEventListener('resize', () => {
  cancelAnimationFrame(rafId);
  resize();
  initParticles();
  loop();
}, { passive: true });

resize();
initParticles();
loop();

/* ============================================================
   Hero on-load reveal
   ============================================================ */
window.addEventListener('load', () => {
  document.querySelectorAll('.hero .reveal-up, .hero .reveal-right').forEach(el => {
    el.classList.add('is-visible');
  });
});

/* ============================================================
   Smooth Scroll for anchor links
   ============================================================ */
document.querySelectorAll('a[href^="#"]').forEach(link => {
  link.addEventListener('click', e => {
    const target = document.querySelector(link.getAttribute('href'));
    if (target) {
      e.preventDefault();
      target.scrollIntoView({ behavior: 'smooth', block: 'start' });
    }
  });
});
