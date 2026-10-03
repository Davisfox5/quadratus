"use strict";

// Purpose-built app.js test DOM: supported selectors are tag/#id/.class chains,
// attributes, :checked, and comma lists (combinators throw). Collections are
// arrays; ids own stable detached stubs; there is no layout or text-node fidelity.
function createDocument() {
  const ids = Object.create(null), handlers = new Map();
  const escapeHtml = value => String(value).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  const voidTags = new Set("area base br col embed hr img input link meta source track wbr".split(" "));
  const createEvent = (type, props = {}) => Object.assign({ type, bubbles: true,
    defaultPrevented: false, propagationStopped: false, immediatePropagationStopped: false,
    preventDefault() { this.defaultPrevented = true; },
    stopPropagation() { this.propagationStopped = true; },
    stopImmediatePropagation() { this.propagationStopped = this.immediatePropagationStopped = true; },
  }, props);
  const addListener = (target, type, fn) => {
    if (!handlers.has(target)) handlers.set(target, new Map());
    const events = handlers.get(target); events.set(type, [...(events.get(type) || []), fn]);
  };
  const removeListener = (target, type, fn) => {
    const events = handlers.get(target), list = events?.get(type); if (list) events.set(type, list.filter(item => item !== fn));
  };
  const dispatch = (target, input) => {
    const e = typeof input === "string" ? createEvent(input) : input;
    if (!e.target) e.target = target;
    e.currentTarget = target;
    for (const fn of [...(handlers.get(target)?.get(e.type) || [])]) {
      fn.call(target, e); if (e.immediatePropagationStopped) break;
    }
    if (e.bubbles !== false && !e.propagationStopped && target.parentNode) dispatch(target.parentNode, e);
    return !e.defaultPrevented;
  };
  const matches = (el, raw) => {
    let selector = raw, checked = false;
    if (selector.endsWith(":checked")) { selector = selector.slice(0, -8); checked = true; }
    const attrs = [...selector.matchAll(/\[([\w-]+)(?:=["']?([^\]"']+)["']?)?\]/g)];
    selector = selector.replace(/\[[^\]]+\]/g, "");
    if (/[\s>+~]/.test(selector)) throw new Error(`fake_dom: unsupported selector: ${raw}`);
    const id = selector.match(/#([\w-]+)/)?.[1], tag = selector.match(/^[\w-]+/)?.[0];
    const classes = [...selector.matchAll(/\.([\w-]+)/g)].map(match => match[1]);
    if ((tag && el.tagName !== tag.toUpperCase()) || (id && el.id !== id) || checked && !el.checked) return false;
    if (classes.some(name => !el.classList.contains(name))) return false;
    return attrs.every(([, name, expected]) => {
      const key = name.slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase());
      const actual = name.startsWith("data-") ? el.dataset[key] : el[name];
      return expected === undefined ? actual !== undefined && actual !== false : String(actual) === expected;
    });
  };
  const selectAll = (root, query, first = false) => {
    const selectors = query.split(",").map(item => item.trim()).filter(Boolean), found = [];
    const visit = node => { for (const child of node.children || []) {
      if (selectors.some(selector => matches(child, selector))) found.push(child);
      if (!first || !found.length) visit(child);
      if (first && found.length) return;
    } };
    visit(root); return found;
  };
  const serialize = el => {
    const attrs = [], tag = el.tagName.toLowerCase();
    if (el.id) attrs.push(`id="${escapeHtml(el.id)}"`); if (el.className) attrs.push(`class="${escapeHtml(el.className)}"`);
    if (el.tagName === "OPTION") attrs.push(`value="${escapeHtml(el.value)}"`);
    for (const [key, value] of Object.entries(el.dataset))
      attrs.push(`data-${key.replace(/[A-Z]/g, c => `-${c.toLowerCase()}`)}="${escapeHtml(value)}"`);
    return `<${tag}${attrs.length ? ` ${attrs.join(" ")}` : ""}>${el.innerHTML}</${tag}>`;
  };

  class Element {
    constructor(tag) {
      this.tagName = String(tag).toUpperCase(); this.parentNode = null; this.children = [];
      this.style = {}; this.dataset = {}; this.checked = false; this.selected = false;
      this.paused = true; this.duration = 0; this.currentTime = 0;
      this._id = this._value = this._text = this._html = "";
      this._classes = new Set(); this._disabled = this._hidden = this._inert = false;
      this.classList = {
        add: (...names) => names.forEach(name => this._classes.add(name)),
        remove: (...names) => names.forEach(name => this._classes.delete(name)),
        contains: name => this._classes.has(name),
        toggle: (name, force) => { const add = force === undefined ? !this._classes.has(name) : !!force;
          add ? this._classes.add(name) : this._classes.delete(name); return add; },
      };
    }
    get id() { return this._id; }
    set id(value) { if (this._id && ids[this._id] === this) delete ids[this._id]; this._id = String(value); if (this._id) ids[this._id] = this; }
    get className() { return [...this._classes].join(" "); }
    set className(value) { this._classes = new Set(String(value).split(/\s+/).filter(Boolean)); }
    get disabled() { return this._disabled; }
    set disabled(value) { this._disabled = !!value; if (this._disabled && this.contains(doc._activeElement)) doc._activeElement = doc.body; }
    get hidden() { return this._hidden; }
    set hidden(value) { this._hidden = !!value; if (this._hidden && this.contains(doc._activeElement)) doc._activeElement = doc.body; }
    get inert() { return this._inert; }
    set inert(value) { this._inert = !!value; if (this._inert && this.contains(doc._activeElement)) doc._activeElement = doc.body; }
    get value() { return this.tagName !== "SELECT" || this.options.some(o => o.value === this._value) ? this._value : ""; }
    set value(value) { value = String(value); this._value = this.tagName !== "SELECT" || this.options.some(o => o.value === value) ? value : ""; }
    get options() { return this.tagName === "SELECT" ? selectAll(this, "option") : []; }
    get textContent() { return this._text; }
    set textContent(value) { this._clear(); this._text = String(value ?? ""); this._html = escapeHtml(this._text); }
    get innerHTML() { return this.children.length ? this.children.map(serialize).join("") : this._html; }
    set innerHTML(value) {
      this._clear(); this._text = ""; this._html = String(value ?? "");
      const stack = [this];
      for (const [, closing, tag, raw = ""] of this._html.matchAll(/<\s*(\/)?\s*([\w-]+)([^>]*)>/g)) {
        const lower = tag.toLowerCase();
        if (closing) { if (stack.length > 1) stack.pop(); continue; }
        const child = doc.createElement(lower);
        for (const match of raw.matchAll(/([\w-]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+)))?/g)) {
          const name = match[1].toLowerCase(), value = match[2] ?? match[3] ?? match[4] ?? "";
          if (name === "id") child.id = value; else if (name === "class") child.className = value;
          else if (name === "value") child._value = value;
          else if (["checked", "disabled", "hidden", "inert", "selected"].includes(name)) child[name] = true;
          else if (name.startsWith("data-")) child.dataset[name.slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase())] = value;
          else if (name === "style") {
            child.style.cssText = value;
            for (const part of value.split(";")) {
              const [property, ...rest] = part.split(":"); if (property && rest.length)
                child.style[property.trim().replace(/-([a-z])/g, (_, c) => c.toUpperCase())] = rest.join(":").trim();
            }
          } else child[name] = value;
        }
        stack.at(-1).appendChild(child);
        if (!voidTags.has(lower) && !raw.trim().endsWith("/")) stack.push(child);
      }
      if (this.tagName === "SELECT" && !this.options.some(o => o.value === this._value)) this._value = this.options[0]?.value || "";
    }
    _clear() { if (this.children.some(child => child.contains(doc._activeElement))) doc._activeElement = doc.body;
      this.children.forEach(child => { child.parentNode = null; }); this.children = []; }
    appendChild(child) {
      if (child === this || child.contains(this)) throw new Error("HierarchyRequestError");
      const firstOption = this.tagName === "SELECT" && !this.options.length;
      child.remove(); child.parentNode = this; this.children.push(child);
      if (child.tagName === "OPTION" && (firstOption || child.selected)) this._value = child.value; return child;
    }
    remove() { if (this.contains(doc._activeElement)) doc._activeElement = doc.body;
      if (this.parentNode?.children) this.parentNode.children = this.parentNode.children.filter(child => child !== this);
      this.parentNode = null; }
    contains(node) { return node === this || this.children.some(child => child.contains(node)); }
    querySelector(query) { return selectAll(this, query, true)[0] || null; }
    querySelectorAll(query) { return selectAll(this, query); }
    closest(query) { for (let node = this; node instanceof Element; node = node.parentNode) if (matches(node, query)) return node; return null; }
    addEventListener(type, fn) { addListener(this, type, fn); }
    removeEventListener(type, fn) { removeListener(this, type, fn); }
    dispatchEvent(e) { return dispatch(this, e); }
    click() { if (!this.disabled) this.dispatchEvent(createEvent("click")); }
    focus() { if (!doc.body.contains(this) || this.disabled) return;
      for (let node = this; node instanceof Element; node = node.parentNode) if (node.hidden || node.inert) return;
      doc._activeElement = this; }
    blur() { if (doc._activeElement === this) doc._activeElement = doc.body; }
    play() { this.paused = false; return Promise.resolve(); }
    pause() { this.paused = true; }
  }

  const doc = {
    ids, _activeElement: null, parentNode: null,
    createElement: tag => new Element(tag), createEvent,
    getElementById(id) { if (!ids[id]) { const stub = this.createElement("div"); stub.id = id; } return ids[id]; },
    querySelector: query => selectAll(doc.body, query, true)[0] || null,
    querySelectorAll: query => selectAll(doc.body, query),
    addEventListener(type, fn) { addListener(this, type, fn); },
    removeEventListener(type, fn) { removeListener(this, type, fn); },
    dispatchEvent(e) { return dispatch(this, e); },
  };
  doc.body = new Element("body"); doc.body.parentNode = doc; doc._activeElement = doc.body;
  Object.defineProperty(doc, "activeElement", { get: () => doc._activeElement });
  return doc;
}

module.exports = { createDocument };
