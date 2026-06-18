(function () {
  "use strict";

  const pages = [
    ["Home", "/"],
    ["Harness", "/harness.html"],
    ["Research", "/research.html"],
    ["RRG", "/rrg.html"],
    ["Breadth", "/breadth.html"],
    ["Schwab", "/schwab.html"],
    ["Screener", "/screener.html"],
    ["Rankings", "/rankings.html"],
    ["Themes", "/themes.html"],
    ["Flow", "/flow.html"],
    ["CANSLIM", "/canslim.html"],
    ["News", "/news.html"],
    ["Macro", "/macro.html"]
  ];

  const css = `
    body { scroll-padding-top: 76px; }
    body.mi-shell-ready { padding-top: max(18px, env(safe-area-inset-top)); }
    .mi-shell {
      position: sticky;
      top: 0;
      z-index: 1000;
      display: flex;
      align-items: center;
      gap: 10px;
      width: 100%;
      margin: 0 0 18px;
      padding: 8px;
      border: 1px solid var(--grid, #d7e0ee);
      border-radius: 8px;
      background: var(--bg, #fff);
      background: color-mix(in srgb, var(--bg, #fff) 92%, transparent);
      box-shadow: 0 8px 24px rgba(14, 33, 72, 0.08);
      backdrop-filter: blur(14px);
      -webkit-backdrop-filter: blur(14px);
    }
    .mi-shell.mi-dark {
      box-shadow: 0 8px 24px rgba(0, 0, 0, 0.28);
    }
    .mi-brand {
      flex: 0 0 auto;
      display: inline-flex;
      align-items: center;
      min-height: 30px;
      padding: 0 10px;
      border-right: 1px solid var(--grid, #d7e0ee);
      color: var(--ink, #0e2148);
      font: 800 12px/1 'Bricolage Grotesque', system-ui, sans-serif;
      letter-spacing: 0.02em;
      text-decoration: none;
      white-space: nowrap;
    }
    .mi-links {
      display: flex;
      align-items: center;
      gap: 4px;
      min-width: 0;
      overflow-x: auto;
      scrollbar-width: thin;
    }
    .mi-link {
      flex: 0 0 auto;
      display: inline-flex;
      align-items: center;
      min-height: 30px;
      padding: 0 9px;
      border: 1px solid transparent;
      border-radius: 6px;
      color: var(--dim, #5d6b85);
      font: 600 10px/1 'Spline Sans Mono', ui-monospace, monospace;
      letter-spacing: 0.06em;
      text-decoration: none;
      text-transform: uppercase;
      white-space: nowrap;
    }
    .mi-link:hover {
      border-color: var(--grid, #d7e0ee);
      color: var(--ink, #0e2148);
      background: var(--panel, #f4f7fc);
      background: color-mix(in srgb, var(--panel, #f4f7fc) 70%, transparent);
    }
    .mi-link.active {
      background: var(--accent, #16336e);
      border-color: var(--accent, #16336e);
      color: #fff;
    }
    .mi-shell.mi-dark .mi-link.active {
      color: #0a0e14;
    }
    .mi-shell.mi-dark .mi-link:hover {
      background: var(--panel, #111722);
      background: color-mix(in srgb, var(--panel, #111722) 82%, transparent);
    }
    a:focus-visible,
    button:focus-visible,
    input:focus-visible,
    select:focus-visible,
    textarea:focus-visible,
    [tabindex]:focus-visible {
      outline: 2px solid var(--accent, #16336e);
      outline-offset: 2px;
    }
    button,
    select,
    input,
    textarea {
      min-height: 32px;
    }
    .panel,
    .card,
    aside,
    .chart-wrap,
    .calls-panel,
    .summary-card,
    .table-wrap,
    .wcard,
    .vote,
    .event,
    .caveat {
      border-radius: 8px !important;
    }
    table {
      font-variant-numeric: tabular-nums;
    }
    .empty {
      color: var(--dim, #5d6b85);
    }
    @media (max-width: 760px) {
      body {
        padding-left: 14px !important;
        padding-right: 14px !important;
      }
      .mi-shell {
        align-items: flex-start;
        flex-direction: column;
        gap: 7px;
      }
      .mi-brand {
        border-right: 0;
        border-bottom: 1px solid var(--grid, #d7e0ee);
        width: 100%;
        padding: 0 4px 8px;
      }
      .mi-links {
        width: 100%;
        padding-bottom: 2px;
      }
    }
  `;

  function isActive(href) {
    const path = window.location.pathname || "/";
    if (href === "/") return path === "/" || path === "/index.html";
    return path === href;
  }

  function hideLegacyNav() {
    document.querySelectorAll("header .nav-link, header .nav-back").forEach(el => {
      el.style.display = "none";
    });
    const first = document.body.firstElementChild;
    if (!first || first.classList.contains("mi-shell")) return;
    const hasLegacyLinks = first.querySelector && first.querySelector(".nav-link, .nav-back");
    if ((first.tagName === "NAV" || first.tagName === "DIV") && hasLegacyLinks) {
      first.style.display = "none";
    }
  }

  function install() {
    if (document.querySelector(".mi-shell")) return;

    const style = document.createElement("style");
    style.textContent = css;
    document.head.appendChild(style);

    hideLegacyNav();

    const shell = document.createElement("nav");
    const bg = getComputedStyle(document.documentElement).getPropertyValue("--bg").trim();
    shell.className = "mi-shell" + (/^#0|^#1|rgb\((?:0|1[0-9]|2[0-9])/.test(bg) ? " mi-dark" : "");
    shell.setAttribute("aria-label", "Module navigation");

    const links = pages.map(([label, href]) => {
      const active = isActive(href) ? " active" : "";
      const current = active ? ' aria-current="page"' : "";
      return `<a class="mi-link${active}" href="${href}"${current}>${label}</a>`;
    }).join("");

    shell.innerHTML =
      `<a class="mi-brand" href="/">Market Intelligence</a>` +
      `<div class="mi-links">${links}</div>`;

    document.body.classList.add("mi-shell-ready");
    document.body.insertBefore(shell, document.body.firstChild);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", install, { once: true });
  } else {
    install();
  }
})();
