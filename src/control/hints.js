// Find every interactive element in the viewport, stamp it with an index, and
// draw a numbered badge over it. Returns the hint records.
//
// Stamping (data-drc-hint) is what makes a click reliable: Python clicks by
// that attribute, so Playwright does its own actionability checks and the
// stamps die with the document on navigation. The badges are cosmetic only --
// they are pointer-events:none and are removed right after the screenshot.
(maxHints) => {
  document
    .querySelectorAll('[data-drc-hint]')
    .forEach((el) => el.removeAttribute('data-drc-hint'));
  document.getElementById('__drc_hints')?.remove();

  const SELECTOR = [
    'a[href]',
    'button',
    'input:not([type=hidden])',
    'select',
    'textarea',
    'summary',
    '[role=button]',
    '[role=link]',
    '[role=checkbox]',
    '[role=radio]',
    '[role=tab]',
    '[role=menuitem]',
    '[contenteditable=""]',
    '[contenteditable="true"]',
    '[onclick]',
    '[tabindex]:not([tabindex="-1"])',
  ].join(', ');

  const W = window.innerWidth;
  const H = window.innerHeight;
  const hints = [];

  for (const el of document.querySelectorAll(SELECTOR)) {
    if (hints.length >= maxHints) break;
    if (el.disabled) continue;

    const rect = el.getBoundingClientRect();
    if (rect.width < 4 || rect.height < 4) continue;
    if (rect.bottom <= 0 || rect.top >= H || rect.right <= 0 || rect.left >= W) continue;

    const style = getComputedStyle(el);
    if (style.visibility === 'hidden' || style.opacity === '0') continue;

    // elementFromPoint drops anything hidden behind a modal or cookie banner,
    // and collapses nested clickables (<a><span>) down to a single hint.
    const cx = Math.min(Math.max(rect.left + rect.width / 2, 1), W - 1);
    const cy = Math.min(Math.max(rect.top + rect.height / 2, 1), H - 1);
    const hit = document.elementFromPoint(cx, cy);
    if (!hit || !(el === hit || el.contains(hit) || hit.contains(el))) continue;

    const index = hints.length + 1;
    el.setAttribute('data-drc-hint', String(index));
    const label = (
      el.innerText ||
      el.getAttribute('aria-label') ||
      el.value ||
      el.placeholder ||
      el.title ||
      el.tagName
    )
      .trim()
      .replace(/\s+/g, ' ')
      .slice(0, 40);

    hints.push({
      index: index,
      label: label,
      tag: el.tagName.toLowerCase(),
      x: rect.left,
      y: rect.top,
      w: rect.width,
      h: rect.height,
    });
  }

  const overlay = document.createElement('div');
  overlay.id = '__drc_hints';
  overlay.style.cssText =
    'position:fixed;inset:0;z-index:2147483647;pointer-events:none;';
  for (const hint of hints) {
    const badge = document.createElement('span');
    badge.textContent = hint.index;
    badge.style.cssText =
      `position:absolute;left:${Math.max(hint.x, 0)}px;top:${Math.max(hint.y, 0)}px;` +
      'background:#ff0;color:#000;font:bold 12px/1 monospace;padding:2px 4px;' +
      'border:1px solid #a80;border-radius:3px;box-shadow:0 1px 2px #0007;';
    overlay.appendChild(badge);
  }
  document.documentElement.appendChild(overlay);

  return hints;
}
