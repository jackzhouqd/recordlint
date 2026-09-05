/* RecordLint 质量记录预审 — 前端交互
 *
 * 原则：主题切换与快捷键属于全局；页面级行为由各页自行注册到 QA.on()。
 * 不引入框架，htmx 负责局部刷新，这里只补它不管的部分。
 */
(function () {
  "use strict";

  var QA = window.QA = {
    theme: null,
    _ready: [],
    on: function (fn) { this._ready.push(fn); },
  };

  /* ---------------------------------------------------------- 主题 */

  var THEME_KEY = "qaudit_theme";

  function currentTheme() {
    return document.documentElement.getAttribute("data-theme") || "dark";
  }

  QA.setTheme = function (name) {
    document.documentElement.setAttribute("data-theme", name);
    // 一年有效期，服务端渲染首屏时直接读，避免刷新闪白
    document.cookie = THEME_KEY + "=" + name + ";path=/;max-age=31536000;SameSite=Strict";
    var btn = document.getElementById("theme-btn");
    if (btn) {
      btn.textContent = name === "dark" ? "☾" : "☀";
      btn.title = name === "dark" ? "切换到浅色主题（供打印/投影）" : "切换到深色主题";
    }
    document.dispatchEvent(new CustomEvent("qa:theme", { detail: name }));
  };

  QA.toggleTheme = function () {
    QA.setTheme(currentTheme() === "dark" ? "light" : "dark");
  };

  /* ---------------------------------------------------------- 快捷键 */

  var handlers = {};   // key -> {fn, desc}

  QA.key = function (key, desc, fn) {
    handlers[key.toLowerCase()] = { fn: fn, desc: desc };
  };

  function isTyping(el) {
    if (!el) return false;
    var t = el.tagName;
    return t === "INPUT" || t === "TEXTAREA" || t === "SELECT" || el.isContentEditable;
  }

  document.addEventListener("keydown", function (e) {
    if (isTyping(e.target) || e.ctrlKey || e.altKey || e.metaKey) return;
    var k = e.key.toLowerCase();
    if (k === "escape") { closeModal(); return; }
    var h = handlers[k];
    if (h) { e.preventDefault(); h.fn(e); }
  });

  /* ---------------------------------------------------------- 帮助浮层 */

  function closeModal() {
    var m = document.getElementById("help-modal");
    if (m) m.classList.remove("open");
  }

  QA.showHelp = function () {
    var m = document.getElementById("help-modal");
    if (!m) return;
    var rows = Object.keys(handlers).map(function (k) {
      return '<tr><td style="width:70px"><span class="kbd">' +
        (k === " " ? "空格" : k.toUpperCase()) + "</span></td><td>" + handlers[k].desc + "</td></tr>";
    }).join("");
    m.querySelector("tbody").innerHTML = rows ||
      '<tr><td class="dim2">本页没有注册快捷键</td></tr>';
    m.classList.add("open");
  };

  /* ---------------------------------------------------------- 证据图缩放 */

  /* 扫描件细节（红框内的手写、印章边缘）必须能放大看。
   *
   * 用 transform 平移而不是容器滚动：证据图按「适应」铺满后，容器本身没有
   * 滚动条可用（早期版本靠 scrollLeft/scrollTop 平移，1 倍时容器 overflow:hidden
   * 且图高于容器，底部内容既滚不到也拖不动，等于看不全）。
   * transform 与容器尺寸无关，配合 clamp 保证任何倍率下四条边都够得着。
   */
  var MAX_SCALE = 8;

  QA.attachZoom = function (box) {
    var img = box.querySelector("img");
    if (!img || box.dataset.zoomReady) return;
    box.dataset.zoomReady = "1";
    var scale = 1, tx = 0, ty = 0;

    /* 1 倍 = 整图适应容器（contain）。以此为基准算平移边界。 */
    function fitted() {
      var bw = box.clientWidth, bh = box.clientHeight;
      var nw = img.naturalWidth || img.clientWidth || 1;
      var nh = img.naturalHeight || img.clientHeight || 1;
      var k = Math.min(bw / nw, bh / nh);
      return { w: nw * k, h: nh * k, bw: bw, bh: bh };
    }

    function apply() {
      var f = fitted();
      // 放大后超出容器的那一半就是可平移的余量；未超出的方向锁死在居中
      var mx = Math.max(0, (f.w * scale - f.bw) / 2);
      var my = Math.max(0, (f.h * scale - f.bh) / 2);
      tx = Math.min(mx, Math.max(-mx, tx));
      ty = Math.min(my, Math.max(-my, ty));
      img.style.transform = "translate(" + tx + "px," + ty + "px) scale(" + scale + ")";
      box.classList.toggle("pannable", scale > 1);
      var tip = box.parentNode && box.parentNode.querySelector("[data-zoom-level]");
      if (tip) tip.textContent = Math.round(scale * 100) + "%";
    }

    function zoom(factor, at) {
      var prev = scale;
      scale = Math.min(MAX_SCALE, Math.max(1, scale * factor));
      if (scale === prev) return;
      if (at) {   // 以光标为锚点缩放，放大时视线不跑偏
        var r = box.getBoundingClientRect();
        var dx = at.x - (r.left + r.width / 2), dy = at.y - (r.top + r.height / 2);
        var k = scale / prev;
        tx = dx - (dx - tx) * k;
        ty = dy - (dy - ty) * k;
      }
      apply();
    }

    box.addEventListener("wheel", function (e) {
      // 证据框自身不滚动，滚轮专用于缩放，不必再按住 Shift
      e.preventDefault();
      zoom(e.deltaY < 0 ? 1.2 : 1 / 1.2, { x: e.clientX, y: e.clientY });
    }, { passive: false });

    box.addEventListener("dblclick", function (e) {
      if (scale > 1) { api.reset(); return; }
      zoom(2.5 / scale, { x: e.clientX, y: e.clientY });
    });

    var drag = null;

    function onMove(e) {
      if (!drag) return;
      tx = drag.tx + (e.clientX - drag.x);
      ty = drag.ty + (e.clientY - drag.y);
      apply();
    }

    function onUp() {
      if (!drag) return;
      drag = null;
      document.removeEventListener("mousemove", onMove);
      document.removeEventListener("mouseup", onUp);
    }

    box.addEventListener("mousedown", function (e) {
      if (scale <= 1 || e.button !== 0) return;
      drag = { x: e.clientX, y: e.clientY, tx: tx, ty: ty };
      // 必须阻止默认行为：否则 Chrome 把它当成拖拽图片，mousemove 会断流
      e.preventDefault();
      document.addEventListener("mousemove", onMove);
      document.addEventListener("mouseup", onUp);
    });

    // 监听器挂在 document 上并随 mouseup 摘除，避免 htmx 换掉详情区后残留
    var api = {
      reset: function () { scale = 1; tx = ty = 0; apply(); },
      zoomIn: function () { zoom(1.35, null); },
      zoomOut: function () { zoom(1 / 1.35, null); },
    };
    box._zoom = api;
    QA.resetZoom = api.reset;   // 兼容旧调用

    if (img.complete) apply();
    else img.addEventListener("load", api.reset);
    window.addEventListener("resize", apply);
  };

  /* 供页面上的 ＋ / － / 适应 按钮调用，作用于当前详情区的证据图 */
  QA.zoom = function (action) {
    var box = document.querySelector(".evidence");
    if (box && box._zoom && box._zoom[action]) box._zoom[action]();
  };

  /* ---------------------------------------------------------- 启动 */

  function boot() {
    var btn = document.getElementById("theme-btn");
    if (btn) btn.addEventListener("click", QA.toggleTheme);
    QA.setTheme(currentTheme());

    QA.key("?", "显示快捷键帮助", QA.showHelp);
    QA.key("t", "切换深色 / 浅色主题", QA.toggleTheme);

    var back = document.getElementById("help-modal");
    if (back) {
      back.addEventListener("click", function (e) { if (e.target === back) closeModal(); });
    }

    // 档案来源页：在服务端目录选择器里点「选此目录」，把路径填回新增表单。
    // 用事件委托——选择器是 htmx 片段，每次下钻都会被整块换掉。
    document.addEventListener("click", function (e) {
      var el = e.target.closest ? e.target.closest("[data-pick]") : null;
      if (!el) return;
      var picked = el.getAttribute("data-pick");
      var pathBox = document.getElementById("mount-path");
      var nameBox = document.getElementById("mount-name");
      if (!pathBox) return;
      pathBox.value = picked;
      if (nameBox && !nameBox.value) {
        var segs = picked.split(/[\\/]/).filter(Boolean);
        nameBox.value = segs.length ? segs[segs.length - 1] : picked;
      }
      if (nameBox) nameBox.focus();
    });

    QA._ready.forEach(function (fn) {
      try { fn(); } catch (err) { console.error("[qaudit] 页面初始化失败", err); }
    });
    document.querySelectorAll(".evidence").forEach(QA.attachZoom);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
